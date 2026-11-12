"""Odtworzenie systemu z backupu na goły dysk albo na istniejącą partycję (dual boot).

Dwa tryby:
  * partition – formatuje WYŁĄCZNIE wskazaną partycję Ubuntu, GRUB trafia do istniejącej
                partycji EFI (obok Windows Boot Managera). Partycje Windows są nietykalne.
  * disk      – nowy / wymieniony dysk: wymazuje cały dysk, tworzy EFI + root.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from . import blockdev, sh
from .disk import CONFIRM_WORD, GiB, MiB, SafetyError, assert_disk_wipeable
from .i18n import L

DEFAULT_TARGET = "/mnt/timevault-target"
ESP_SIZE = 512 * MiB


@dataclass
class RestorePlan:
    mode: str                      # "partition" | "disk"
    disk: str
    boot_mode: str                 # "uefi" | "bios"
    root_part: str | None = None
    esp_part: str | None = None
    format_esp: bool = False
    needed: int = 0
    vm_test: bool = False          # odtworzenie do obrazu VM: bez NVRAM hosta, wszystkie dyski nofail
    warnings: list[str] = field(default_factory=list)
    untouched: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [L("Tryb: ", "Mode: ") + (L("cały dysk", "whole disk") if self.mode == "disk"
                                          else L("jedna partycja (dual boot)", "single partition (dual boot)")),
                 L("Dysk: ", "Disk: ") + self.disk]
        if self.mode == "disk":
            lines.append(L(f"  ZOSTANIE WYMAZANY W CAŁOŚCI ({blockdev.size_bytes(self.disk) / GiB:.1f} GiB)",
                            f"  WILL BE COMPLETELY ERASED ({blockdev.size_bytes(self.disk) / GiB:.1f} GiB)"))
            lines.append(L("  Nowy układ: ", "  New layout: ") + ("EFI 512 MiB + " if self.boot_mode == "uefi"
                          else "bios_grub 1 MiB + ") + L("Ubuntu (reszta)", "Ubuntu (rest)"))
        else:
            lines.append(L("  Formatowana partycja Ubuntu: ", "  Ubuntu partition to format: ") + str(self.root_part))
            lines.append(L("  Partycja EFI (bez formatowania): ", "  EFI partition (not formatted): ") + str(self.esp_part))
            for u in self.untouched:
                lines.append(L("  Nietknięta: ", "  Untouched: ") + u)
        lines.append(L("Dane do odtworzenia: ", "Data to restore: ") + f"~{self.needed / GiB:.1f} GiB, "
                     + L("start: ", "boot: ") + self.boot_mode.upper())
        lines += [L("Uwaga: ", "Note: ") + w for w in self.warnings]
        return "\n".join(lines)


def _needed_bytes(manifest) -> int:
    return int(manifest["root"]["used"] * 1.05) + 512 * MiB


def current_boot_mode() -> str:
    return "uefi" if os.path.isdir("/sys/firmware/efi") else "bios"


# --------------------------------------------------------------------------- planowanie
def plan_partition(manifest: dict, root_part: str, esp_part: str | None = None,
                   boot_mode: str | None = None) -> RestorePlan:
    boot_mode = boot_mode or current_boot_mode()
    if manifest.get("luks"):
        raise SafetyError(L("Backup pochodzi z systemu z LUKS — ta wersja jeszcze tego nie obsługuje.",
                             "The backup comes from a LUKS system — not supported by this version yet."))
    if not os.path.exists(root_part):
        raise SafetyError(L(f"Partycja {root_part} nie istnieje.", f"Partition {root_part} does not exist."))
    disk = blockdev.parent_disk(root_part)
    if disk == root_part:
        raise SafetyError(L(f"{root_part} to cały dysk, a nie partycja — użyj trybu 'disk'.",
                             f"{root_part} is a whole disk, not a partition — use 'disk' mode."))
    _assert_root_target(root_part)
    if blockdev.is_mounted(root_part):
        raise SafetyError(f"{root_part} jest zamontowana — odmawiam formatowania.")

    needed = _needed_bytes(manifest)
    size = blockdev.size_bytes(root_part)
    if size < needed:
        raise SafetyError(L(f"Partycja {root_part} ma {size / GiB:.1f} GiB, potrzeba ~{needed / GiB:.1f} GiB.",
                             f"Partition {root_part} has {size / GiB:.1f} GiB, ~{needed / GiB:.1f} GiB needed."))

    plan = RestorePlan(mode="partition", disk=disk, boot_mode=boot_mode,
                       root_part=root_part, needed=needed)

    if boot_mode == "uefi":
        if not esp_part and manifest.get("esp"):
            esp_part = blockdev.find_by("UUID", manifest["esp"]["uuid"])
        if not esp_part:
            raise SafetyError(L("Nie znaleziono partycji EFI — wskaż ją ręcznie (--esp).",
                                 "EFI partition not found — specify it manually (--esp)."))
        info = blockdev.probe(esp_part)
        if info.get("TYPE") != "vfat":
            raise SafetyError(L(f"{esp_part} nie jest partycją EFI (FAT32), jest: {info.get('TYPE') or 'pusta'}.",
                                 f"{esp_part} is not an EFI (FAT32) partition, it is: {info.get('TYPE') or 'empty'}."))
        if blockdev.is_mounted(esp_part):
            raise SafetyError(L(f"Partycja EFI {esp_part} jest zamontowana — odmontuj ją.",
                                 f"EFI partition {esp_part} is mounted — unmount it."))
        plan.esp_part = esp_part

    d = blockdev.read_disk(disk)
    for p in d.parts:
        if p.path not in (root_part, esp_part):
            plan.untouched.append(f"{p.path} {p.fstype or ''} {p.label or p.partlabel}".strip())
    if any("ntfs" in u or "BitLocker" in u for u in plan.untouched):
        plan.warnings.append(L("Na dysku jest Windows — jego partycje nie będą ruszane, GRUB dopisze go do menu.",
                                "Windows is on this disk — its partitions stay untouched, GRUB will list it in the menu."))
    return plan


def plan_disk(manifest: dict, disk: str, boot_mode: str | None = None) -> RestorePlan:
    boot_mode = boot_mode or current_boot_mode()
    if manifest.get("luks"):
        raise SafetyError(L("Backup pochodzi z systemu z LUKS — ta wersja jeszcze tego nie obsługuje.",
                             "The backup comes from a LUKS system — not supported by this version yet."))
    if blockdev.parent_disk(disk) != disk:
        raise SafetyError(L(f"{disk} to partycja — tryb 'disk' wymaga całego dysku.",
                             f"{disk} is a partition — 'disk' mode needs a whole disk."))
    assert_disk_wipeable(disk)
    needed = _needed_bytes(manifest)
    size = blockdev.size_bytes(disk)
    overhead = ESP_SIZE + 4 * MiB
    if size < needed + overhead:
        raise SafetyError(L(f"Dysk {disk} ma {size / GiB:.1f} GiB, potrzeba ~{(needed + overhead) / GiB:.1f} GiB.",
                             f"Disk {disk} has {size / GiB:.1f} GiB, ~{(needed + overhead) / GiB:.1f} GiB needed."))
    plan = RestorePlan(mode="disk", disk=disk, boot_mode=boot_mode, format_esp=True, needed=needed)
    if size < manifest["root"]["size"]:
        plan.warnings.append(L("Nowy dysk jest mniejszy od oryginalnej partycji — dane się mieszczą, partycja będzie mniejsza.",
                                "The new disk is smaller than the original partition — the data fits, the partition will be smaller."))
    d = blockdev.read_disk(disk)
    if any(p.fstype in ("ntfs", "BitLocker") for p in d.parts):
        plan.warnings.append(L("Na tym dysku jest Windows — W TRYBIE 'disk' ZOSTANIE USUNIĘTY.",
                                "Windows is on this disk — IN 'disk' MODE IT WILL BE DELETED."))
    return plan


def _assert_root_target(part: str):
    fstype = blockdev.probe(part).get("TYPE", "")
    if fstype in blockdev.PROTECTED_FSTYPES:
        raise SafetyError(L(f"{part} zawiera {fstype} — to nie wygląda na partycję Ubuntu. Odmawiam.",
                             f"{part} contains {fstype} — it does not look like an Ubuntu partition. Refusing."))


# --------------------------------------------------------------------------- wykonanie
def execute(plan: RestorePlan, repo, snapshot: str, manifest: dict, confirm: str,
            target: str = DEFAULT_TARGET, skip_bootloader: bool = False, progress=print) -> dict:
    if confirm != CONFIRM_WORD:
        raise SafetyError(L(f"Brak potwierdzenia. Wpisz {CONFIRM_WORD}, aby kontynuować.",
                             f"Not confirmed. Type {CONFIRM_WORD} to continue."))

    # 1. Partycje
    if plan.mode == "disk":
        assert_disk_wipeable(plan.disk)          # ponowna kontrola tuż przed zapisem
        progress(L("Tworzenie partycji…", "Creating partitions…"))
        _partition_disk(plan)
    else:
        _assert_root_target(plan.root_part)
        if blockdev.is_mounted(plan.root_part):
            raise SafetyError(L(f"{plan.root_part} została zamontowana w międzyczasie — przerywam.",
                                 f"{plan.root_part} got mounted in the meantime — aborting."))

    progress(L(f"Formatowanie {plan.root_part} (ext4)…", f"Formatting {plan.root_part} (ext4)…"))
    label = manifest["root"].get("label") or ""
    cmd = ["mkfs.ext4", "-q", "-F"] + (["-L", label] if label else []) + [plan.root_part]
    sh.run(cmd)
    if plan.format_esp and plan.esp_part:
        sh.run(["mkfs.vfat", "-F", "32", "-n", "EFI", plan.esp_part])

    root_uuid = blockdev.probe(plan.root_part).get("UUID", "")
    esp_uuid = blockdev.probe(plan.esp_part).get("UUID", "") if plan.esp_part else ""

    # 2. Montowanie i wylanie danych
    os.makedirs(target, exist_ok=True)
    blockdev.mount_dev(plan.root_part, target)
    try:
        progress(L(f"Odtwarzanie kopii {snapshot[:8]}… (to potrwa)", f"Restoring snapshot {snapshot[:8]}… (this takes a while)"))
        repo.restore(snapshot, manifest["source_root"], target)
        # EFI montujemy dopiero teraz: backup nie zawiera jej zawartości, a restore
        # w ten sposób w ogóle jej nie dotyka (ważne przy wspólnej EFI z Windows).
        if plan.esp_part:
            os.makedirs(os.path.join(target, "boot/efi"), exist_ok=True)
            blockdev.mount_dev(plan.esp_part, os.path.join(target, "boot/efi"))

        # 3. Poprawki po restore
        progress(L("Aktualizacja fstab i UUID…", "Updating fstab and UUIDs…"))
        fstab_report = fix_fstab(target, root_uuid, esp_uuid, force_nofail=plan.vm_test)
        _recreate_runtime_dirs(target)
        _recreate_swapfiles(target, manifest)
        _hardware_cleanup(target)
        install_firstboot(target, manifest)
        if plan.vm_test:
            _vm_tweaks(target)

        if not skip_bootloader:
            progress(L("Instalacja GRUB-a i przebudowa initramfs…", "Installing GRUB and rebuilding initramfs…"))
            chroot_bootloader(target, plan)
        sh.run(["sync"], check=False)
    finally:
        sh.run(["umount", "-R", target], check=False)

    return {"root_part": plan.root_part, "root_uuid": root_uuid,
            "esp_part": plan.esp_part, "esp_uuid": esp_uuid, "fstab": fstab_report}


def _partition_disk(plan: RestorePlan):
    sh.run(["wipefs", "-a", plan.disk])
    if plan.boot_mode == "uefi":
        script = (f"label: gpt\nsize={ESP_SIZE // 512}, type={blockdev.ESP_GUID}, name=\"EFI\"\n"
                  f"type={blockdev.LINUX_GUID}, name=\"ubuntu\"\n")
    else:
        script = (f"label: gpt\nsize={1 * MiB // 512}, type={blockdev.BIOS_BOOT_GUID}, name=\"bios_grub\"\n"
                  f"type={blockdev.LINUX_GUID}, name=\"ubuntu\"\n")
    sh.run(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", plan.disk], input=script)
    blockdev.settle(plan.disk)
    parts = blockdev.partition_paths(plan.disk)
    if len(parts) != 2:
        raise RuntimeError(f"Oczekiwano 2 partycji na {plan.disk}, jest {len(parts)}")
    plan.root_part = parts[1]
    plan.esp_part = parts[0] if plan.boot_mode == "uefi" else None


# --------------------------------------------------------------------------- fstab
_DEVICE_KEYS = ("UUID=", "PARTUUID=", "LABEL=", "PARTLABEL=")


def _device_present(spec: str) -> bool:
    for k in _DEVICE_KEYS:
        if spec.startswith(k):
            return blockdev.find_by(k[:-1], spec[len(k):].strip('"')) is not None
    if spec.startswith("/dev/"):
        return os.path.exists(spec)
    return True  # tmpfs, nfs, pliki swap itp.


def fix_fstab(target: str, root_uuid: str, esp_uuid: str, force_nofail: bool = False) -> list[str]:
    """Podmienia UUID roota i EFI; brakujące dyski (np. 'Dane' z innego laptopa) dostają nofail,
    żeby system nie utknął przy starcie. force_nofail: traktuj wszystkie inne dyski jak
    nieobecne (test w VM — maszyna wirtualna nie widzi dysków laptopa)."""
    path = os.path.join(target, "etc/fstab")
    if not os.path.exists(path):
        raise RuntimeError("W odtworzonym systemie brakuje /etc/fstab")
    shutil.copy2(path, path + ".timevault-orig")
    report, out = [], []
    for line in open(path).read().splitlines():
        f = line.split()
        if len(f) < 4 or line.lstrip().startswith("#"):
            out.append(line)
            continue
        spec, mnt, fstype, opts = f[0], f[1], f[2], f[3]
        rest = f[4:] or ["0", "0"]
        changed = False
        if mnt == "/":
            spec, changed = f"UUID={root_uuid}", True
            report.append(f"/ -> UUID={root_uuid}")
        elif mnt == "/boot/efi" and esp_uuid:
            spec, changed = f"UUID={esp_uuid}", True
            report.append(f"/boot/efi -> UUID={esp_uuid}")
        elif fstype != "swap" and spec.startswith(_DEVICE_KEYS + ("/dev/",)) \
                and (force_nofail or not _device_present(spec)):
            o = opts.split(",")
            for add in ("nofail", "x-systemd.device-timeout=5s"):
                if not any(x.startswith(add.split("=")[0]) for x in o):
                    o.append(add)
            opts, changed = ",".join(o), True
            report.append(f"{mnt}: urządzenie {spec} nieobecne -> nofail")
        elif fstype == "swap" and spec.startswith(_DEVICE_KEYS + ("/dev/",)) \
                and (force_nofail or not _device_present(spec)):
            line = "# timevault: brak urządzenia swap: " + line
            out.append(line)
            report.append(f"swap {spec} nieobecny -> zakomentowany")
            continue
        if mnt.startswith("/") and fstype != "swap":
            os.makedirs(os.path.join(target, mnt.lstrip("/")), exist_ok=True)
        out.append("\t".join([spec, mnt, fstype, opts, *rest]) if changed else line)
    with open(path, "w") as fh:
        fh.write("\n".join(out) + "\n")
    return report


def _recreate_runtime_dirs(target: str):
    for d, mode in (("proc", 0o555), ("sys", 0o555), ("dev", 0o755), ("run", 0o755),
                    ("tmp", 0o1777), ("var/tmp", 0o1777), ("mnt", 0o755), ("media", 0o755)):
        p = os.path.join(target, d)
        os.makedirs(p, exist_ok=True)
        os.chmod(p, mode)


def _recreate_swapfiles(target: str, manifest: dict):
    for s in manifest.get("swap_files", []):
        p = os.path.join(target, s["path"].lstrip("/"))
        size_mib = max(1, s["size"] // MiB)
        if os.path.exists(p):
            continue
        sh.run(["fallocate", "-l", f"{size_mib}M", p], check=False)
        if not os.path.exists(p):
            sh.run(["dd", "if=/dev/zero", f"of={p}", "bs=1M", f"count={size_mib}", "status=none"])
        os.chmod(p, 0o600)
        sh.run(["mkswap", "-q", p])


def _vm_tweaks(target: str):
    """Tylko obraz testowy VM: dysk wirtualny bywa na wolnym USB — pierwszy start pulpitu
    trwa wtedy dłużej niż domyślny limit systemd na start GNOME Shell (90 s)."""
    d = os.path.join(target, "etc/systemd/user/org.gnome.Shell@.service.d")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "timevault-vm.conf"), "w") as f:
        f.write("[Service]\nTimeoutStartSec=15min\n")


def _hardware_cleanup(target: str):
    """Rzeczy przypięte do starego sprzętu."""
    for rel in ("etc/udev/rules.d/70-persistent-net.rules",):
        p = os.path.join(target, rel)
        if os.path.exists(p):
            os.remove(p)


# --------------------------------------------------------------------------- pierwszy start
FIRSTBOOT_SCRIPT = r"""#!/bin/sh
# timevault: jednorazowe dopasowanie do nowego sprzętu po odtworzeniu z backupu.
LOG=/var/log/timevault-firstboot.log
exec >>"$LOG" 2>&1
echo "== $(date) timevault firstboot =="
has_nvidia() {
  for d in /sys/bus/pci/devices/*; do
    [ "$(cat "$d/vendor" 2>/dev/null)" = 0x10de ] || continue
    case "$(cat "$d/class" 2>/dev/null)" in 0x03*) return 0 ;; esac
  done
  return 1
}
if has_nvidia; then
  if ! lsmod | grep -q '^nvidia '; then
    if command -v ubuntu-drivers >/dev/null && timeout 5 getent hosts archive.ubuntu.com >/dev/null; then
      echo "NVIDIA bez załadowanego sterownika -> ubuntu-drivers install"
      timeout 600 ubuntu-drivers install || true
    else
      echo "NVIDIA bez sterownika, brak sieci — pomijam (doinstaluj: sudo ubuntu-drivers install)"
    fi
  fi
else
  if command -v prime-select >/dev/null && [ "$(prime-select query 2>/dev/null)" = "nvidia" ]; then
    echo "Brak karty NVIDIA -> prime-select on-demand"
    prime-select on-demand || true
  fi
  if [ -f /etc/X11/xorg.conf ] && grep -q nvidia /etc/X11/xorg.conf; then
    mv /etc/X11/xorg.conf /etc/X11/xorg.conf.timevault-disabled
  fi
fi
systemctl disable timevault-firstboot.service
rm -f /etc/systemd/system/timevault-firstboot.service /usr/local/lib/timevault/firstboot.sh
"""

FIRSTBOOT_UNIT = """[Unit]
Description=TimeVault: dopasowanie systemu do sprzętu po odtworzeniu
After=local-fs.target
Before=display-manager.service

[Service]
Type=oneshot
ExecStart=/usr/local/lib/timevault/firstboot.sh
TimeoutStartSec=15min

[Install]
WantedBy=multi-user.target
"""


def install_firstboot(target: str, manifest: dict):
    lib = os.path.join(target, "usr/local/lib/timevault")
    os.makedirs(lib, exist_ok=True)
    script = os.path.join(lib, "firstboot.sh")
    with open(script, "w") as f:
        f.write(FIRSTBOOT_SCRIPT)
    os.chmod(script, 0o755)
    unit_dir = os.path.join(target, "etc/systemd/system")
    os.makedirs(os.path.join(unit_dir, "multi-user.target.wants"), exist_ok=True)
    with open(os.path.join(unit_dir, "timevault-firstboot.service"), "w") as f:
        f.write(FIRSTBOOT_UNIT)
    link = os.path.join(unit_dir, "multi-user.target.wants/timevault-firstboot.service")
    if not os.path.lexists(link):
        os.symlink("/etc/systemd/system/timevault-firstboot.service", link)


# --------------------------------------------------------------------------- bootloader
def _update_grub_by_uuid(target: str, plan: RestorePlan, chroot):
    """update-grub wpisuje root=UUID=… tylko gdy istnieje /dev/disk/by-uuid/<uuid>.
    Bez udev (albo zanim udev zdąży) wpisałby nazwę urządzenia, np. /dev/loop2p2 albo
    /dev/sdb2 — a po restarcie to urządzenie nazywa się inaczej i system nie wstaje."""
    uuid = blockdev.probe(plan.root_part).get("UUID", "")
    sh.run(["udevadm", "settle"], check=False)
    link = f"/dev/disk/by-uuid/{uuid}"
    created = False
    if uuid and not os.path.lexists(link):
        os.makedirs("/dev/disk/by-uuid", exist_ok=True)
        os.symlink(os.path.realpath(plan.root_part), link)
        created = True
    try:
        if plan.vm_test:
            # os-prober dopisałby do menu VM dyski laptopa (Windows, obecne Ubuntu)
            sh.run(["chroot", target, "update-grub"], env={"GRUB_DISABLE_OS_PROBER": "true"})
        else:
            chroot("update-grub")
    finally:
        if created:
            os.remove(link)
    cfg = open(os.path.join(target, "boot/grub/grub.cfg")).read()
    if f"root=UUID={uuid}" not in cfg:
        raise RuntimeError(L(f"grub.cfg nie wskazuje root=UUID={uuid} — system by nie wystartował. Przerywam.",
                             f"grub.cfg does not point to root=UUID={uuid} — the system would not boot. Aborting."))


def _write_efi_stubs(target: str, plan: RestorePlan, dirs: list[str]):
    """Podpisany GRUB Ubuntu szuka konfiguracji w EFI/ubuntu/grub.cfg (na sztywno w obrazie),
    a grub-install --removable jej tam nie tworzy -> konsola grub>. Zawsze zapisujemy
    stub wskazujący NOWE UUID roota (stary stub mógł wskazywać poprzednią instalację)."""
    uuid = blockdev.probe(plan.root_part).get("UUID", "")
    stub = (f"search.fs_uuid {uuid} root\n"
            "set prefix=($root)'/boot/grub'\n"
            "configfile $prefix/grub.cfg\n")
    for d in dirs:
        path = os.path.join(target, "boot/efi/EFI", d)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "grub.cfg"), "w") as f:
            f.write(stub)


def chroot_bootloader(target: str, plan: RestorePlan):
    binds = ["dev", "dev/pts", "proc", "sys", "run"]
    mounted = []
    try:
        for b in binds:
            dst = os.path.join(target, b)
            os.makedirs(dst, exist_ok=True)
            sh.run(["mount", "--bind", "/" + b, dst])
            mounted.append(dst)
        efivars = "/sys/firmware/efi/efivars"
        if plan.boot_mode == "uefi" and os.path.isdir(efivars):
            dst = os.path.join(target, efivars.lstrip("/"))
            sh.run(["mount", "--bind", efivars, dst], check=False)
            mounted.append(dst)

        def chroot(*cmd, check=True):
            return sh.run(["chroot", target, *cmd], check=check)

        efi = ["grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi"]
        if plan.boot_mode == "uefi" and plan.vm_test:
            # NIGDY nie dotykamy NVRAM prawdziwego laptopa. EFI/ubuntu + EFI/BOOT (domyślna
            # ścieżka, którą firmware VM uruchamia sam).
            chroot(*efi, "--bootloader-id=ubuntu", "--no-nvram")
            chroot(*efi, "--removable", "--no-nvram")
            _write_efi_stubs(target, plan, ["ubuntu", "BOOT"])
        elif plan.boot_mode == "uefi":
            res = chroot(*efi, "--bootloader-id=ubuntu", "--recheck", check=False)
            if res.returncode != 0 and plan.mode != "disk":
                raise RuntimeError(f"grub-install nie powiódł się:\n{res.stderr}")
            if res.returncode != 0:
                # Firmware odrzuca wpis NVRAM — pliki bez wpisu.
                chroot(*efi, "--bootloader-id=ubuntu", "--no-nvram")
            dirs = ["ubuntu"]
            if plan.mode == "disk":
                # Nowy dysk: zapasowa kopia w ścieżce domyślnej — wystartuje nawet, gdy
                # firmware zgubi wpis NVRAM. W trybie dual boot EFI/BOOT należy do Windows.
                chroot(*efi, "--removable", "--no-nvram")
                dirs.append("BOOT")
            _write_efi_stubs(target, plan, dirs)
        else:
            chroot("grub-install", "--target=i386-pc", "--recheck", plan.disk)
        chroot("update-initramfs", "-u", "-k", "all")
        _update_grub_by_uuid(target, plan, chroot)
    finally:
        for m in reversed(mounted):
            sh.run(["umount", "-l", m], check=False)
