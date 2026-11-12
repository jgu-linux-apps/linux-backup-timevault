"""Konfiguracja „wszystko w jednym”: dysk -> partycje -> hasło -> repozytorium ->
Recovery -> automatyczne kopie (pierwszy backup startuje w tle)."""
from __future__ import annotations

from . import disk, recovery, service, sh
from .i18n import L


def new_disk(disk_path: str, password: str, confirm: str, codename=None,
             with_recovery=True, progress=print, erase_existing_timevault=False) -> dict:
    if len(password) < 8:
        raise disk.SafetyError(L("Hasło musi mieć co najmniej 8 znaków.", "The password must have at least 8 characters."))
    progress(L("1/4 Przygotowanie dysku (partycje)…", "1/4 Preparing the disk (partitions)…"))
    parts = disk.prepare_backup_disk(disk_path, confirm, erase_existing_timevault=erase_existing_timevault)
    progress(L("2/4 Zapis hasła i utworzenie repozytorium…", "2/4 Saving the password and creating the repository…"))
    service.store_password(password)
    _, data = service.mount_data()
    repo = service.open_repo(data)
    repo.init()
    service.unmount_data()
    service.save_config({**service.load_config(), "auto": True})
    service.write_status(state="idle", disk_connected=True, snapshots=[])
    if with_recovery:
        progress(L("3/4 Instalacja systemu ratunkowego (pobieranie, kilka minut)…", "3/4 Installing the rescue system (download, a few minutes)…"))
        recovery.install(disk_path, codename, progress)
    progress(L("4/4 Start pierwszej kopii w tle…", "4/4 Starting the first backup in the background…"))
    start_backup_service()
    return parts


def existing_disk(password: str, progress=print) -> int:
    """Dysk już przygotowany (np. wcześniejszą wersją): sprawdź hasło, zapamiętaj, włącz auto."""
    mounted = service.mount_data()
    if not mounted:
        raise disk.SafetyError(L("Nie znaleziono dysku TimeVault (partycja TM-DATA). Podłącz go.", "TimeVault disk not found (TM-DATA partition). Plug it in."))
    _, data = mounted
    service.store_password(password)
    repo = service.open_repo(data)
    try:
        snaps = repo.snapshots()
    except sh.CommandError:
        from . import secrets
        secrets.remove("password")
        raise disk.SafetyError(L("Złe hasło — nie udało się otworzyć kopii.", "Wrong password — could not open the backups."))
    finally:
        if data == service.DATA_MOUNT:
            service.unmount_data()
    service.save_config({**service.load_config(), "auto": True})
    service.write_status(state="idle", disk_connected=True,
                         snapshots=[{"id": s["short_id"], "time": service.iso_utc(s["time"]),
                                     "tags": s.get("tags", [])} for s in snaps])
    progress(L(f"Połączono z dyskiem TimeVault: {len(snaps)} kopii.", f"Connected to the TimeVault disk: {len(snaps)} backups."))
    return len(snaps)


def start_backup_service():
    sh.run(["systemctl", "start", "--no-block", "timevault-backup-now.service"], check=False)
