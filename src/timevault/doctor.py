"""Sprawdzanie zgodności TimeVault z bieżącym systemem — ostrzega ZANIM coś przestanie działać.

Uruchamiane przez usługę po każdej kopii (root, dysk podłączony) i poleceniem `timevault doctor`.
Wynik trafia do status.json („compat”), aplikacja pokazuje ostrzeżenia i powiadomienia.

Poziomy: ok | info | warn | error
"""
from __future__ import annotations

import json
import os
import shutil

from . import blockdev, recovery, sh, system
from .i18n import L

SUPPORTED_ROOT_FS = {"ext4"}


def _item(cid, level, pl, en):
    return {"id": cid, "level": level, "pl": pl, "en": en}


def os_info(root: str = "/") -> dict:
    o = system._os_release(root)
    return {"id": o.get("ID", ""), "version_id": o.get("VERSION_ID", ""),
            "codename": o.get("VERSION_CODENAME", ""), "pretty": o.get("PRETTY_NAME", "")}


def check_root_fs(root: str = "/") -> list[dict]:
    res = []
    fm = system._findmnt(root) or {}
    fstype = fm.get("fstype", "")
    src = fm.get("source", "")
    if fstype not in SUPPORTED_ROOT_FS:
        res.append(_item("rootfs", "error",
                         f"System plików / to {fstype or 'nieznany'} — TimeVault obsługuje na razie tylko ext4. "
                         "Kopie nie będą tworzone.",
                         f"The / filesystem is {fstype or 'unknown'} — TimeVault currently supports ext4 only. "
                         "Backups will not be made."))
    elif src.startswith("/dev/mapper/") or src.startswith("/dev/dm-"):
        res.append(_item("rootfs", "error",
                         "System jest na LVM lub szyfrowaniu (LUKS) — ta wersja TimeVault tego nie obsługuje.",
                         "The system is on LVM or encryption (LUKS) — not supported by this TimeVault version."))
    else:
        res.append(_item("rootfs", "ok", "System plików: ext4", "Filesystem: ext4"))
    if system._findmnt(os.path.join(root, "boot")):
        res.append(_item("bootpart", "error", "Osobna partycja /boot — jeszcze nieobsługiwana.",
                         "Separate /boot partition — not supported yet."))
    return res


def check_bootloader(root: str = "/") -> list[dict]:
    has_grub = any(os.path.exists(os.path.join(root, p)) for p in
                   ("usr/sbin/grub-install", "usr/sbin/update-grub"))
    sd_boot = os.path.isdir(os.path.join(root, "boot/efi/loader/entries")) or \
        os.path.isdir(os.path.join(root, "efi/loader/entries"))
    if not has_grub and sd_boot:
        return [_item("bootloader", "error",
                      "System uruchamia się przez systemd-boot, nie przez GRUB — odtwarzanie wymaga aktualizacji "
                      "TimeVault. Kopie są tworzone, ale odtworzenie może się nie powieść.",
                      "The system boots with systemd-boot, not GRUB — restoring needs a TimeVault update. "
                      "Backups are made, but a restore may fail.")]
    if not has_grub:
        return [_item("bootloader", "error", "Nie znaleziono GRUB-a (grub-install/update-grub).",
                      "GRUB not found (grub-install/update-grub).")]
    return [_item("bootloader", "ok", "Program rozruchowy: GRUB", "Boot loader: GRUB")]


def check_secrets() -> list[dict]:
    from . import secrets
    how = secrets.protection("password")
    if how == "tpm2+host":
        return [_item("secrets", "ok", "Hasło zaszyfrowane (TPM 2.0 + klucz systemu)",
                      "Password encrypted (TPM 2.0 + system key)")]
    if how == "host":
        return [_item("secrets", "ok", "Hasło zaszyfrowane kluczem systemu (brak TPM 2.0)",
                      "Password encrypted with the system key (no TPM 2.0)")]
    if how == "plain":
        return [_item("secrets", "warn", "Hasło zapisane jawnie (tylko root) — brak systemd-creds.",
                      "Password stored in plain text (root only) — systemd-creds missing.")]
    return []


def check_tools() -> list[dict]:
    res = []
    missing = [t for t in ("restic", "mcopy", "debootstrap", "sfdisk", "mkfs.ext4", "mkfs.vfat")
               if not shutil.which(t)]
    if missing:
        res.append(_item("tools", "error", "Brak programów: " + ", ".join(missing),
                         "Missing programs: " + ", ".join(missing)))
    else:
        res.append(_item("tools", "ok", "Wymagane programy są zainstalowane", "Required programs are installed"))
    code = recovery.host_codename()
    if not os.path.exists(f"/usr/share/debootstrap/scripts/{code}"):
        res.append(_item("debootstrap", "warn",
                         f"debootstrap nie zna jeszcze wersji „{code}” — odświeżenie systemu ratunkowego "
                         "może się nie udać (zaktualizuj pakiet debootstrap).",
                         f"debootstrap does not know release “{code}” yet — refreshing the rescue system may "
                         "fail (update the debootstrap package)."))
    return res


