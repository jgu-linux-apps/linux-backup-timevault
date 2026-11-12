"""Część działająca jako root w tle (systemd): automatyczne backupy, status dla GUI,
przeglądanie kopii.

  /etc/timevault/password      hasło repozytorium (0600 root) — do backupów bez pytania
  /etc/timevault/config.json   {"auto": true}
  /var/lib/timevault/status.json  stan dla GUI (czytelny dla wszystkich, BEZ sekretów)
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import shutil
import time

from . import blockdev, disk, sh, system
from .repo import Repo

ETC = "/etc/timevault"
PASSWORD_FILE = f"{ETC}/password"
CONFIG_FILE = "/var/lib/timevault/config.json"   # bez sekretów — GUI czyta bez roota
STATE_DIR = "/var/lib/timevault"
STATUS_FILE = f"{STATE_DIR}/status.json"
LOCK_FILE = "/run/timevault.lock"
DATA_MOUNT = "/run/timevault/data"
# AppArmor (Ubuntu 25.04+) pozwala fusermount3 montować tylko w /mnt, /media, /tmp, $HOME, /run/user/<uid>
BROWSE_MOUNT = "/mnt/timevault-browse"
# Częstotliwość kopii automatycznych (timer systemd budzi usługę co godzinę; tu decydujemy,
# czy kopia jest już „należna”). Margines 10 min, żeby kopia nie przeskoczyła o całe okno.
INTERVALS = {"1h": 3600, "4h": 4 * 3600, "24h": 24 * 3600, "7d": 7 * 24 * 3600}
MARGIN = 10 * 60
CONNECT_MIN_GAP = 15 * 60       # po podłączeniu: nie częściej niż co 15 min (wielokrotne podłączanie)
DEFAULT_CONFIG = {"auto": True, "interval": "1h", "on_connect": True, "language": "auto",
                  "cloud": False, "cloud_interval": "24h", "cloud_ac_only": True}
CLOUD_INTERVALS = {"6h": 6 * 3600, "24h": 24 * 3600, "7d": 7 * 24 * 3600, "off": None}
CLOUD_LOCK_FILE = "/run/timevault-cloud.lock"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- konfiguracja
def load_config() -> dict:
    """Ustawienia z walidacją — plik może edytować administrator bez roota (grupa sudo)."""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        raw = {}
    if isinstance(raw.get("auto"), bool):
        cfg["auto"] = raw["auto"]
    if raw.get("interval") in INTERVALS:
        cfg["interval"] = raw["interval"]
    if isinstance(raw.get("on_connect"), bool):
        cfg["on_connect"] = raw["on_connect"]
    if raw.get("language") in ("auto", "pl", "en"):
        cfg["language"] = raw["language"]
    for key in ("cloud", "cloud_ac_only"):
        if isinstance(raw.get(key), bool):
            cfg[key] = raw[key]
    if raw.get("cloud_interval") in CLOUD_INTERVALS:
        cfg["cloud_interval"] = raw["cloud_interval"]
    return cfg


def save_config(cfg: dict):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o664)
    try:                                          # administrator zmienia ustawienia bez hasła
        import grp
        os.chown(tmp, 0, grp.getgrnam("sudo").gr_gid)
    except (KeyError, PermissionError):
        pass
    os.replace(tmp, CONFIG_FILE)


def store_password(password: str):
    """Hasło do kopii w sejfie (systemd-creds: TPM 2.0 + klucz systemu) — nigdy jawnie."""
    from . import secrets
    secrets.store("password", password)


def has_password() -> bool:
    from . import secrets
    return secrets.exists("password")


def password_usable() -> bool:
    """Czy hasło da się odszyfrować (po odtworzeniu na innym komputerze — nie: TPM inny)."""
    from . import secrets
    return secrets.load("password") is not None


# --------------------------------------------------------------------------- status
def read_status() -> dict:
    try:
        return json.load(open(STATUS_FILE))
    except (OSError, json.JSONDecodeError):
        return {}


def write_status(**changes):
    os.makedirs(STATE_DIR, exist_ok=True)
    st = read_status()
    st.update(changes)
    st["updated"] = now()
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATUS_FILE)


# --------------------------------------------------------------------------- dysk i repo
def mount_data() -> tuple[str, str] | None:
    """Montuje TM-DATA (jeśli dysk jest podłączony). Zwraca (urządzenie, katalog)."""
    dev = disk.find_data_partition()
    if not dev:
        return None
    for d, mnt, _ in blockdev.mounts():
        if os.path.realpath(d) == os.path.realpath(dev):
            return dev, mnt
    disk.mount(dev, DATA_MOUNT)
    return dev, DATA_MOUNT


def unmount_data():
    sh.run(["umount", DATA_MOUNT], check=False)


def open_repo(data_dir: str) -> Repo:
    from . import secrets
    cmd = secrets.restic_password_command("password")
    return Repo(os.path.join(data_dir, "restic"), password_command=cmd,
                password=None if cmd else secrets.load("password"),
                cache_dir=os.path.join(data_dir, "cache"))


def iso_utc(restic_time: str) -> str:
    """restic podaje czas z nanosekundami i strefą — normalizujemy do ISO UTC."""
    import re
    t = re.sub(r"(\.\d{6})\d+", r"\1", restic_time).replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(t).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return restic_time


def snapshot_summary(repo: Repo) -> list[dict]:
    return [{"id": s["short_id"], "time": iso_utc(s["time"]), "tags": s.get("tags", [])}
            for s in repo.snapshots()]


def disk_usage(path: str) -> dict:
    u = shutil.disk_usage(path)
    return {"total": u.total, "used": u.used, "free": u.free}


# --------------------------------------------------------------------------- backup
def backup_due(trigger: str, cfg: dict, st: dict, now_epoch: float | None = None) -> bool:
    """Czy kopia powinna teraz powstać.
    trigger: manual (przycisk), timer (co godzinę), connect (podłączenie dysku)."""
    if trigger == "manual":
        return True
    last = st.get("last_success_epoch") or 0
    age = (now_epoch or time.time()) - last
    if trigger == "connect":
        return cfg["on_connect"] and age >= CONNECT_MIN_GAP
    return cfg["auto"] and age >= INTERVALS[cfg["interval"]] - MARGIN


def run_backup(manual: bool = False, root: str = "/", trigger: str | None = None) -> int:
    """Wywoływane przez usługi: timevault-backup (timer), timevault-backup-connect (udev /
    aplikacja po wykryciu dysku), timevault-backup-now (przycisk)."""
    trigger = trigger or ("manual" if manual else "timer")
    cfg = load_config()
    if not backup_due(trigger, cfg, read_status()):
        return 0
    if not has_password():
        write_status(state="not-configured")
        return 0
    if not password_usable():
        from .i18n import L
        write_status(state="not-configured", error=L(
            "Zapisanego hasła nie da się odszyfrować (np. system odtworzony na innym komputerze). "
            "Podaj hasło ponownie: „Użyj tego dysku”.",
            "The stored password cannot be decrypted (e.g. the system was restored on another computer). "
            "Enter the password again: “Use this disk”."))
        return 0
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0                                   # backup już trwa
    if not backup_due(trigger, cfg, read_status()):     # ponownie, już pod blokadą
        return 0
    mounted = mount_data()
    if not mounted:
        write_status(state="disk-absent", disk_connected=False)
        return 0
    dev, data = mounted
    write_status(state="running", disk_connected=True, started=now(), error=None)
    repo = open_repo(data)
    try:
        manifest = system.inspect(root)
        system.write_manifest(manifest, root)
        summary = repo.backup(root, tags=["timevault", "full-system"])
        snap = summary.get("snapshot_id", "")
        os.makedirs(os.path.join(data, "manifests"), exist_ok=True)
        with open(os.path.join(data, "manifests", f"{snap}.json"), "w") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        repo.forget_tm()
        compat_status(root, st_before=read_status())
        write_status(state="idle", last_success=now(), last_success_epoch=time.time(),
                     last_snapshot=snap[:8], last_added=summary.get("data_added", 0),
                     last_duration=summary.get("total_duration", 0),
                     warnings=len(summary.get("warnings", [])),
                     last_incomplete=bool(summary.get("incomplete")),
                     last_skipped=summary.get("skipped", []),
                     snapshots=snapshot_summary(repo), disk=disk_usage(data))
        return 0
    except Exception as e:                           # noqa: BLE001 — status dla GUI
        write_status(state="error", error=str(e)[:500], last_error=now())
        return 1
    finally:
        if data == DATA_MOUNT and not os.path.ismount(BROWSE_MOUNT):
            unmount_data()                        # dysk można bezpiecznie odłączyć


def compat_status(root: str = "/", st_before: dict | None = None):
    """Sprawdzenie zgodności po kopii; wykrycie aktualizacji Ubuntu (zmiana wersji)."""
    from . import doctor
    try:
        rep = doctor.run(root)
    except Exception as e:  # noqa: BLE001 — sprawdzenie nie może zepsuć kopii
        write_status(compat_error=str(e)[:200])
        return
    changes = {"compat": rep["items"], "compat_worst": rep["worst"], "compat_checked": now(),
               "os": rep["os"], "recovery": rep["recovery"]}
    prev_os = (st_before or {}).get("os") or {}
    if prev_os.get("version_id") and prev_os.get("version_id") != rep["os"].get("version_id"):
        changes["os_upgraded"] = {"from": prev_os.get("pretty"), "to": rep["os"].get("pretty"), "at": now()}
    write_status(**changes)


def run_check() -> int:
    mounted = mount_data()
    if not mounted:
        write_status(state="disk-absent", disk_connected=False)
        return 1
    _, data = mounted
    write_status(state="checking")
    try:
        open_repo(data).check("5%")
        write_status(state="idle", last_check=now(), last_check_ok=True)
        return 0
    except sh.CommandError as e:
        write_status(state="error", last_check=now(), last_check_ok=False, error=str(e)[:500])
        return 1


# --------------------------------------------------------------------------- przeglądanie
def run_browse() -> int:
    """timevault-browse.service: restic mount z dostępem dla użytkownika (tylko odczyt).
    Działa do zatrzymania usługi."""
    import subprocess
    mounted = mount_data()
    if not mounted:
        write_status(browse=False, state="disk-absent")
        return 1
    _, data = mounted
    sh.run(["umount", "-l", BROWSE_MOUNT], check=False)        # pozostałość po przerwanym podglądzie
    try:
        os.makedirs(BROWSE_MOUNT, exist_ok=True)
    except OSError as e:
        write_status(browse=False, browse_starting=False, browse_error=str(e))
        return 1
    write_status(browse=False, browse_path=BROWSE_MOUNT, browse_error=None, browse_starting=True)
    repo = open_repo(data)
    log_path = os.path.join(STATE_DIR, "browse.log")
    env = os.environ.copy()
    env.update(repo._env())
    with open(log_path, "w") as log:
        # --allow-other: użytkownik przegląda w Plikach; restic mount jest zawsze tylko do odczytu
        proc = subprocess.Popen(repo._cmd("mount", "--allow-other", BROWSE_MOUNT),
                                stdout=log, stderr=subprocess.STDOUT, env=env)
        try:
            while proc.poll() is None and not os.path.ismount(BROWSE_MOUNT):
                time.sleep(0.5)
            if proc.poll() is not None:
                tail = open(log_path, errors="replace").read()[-400:]
                write_status(browse=False, browse_starting=False, browse_error=tail.strip())
                return 1
            write_status(browse=True, browse_starting=False)
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.terminate()
            write_status(browse=False, browse_starting=False)
            sh.run(["umount", BROWSE_MOUNT], check=False)
    return 0


# --------------------------------------------------------------------------- chmura
def cloud_due(trigger: str, cfg: dict, st: dict, now_epoch: float | None = None) -> bool:
    if trigger == "manual":
        return True
    if not cfg.get("cloud") or cfg.get("cloud_interval") == "off":
        return False
    last = st.get("cloud_last_success_epoch") or 0
    return (now_epoch or time.time()) - last >= CLOUD_INTERVALS[cfg["cloud_interval"]] - MARGIN


def run_cloud_backup(trigger: str = "timer", root: str = "/") -> int:
    """timevault-cloud.service (timer) / timevault-cloud-now.service (przycisk)."""
    from . import cloud
    cfg = load_config()
    from . import secrets
    if not secrets.exists("rclone") or not has_password():
        write_status(cloud_state="not-configured")
        return 0
    if not cloud_due(trigger, cfg, read_status()):
        return 0
    if trigger != "manual" and cfg.get("cloud_ac_only") and not cloud.on_ac_power():
        write_status(cloud_state="waiting-power")
        return 0
    os.makedirs(os.path.dirname(CLOUD_LOCK_FILE), exist_ok=True)
    lock = open(CLOUD_LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    with secrets.materialized("rclone") as conf:
        return _cloud_backup_with(conf, root)


def _cloud_backup_with(conf: str, root: str) -> int:
    from . import cloud
    if not conf or not cloud.online(conf):
        write_status(cloud_state="offline", cloud_last_attempt=now())
        return 0
    write_status(cloud_state="running", cloud_started=now(), cloud_error=None)
    repo = cloud.open_repo(conf)
    try:
        manifest = system.inspect(root)
        system.write_manifest(manifest, root)
        summary = repo.backup(root, tags=["timevault", "full-system", "cloud"])
        repo.forget_tm()
        snaps = snapshot_summary(repo)
        write_status(cloud_state="idle", cloud_last_success=now(), cloud_last_success_epoch=time.time(),
                     cloud_last_added=summary.get("data_added", 0), cloud_snapshots=snaps,
                     cloud_quota=cloud.quota(conf))
        return 0
    except Exception as e:  # noqa: BLE001
        write_status(cloud_state="error", cloud_error=str(e)[:500], cloud_last_error=now())
        return 1


def setup_cloud(token: str | None, separate_password: str | None = None, progress=print,
                remote_type: str = "drive", prefix: str | None = None) -> dict:
    """Po zalogowaniu w przeglądarce: zapis konfiguracji, repozytorium na Drive, szacunek
    miejsca i czasu, kopia danych dostępowych dla systemu ratunkowego."""
    from . import cloud, secrets
    from .i18n import L
    if not has_password():
        raise RuntimeError(L("Najpierw skonfiguruj dysk TimeVault.", "Set up the TimeVault disk first."))
    secrets.store("rclone", cloud.build_conf(token, remote_type=remote_type))
    if separate_password:
        secrets.store("cloud-password", separate_password)
    else:
        secrets.remove("cloud-password")
    progress(L("Sprawdzanie połączenia z Google Drive…", "Checking the Google Drive connection…"))
    with secrets.materialized("rclone") as conf:
        if not cloud.online(conf):
            raise RuntimeError(L("Brak połączenia z Google Drive.", "Cannot reach Google Drive."))
        repo = cloud.open_repo(conf, prefix=prefix)
        if not repo.exists():
            progress(L("Tworzenie zaszyfrowanego repozytorium na Drive…", "Creating the encrypted repository on Drive…"))
            repo.init()
        q = cloud.quota(conf)
        snaps = snapshot_summary(repo)
    st = read_status()
    est = estimate_backup_size(st)
    info = {"quota": q, "estimate": est,
            "hours_20mbit": round(cloud.estimate_seconds(est, 20) / 3600, 1) if est else None,
            "hours_50mbit": round(cloud.estimate_seconds(est, 50) / 3600, 1) if est else None,
            "fits": (q.get("free") is None or not est or q["free"] > est * 1.2)}
    cfg = load_config()
    if not cfg.get("cloud"):
        cfg["cloud_interval"] = "off"            # samo połączenie niczego nie wysyła
    save_config({**cfg, "cloud": True})
    write_status(cloud_state="idle", cloud_quota=q, cloud_estimate=est, cloud_snapshots=snaps)
    update_recovery_secret(progress)
    return info


def estimate_backup_size(st: dict) -> int:
    """Szacowany rozmiar pierwszej kopii w chmurze ≈ rozmiar repozytorium na dysku USB."""
    mounted = mount_data()
    try:
        if mounted:
            # Na TM-DATA leży praktycznie tylko repozytorium — odczyt natychmiastowy
            # (przechodzenie po ~60 GB plików na dysku USB trwało minutami).
            used = shutil.disk_usage(mounted[1]).used
            if used:
                return used
    finally:
        if mounted and mounted[1] == DATA_MOUNT:
            unmount_data()
    return int(system.used_bytes("/") * 0.6)


def update_recovery_secret(progress=print) -> bool:
    """Zaszyfrowane dane dostępowe do Drive w aktywnym slocie systemu ratunkowego."""
    from . import cloud, recovery
    from .i18n import L
    try:
        parts = recovery.find_parts()
    except Exception:  # noqa: BLE001 — dysk niepodłączony: zrobi to następne odświeżenie Recovery
        return False
    from . import secrets
    state = recovery.read_state(parts["efi"])
    slot = state.get("active")
    if slot not in ("A", "B") or not secrets.exists("rclone"):
        return False
    mnt = "/run/timevault/recovery-secret"
    os.makedirs(mnt, exist_ok=True)
    sh.run(["mount", parts[slot], mnt])
    try:
        if write_cloud_secret(mnt):
            progress(L("System ratunkowy może teraz odtwarzać także z Google Drive.",
                       "The rescue system can now also restore from Google Drive."))
            return True
        return False
    finally:
        sh.run(["umount", mnt], check=False)


def write_cloud_secret(slot_root: str) -> bool:
    """Dane dostępowe do Drive w systemie ratunkowym — zaszyfrowane hasłem do kopii w chmurze
    (w Recovery nie ma TPM tego laptopa, więc tam decyduje hasło, które zna tylko użytkownik)."""
    from . import cloud, secrets
    pw = secrets.load(cloud.password_name())
    if not pw:
        return False
    with secrets.materialized("rclone") as conf:
        if not conf:
            return False
        cloud.encrypt_for_recovery(conf, pw, os.path.join(slot_root, cloud.RECOVERY_SECRET))
    return True


def disconnect_cloud():
    """Odłącza chmurę na tym komputerze. Kopie na Drive zostają (można je odtworzyć)."""
    from . import cloud
    cloud.remove_local()
    save_config({**load_config(), "cloud": False})
    write_status(cloud_state="not-configured")
