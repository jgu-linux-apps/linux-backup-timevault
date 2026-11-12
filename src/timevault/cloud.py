"""Kopia w chmurze (Google Drive) przez rclone — druga, dodatkowa kopia poza domem.

* Logowanie: `rclone authorize drive` w sesji użytkownika (przeglądarka), dostęp ograniczony
  do plików utworzonych przez TimeVault (scope drive.file).
* Dane dostępowe: /etc/timevault/rclone.conf (root, 0600). Kopia dla systemu ratunkowego:
  zaszyfrowana hasłem do kopii (openssl AES-256, PBKDF2) — bez hasła bezużyteczna.
* Repozytorium: rclone:gdrive:TimeVault — dane kompresowane, deduplikowane i szyfrowane
  przez restic PRZED wysłaniem.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil

from . import sh
from .i18n import L
from .repo import Repo

REMOTE = "gdrive"
CLOUD_DIR = "TimeVault"
CACHE_DIR = "/var/cache/timevault/cloud"
RECOVERY_SECRET = "etc/timevault-recovery/cloud.conf.enc"   # względem slotu Recovery

# rclone authorize "drive" "<base64 {"scope":"drive.file"}>" — tylko pliki TimeVault.
# rclone dekoduje base64 BEZ dopełnienia „=” (inaczej: "illegal base64 data").
AUTH_SCOPE_BLOB = base64.b64encode(json.dumps({"scope": "drive.file"}, separators=(",", ":"))
                                   .encode()).decode().rstrip("=")


def authorize_command() -> list[str]:
    return ["rclone", "authorize", "drive", AUTH_SCOPE_BLOB]


def _b64decode(s: str) -> bytes:
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    for dec in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return dec(s + pad)
        except (ValueError, base64.binascii.Error):
            continue
    raise ValueError("base64")


def parse_authorize_output(text: str) -> str:
    """Token z wyjścia `rclone authorize`. Starsze rclone podają JSON tokenu, nowsze (≥1.59,
    m.in. Ubuntu) blob base64 z mapą {"token": "<JSON tokenu>"}."""
    m = re.search(r"--->\s*(.*?)\s*<---", text, re.S)
    candidates = [m.group(1).strip()] if m else []
    # bez znaczników: ostatnia długa linia JSON albo base64
    candidates += [l.strip() for l in reversed(text.splitlines())
                   if l.strip().startswith("{") or re.fullmatch(r"[A-Za-z0-9+/=_-]{40,}", l.strip())]
    for c in candidates:
        try:
            if c.startswith("{"):
                obj = json.loads(c)
            else:
                obj = json.loads(_b64decode(c))
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict) and "token" in obj:          # mapa konfiguracji rclone
            tok = obj["token"]
            tok_obj = json.loads(tok) if isinstance(tok, str) else tok
        else:
            tok_obj = obj
        if isinstance(tok_obj, dict) and ("access_token" in tok_obj or "refresh_token" in tok_obj):
            return json.dumps(tok_obj, separators=(",", ":"))
    raise ValueError(L("Nie znaleziono tokenu w odpowiedzi logowania.", "No token found in the login output."))


def build_conf(token: str | None, remote_type: str = "drive") -> str:
    """Treść konfiguracji rclone (trafia do sejfu, nie na dysk). remote_type=local: testy."""
    lines = [f"[{REMOTE}]", f"type = {remote_type}"]
    if remote_type == "drive":
        lines += ["scope = drive.file", f"token = {token}"]
    return "\n".join(lines) + "\n"


def location(prefix: str | None = None) -> str:
    return f"rclone:{REMOTE}:{prefix or CLOUD_DIR}"


def password_name() -> str:
    from . import secrets
    return "cloud-password" if secrets.exists("cloud-password") else "password"


def open_repo(conf: str, prefix: str | None = None, password: str | None = None) -> Repo:
    """Repozytorium na Drive. Bez podanego hasła — hasło z sejfu (osobne do chmury albo wspólne)."""
    from . import secrets
    cmd = pw = None
    if password is None:
        cmd = secrets.restic_password_command(password_name())
        if not cmd:
            pw = secrets.load(password_name())
    repo = Repo(location(prefix), password=password if password is not None else pw,
                password_command=cmd, cache_dir=CACHE_DIR)
    repo.extra_env = {"RCLONE_CONFIG": conf}
    return repo


def rclone(*args, conf: str, check=True, timeout_s: int = 60):
    return sh.run(["timeout", str(timeout_s), "rclone", "--config", conf, *args],
                  check=check, mutating=False)


def quota(conf: str) -> dict:
    """Miejsce na Drive: {"total","used","free"} (bajty) albo {} gdy nieznane."""
    res = rclone("about", f"{REMOTE}:", "--json", conf=conf, check=False)
    try:
        q = json.loads(res.stdout)
        return {k: q.get(k) for k in ("total", "used", "free") if q.get(k) is not None}
    except (json.JSONDecodeError, TypeError):
        return {}


def online(conf: str) -> bool:
    return rclone("lsd", f"{REMOTE}:", "--max-depth", "1", conf=conf, check=False, timeout_s=30).returncode == 0


def on_ac_power() -> bool:
    """True przy zasilaniu z sieci (albo gdy to komputer bez baterii)."""
    base = "/sys/class/power_supply"
    try:
        supplies = os.listdir(base)
    except OSError:
        return True
    has_battery = False
    for s in supplies:
        typ = open(os.path.join(base, s, "type")).read().strip() if os.path.exists(os.path.join(base, s, "type")) else ""
        if typ == "Mains":
            try:
                if open(os.path.join(base, s, "online")).read().strip() == "1":
                    return True
            except OSError:
                pass
        if typ == "Battery":
            has_battery = True
    return not has_battery


def estimate_seconds(nbytes: int, mbit: float) -> int:
    return int(nbytes * 8 / (mbit * 1_000_000))


# --------------------------------------------------------------------------- dla Recovery
def encrypt_for_recovery(conf: str, password: str, out_path: str):
    """rclone.conf zaszyfrowany hasłem do kopii — do systemu ratunkowego (hasło przez stdin)."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    sh.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
            "-in", conf, "-out", out_path, "-pass", "stdin"], input=password + "\n")
    os.chmod(out_path, 0o600)


def decrypt_from_recovery(enc_path: str, password: str, out_path: str) -> bool:
    res = sh.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                  "-in", enc_path, "-out", out_path, "-pass", "stdin"],
                 input=password + "\n", check=False, mutating=False)
    ok = res.returncode == 0 and os.path.getsize(out_path) > 0 and \
        open(out_path, errors="replace").read().startswith(f"[{REMOTE}]")
    if not ok and os.path.exists(out_path):
        os.remove(out_path)
    return ok


def remove_local():
    from . import secrets
    secrets.remove("rclone")
    secrets.remove("cloud-password")
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
