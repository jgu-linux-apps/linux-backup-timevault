"""Informacje o dyskach i partycjach.

Celowo nie polegamy wyłącznie na bazie udev (lsblk), bo w środowisku Recovery
i w kontenerach może jej nie być: atrybuty czytamy przez `blkid -p` i sysfs.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from . import sh

ESP_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
BIOS_BOOT_GUID = "21686148-6449-6e6f-744e-656564454649"
LINUX_GUID = "0fc63daf-8483-4772-8e79-3d69d8477de4"

# Systemy plików, których restore NIGDY nie nadpisze jako root (np. Windows w dual boot).
PROTECTED_FSTYPES = {"ntfs", "ntfs3", "BitLocker", "exfat", "vfat", "crypto_LUKS",
                     "swap", "LVM2_member", "zfs_member", "linux_raid_member"}


@dataclass
class Part:
    path: str
    number: int
    size: int
    fstype: str = ""
    uuid: str = ""
    label: str = ""
    partuuid: str = ""
    partlabel: str = ""
    parttype: str = ""

    def to_dict(self):
        return self.__dict__.copy()


@dataclass
class Disk:
    path: str
    size: int
    model: str = ""
    removable: bool = False
    transport: str = ""
    parts: list[Part] = field(default_factory=list)


def probe(dev: str) -> dict:
    """Atrybuty urządzenia bezpośrednio z nagłówków (bez udev)."""
    res = sh.run(["blkid", "-p", "-o", "export", dev], check=False, mutating=False)
    info = {}
    for line in res.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v.replace("\\ ", " ").replace("\\\\", "\\")
    return info


def _sys_name(dev: str) -> str:
    return os.path.basename(os.path.realpath(dev))


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def size_bytes(dev: str) -> int:
    name = _sys_name(dev)
    for base in (f"/sys/class/block/{name}",):
        s = _read(f"{base}/size")
        if s:
            return int(s) * 512
    return int(sh.out(["blockdev", "--getsize64", dev]).strip())


def parent_disk(part: str) -> str:
    """/dev/nvme0n1p5 -> /dev/nvme0n1"""
    name = _sys_name(part)
    link = os.path.realpath(f"/sys/class/block/{name}")
    if os.path.exists(os.path.join(link, "partition")):
        return "/dev/" + os.path.basename(os.path.dirname(link))
    return "/dev/" + name


def partition_paths(disk: str) -> list[str]:
    """Partycje dysku w kolejności numerów, z sysfs."""
    name = _sys_name(disk)
    base = f"/sys/block/{name}"
    found = []
    try:
        entries = os.listdir(base)
    except OSError:
        return []
    for e in entries:
        if e.startswith(name) and os.path.exists(f"{base}/{e}/partition"):
            found.append((int(_read(f"{base}/{e}/partition", "0")), "/dev/" + e))
    return [p for _, p in sorted(found)]


def read_disk(disk: str) -> Disk:
    name = _sys_name(disk)
    model = sh.run(["lsblk", "-dno", "MODEL", disk], check=False, mutating=False).stdout.strip()
    d = Disk(path=disk, size=size_bytes(disk),
             model=model or _read(f"/sys/block/{name}/device/model"),
             removable=_read(f"/sys/block/{name}/removable") == "1")
    # Typy i etykiety partycji z tablicy GPT (sfdisk działa bez udev).
    table = {}
    res = sh.run(["sfdisk", "--json", disk], check=False, mutating=False)
    if res.returncode == 0 and res.stdout.strip():
        for p in json.loads(res.stdout)["partitiontable"].get("partitions", []):
            table[os.path.realpath(p["node"])] = p
    for i, pth in enumerate(partition_paths(disk), 1):
        info = probe(pth)
        t = table.get(os.path.realpath(pth), {})
        d.parts.append(Part(
            path=pth,
            number=int(_read(f"/sys/class/block/{_sys_name(pth)}/partition", str(i))),
            size=size_bytes(pth),
            fstype=info.get("TYPE", ""),
            uuid=info.get("UUID", ""),
            label=info.get("LABEL", ""),
            partuuid=(info.get("PART_ENTRY_UUID") or t.get("uuid", "")).lower(),
            partlabel=info.get("PART_ENTRY_NAME") or t.get("name", ""),
            parttype=(info.get("PART_ENTRY_TYPE") or t.get("type", "")).lower(),
        ))
    return d


def list_disks() -> list[Disk]:
    disks = []
    for name in sorted(os.listdir("/sys/block")):
        if name.startswith(("loop", "ram", "zram", "sr", "dm-", "md")):
            continue
        if _read(f"/sys/block/{name}/size", "0") == "0":
            continue
        disks.append(read_disk("/dev/" + name))
    return disks


def mounts() -> list[tuple[str, str, str]]:
    """(urządzenie, punkt montowania, fstype) z /proc/self/mounts."""
    res = []
    with open("/proc/self/mounts") as f:
        for line in f:
            dev, mnt, fstype, *_ = line.split()
            res.append((dev, mnt.replace("\\040", " "), fstype))
    return res


def active_swaps() -> list[str]:
    lines = _read("/proc/swaps").splitlines()[1:]
    return [l.split()[0] for l in lines if l.strip()]


def is_mounted(dev: str) -> bool:
    real = os.path.realpath(dev)
    return any(os.path.realpath(d) == real for d, _, _ in mounts() if d.startswith("/dev/")) \
        or any(os.path.realpath(s) == real for s in active_swaps())


def disk_busy(disk: str) -> list[str]:
    """Zamontowane/używane partycje danego dysku (lub sam dysk)."""
    busy = [disk] if is_mounted(disk) else []
    return busy + [p for p in partition_paths(disk) if is_mounted(p)]


def find_by(key: str, value: str) -> str | None:
    """Szuka urządzenia po UUID / PARTUUID / PARTLABEL / LABEL przez blkid (bez udev)."""
    res = sh.run(["blkid", "-o", "device", "-t", f"{key}={value}"], check=False, mutating=False)
    devs = [l for l in res.stdout.split() if l]
    if devs:
        return devs[0]
    # blkid -t potrafi nie znaleźć urządzeń bez udev; pełne przeszukanie partycji.
    names = {"UUID": "UUID", "PARTUUID": "PART_ENTRY_UUID",
             "PARTLABEL": "PART_ENTRY_NAME", "LABEL": "LABEL"}
    for name in os.listdir("/sys/class/block"):
        dev = "/dev/" + name
        if not os.path.exists(dev):
            continue
        if probe(dev).get(names[key], "").lower() == value.lower():
            return dev
    return None


def mount_dev(dev: str, target: str, options: str | None = None):
    """mount z zapasową ścieżką FUSE dla FAT32 (gdy jądro nie ma vfat)."""
    os.makedirs(target, exist_ok=True)
    cmd = ["mount"] + (["-o", options] if options else []) + [dev, target]
    res = sh.run(cmd, check=False)
    if res.returncode == 0:
        return
    if probe(dev).get("TYPE") == "vfat" and sh.which("fusefat"):
        sh.run(["fusefat", "-o", "rw+", dev, target])
        return
    raise sh.CommandError(cmd, res.returncode, res.stderr)


def settle(disk: str):
    """Po zmianie tablicy partycji: odśwież jądro i poczekaj na węzły urządzeń."""
    sh.run(["partx", "-u", disk], check=False)
    sh.run(["udevadm", "settle"], check=False)
    if sh.DRY_RUN:
        return
    for _ in range(50):
        if all(os.path.exists(p) for p in partition_paths(disk)) and partition_paths(disk):
            return
        time.sleep(0.1)
