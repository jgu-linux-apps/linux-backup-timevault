"""Przechowywanie sekretów (hasło do kopii, token Google Drive) — zaszyfrowane, nie jawnie.

Używa systemd-creds: dane szyfrowane kluczem z układu TPM 2.0 (jeśli jest) i kluczem systemu
(/var/lib/systemd/credential.secret, tylko root). Plik .cred skopiowany z dysku albo odczytany
z innego systemu (np. uruchomionego z pendrive'a na innym komputerze) jest bezużyteczny.

Gdy systemd-creds nie ma (bardzo stare systemy) — jawny plik 0600 i ostrzeżenie w „doctor”.
Stare jawne pliki są automatycznie szyfrowane i bezpiecznie usuwane (migrate()).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager

ETC = "/etc/timevault"
RUN_DIR = "/run/timevault/secrets"         # tmpfs — pliki chwilowe, tylko na czas operacji
NAMES = {"password": "password", "cloud-password": "cloud-password", "rclone": "rclone.conf"}


def _cred(name: str) -> str:
    return os.path.join(ETC, f"{name}.cred")


def _plain(name: str) -> str:
    return os.path.join(ETC, NAMES[name])


def available() -> bool:
    return shutil.which("systemd-creds") is not None


def has_tpm2() -> bool:
    if not available():
        return False
    res = subprocess.run(["systemd-creds", "has-tpm2", "--quiet"], capture_output=True)
    return res.returncode == 0


def _shred(path: str):
    """Nadpisanie zerami przed usunięciem (jawne hasło nie zostaje w wolnym miejscu)."""
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            f.write(b"\0" * max(size, 1))
            f.flush()
            os.fsync(f.fileno())
        os.remove(path)
    except OSError:
        pass


def store(name: str, data: str):
    os.makedirs(ETC, mode=0o700, exist_ok=True)
    os.chmod(ETC, 0o700)
    if available():
        key = "auto"            # TPM 2.0 + klucz systemu, gdy jest TPM; inaczej klucz systemu
        res = subprocess.run(["systemd-creds", "encrypt", f"--name=timevault-{name}", f"--with-key={key}",
                              "-", _cred(name)], input=data.encode(), capture_output=True)
        if res.returncode == 0:
            os.chmod(_cred(name), 0o600)
            if os.path.exists(_plain(name)):
                _shred(_plain(name))
            _write_meta(name, "tpm2+host" if has_tpm2() else "host")
            return
    fd = os.open(_plain(name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    _write_meta(name, "plain")


def load(name: str) -> str | None:
    """Odszyfrowany sekret albo None (brak / nie da się odszyfrować — np. inny komputer)."""
    if os.path.exists(_cred(name)) and available():
        res = subprocess.run(["systemd-creds", "decrypt", f"--name=timevault-{name}", _cred(name), "-"],
                             capture_output=True)
        return res.stdout.decode() if res.returncode == 0 else None
    if os.path.exists(_plain(name)):
        return open(_plain(name)).read()
    return None


def exists(name: str) -> bool:
    return os.path.exists(_cred(name)) or os.path.exists(_plain(name))


def remove(name: str):
    for p in (_cred(name), _plain(name)):
        if os.path.exists(p):
            _shred(p)
    meta = _read_meta()
    meta.pop(name, None)
    _save_meta(meta)


def restic_password_command(name: str = "password") -> str | None:
    """Polecenie dla resticu (RESTIC_PASSWORD_COMMAND) — hasło trafia do resticu w pamięci."""
    if os.path.exists(_cred(name)) and available():
        return f"systemd-creds decrypt --name=timevault-{name} {_cred(name)} -"
    return None


@contextmanager
def materialized(name: str):
    """Chwilowy jawny plik w /run (tmpfs, 0600) dla programów, które wymagają pliku (rclone).
    Po operacji: zmiany (np. odświeżony token Google) są szyfrowane z powrotem, plik usuwany."""
    data = load(name)
    if data is None:
        yield None
        return
    os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=f"{name}-", dir=RUN_DIR)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    try:
        yield path
    finally:
        try:
            new = open(path).read()
            if new != data and new.strip():
                store(name, new)
        finally:
            _shred(path)


def migrate() -> list[str]:
    """Jawne pliki ze starszych wersji -> zaszyfrowane; oryginały nadpisane i usunięte."""
    done = []
    if not available():
        return done
    for name in NAMES:
        if os.path.exists(_plain(name)) and not os.path.exists(_cred(name)):
            store(name, open(_plain(name)).read())
            if os.path.exists(_cred(name)):
                done.append(name)
    return done


def protection(name: str = "password") -> str:
    """tpm2+host | host | plain | none"""
    if os.path.exists(_cred(name)):
        return _read_meta().get(name, "host")
    if os.path.exists(_plain(name)):
        return "plain"
    return "none"


def _meta_path() -> str:
    return os.path.join(ETC, "secrets.json")


def _read_meta() -> dict:
    try:
        return json.load(open(_meta_path()))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_meta(meta: dict):
    try:
        fd = os.open(_meta_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(meta, f)
    except OSError:
        pass


def _write_meta(name: str, how: str):
    meta = _read_meta()
    meta[name] = how
    _save_meta(meta)
