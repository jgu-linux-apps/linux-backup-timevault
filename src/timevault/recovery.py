"""System Recovery na dysku backupu (partycje TM-RECOVERY-A/B + TM-EFI).

* Budowany przez debootstrap (minimalne Ubuntu, wersja jak w systemie albo wskazana),
  z jądrem, firmware, resticiem i TimeVault.
* Instalacja do NIEAKTYWNEGO slotu; dopiero po sukcesie menu GRUB przełącza się na niego.
  Nieudana aktualizacja zostawia stary, działający slot.
* Partycja TM-EFI jest zapisywana przez mtools, bez montowania — nic nie montuje się
  w pulpicie, a EFI laptopa nigdy nie jest dotykana.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

from . import blockdev, sh
from .disk import LABEL_EFI, LABEL_RECOVERY_A, LABEL_RECOVERY_B, SafetyError
from .i18n import L

STATE_FILE = "timevault.json"          # na TM-EFI: {"active": "A"|"B", ...}
PACKAGES = [
    "linux-image-generic", "initramfs-tools", "systemd-sysv", "udev", "kmod",
    "e2fsprogs", "dosfstools", "fdisk", "util-linux", "mtools", "restic", "rclone",
    "python3", "whiptail", "kbd", "less", "nano", "pciutils", "zstd",
    "grub-efi-amd64-signed", "shim-signed", "network-manager", "ca-certificates", "openssl",
]
MIRROR = "http://archive.ubuntu.com/ubuntu"
SRC_PKG = os.path.dirname(os.path.abspath(__file__))     # katalog pakietu timevault


# --------------------------------------------------------------------------- lokalizacja
def find_parts(disk: str | None = None) -> dict:
    """Partycje dysku backupu po etykietach GPT."""
    labels = {"efi": LABEL_EFI, "A": LABEL_RECOVERY_A, "B": LABEL_RECOVERY_B}
    found = {}
    for key, label in labels.items():
        dev = blockdev.find_by("PARTLABEL", label)
        if disk and dev and blockdev.parent_disk(dev) != disk:
            dev = None
        if not dev:
            raise SafetyError(L(f"Nie znaleziono partycji {label}. Czy to dysk przygotowany przez TimeVault?", f"Partition {label} not found. Is this a disk prepared by TimeVault?"))
        found[key] = dev
    disks = {blockdev.parent_disk(d) for d in found.values()}
    if len(disks) != 1:
        raise SafetyError(f"Partycje Recovery są na różnych dyskach: {disks}")
    found["disk"] = disks.pop()
    return found


def _mtools_env():
    return {"MTOOLS_SKIP_CHECK": "1"}


def read_state(efi: str) -> dict:
    res = sh.run(["mtype", "-i", efi, f"::/{STATE_FILE}"], env=_mtools_env(), check=False, mutating=False)
    try:
        return json.loads(res.stdout) if res.returncode == 0 else {}
    except json.JSONDecodeError:
        return {}


def _mcopy(efi: str, src: str, dst: str):
    sh.run(["mcopy", "-o", "-i", efi, src, dst], env=_mtools_env())


def _mmd(efi: str, *dirs: str):
    for d in dirs:
        sh.run(["mmd", "-i", efi, d], env=_mtools_env(), check=False)


def _release_desktop_mount(dev: str):
    """GNOME sam montuje partycje podłączonego dysku w /media lub /run/media — zwalniamy je.
    Innych montowań (zrobionych świadomie) nie ruszamy."""
    real = os.path.realpath(dev)
    for d, mnt, _ in blockdev.mounts():
        if os.path.realpath(d) == real and mnt.startswith(("/media/", "/run/media/")):
            sh.run(["umount", mnt], check=False)


# --------------------------------------------------------------------------- budowa
def host_codename() -> str:
    try:
        for line in open("/etc/os-release"):
            if line.startswith("VERSION_CODENAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "noble"


BASE = ["systemd-sysv", "udev", "kmod", "dbus", "ca-certificates", "apt"]


def build_rootfs(target: str, codename: str, progress=print):
    """debootstrap nie rozwiązuje zależności wirtualnych — baza z debootstrapa,
    reszta przez apt w chroocie (wymaga podmontowanych /dev, /proc, /sys)."""
    progress(L(f"Pobieranie minimalnego Ubuntu ({codename})…", f"Downloading minimal Ubuntu ({codename})…"))
    sh.run(["debootstrap", "--variant=minbase", "--components=main,universe",
            f"--include={','.join(BASE)}", codename, target, MIRROR])


def install_packages(target: str, progress=print):
    progress(L("Instalacja jądra, sterowników i narzędzi…", "Installing kernel, drivers and tools…"))
    env = {"DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C.UTF-8"}
    shutil.copyfile("/etc/resolv.conf", os.path.join(target, "etc/resolv.conf"), follow_symlinks=True) \
        if os.path.exists("/etc/resolv.conf") else None
    sh.run(["chroot", target, "apt-get", "update", "-q"], env=env)
    sh.run(["chroot", target, "apt-get", "install", "-y", "-q", "--no-install-recommends",
            *PACKAGES, "linux-firmware"], env=env)
    sh.run(["chroot", target, "apt-get", "clean"], env=env)


def configure_rootfs(target: str, slot_uuid: str, slot: str):
    def w(rel, content, mode=0o644):
        p = os.path.join(target, rel.lstrip("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
        os.chmod(p, mode)

    w("/etc/fstab", f"UUID={slot_uuid} / ext4 defaults,noatime 0 1\n"
                    "tmpfs /tmp tmpfs defaults,mode=1777 0 0\n")
    w("/etc/hostname", "timevault-recovery\n")
    w("/etc/hosts", "127.0.0.1 localhost\n127.0.1.1 timevault-recovery\n")
    w("/etc/timevault-recovery", f"slot={slot}\n")
    w("/etc/default/locale", "LANG=C.UTF-8\n")
    # Autologowanie roota na tty1 i od razu menu Recovery.
    w("/etc/systemd/system/getty@tty1.service.d/autologin.conf",
      "[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin root --noclear %I $TERM\n")
    w("/root/.bash_profile",
      'if [ "$(tty)" = /dev/tty1 ]; then /usr/local/bin/timevault-recovery; fi\n')
    w("/usr/local/bin/timevault", "#!/bin/sh\nexport PYTHONPATH=/opt\nexec python3 -m timevault \"$@\"\n", 0o755)
    w("/usr/local/bin/timevault-recovery", "#!/bin/sh\nexport PYTHONPATH=/opt\nexec python3 -m timevault.recovery_ui \"$@\"\n", 0o755)
    w("/etc/profile.d/timevault.sh", "export PYTHONPATH=/opt\n")
    # initramfs: sterowniki USB/NVMe dostępne zawsze (root Recovery leży na dysku USB).
    w("/etc/initramfs-tools/conf.d/timevault", "MODULES=most\nCOMPRESS=zstd\n")
    # Kod TimeVault
    dst = os.path.join(target, "opt/timevault")
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(SRC_PKG, dst, ignore=shutil.ignore_patterns("__pycache__"))
    # Sieć przez NetworkManager (nmtui) — dla odtwarzania z Google Drive.
    w("/etc/NetworkManager/conf.d/10-globally-managed-devices.conf", "[keyfile]\nunmanaged-devices=none\n")
    sh.run(["chroot", target, "passwd", "-d", "root"], check=False)
    sh.run(["chroot", target, "update-initramfs", "-u", "-k", "all"])


def write_efi(efi: str, slot_root: str, uuids: dict, active: str, version: str, codename: str = ""):
    """Shim + podpisany GRUB na TM-EFI i menu startowe z oboma slotami."""
    shim = os.path.join(slot_root, "usr/lib/shim/shimx64.efi.signed.latest")
    if not os.path.exists(shim):
        shim = os.path.join(slot_root, "usr/lib/shim/shimx64.efi.signed")
    grub = os.path.join(slot_root, "usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed")
    mm = os.path.join(slot_root, "usr/lib/shim/mmx64.efi")
    _mmd(efi, "::/EFI", "::/EFI/BOOT", "::/EFI/ubuntu")
    _mcopy(efi, shim, "::/EFI/BOOT/BOOTX64.EFI")
    _mcopy(efi, grub, "::/EFI/BOOT/grubx64.efi")
    if os.path.exists(mm):
        _mcopy(efi, mm, "::/EFI/BOOT/mmx64.efi")

    def entry(slot, title):
        u = uuids[slot]
        return (f'menuentry "{title}" {{\n'
                f"  search --no-floppy --fs-uuid --set=root {u}\n"
                f"  linux /boot/vmlinuz root=UUID={u} ro quiet loglevel=3\n"
                f"  initrd /boot/initrd.img\n}}\n")
    other = "B" if active == "A" else "A"
    cfg = ("set timeout=8\nset default=0\n"
           "set menu_color_normal=white/black\nset menu_color_highlight=black/light-gray\n"
           + entry(active, "Linux Backup TimeVault - Recovery")
           + (entry(other, "Linux Backup TimeVault - Recovery (poprzednia wersja / previous)") if uuids.get(other) else "")
           + 'menuentry "Uruchom normalnie (dysk wewnetrzny)" {\n  exit\n}\n'
           + 'menuentry "Ustawienia UEFI" {\n  fwsetup\n}\n')
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "grub.cfg")
        open(p, "w").write(cfg)
        _mcopy(efi, p, "::/EFI/ubuntu/grub.cfg")
        _mcopy(efi, p, "::/EFI/BOOT/grub.cfg")
        st = os.path.join(tmp, STATE_FILE)
        json.dump({"active": active, "version": version, "codename": codename}, open(st, "w"))
        _mcopy(efi, st, f"::/{STATE_FILE}")


def _copy_cloud_secret(slot_root: str):
    """Jeśli skonfigurowano chmurę: zaszyfrowane dane dostępowe do Drive w nowym slocie."""
    from . import secrets, service
    if secrets.exists("rclone") and service.has_password():
        service.write_cloud_secret(slot_root)


def install(disk: str | None = None, codename: str | None = None, progress=print,
            mountpoint="/run/timevault/recovery-build") -> dict:
    from . import __version__
    for tool in ("debootstrap", "mcopy"):
        if not sh.which(tool):
            raise SafetyError("Potrzebne: sudo apt install debootstrap mtools")
    parts = find_parts(disk)
    for key in ("efi", "A", "B"):
        _release_desktop_mount(parts[key])
        if blockdev.is_mounted(parts[key]):
            raise SafetyError(L(f"{parts[key]} jest zamontowana — odmontuj ją.", f"{parts[key]} is mounted — unmount it."))
    state = read_state(parts["efi"])
    active = state.get("active")
    slot = "B" if active == "A" else "A"          # zawsze nieaktywny slot
    dev = parts[slot]
    codename = codename or host_codename()
    progress(L(f"Instalacja systemu ratunkowego do slotu {slot} ({dev}).", f"Installing the rescue system into slot {slot} ({dev})."))

    sh.run(["mkfs.ext4", "-q", "-F", "-L", f"TM-RECOV{slot}", dev])
    uuids = {s: blockdev.probe(parts[s]).get("UUID", "") for s in ("A", "B")}
    if not active:
        uuids[("B" if slot == "A" else "A")] = ""           # drugi slot jeszcze pusty
    os.makedirs(mountpoint, exist_ok=True)
    sh.run(["mount", dev, mountpoint])
    try:
        build_rootfs(mountpoint, codename, progress)
        binds = []
        try:
            for b in ("dev", "dev/pts", "proc", "sys"):
                d = os.path.join(mountpoint, b)
                os.makedirs(d, exist_ok=True)
                sh.run(["mount", "--bind", "/" + b, d])
                binds.append(d)
            install_packages(mountpoint, progress)
            progress(L("Konfiguracja…", "Configuring…"))
            configure_rootfs(mountpoint, uuids[slot], slot)
            _copy_cloud_secret(mountpoint)
        finally:
            for d in reversed(binds):
                sh.run(["umount", "-l", d], check=False)
        for need in ("boot/vmlinuz", "boot/initrd.img", "usr/bin/restic", "opt/timevault/cli.py"):
            if not os.path.exists(os.path.join(mountpoint, need)):
                raise RuntimeError(L(f"System ratunkowy niekompletny: brak /{need} — menu NIE zostało przełączone.", f"Rescue system incomplete: /{need} missing — the boot menu was NOT switched."))
        progress(L("Zapis menu startowego…", "Writing the boot menu…"))
        write_efi(parts["efi"], mountpoint, uuids, slot, __version__, codename)
    finally:
        sh.run(["umount", "-R", mountpoint], check=False)
    return {"slot": slot, "device": dev, "codename": codename, "disk": parts["disk"]}
