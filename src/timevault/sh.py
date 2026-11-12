"""Uruchamianie poleceń systemowych z logowaniem i trybem dry-run."""
from __future__ import annotations

import logging
import os
import shlex
import subprocess

log = logging.getLogger("timevault")

DRY_RUN = False


class CommandError(RuntimeError):
    def __init__(self, cmd, code, stderr):
        super().__init__(f"Polecenie nie powiodło się ({code}): {shlex.join(cmd)}\n{stderr.strip()}")
        self.cmd, self.code, self.stderr = cmd, code, stderr


def run(cmd, *, check=True, capture=True, input=None, env=None, mutating=True, cwd=None):
    """Uruchamia polecenie. W trybie DRY_RUN polecenia zmieniające stan tylko są logowane."""
    cmd = [str(c) for c in cmd]
    if DRY_RUN and mutating:
        log.info("[dry-run] %s", shlex.join(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    log.debug("$ %s", shlex.join(cmd))
    full_env = None
    if env:
        full_env = os.environ.copy()
        full_env.update(env)
    try:
        res = subprocess.run(cmd, input=input, text=True, capture_output=capture,
                             env=full_env, cwd=cwd)
    except FileNotFoundError:
        if check:
            raise CommandError(cmd, 127, f"Brak programu: {cmd[0]}")
        return subprocess.CompletedProcess(cmd, 127, "", f"Brak programu: {cmd[0]}")
    if check and res.returncode != 0:
        raise CommandError(cmd, res.returncode, res.stderr or "")
    return res


def out(cmd, **kw) -> str:
    """Polecenie tylko do odczytu, zwraca stdout."""
    return run(cmd, mutating=False, **kw).stdout


def which(name: str) -> bool:
    from shutil import which as _w
    return _w(name) is not None