def check_recovery_packages() -> list[dict]:
    """Czy pakiety potrzebne do zbudowania systemu ratunkowego są dostępne w tej wersji Ubuntu."""
    names = recovery.PACKAGES + ["linux-firmware"]
    res = sh.run(["apt-cache", "policy", *names], check=False, mutating=False)
    if res.returncode != 0:
        return [_item("pkgs", "info", "Nie udało się sprawdzić pakietów (apt-cache).",
                      "Could not check packages (apt-cache).")]
    missing, current = [], None
    for line in res.stdout.splitlines():
        if line and not line.startswith(" ") and line.endswith(":"):
            current = line[:-1]
        elif current and line.strip().startswith("Candidate:") and "(none)" in line:
            missing.append(current)
    found = {l[:-1] for l in res.stdout.splitlines() if l and not l.startswith(" ") and l.endswith(":")}
    missing += [n for n in names if n not in found]
    if missing:
        return [_item("pkgs", "warn",
                      "W tej wersji Ubuntu brakuje pakietów dla systemu ratunkowego: " + ", ".join(sorted(set(missing)))
                      + ". Odświeżenie systemu ratunkowego się nie uda — obecny nadal działa.",
                      "Packages for the rescue system are missing in this Ubuntu release: "
                      + ", ".join(sorted(set(missing))) + ". Refreshing the rescue system will fail — "
                      "the current one keeps working.")]
    return [_item("pkgs", "ok", "Pakiety systemu ratunkowego dostępne", "Rescue system packages available")]


def check_recovery_state(osi: dict) -> tuple[list[dict], dict]:
    """Wersja systemu ratunkowego na dysku vs. bieżące Ubuntu (wymaga roota i podłączonego dysku)."""
    try:
        parts = recovery.find_parts()
    except Exception:  # noqa: BLE001 — brak dysku/partycji
        return [], {}
    state = recovery.read_state(parts["efi"])
    if not state:
        return [_item("recovery", "warn", "Na dysku nie ma jeszcze systemu ratunkowego — zainstaluj go "
                      "(Więcej → Odśwież system ratunkowy).",
                      "There is no rescue system on the disk yet — install it "
                      "(More → Refresh the rescue system).")], {}
    built = state.get("codename")
    if not built:
        return [_item("recovery", "info",
                      "System ratunkowy pochodzi ze starszej wersji TimeVault — odśwież go, aby aplikacja "
                      "mogła śledzić jego zgodność z Ubuntu.",
                      "The rescue system comes from an older TimeVault version — refresh it so the app "
                      "can track its compatibility with Ubuntu.")], state
    if osi.get("codename") and built != osi["codename"]:
        return [_item("recovery", "warn",
                      f"Ubuntu zaktualizowano ({built} → {osi['codename']}). Odśwież system ratunkowy, żeby "
                      "miał nowe jądro i sterowniki (Więcej → Odśwież system ratunkowy).",
                      f"Ubuntu was upgraded ({built} → {osi['codename']}). Refresh the rescue system so it has "
                      "the new kernel and drivers (More → Refresh the rescue system).")], state
    return [_item("recovery", "ok", f"System ratunkowy zgodny z Ubuntu ({built})",
                  f"Rescue system matches Ubuntu ({built})")], state


def run(root: str = "/", with_recovery: bool = True) -> dict:
    osi = os_info(root)
    items = check_root_fs(root) + check_bootloader(root) + check_tools() + check_recovery_packages()
    if os.geteuid() == 0:
        items += check_secrets()
    rec_state = {}
    if with_recovery and os.geteuid() == 0:
        rec_items, rec_state = check_recovery_state(osi)
        items += rec_items
    order = {"error": 3, "warn": 2, "info": 1, "ok": 0}
    worst = max((order[i["level"]] for i in items), default=0)
    return {"os": osi, "items": items, "worst": [k for k, v in order.items() if v == worst][0],
            "recovery": {"codename": rec_state.get("codename"), "version": rec_state.get("version")}}


def text(items: list[dict]) -> str:
    sym = {"ok": "✓", "info": "ℹ", "warn": "⚠", "error": "✗"}
    return "\n".join(f" {sym[i['level']]} {L(i['pl'], i['en'])}" for i in items)
