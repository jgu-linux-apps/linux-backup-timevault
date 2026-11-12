"""Wykrywanie dysków po stronie użytkownika (bez roota, bez Qt): lsblk + dowiązania udev."""
from __future__ import annotations

import json
import os
import subprocess

from .i18n import L


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def build_tree(flat: list[dict]) -> list[dict]:
    """lsblk -J zwraca drzewo tylko z kolumną NAME — bez niej płaską listę (tak jest na
    Ubuntu 26.10). Składamy drzewo sami po PKNAME, niezależnie od kształtu odpowiedzi."""
    devs = []

    def walk(items):
        for d in items:
            kids = d.pop("children", None) or []
            devs.append(d)
            walk(kids)
    walk(flat)
    by_name = {d.get("name"): d for d in devs if d.get("name")}
    roots = []
    for d in devs:
        d.setdefault("children", [])
    for d in devs:
        parent = by_name.get(d.get("pkname"))
        if parent is not None and parent is not d:
            parent["children"].append(d)
        else:
            roots.append(d)
    return roots


def lsblk() -> list[dict]:
    try:
        out = subprocess.run(["lsblk", "-J", "-b", "-o",
                              "NAME,PKNAME,PATH,SIZE,MODEL,TRAN,TYPE,HOTPLUG,RM,PARTLABEL,LABEL,FSTYPE,MOUNTPOINTS"],
                             capture_output=True, text=True, timeout=10).stdout
        return build_tree(json.loads(out).get("blockdevices", []))
    except Exception:  # noqa: BLE001
        return []


def _children(d):
    for c in d.get("children") or []:
        yield c
        yield from _children(c)


TV_PARTLABELS = {"TM-EFI", "TM-RECOVERY-A", "TM-RECOVERY-B", "TM-DATA"}
TV_FSLABELS = {"TM-EFI", "TM-RECOVA", "TM-RECOVB", "TM-DATA"}


def _udev_link_disks() -> set[str]:
    """Dyski, na które wskazują dowiązania /dev/disk/by-partlabel|by-label/TM-* (drugie
    źródło obok lsblk — gdy jedno zawiedzie, drugie nadal chroni dysk z kopiami)."""
    disks = set()
    for base, names in (("/dev/disk/by-partlabel", TV_PARTLABELS), ("/dev/disk/by-label", TV_FSLABELS)):
        for n in names:
            p = os.path.join(base, n)
            if os.path.exists(p):
                part = os.path.basename(os.path.realpath(p))
                sysp = os.path.realpath(f"/sys/class/block/{part}")
                if os.path.exists(os.path.join(sysp, "partition")):
                    disks.add("/dev/" + os.path.basename(os.path.dirname(sysp)))
    return disks


def is_timevault_disk(d: dict) -> bool:
    if any(c.get("partlabel") in TV_PARTLABELS or c.get("label") in TV_FSLABELS for c in _children(d)):
        return True
    return os.path.realpath(d["path"]) in {os.path.realpath(x) for x in _udev_link_disks()}


def timevault_disk() -> dict | None:
    for d in lsblk():
        if d.get("type") == "disk" and is_timevault_disk(d):
            return d
    return None


def timevault_disk_connected() -> bool:
    return timevault_disk() is not None


def disk_name(d: dict) -> str:
    return f"{(d.get('model') or L('dysk', 'disk')).strip()} {human(int(d['size']))}"


def _external(d: dict) -> bool:
    """Tylko dyski zewnętrzne (USB / hot-plug / wymienne) — nigdy wewnętrzny dysk laptopa."""
    return (d.get("tran") == "usb" or d.get("hotplug") in (True, "1", 1)
            or d.get("rm") in (True, "1", 1))


def candidate_disks() -> list[dict]:
    res = []
    for d in lsblk():
        if d.get("type") != "disk" or d["path"].startswith(("/dev/loop", "/dev/zram")):
            continue
        if not _external(d):
            continue
        kids = list(_children(d))
        mps = [m for c in kids for m in (c.get("mountpoints") or []) if m]
        if any(m in ("/", "/boot", "/boot/efi", "/home", "[SWAP]") for m in mps):
            continue
        if is_timevault_disk(d):          # dysk z kopiami nigdy nie jest „nowym dyskiem”
            continue
        res.append(d)
    return res
