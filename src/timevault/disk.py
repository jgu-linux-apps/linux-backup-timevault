"""Przygotowanie dysku zewnętrznego: bootowalne Recovery + partycja na backup.

Układ (GPT):
  p1  TM-EFI         512 MiB  FAT32   GRUB/shim do startu Recovery
  p2  TM-RECOVERY-A  3 GiB    ext4    system ratunkowy (slot A)
  p3  TM-RECOVERY-B  3 GiB    ext4    system ratunkowy (slot B, bezpieczne aktualizacje)
  p4  TM-DATA        reszta   ext4    repozytorium restic + manifesty + cache

Backup montuje WYŁĄCZNIE TM-DATA. Partycje EFI i Recovery są zapisywane tylko przez
aktualizator Recovery, nigdy przez backup.
"""
from __future__ import annotations

import os

from . import blockdev, sh
from .i18n import L

CONFIRM_WORD = "WYMAZ"

LABEL_EFI = "TM-EFI"
LABEL_RECOVERY_A = "TM-RECOVERY-A"
LABEL_RECOVERY_B = "TM-RECOVERY-B"
LABEL_DATA = "TM-DATA"

MiB = 1024 * 1024
GiB = 1024 * MiB


class SafetyError(RuntimeError):
    """Operacja zablokowana, bo mogłaby zniszczyć dane, których nie powinna dotykać."""


def system_disks() -> set[str]:
    """Dyski, z których działa bieżący system (root, /boot, /boot/efi, swap)."""
    res = set()
    for dev, mnt, _ in blockdev.mounts():
        if mnt in ("/", "/boot", "/boot/efi", "/usr", "/var", "/home") and dev.startswith("/dev/"):
            res.add(os.path.realpath(blockdev.parent_disk(dev)))
    for s in blockdev.active_swaps():
        if s.startswith("/dev/"):
            res.add(os.path.realpath(blockdev.parent_disk(s)))
    return res


def assert_disk_wipeable(disk: str):
    real = os.path.realpath(disk)
    if real in system_disks():
        raise SafetyError(L(f"{disk} to dysk, z którego działa ten system — odmawiam wymazania.",
                            f"{disk} is the disk this system runs from — refusing to erase it."))
    busy = blockdev.disk_busy(disk)
    if busy:
        raise SafetyError(L(f"Na {disk} są zamontowane partycje: {', '.join(busy)}. Odmontuj je najpierw.",
                            f"{disk} has mounted partitions: {', '.join(busy)}. Unmount them first."))


def describe(disk: str) -> str:
    d = blockdev.read_disk(disk)
    parts = ", ".join(f"{os.path.basename(p.path)} {p.fstype or '?'} {p.label or p.partlabel}".strip()
                      for p in d.parts) or "brak partycji"
    return f"{disk}  {d.model or 'nieznany model'}  {d.size / GiB:.1f} GiB  [{parts}]"


TIMEVAULT_PARTLABELS = {"TM-EFI", "TM-RECOVERY-A", "TM-RECOVERY-B", "TM-DATA"}
TIMEVAULT_FSLABELS = {"TM-EFI", "TM-RECOVA", "TM-RECOVB", "TM-DATA"}


def timevault_partitions(disk: str) -> list[str]:
    """Partycje TimeVault na dysku — odczyt bezpośrednio z GPT i nagłówków (blkid -p,
    sfdisk), bez polegania na bazie udev, która bywa niekompletna."""
    return [p.path for p in blockdev.read_disk(disk).parts
            if p.partlabel in TIMEVAULT_PARTLABELS or p.label in TIMEVAULT_FSLABELS]


def prepare_backup_disk(disk: str, confirm: str, recovery_size=3 * GiB, efi_size=512 * MiB,
                        erase_existing_timevault: bool = False) -> dict:
    if confirm != CONFIRM_WORD:
        raise SafetyError(L(f"Brak potwierdzenia. Aby wymazać {disk}, wpisz: {CONFIRM_WORD}",
                            f"Not confirmed. To erase {disk}, type: {CONFIRM_WORD}"))
    assert_disk_wipeable(disk)
    existing = timevault_partitions(disk)
    if existing and not erase_existing_timevault:
        raise SafetyError(L(
            f"{disk} jest już dyskiem TimeVault (z kopiami i systemem ratunkowym: {', '.join(existing)}). "
            "Odmawiam wymazania. Aby go używać, wybierz „Użyj tego dysku” (tylko hasło).",
            f"{disk} is already a TimeVault disk (with backups and the rescue system: {', '.join(existing)}). "
            "Refusing to erase it. To use it, choose “Use this disk” (password only)."))
    size = blockdev.size_bytes(disk)
    minimum = efi_size + 2 * recovery_size + 4 * GiB
    if size < minimum:
        raise SafetyError(L(f"Dysk za mały ({size / GiB:.1f} GiB), potrzeba min. {minimum / GiB:.1f} GiB.",
                            f"Disk too small ({size / GiB:.1f} GiB), at least {minimum / GiB:.1f} GiB needed."))

    script = "\n".join([
        "label: gpt",
        f"size={efi_size // 512}, type={blockdev.ESP_GUID}, name=\"{LABEL_EFI}\"",
        f"size={recovery_size // 512}, type={blockdev.LINUX_GUID}, name=\"{LABEL_RECOVERY_A}\"",
        f"size={recovery_size // 512}, type={blockdev.LINUX_GUID}, name=\"{LABEL_RECOVERY_B}\"",
        f"type={blockdev.LINUX_GUID}, name=\"{LABEL_DATA}\"",
    ]) + "\n"
    sh.run(["wipefs", "-a", disk])
    sh.run(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", disk], input=script)
    blockdev.settle(disk)
    parts = blockdev.partition_paths(disk)
    if not sh.DRY_RUN and len(parts) != 4:
        raise RuntimeError(f"Oczekiwano 4 partycji na {disk}, jest {len(parts)}")
    if sh.DRY_RUN:
        parts = [f"{disk}p{i}" for i in range(1, 5)]
    efi, ra, rb, data = parts
    sh.run(["mkfs.vfat", "-F", "32", "-n", "TM-EFI", efi])
    sh.run(["mkfs.ext4", "-q", "-F", "-L", "TM-RECOVA", ra])
    sh.run(["mkfs.ext4", "-q", "-F", "-L", "TM-RECOVB", rb])
    sh.run(["mkfs.ext4", "-q", "-F", "-L", LABEL_DATA, "-m", "0", data])
    return {"efi": efi, "recovery_a": ra, "recovery_b": rb, "data": data}


def find_data_partition() -> str | None:
    return blockdev.find_by("PARTLABEL", LABEL_DATA)


def mount(dev: str, target: str, options: str | None = None, readonly=False):
    opts = [o for o in [options, "ro" if readonly else None] if o]
    blockdev.mount_dev(dev, target, ",".join(opts) or None)


def umount(target: str, recursive=False):
    sh.run(["umount", "-R", target] if recursive else ["umount", target], check=False)
