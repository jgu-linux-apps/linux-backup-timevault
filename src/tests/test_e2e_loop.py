"""Test end-to-end na wirtualnych dyskach (loop): backup -> restore w obu trybach.

Wymaga roota, resticu, sfdisk, mkfs.vfat, mkfs.ntfs. Bootloader (chroot GRUB) pomijany —
sprawdzany osobno w teście VM.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess

import pytest

os.environ["TIMEVAULT_LANG"] = "pl"   # testy sprawdzają polskie komunikaty

from timevault import blockdev, disk, restore, system
from timevault.disk import CONFIRM_WORD, MiB, SafetyError
from timevault.repo import Repo

pytestmark = pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("restic"),
                                reason="wymaga roota i resticu")

W = "/tmp/timevault-test"
PASSWORD = "test-haslo-123"
DANE_UUID = "ACB66989B66954BC"   # jak "Dane" Jarka — na nowym dysku nie istnieje


def sh(*cmd, input=None):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, input=input).stdout


def make_disk(name, size, script=None):
    img = f"{W}/{name}.img"
    sh("truncate", "-s", size, img)
    dev = sh("losetup", "-P", "-f", "--show", img).strip()
    if script:
        sh("sfdisk", "-q", dev, input=script)
        blockdev.settle(dev)
    return dev


def md5(path, nbytes=8 * MiB):
    with open(path, "rb") as f:
        return hashlib.md5(f.read(nbytes)).hexdigest()


@pytest.fixture(scope="module")
def env():
    subprocess.run(["umount", "-R", f"{W}/src"], capture_output=True)
    subprocess.run(["umount", "-R", f"{W}/data"], capture_output=True)
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(W)
    loops = []

    # --- "Laptop Jarka": EFI + MSR + Windows NTFS + Ubuntu ext4
    src = make_disk("src", "1G", f"label: gpt\nsize=100M, type={blockdev.ESP_GUID}\n"
                                 "size=16M, type=E3C9E316-0B5C-4DB8-817D-F92DF00215AE\n"
                                 "size=200M, type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n"
                                 f"type={blockdev.LINUX_GUID}\n")
    loops.append(src)
    sh("mkfs.vfat", "-F", "32", f"{src}p1")
    sh("mkfs.ntfs", "-Q", "-q", f"{src}p3")
    sh("mkfs.ext4", "-q", "-F", f"{src}p4")
    root = f"{W}/src"
    os.makedirs(root)
    sh("mount", f"{src}p4", root)
    os.makedirs(f"{root}/boot/efi")
    blockdev.mount_dev(f"{src}p1", f"{root}/boot/efi")
    os.makedirs(f"{root}/boot/efi/EFI/Microsoft/Boot")
    open(f"{root}/boot/efi/EFI/Microsoft/Boot/bootmgfw.efi", "w").write("windows")

    ru = blockdev.probe(f"{src}p4")["UUID"]
    eu = blockdev.probe(f"{src}p1")["UUID"]
    for d in ["etc", "home/jarek/.cache/thumbnails", "home/jarek/Dokumenty", "tmp", "var/tmp",
              "mnt/dysk_d", "var/lib/snapd/snaps", "var/lib/snapd/cache", "usr/bin", "snap/firefox"]:
        os.makedirs(f"{root}/{d}", exist_ok=True)
    open(f"{root}/etc/fstab", "w").write(
        "# /etc/fstab\n"
        f"UUID={ru} /               ext4    errors=remount-ro 0       1\n"
        f"UUID={eu}  /boot/efi       vfat    umask=0077      0       1\n"
        f"UUID={DANE_UUID} /mnt/dysk_d ntfs defaults,uid=1000 0 0\n"
        "/swap.img\tnone\tswap\tsw\t0\t0\n")
    open(f"{root}/etc/os-release", "w").write('PRETTY_NAME="Ubuntu 24.04.3 LTS"\nID=ubuntu\nVERSION_ID="24.04"\n')
    open(f"{root}/etc/hostname", "w").write("jarek-ubuntu\n")
    open(f"{root}/home/jarek/Dokumenty/cv.txt", "w").write("ważny plik ąęś\n")
    open(f"{root}/home/jarek/.cache/thumbnails/x.png", "w").write("cache")
    open(f"{root}/tmp/smiec", "w").write("tmp")
    open(f"{root}/var/lib/snapd/snaps/firefox_9113.snap", "wb").write(os.urandom(2 * MiB))
    os.link(f"{root}/var/lib/snapd/snaps/firefox_9113.snap", f"{root}/var/lib/snapd/cache/abc")
    open(f"{root}/usr/bin/sudo", "w").write("#!/bin/sh\n")
    os.chmod(f"{root}/usr/bin/sudo", 0o4755)
    os.chown(f"{root}/home/jarek/Dokumenty/cv.txt", 1000, 1000)
    os.symlink("firefox", f"{root}/snap/firefox/current")
    os.chmod(f"{root}/tmp", 0o1777)
    sh("fallocate", "-l", "8M", f"{root}/swap.img")
    os.chmod(f"{root}/swap.img", 0o600)

    # --- Dysk zewnętrzny
    ext = make_disk("ext", "5G")
    loops.append(ext)

    yield {"src": src, "root": root, "ext": ext, "loops": loops, "root_uuid": ru}

    subprocess.run(["umount", "-R", root], capture_output=True)
    subprocess.run(["umount", "-R", f"{W}/data"], capture_output=True)
    for l in loops:
        subprocess.run(["losetup", "-d", l], capture_output=True)


@pytest.fixture(scope="module")
def backed_up(env):
    parts = disk.prepare_backup_disk(env["ext"], CONFIRM_WORD, recovery_size=64 * MiB, efi_size=64 * MiB)
    data = f"{W}/data"
    disk.mount(parts["data"], data)
    repo = Repo(f"{data}/restic", password=PASSWORD, cache_dir=f"{data}/cache")
    repo.init()
    manifest = system.inspect(env["root"])
    system.write_manifest(manifest, env["root"])
    s1 = repo.backup(env["root"], tags=["timevault"])
    open(f"{env['root']}/home/jarek/Dokumenty/nowy.txt", "w").write("druga wersja\n")
    s2 = repo.backup(env["root"], tags=["timevault"])
    return {"repo": repo, "parts": parts, "manifest": manifest, "s1": s1, "s2": s2, "data": data}


# ------------------------------------------------------------------ dysk backupu
def test_backup_disk_layout(env, backed_up):
    d = blockdev.read_disk(env["ext"])
    assert [p.partlabel for p in d.parts] == ["TM-EFI", "TM-RECOVERY-A", "TM-RECOVERY-B", "TM-DATA"]
    assert d.parts[0].parttype == blockdev.ESP_GUID
    assert d.parts[0].fstype == "vfat" and d.parts[3].fstype == "ext4"
    assert disk.find_data_partition() == backed_up["parts"]["data"]


def test_backup_never_touches_recovery(env, backed_up):
    """Partycje Recovery i EFI nie są montowane ani zmieniane przez backup."""
    for key in ("efi", "recovery_a", "recovery_b"):
        assert not blockdev.is_mounted(backed_up["parts"][key])
    before = {k: md5(backed_up["parts"][k]) for k in ("efi", "recovery_a", "recovery_b")}
    backed_up["repo"].backup(env["root"], tags=["timevault"])
    backed_up["repo"].forget_tm()
    after = {k: md5(backed_up["parts"][k]) for k in ("efi", "recovery_a", "recovery_b")}
    assert before == after


def test_manifest(env, backed_up):
    m = backed_up["manifest"]
    assert m["root"]["uuid"] == env["root_uuid"]
    assert m["esp"]["uuid"]
    assert m["dual_boot"]["windows"] is True
    assert m["os"]["version_id"] == "24.04"
    assert m["swap_files"] == [{"path": "/swap.img", "size": 8 * MiB}]
    # manifest jest też w samym snapshocie
    m2 = backed_up["repo"].manifest(backed_up["s2"]["snapshot_id"], env["root"])
    assert m2["root"]["uuid"] == env["root_uuid"]


def test_snapshots_and_check(backed_up):
    snaps = backed_up["repo"].snapshots()
    assert len(snaps) >= 2
    backed_up["repo"].check("10%")


# ------------------------------------------------------------------ zabezpieczenia
def test_refuses_without_confirmation(env):
    with pytest.raises(SafetyError):
        disk.prepare_backup_disk(env["ext"], "tak")


def test_refuses_mounted_disk(env, backed_up):
    with pytest.raises(SafetyError, match="zamontowane"):
        disk.prepare_backup_disk(env["src"], CONFIRM_WORD)
    with pytest.raises(SafetyError):
        restore.plan_disk(backed_up["manifest"], env["src"], boot_mode="uefi")


def test_refuses_windows_partition(env, backed_up):
    dst = make_disk("win", "1G", f"label: gpt\nsize=100M, type={blockdev.ESP_GUID}\n"
                                 "type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n")
    env["loops"].append(dst)
    sh("mkfs.vfat", "-F", "32", f"{dst}p1")
    sh("mkfs.ntfs", "-Q", "-q", f"{dst}p2")
    with pytest.raises(SafetyError, match="ntfs"):
        restore.plan_partition(backed_up["manifest"], f"{dst}p2", f"{dst}p1", boot_mode="uefi")
    with pytest.raises(SafetyError, match="vfat"):  # EFI jako root też zablokowane
        restore.plan_partition(backed_up["manifest"], f"{dst}p1", f"{dst}p1", boot_mode="uefi")


def test_refuses_too_small_disk(env, backed_up):
    tiny = make_disk("tiny", "64M")
    env["loops"].append(tiny)
    with pytest.raises(SafetyError, match="potrzeba"):
        restore.plan_disk(backed_up["manifest"], tiny, boot_mode="uefi")


# ------------------------------------------------------------------ restore
def _verify_restored(root_part, esp_part, latest=True):
    mnt = f"{W}/verify"
    os.makedirs(mnt, exist_ok=True)
    sh("mount", root_part, mnt)
    try:
        assert open(f"{mnt}/home/jarek/Dokumenty/cv.txt").read() == "ważny plik ąęś\n"
        assert os.path.exists(f"{mnt}/home/jarek/Dokumenty/nowy.txt") == latest
        st = os.stat(f"{mnt}/home/jarek/Dokumenty/cv.txt")
        assert (st.st_uid, st.st_gid) == (1000, 1000)
        assert os.stat(f"{mnt}/usr/bin/sudo").st_mode & stat.S_ISUID
        assert os.readlink(f"{mnt}/snap/firefox/current") == "firefox"
        assert os.path.getsize(f"{mnt}/var/lib/snapd/snaps/firefox_9113.snap") == 2 * MiB
        # wykluczone
        assert not os.path.exists(f"{mnt}/home/jarek/.cache")
        assert not os.path.exists(f"{mnt}/tmp/smiec")
        assert os.listdir(f"{mnt}/var/lib/snapd/cache") == []
        # katalogi i uprawnienia
        assert stat.S_IMODE(os.stat(f"{mnt}/tmp").st_mode) == 0o1777
        assert os.path.isdir(f"{mnt}/mnt/dysk_d")
        assert os.path.isdir(f"{mnt}/proc")
        # swap odtworzony
        assert os.path.getsize(f"{mnt}/swap.img") == 8 * MiB
        assert "swap" in sh("blkid", "-p", "-o", "value", "-s", "TYPE", f"{mnt}/swap.img")
        # fstab
        fstab = open(f"{mnt}/etc/fstab").read()
        new_root = blockdev.probe(root_part)["UUID"]
        new_esp = blockdev.probe(esp_part)["UUID"]
        assert f"UUID={new_root}\t/\t" in fstab
        assert f"UUID={new_esp}\t/boot/efi\t" in fstab
        assert "nofail" in [l for l in fstab.splitlines() if DANE_UUID in l][0]
        assert os.path.exists(f"{mnt}/etc/fstab.timevault-orig")
        # pierwszy start
        assert os.path.islink(f"{mnt}/etc/systemd/system/multi-user.target.wants/timevault-firstboot.service")
        assert os.access(f"{mnt}/usr/local/lib/timevault/firstboot.sh", os.X_OK)
    finally:
        sh("umount", mnt)


def test_restore_whole_disk(env, backed_up):
    """Nowy laptop: pusty dysk -> EFI + Ubuntu."""
    dst = make_disk("newlaptop", "2G")
    env["loops"].append(dst)
    plan = restore.plan_disk(backed_up["manifest"], dst, boot_mode="uefi")
    assert plan.mode == "disk"
    report = restore.execute(plan, backed_up["repo"], backed_up["s2"]["snapshot_id"],
                             backed_up["manifest"], CONFIRM_WORD,
                             target=f"{W}/target", skip_bootloader=True, progress=lambda m: None)
    d = blockdev.read_disk(dst)
    assert [p.parttype for p in d.parts] == [blockdev.ESP_GUID, blockdev.LINUX_GUID]
    assert report["root_uuid"] != env["root_uuid"]          # nowe UUID
    _verify_restored(report["root_part"], report["esp_part"])


def test_restore_older_snapshot(env, backed_up):
    dst = make_disk("older", "2G")
    env["loops"].append(dst)
    plan = restore.plan_disk(backed_up["manifest"], dst, boot_mode="uefi")
    report = restore.execute(plan, backed_up["repo"], backed_up["s1"]["snapshot_id"],
                             backed_up["manifest"], CONFIRM_WORD,
                             target=f"{W}/target", skip_bootloader=True, progress=lambda m: None)
    _verify_restored(report["root_part"], report["esp_part"], latest=False)


def test_restore_partition_dual_boot(env, backed_up):
    """Ten sam laptop z Windowsem: formatowana tylko partycja Ubuntu, EFI i NTFS nietknięte."""
    dst = make_disk("dualboot", "1G", f"label: gpt\nsize=100M, type={blockdev.ESP_GUID}\n"
                                      "size=200M, type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n"
                                      f"type={blockdev.LINUX_GUID}\n")
    env["loops"].append(dst)
    esp, win, ubu = f"{dst}p1", f"{dst}p2", f"{dst}p3"
    sh("mkfs.vfat", "-F", "32", esp)
    sh("mkfs.ntfs", "-Q", "-q", win)
    sh("mkfs.ext4", "-q", "-F", ubu)       # "zepsute" Ubuntu
    m = f"{W}/espm"
    os.makedirs(m, exist_ok=True)
    blockdev.mount_dev(esp, m)
    os.makedirs(f"{m}/EFI/Microsoft/Boot")
    open(f"{m}/EFI/Microsoft/Boot/bootmgfw.efi", "w").write("windows")
    sh("umount", m)
    esp_uuid = blockdev.probe(esp)["UUID"]
    win_hash = md5(win, 200 * MiB)

    plan = restore.plan_partition(backed_up["manifest"], ubu, esp, boot_mode="uefi")
    assert any("ntfs" in u for u in plan.untouched)
    report = restore.execute(plan, backed_up["repo"], backed_up["s2"]["snapshot_id"],
                             backed_up["manifest"], CONFIRM_WORD,
                             target=f"{W}/target", skip_bootloader=True, progress=lambda m: None)

    assert md5(win, 200 * MiB) == win_hash                  # Windows bit w bit ten sam
    assert blockdev.probe(esp)["UUID"] == esp_uuid           # EFI nie formatowana
    blockdev.mount_dev(esp, m)
    assert open(f"{m}/EFI/Microsoft/Boot/bootmgfw.efi").read() == "windows"
    sh("umount", m)
    _verify_restored(report["root_part"], esp)


# ------------------------------------------------------------------ tryb testu VM
def test_fstab_vm_mode_forces_nofail(env, backed_up, tmp_path):
    """W VM nie ma dysków laptopa: nawet obecny na hoście dysk dostaje nofail."""
    present = blockdev.probe(backed_up["parts"]["data"])["UUID"]
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/fstab").write_text(
        "UUID=old / ext4 defaults 0 1\n"
        f"UUID={present} /mnt/x ext4 defaults 0 2\n")
    normal = restore.fix_fstab(str(tmp_path), "new", "")
    assert not any("/mnt/x" in r for r in normal)
    (tmp_path / "etc/fstab").write_text(
        "UUID=old / ext4 defaults 0 1\n"
        f"UUID={present} /mnt/x ext4 defaults 0 2\n")
    vmode = restore.fix_fstab(str(tmp_path), "new", "", force_nofail=True)
    assert any("/mnt/x" in r for r in vmode)
    assert "nofail" in (tmp_path / "etc/fstab").read_text()


def test_vm_restore_image(env, backed_up):
    """Pełny przebieg jak 'vm-test': obraz na TM-DATA, restore w trybie VM."""
    from timevault import vm
    m = backed_up["manifest"]
    img = vm.create_image(f"{backed_up['data']}/vm", 2 * 1024**3)
    loop = vm.attach(img)
    try:
        plan = restore.plan_disk(m, loop, boot_mode="uefi")
        plan.vm_test = True
        report = restore.execute(plan, backed_up["repo"], backed_up["s2"]["snapshot_id"], m,
                                 CONFIRM_WORD, target=f"{W}/target", skip_bootloader=True,
                                 progress=lambda x: None)
        _verify_restored(report["root_part"], report["esp_part"])
    finally:
        vm.detach(loop)
    cmd = vm.qemu_command(img, "/tmp/vars.fd", "/tmp/code.fd")
    assert "-enable-kvm" in cmd and f"file={img},format=raw,if=virtio,cache=unsafe,aio=threads" in cmd
    assert vm.image_size_for({"root": {"used": 100 * 1024**3}}) == 127 * 1024**3


def test_legacy_manifest_and_efi_stubs(env, backed_up, tmp_path):
    """Snapshot sprzed zmiany nazwy (manifest w var/lib/linux-tm) nadal się odtwarza;
    stuby EFI zawsze wskazują NOWE UUID."""
    import json as _j
    root = env["root"]
    os.makedirs(f"{root}/var/lib/linux-tm", exist_ok=True)
    os.replace(f"{root}/var/lib/timevault/manifest.json", f"{root}/var/lib/linux-tm/manifest.json")
    s = backed_up["repo"].backup(root, tags=["linux-tm"])
    m = backed_up["repo"].manifest(s["snapshot_id"], root)
    assert m["root"]["uuid"] == env["root_uuid"]
    plan = restore.RestorePlan(mode="disk", disk="/dev/null", boot_mode="uefi",
                               root_part=backed_up["parts"]["data"])
    (tmp_path / "boot/efi/EFI/ubuntu").mkdir(parents=True)
    (tmp_path / "boot/efi/EFI/ubuntu/grub.cfg").write_text("search.fs_uuid STARE root\n")
    restore._write_efi_stubs(str(tmp_path), plan, ["ubuntu", "BOOT"])
    new = blockdev.probe(backed_up["parts"]["data"])["UUID"]
    for d in ("ubuntu", "BOOT"):
        cfg = (tmp_path / f"boot/efi/EFI/{d}/grub.cfg").read_text()
        assert f"search.fs_uuid {new} root" in cfg and "STARE" not in cfg


# ------------------------------------------------------------------ Recovery: EFI + A/B
def test_recovery_efi_menu_and_ab(env, backed_up, tmp_path):
    os.environ["MTOOLS_SKIP_CHECK"] = "1"
    """Menu startowe zapisywane mtools (bez montowania), A/B, a EFI laptopa nietknięta."""
    from timevault import recovery
    if not shutil.which("mcopy"):
        pytest.skip("brak mtools")
    parts = recovery.find_parts(env["ext"])
    assert parts["disk"] == env["ext"]
    fake = tmp_path / "slot"
    for rel in ("usr/lib/shim/shimx64.efi.signed.latest", "usr/lib/shim/mmx64.efi",
                "usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed"):
        (fake / rel).parent.mkdir(parents=True, exist_ok=True)
        (fake / rel).write_bytes(b"EFI" + rel.encode())
    laptop_esp = md5(f"{env['src']}p1", 100 * MiB)
    ua, ub = blockdev.probe(parts["A"])["UUID"], blockdev.probe(parts["B"])["UUID"]

    recovery.write_efi(parts["efi"], str(fake), {"A": ua, "B": ""}, "A", "test")
    assert recovery.read_state(parts["efi"])["active"] == "A"
    cfg = sh("mtype", "-i", parts["efi"], "::/EFI/ubuntu/grub.cfg")
    assert f"root=UUID={ua}" in cfg and "poprzednia" not in cfg       # B jeszcze pusty

    recovery.write_efi(parts["efi"], str(fake), {"A": ua, "B": ub}, "B", "test")
    cfg = sh("mtype", "-i", parts["efi"], "::/EFI/BOOT/grub.cfg")
    assert cfg.index(ub) < cfg.index(ua)                                # nowy slot pierwszy
    assert "poprzednia wersja" in cfg                                   # stary jako zapas
    assert recovery.read_state(parts["efi"])["active"] == "B"
    assert sh("mtype", "-i", parts["efi"], "::/EFI/BOOT/BOOTX64.EFI").startswith("EFIusr/lib/shim")
    assert md5(f"{env['src']}p1", 100 * MiB) == laptop_esp              # EFI "laptopa" bez zmian
    assert not blockdev.is_mounted(parts["efi"])


# ------------------------------------------------------------------ usługa w tle + setup
@pytest.fixture
def svc(tmp_path, monkeypatch):
    from timevault import service
    monkeypatch.setattr(service, "ETC", str(tmp_path / "etc"))
    monkeypatch.setattr(service, "PASSWORD_FILE", str(tmp_path / "etc/password"))
    monkeypatch.setattr(service, "CONFIG_FILE", str(tmp_path / "state/config.json"))
    monkeypatch.setattr(service, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(service, "STATUS_FILE", str(tmp_path / "state/status.json"))
    monkeypatch.setattr(service, "LOCK_FILE", str(tmp_path / "tv.lock"))
    from timevault import secrets
    monkeypatch.setattr(secrets, "ETC", str(tmp_path / "etc"))
    monkeypatch.setattr(secrets, "RUN_DIR", str(tmp_path / "run-secrets"))
    return service


def test_setup_existing_and_auto_backup(env, backed_up, svc):
    from timevault import setup
    with pytest.raises(SafetyError, match="hasło"):
        setup.existing_disk("zle-haslo")
    from timevault import secrets
    assert not secrets.exists("password")                    # złe hasło nie zostaje zapisane

    n = setup.existing_disk(PASSWORD, progress=lambda m: None)
    assert n >= 2
    cred = os.path.join(secrets.ETC, "password.cred")
    assert os.path.exists(cred) and stat.S_IMODE(os.stat(cred).st_mode) == 0o600
    assert not os.path.exists(os.path.join(secrets.ETC, "password"))   # nigdy jawnie
    assert PASSWORD.encode() not in open(cred, "rb").read()
    assert svc.load_config()["auto"] is True

    assert svc.run_backup(manual=True, root=env["root"]) == 0
    st = svc.read_status()
    assert st["state"] == "idle" and st["last_snapshot"] and len(st["snapshots"]) >= n
    assert st["snapshots"][-1]["time"].endswith("+00:00")
    assert "password" not in json.dumps(st).lower() or PASSWORD not in json.dumps(st)
    assert stat.S_IMODE(os.stat(svc.STATUS_FILE).st_mode) == 0o644   # GUI czyta bez roota

    before = len(backed_up["repo"].snapshots())
    assert svc.run_backup(manual=False, root=env["root"]) == 0      # < godzina od ostatniego
    assert len(backed_up["repo"].snapshots()) == before

    svc.save_config({"auto": False})
    st_before = svc.read_status()["last_success"]
    svc.read_status()
    assert svc.run_backup(manual=False, root=env["root"]) == 0
    assert svc.read_status()["last_success"] == st_before           # auto wyłączone


def test_secrets_encrypted_and_migrated(svc):
    """Sekrety w sejfie systemd-creds: zaszyfrowane, związane z nazwą; stare jawne pliki
    są szyfrowane i bezpiecznie usuwane."""
    from timevault import secrets
    if not secrets.available():
        pytest.skip("brak systemd-creds")
    os.makedirs(secrets.ETC, exist_ok=True)
    plain = os.path.join(secrets.ETC, "password")
    open(plain, "w").write("stare-jawne-haslo")                     # jak w wersjach ≤ 0.6.3
    assert secrets.migrate() == ["password"]
    assert not os.path.exists(plain)
    assert b"stare-jawne-haslo" not in open(os.path.join(secrets.ETC, "password.cred"), "rb").read()
    assert secrets.load("password") == "stare-jawne-haslo"
    assert secrets.protection("password") in ("host", "tpm2+host")
    cmd = secrets.restic_password_command("password")
    assert subprocess.run(cmd.split(), capture_output=True, text=True).stdout == "stare-jawne-haslo"
    # zmiana tokenu przez rclone w trakcie operacji wraca do sejfu, plik chwilowy znika
    secrets.store("rclone", "[gdrive]\ntoken = A\n")
    with secrets.materialized("rclone") as p:
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
        open(p, "w").write("[gdrive]\ntoken = B\n")
    assert "token = B" in secrets.load("rclone") and not os.listdir(secrets.RUN_DIR)
    secrets.remove("password")
    assert not secrets.exists("password")


def test_auto_backup_disk_absent(svc, monkeypatch):
    from timevault import disk as d
    svc.store_password("x" * 10)
    monkeypatch.setattr(d, "find_data_partition", lambda: None)
    assert svc.run_backup(manual=True) == 0
    assert svc.read_status()["state"] == "disk-absent"


def test_recovery_targets_describe_contents(env, backed_up, monkeypatch):
    """Menu Recovery: najpierw 'poprzednie Ubuntu', przy całym dysku widać co zostanie skasowane,
    dysk backupu nigdy nie jest celem."""
    from timevault import recovery_ui
    real = blockdev.list_disks
    loops = [blockdev.read_disk(l) for l in (env["src"], env["ext"])]
    monkeypatch.setattr(blockdev, "list_disks", lambda: loops)
    monkeypatch.setattr(recovery_ui, "MIN_TARGET", 0)
    opts = recovery_ui.target_options(backed_up["manifest"], env["ext"])
    (kind, dev), first = opts[0]
    assert (kind, dev) == ("P", f"{env['src']}p4") and "zalecane" in first
    whole = [t for (k, d), t in opts if k == "D"]
    assert len(whole) == 1 and "Windows (NTFS)" in whole[0] and "EFI" in whole[0]
    assert "WSZYSTKO ZOSTANIE USUNIETE" in whole[0]
    assert all(env["ext"] not in d for (k, d), _ in opts)              # nie dysk backupu
    assert all(t.isascii() for _, t in opts)                           # konsola bez polskich znaków


def test_backup_due_schedule():
    """Harmonogram: timer wg częstotliwości, podłączenie dysku, ręcznie zawsze."""
    from timevault import service as sv
    H = 3600
    cfg = dict(sv.DEFAULT_CONFIG)
    now = 1_000_000.0
    st = lambda age: {"last_success_epoch": now - age}
    assert sv.backup_due("manual", dict(cfg, auto=False, on_connect=False), st(1), now)
    assert sv.backup_due("timer", cfg, st(55 * 60), now)                  # 1h z marginesem
    assert not sv.backup_due("timer", cfg, st(30 * 60), now)
    c4 = dict(cfg, interval="4h")
    assert not sv.backup_due("timer", c4, st(2 * H), now) and sv.backup_due("timer", c4, st(4 * H), now)
    cd = dict(cfg, interval="24h")
    assert not sv.backup_due("timer", cd, st(12 * H), now) and sv.backup_due("timer", cd, st(24 * H), now)
    cw = dict(cfg, interval="7d")
    assert not sv.backup_due("timer", cw, st(3 * 24 * H), now) and sv.backup_due("timer", cw, st(7 * 24 * H), now)
    assert not sv.backup_due("timer", dict(cfg, auto=False), st(30 * 24 * H), now)
    assert sv.backup_due("connect", cfg, st(20 * 60), now)               # po podłączeniu
    assert not sv.backup_due("connect", cfg, st(5 * 60), now)            # wielokrotne podłączanie
    assert not sv.backup_due("connect", dict(cfg, on_connect=False), st(30 * 24 * H), now)
    assert sv.backup_due("timer", cfg, {}, now)                          # nigdy nie było kopii


def test_config_validation(svc):
    with open(svc.CONFIG_FILE, "w") if os.path.isdir(os.path.dirname(svc.CONFIG_FILE)) else open(os.devnull, "w"):
        pass
    os.makedirs(os.path.dirname(svc.CONFIG_FILE), exist_ok=True)
    with open(svc.CONFIG_FILE, "w") as f:
        json.dump({"auto": "tak", "interval": "5min", "on_connect": False, "language": "de"}, f)
    cfg = svc.load_config()
    assert cfg == {"auto": True, "interval": "1h", "on_connect": False, "language": "auto",
                   "cloud": False, "cloud_interval": "24h", "cloud_ac_only": True}
    svc.save_config(dict(cfg, interval="24h"))
    assert stat.S_IMODE(os.stat(svc.CONFIG_FILE).st_mode) == 0o664
    assert svc.load_config()["interval"] == "24h"


def test_refuses_to_erase_existing_timevault_disk(env, backed_up):
    """Twarda blokada po stronie systemu: dysk z kopiami TimeVault nie zostanie wymazany
    przez „przygotuj dysk”, nawet z poprawnym potwierdzeniem."""
    data = backed_up["data"]
    subprocess.run(["umount", data], capture_output=True)
    try:
        assert disk.timevault_partitions(env["ext"])
        with pytest.raises(SafetyError, match="już dyskiem TimeVault"):
            disk.prepare_backup_disk(env["ext"], CONFIRM_WORD, recovery_size=64 * MiB, efi_size=64 * MiB)
        assert blockdev.probe(backed_up["parts"]["data"])["TYPE"] == "ext4"     # nic nie wymazano
    finally:
        disk.mount(backed_up["parts"]["data"], data)


def test_gui_detects_timevault_disk_without_lsblk_labels(monkeypatch):
    """Nawet gdy lsblk nie poda etykiet, dysk z kopiami nie trafia do „przygotuj ten dysk”."""
    from timevault import userdisks as u
    wdc = {"path": "/dev/sda", "size": 320072933376, "model": "WDC", "tran": "usb", "type": "disk",
           "children": [{"path": "/dev/sda1"}, {"path": "/dev/sda4"}]}           # brak partlabel/label
    monkeypatch.setattr(u, "lsblk", lambda: [wdc])
    monkeypatch.setattr(u, "_udev_link_disks", lambda: {"/dev/sda"})
    assert u.timevault_disk() is wdc and u.candidate_disks() == []
    monkeypatch.setattr(u, "_udev_link_disks", lambda: set())
    wdc["children"][1]["label"] = "TM-DATA"                                     # sama etykieta fs
    assert u.timevault_disk() is wdc and u.candidate_disks() == []


def test_lsblk_flat_output_from_ubuntu_2610(monkeypatch):
    """Regresja 0.4.0: lsblk -J bez kolumny NAME zwraca płaską listę (Ubuntu 26.10) —
    aplikacja uznała dysk z kopiami za pusty. Drzewo składamy po PKNAME."""
    import copy
    from timevault import userdisks as u
    flat = [
        {"name": "sda", "pkname": None, "path": "/dev/sda", "size": 320072933376, "model": "WDC",
         "tran": "usb", "type": "disk", "hotplug": True, "mountpoints": []},
        {"name": "sda1", "pkname": "sda", "path": "/dev/sda1", "type": "part", "partlabel": "TM-EFI"},
        {"name": "sda4", "pkname": "sda", "path": "/dev/sda4", "type": "part", "partlabel": "TM-DATA"},
        {"name": "nvme0n1", "pkname": None, "path": "/dev/nvme0n1", "size": 512 * 10**9, "tran": "nvme",
         "type": "disk", "hotplug": False},
        {"name": "nvme0n1p1", "pkname": "nvme0n1", "path": "/dev/nvme0n1p1", "type": "part",
         "fstype": "ntfs", "label": "Dane", "mountpoints": ["/mnt/dysk_d"]},
        {"name": "sdb", "pkname": None, "path": "/dev/sdb", "size": 64 * 10**9, "tran": "usb",
         "type": "disk", "hotplug": True},
        {"name": "sdb1", "pkname": "sdb", "path": "/dev/sdb1", "type": "part", "fstype": "exfat"},
    ]
    monkeypatch.setattr(u, "_udev_link_disks", lambda: set())
    monkeypatch.setattr(u, "lsblk", lambda: u.build_tree(copy.deepcopy(flat)))
    assert u.timevault_disk()["path"] == "/dev/sda"
    assert [d["path"] for d in u.candidate_disks()] == ["/dev/sdb"]   # nie WDC, nie wewnętrzny NVMe
    nested = [dict(flat[0], children=[flat[1], flat[2]])]
    monkeypatch.setattr(u, "lsblk", lambda: u.build_tree(copy.deepcopy(nested)))
    assert u.timevault_disk()["path"] == "/dev/sda" and u.candidate_disks() == []


def test_compat_after_backup_detects_ubuntu_upgrade(env, backed_up, svc):
    """Po każdej kopii: sprawdzenie zgodności w statusie; zmiana wersji Ubuntu wykrywana."""
    from timevault import setup
    setup.existing_disk(PASSWORD, progress=lambda m: None)
    root = env["root"]
    osr = f"{root}/etc/os-release"
    orig = open(osr).read()
    try:
        assert svc.run_backup(manual=True, root=root) == 0
        st = svc.read_status()
        assert st["os"]["version_id"] == "24.04" and st["compat"]
        ids = {i["id"]: i["level"] for i in st["compat"]}
        assert ids["rootfs"] == "ok" and "recovery" in ids
        assert "os_upgraded" not in st
        open(osr, "w").write('PRETTY_NAME="Ubuntu 27.04"\nID=ubuntu\nVERSION_ID="27.04"\nVERSION_CODENAME=tenacious\n')
        assert svc.run_backup(manual=True, root=root) == 0
        st = svc.read_status()
        assert st["os_upgraded"]["from"] == "Ubuntu 24.04.3 LTS" and st["os_upgraded"]["to"] == "Ubuntu 27.04"
    finally:
        open(osr, "w").write(orig)


def test_doctor_unsupported_root(monkeypatch):
    from timevault import doctor, system
    monkeypatch.setattr(system, "_findmnt", lambda t: {"fstype": "btrfs", "source": "/dev/nvme0n1p5"}
                        if t == "/" else None)
    items = doctor.check_root_fs("/")
    assert items[0]["level"] == "error" and "btrfs" in items[0]["en"]
    monkeypatch.setattr(system, "_findmnt", lambda t: {"fstype": "ext4", "source": "/dev/mapper/vgubuntu-root"}
                        if t == "/" else None)
    assert doctor.check_root_fs("/")[0]["level"] == "error"


# ------------------------------------------------------------------ chmura (atrapa Google Drive)
@pytest.fixture
def cloudenv(svc, tmp_path, monkeypatch):
    from timevault import cloud
    monkeypatch.setattr(cloud, "CACHE_DIR", str(tmp_path / "cloudcache"))
    monkeypatch.setattr(cloud, "CLOUD_DIR", str(tmp_path / "drive/TimeVault"))   # lokalny „Drive”
    os.makedirs(tmp_path / "drive", exist_ok=True)
    return cloud


def test_parse_rclone_authorize_output():
    from timevault import cloud
    out = ('2026/11/05 13:40:01 NOTICE: Make sure your Redirect URL is set to "http://127.0.0.1:53682/"\n'
           "Paste the following into your remote machine --->\n"
           '{"access_token":"ya29.x","token_type":"Bearer","refresh_token":"1//0g","expiry":"2026-11-05T14:40:01+01:00"}\n'
           "<---End paste\n")
    tok = cloud.parse_authorize_output(out)
    assert json.loads(tok)["refresh_token"] == "1//0g"
    # rclone ≥1.59 (m.in. 1.60 z Ubuntu): blob base64 z mapą {"token": "<JSON>"} — jak u Jarka
    import base64 as _b
    t = {"access_token": "ya29.b", "token_type": "Bearer", "refresh_token": "1//09", "expiry": "2026-11-05T14:57:00+01:00"}
    blob = _b.b64encode(json.dumps({"token": json.dumps(t)}).encode()).decode().rstrip("=")
    out2 = f"NOTICE: Got code\nPaste the following into your remote machine --->\n{blob}\n<---End paste\n"
    assert json.loads(cloud.parse_authorize_output(out2))["refresh_token"] == "1//09"
    assert json.loads(cloud.parse_authorize_output(blob))["access_token"] == "ya29.b"
    with pytest.raises(ValueError):
        cloud.parse_authorize_output("Error: failed to get token")
    assert cloud.authorize_command()[:3] == ["rclone", "authorize", "drive"]
    import base64 as b64
    blob = cloud.authorize_command()[3]
    assert "=" not in blob                                   # rclone: base64 bez dopełnienia
    assert json.loads(b64.b64decode(blob + "=" * (-len(blob) % 4)))["scope"] == "drive.file"


def test_cloud_backup_and_restore_from_cloud(env, backed_up, cloudenv, svc, tmp_path):
    """Połączenie z „Drive”, kopia w chmurze, sekret w systemie ratunkowym zaszyfrowany hasłem,
    a potem pełne odtworzenie systemu z chmury na pusty dysk."""
    from timevault import setup
    cloud = cloudenv
    setup.existing_disk(PASSWORD, progress=lambda m: None)
    info = svc.setup_cloud(None, remote_type="local", progress=lambda m: None)
    assert info["estimate"] > 0 and svc.load_config()["cloud"] is True
    assert svc.load_config()["cloud_interval"] == "off"              # samo połączenie nic nie wysyła
    assert svc.run_cloud_backup(trigger="timer", root=env["root"]) == 0
    assert not svc.read_status().get("cloud_snapshots")
    from timevault import secrets
    assert secrets.exists("rclone") and not os.path.exists(os.path.join(secrets.ETC, "rclone.conf"))
    assert not os.listdir(secrets.RUN_DIR)                    # chwilowy plik rclone usunięty

    assert svc.run_cloud_backup(trigger="manual", root=env["root"]) == 0
    st = svc.read_status()
    assert st["cloud_state"] == "idle" and len(st["cloud_snapshots"]) == 1
    assert svc.run_cloud_backup(trigger="timer", root=env["root"]) == 0      # < 24 h — pominięta
    assert len(svc.read_status()["cloud_snapshots"]) == 1

    # dane w „chmurze” są zaszyfrowane — żadnego czytelnego pliku z systemu
    blob = b"".join(open(os.path.join(d, f), "rb").read() for d, _, fs in os.walk(cloud.CLOUD_DIR) for f in fs)
    assert "ważny plik".encode() not in blob

    # sekret w aktywnym slocie Recovery: działa tylko z hasłem
    from timevault import recovery
    parts = recovery.find_parts(env["ext"])
    slot = recovery.read_state(parts["efi"])["active"]
    mnt = tmp_path / "slot"
    mnt.mkdir()
    sh("mount", parts[slot], str(mnt))
    try:
        enc = mnt / cloud.RECOVERY_SECRET
        assert enc.exists()
        assert not cloud.decrypt_from_recovery(str(enc), "zle-haslo", str(tmp_path / "x.conf"))
        assert cloud.decrypt_from_recovery(str(enc), PASSWORD, str(tmp_path / "rec.conf"))
    finally:
        sh("umount", str(mnt))

    # odtworzenie jak w Recovery: konfiguracja odszyfrowana hasłem, repo w chmurze
    repo = cloud.open_repo(str(tmp_path / "rec.conf"), password=PASSWORD)
    snap = repo.snapshots()[-1]
    m = repo.manifest(snap["id"], snap["paths"][0])
    dst = make_disk("fromcloud", "2G")
    env["loops"].append(dst)
    plan = restore.plan_disk(m, dst, boot_mode="uefi")
    report = restore.execute(plan, repo, snap["id"], m, CONFIRM_WORD, target=f"{W}/target",
                             skip_bootloader=True, progress=lambda x: None)
    _verify_restored(report["root_part"], report["esp_part"])

    svc.disconnect_cloud()
    assert not secrets.exists("rclone") and svc.load_config()["cloud"] is False
    assert os.listdir(cloud.CLOUD_DIR)                                        # kopie na Drive zostają


def test_cloud_due_and_power(svc, monkeypatch):
    cfg = dict(svc.DEFAULT_CONFIG, cloud=True)
    now = 2_000_000.0
    assert svc.cloud_due("timer", cfg, {}, now)
    assert not svc.cloud_due("timer", cfg, {"cloud_last_success_epoch": now - 3600}, now)
    assert svc.cloud_due("timer", dict(cfg, cloud_interval="6h"), {"cloud_last_success_epoch": now - 6 * 3600}, now)
    assert not svc.cloud_due("timer", dict(cfg, cloud=False), {}, now)
    assert svc.cloud_due("manual", dict(cfg, cloud=False), {}, now)


def test_browse_mount_ready_and_error(env, backed_up, svc, tmp_path, monkeypatch):
    """Przeglądanie kopii: montowanie w /mnt (dozwolone przez AppArmor), status „browse” dopiero
    gdy kopie są widoczne, a błąd montowania trafia do statusu dla aplikacji."""
    import threading, time as _t
    from timevault import setup
    setup.existing_disk(PASSWORD, progress=lambda m: None)
    assert svc.BROWSE_MOUNT.startswith("/mnt/")
    mnt = "/mnt/timevault-browse-test"
    monkeypatch.setattr(svc, "BROWSE_MOUNT", mnt)
    t = threading.Thread(target=svc.run_browse, daemon=True)
    t.start()
    for _ in range(60):
        if svc.read_status().get("browse"):
            break
        _t.sleep(0.5)
    try:
        st = svc.read_status()
        assert st["browse"] is True and os.path.ismount(mnt)
        sid = backed_up["repo"].snapshots()[-1]["short_id"]
        home = os.path.join(mnt, "ids", sid, env["root"].lstrip("/"), "home/jarek/Dokumenty")
        assert open(os.path.join(home, "cv.txt")).read() == "ważny plik ąęś\n"
    finally:
        subprocess.run(["umount", mnt], capture_output=True)
        t.join(20)
    assert svc.read_status()["browse"] is False

    # błąd montowania (np. AppArmor) — widoczny w statusie, usługa nie wisi
    monkeypatch.setattr(svc, "BROWSE_MOUNT", "/proc/timevault-nie-da-sie")
    assert svc.run_browse() == 1
    assert svc.read_status()["browse_error"]


def test_restic_exit_code_3_is_saved_backup(tmp_path, monkeypatch):
    """Regresja 06.11: restic kod 3 („incomplete metadata … xattr” w pliku Flatpaka) = kopia
    zapisana z ostrzeżeniem, nie porażka. Inne kody nadal są błędem."""
    from timevault.repo import Repo
    from timevault import sh as tvsh
    fake = tmp_path / "restic"
    fake.write_text("#!/bin/sh\n"
                    "echo '{\"message_type\":\"error\",\"error\":{\"message\":\"incomplete metadata for "
                    "/var/lib/flatpak/app/com.viber.Viber/x86_64/stable/files/lib/x.typelib: xattr.list: "
                    "no data available\"},\"during\":\"archival\",\"item\":\"/var/lib/flatpak/app/com.viber.Viber/x86_64/stable/files/lib/x.typelib\"}' >&2\n"
                    "echo '{\"message_type\":\"summary\",\"snapshot_id\":\"abc123def456\",\"data_added\":1234}'\n"
                    "exit ${FAKE_RC:-3}\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    repo = Repo("/nie/istotne", password="x")
    s = repo.backup(str(tmp_path))
    assert s["snapshot_id"] == "abc123def456" and s["incomplete"] is True
    assert s["skipped"] == ["/var/lib/flatpak/app/com.viber.Viber/x86_64/stable/files/lib/x.typelib"]
    monkeypatch.setenv("FAKE_RC", "1")
    with pytest.raises(tvsh.CommandError):
        repo.backup(str(tmp_path))
