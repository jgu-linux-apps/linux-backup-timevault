"""Inspekcja działającego systemu -> manifest potrzebny do odtworzenia na goły dysk."""
from __future__ import annotations

import datetime as dt
import json
import os
import platform
import socket

from . import blockdev, sh

MANIFEST_VERSION = 1
MANIFEST_REL = "var/lib/timevault/manifest.json"
LEGACY_MANIFEST_REL = "var/lib/linux-tm/manifest.json"   # backupy sprzed zmiany nazwy


def _findmnt(target: str) -> dict | None:
    res = sh.run(["findmnt", "-J", "-n", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS", "--mountpoint", target],
                 check=False, mutating=False)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    return json.loads(res.stdout)["filesystems"][0]


def _os_release(root: str) -> dict:
    data = {}
    for p in ("etc/os-release", "usr/lib/os-release"):
        try:
            with open(os.path.join(root, p)) as f:
                for line in f:
                    if "=" in line:
                        k, v = line.rstrip("\n").split("=", 1)
                        data[k] = v.strip('"')
            break
        except OSError:
            continue
    return data


def detect_gpus() -> list[dict]:
    """Karty graficzne z sysfs (klasa PCI 0x03xxxx)."""
    vendors = {"0x10de": "nvidia", "0x8086": "intel", "0x1002": "amd"}
    gpus = []
    base = "/sys/bus/pci/devices"
    try:
        devices = os.listdir(base)
    except OSError:
        return gpus
    for d in devices:
        cls = blockdev._read(f"{base}/{d}/class")
        if cls.startswith("0x03"):
            vid = blockdev._read(f"{base}/{d}/vendor")
            gpus.append({"slot": d, "vendor": vendors.get(vid, vid),
                         "device": blockdev._read(f"{base}/{d}/device")})
    return gpus


def _swap_files(root: str) -> list[dict]:
    swaps = []
    fstab = os.path.join(root, "etc/fstab")
    try:
        lines = open(fstab).read().splitlines()
    except OSError:
        return swaps
    for line in lines:
        f = line.split()
        if len(f) >= 3 and not f[0].startswith("#") and f[2] == "swap" and f[0].startswith("/"):
            path = os.path.join(root, f[0].lstrip("/"))
            size = os.path.getsize(path) if os.path.exists(path) else 0
            swaps.append({"path": f[0], "size": size})
    return swaps


def _package_lists(root: str) -> dict:
    """Listy pakietów: informacyjnie (restore jest 1:1, ale to pomaga przy diagnostyce)."""
    pk = {}
    if root == "/":
        if sh.which("dpkg-query"):
            r = sh.run(["dpkg-query", "-W", "-f", "${Package}\n"], check=False, mutating=False)
            pk["apt_count"] = len(r.stdout.split())
        if sh.which("snap"):
            r = sh.run(["snap", "list"], check=False, mutating=False)
            pk["snaps"] = [l.split()[0] for l in r.stdout.splitlines()[1:] if l.strip()]
        if sh.which("flatpak"):
            r = sh.run(["flatpak", "list", "--columns=application"], check=False, mutating=False)
            pk["flatpaks"] = r.stdout.split()
    return pk


def used_bytes(path: str) -> int:
    st = os.statvfs(path)
    return (st.f_blocks - st.f_bfree) * st.f_frsize


def inspect(root_mount: str = "/") -> dict:
    root_mount = os.path.abspath(root_mount)
    root = _findmnt(root_mount)
    if not root:
        raise RuntimeError(f"{root_mount} nie jest punktem montowania")
    root_dev = root["source"]
    if not root_dev.startswith("/dev/"):
        raise RuntimeError(f"Nieobsługiwane źródło roota: {root_dev}")
    root_info = blockdev.probe(root_dev)
    if root_info.get("TYPE") != "ext4" and root["fstype"] != "ext4":
        raise RuntimeError(f"Na razie obsługiwany jest root na ext4 (jest: {root['fstype']})")

    disk = blockdev.parent_disk(root_dev)
    disk_info = blockdev.read_disk(disk)

    esp = _findmnt(os.path.join(root_mount, "boot/efi"))
    esp_entry = None
    if esp and not esp["source"].startswith("/dev/"):
        # Montowanie bez nazwy urządzenia (np. FUSE) — weź partycję EFI z dysku systemowego.
        cand = [p for p in disk_info.parts if p.parttype == blockdev.ESP_GUID]
        esp = dict(esp, source=cand[0].path) if cand else None
    if esp:
        ei = blockdev.probe(esp["source"])
        esp_entry = {"device": esp["source"], "uuid": ei.get("UUID", ""),
                     "partuuid": ei.get("PART_ENTRY_UUID", ""),
                     "size": blockdev.size_bytes(esp["source"]),
                     "disk": blockdev.parent_disk(esp["source"])}
    boot = _findmnt(os.path.join(root_mount, "boot"))
    if boot:
        raise RuntimeError("Osobna partycja /boot nie jest jeszcze obsługiwana")

    others = [p for p in disk_info.parts
              if p.path != root_dev and (not esp or p.path != esp["source"])]
    windows = [p.to_dict() for p in others if p.fstype in ("ntfs", "BitLocker")
               or p.parttype == "e3c9e316-0b5c-4db8-817d-f92df00215ae"]  # MSR

    osr = _os_release(root_mount)
    luks = any(p.fstype == "crypto_LUKS" for p in disk_info.parts)

    return {
        "manifest_version": MANIFEST_VERSION,
        "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "hostname": socket.gethostname() if root_mount == "/" else osr.get("NAME", ""),
        "source_root": root_mount,
        "os": {"id": osr.get("ID", ""), "version_id": osr.get("VERSION_ID", ""),
               "pretty": osr.get("PRETTY_NAME", "")},
        "kernel": platform.release() if root_mount == "/" else "",
        "boot_mode": "uefi" if os.path.isdir("/sys/firmware/efi") else "bios",
        "root": {"device": root_dev, "fstype": "ext4", "uuid": root_info.get("UUID", ""),
                 "partuuid": root_info.get("PART_ENTRY_UUID", ""),
                 "size": blockdev.size_bytes(root_dev), "used": used_bytes(root_mount),
                 "label": root_info.get("LABEL", "")},
        "esp": esp_entry,
        "disk": {"path": disk, "size": disk_info.size, "model": disk_info.model,
                 "partitions": [p.to_dict() for p in disk_info.parts]},
        "dual_boot": {"windows": bool(windows), "windows_partitions": windows},
        "luks": luks,
        "swap_files": _swap_files(root_mount),
        "gpus": detect_gpus() if root_mount == "/" else [],
        "packages": _package_lists(root_mount),
    }


def write_manifest(manifest: dict, root_mount: str) -> str:
    path = os.path.join(root_mount, MANIFEST_REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    return path
