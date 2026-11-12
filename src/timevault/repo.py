"""Opakowanie resticu: repozytorium na dysku zewnętrznym (lub rclone:gdrive:...)."""
from __future__ import annotations

import json
import os
import re
import tempfile

from . import sh

# Względem roota backupowanego systemu. Montowania (/proc, /sys, /dev, /run, /mnt/dysk_d,
# zewnętrzne dyski, /snap/*) pomija --one-file-system, ale ich PUSTE katalogi zostają,
# więc fstab po restore ma gdzie montować.
EXCLUDES = [
    "/tmp/*",
    "/var/tmp/*",
    "/swapfile",
    "/swap.img",
    "/lost+found",
    "/var/cache/apt/archives/*.deb",
    "/var/lib/snapd/cache/*",          # twarde linki do snapów, które i tak są w /var/lib/snapd/snaps
    "/var/lib/timevault/cache",
    "/var/lib/linux-tm/cache",
    "/home/*/.cache",
    "/root/.cache",
    "/home/*/.local/share/Trash",
    "/home/*/snap/*/*/.cache",
    "/home/*/.var/app/*/cache",
    "/var/lib/systemd/coredump/*",
]

# Retencja jak w Time Machine: godzinowe z doby, dzienne z miesiąca, tygodniowe "do zapełnienia".
# --keep-within 24h: każdy snapshot z ostatniej doby zostaje (także kilka w ciągu godziny),
# --keep-last 3: nigdy nie zostaje mniej niż 3 ostatnie, nawet po długiej przerwie.
TM_RETENTION = ["--keep-within", "24h", "--keep-last", "3",
                "--keep-daily", "31", "--keep-weekly", "10000"]


def skipped_paths(warnings: list[dict], limit: int = 20) -> list[str]:
    """Ścieżki plików z ostrzeżeń resticu (do pokazania użytkownikowi)."""
    paths = []
    for w in warnings:
        item = w.get("item") or ""
        if not item:
            msg = (w.get("error") or {}).get("message", "")
            m = re.search(r"(/\S+?)(?::|$)", msg)
            item = m.group(1) if m else msg[:120]
        if item and item not in paths:
            paths.append(item)
    return paths[:limit]


class Repo:
    def __init__(self, location: str, password: str | None = None,
                 password_file: str | None = None, cache_dir: str | None = None,
                 password_command: str | None = None):
        self.location = location
        self.password = password
        self.password_file = password_file
        self.password_command = password_command   # np. systemd-creds decrypt … (sejf)
        self.cache_dir = cache_dir
        self.live_output = False      # Recovery: postęp resticu widoczny na ekranie
        self.extra_env: dict = {}     # np. RCLONE_CONFIG dla repozytorium w chmurze

    # -- niskopoziomowe ---------------------------------------------------
    def _env(self):
        env = {"RESTIC_REPOSITORY": self.location}
        if self.password is not None:
            env["RESTIC_PASSWORD"] = self.password
        if self.password_file:
            env["RESTIC_PASSWORD_FILE"] = self.password_file
        if self.password_command:
            env["RESTIC_PASSWORD_COMMAND"] = self.password_command
        env.update(self.extra_env)
        return env

    def _cmd(self, *args):
        cmd = ["restic"]
        if self.cache_dir:
            cmd += ["--cache-dir", self.cache_dir]
        else:
            cmd += ["--no-cache"]
        return cmd + list(args)

    def restic(self, *args, mutating=True, check=True, capture=True):
        return sh.run(self._cmd(*args), env=self._env(), mutating=mutating,
                      check=check, capture=capture)

    # -- operacje ---------------------------------------------------------
    def exists(self) -> bool:
        return self.restic("cat", "config", mutating=False, check=False).returncode == 0

    def init(self):
        if self.cache_dir:
            os.makedirs(self.cache_dir, exist_ok=True)
        self.restic("init")

    def backup(self, source_root: str, extra_paths=(), tags=(), host="timevault") -> dict:
        source_root = os.path.abspath(source_root)
        prefix = "" if source_root == "/" else source_root
        with tempfile.NamedTemporaryFile("w", suffix=".excl", delete=False) as f:
            for e in EXCLUDES:
                f.write(prefix + e + "\n")
            excl = f.name
        try:
            sh.run(["sync"], check=False)
            args = ["backup", "--json", "--one-file-system", "--exclude-file", excl,
                    "--host", host, "--exclude-caches"]
            for t in tags:
                args += ["--tag", t]
            res = self.restic(*args, source_root, *extra_paths, check=False)
        finally:
            os.unlink(excl)
        summary = {}
        warnings = []
        # restic --json: podsumowanie na stdout, błędy pojedynczych plików na stderr
        for line in (res.stdout or "").splitlines() + (res.stderr or "").splitlines():
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("message_type") == "summary":
                summary = msg
            elif msg.get("message_type") == "error":
                warnings.append(msg)
        # Kod 3 = kopia ZAPISANA, ale części plików nie dało się w pełni odczytać
        # (np. zmieniły się w trakcie, atrybuty rozszerzone). To nie jest porażka.
        if res.returncode == 3 and summary.get("snapshot_id"):
            summary["incomplete"] = True
        elif res.returncode != 0:
            raise sh.CommandError(self._cmd(*args, source_root), res.returncode, res.stderr or "")
        summary["warnings"] = warnings
        summary["skipped"] = skipped_paths(warnings)
        return summary

    def snapshots(self, tag: str | None = None) -> list[dict]:
        args = ["snapshots", "--json"]
        if tag:
            args += ["--tag", tag]
        res = self.restic(*args, mutating=False)
        return json.loads(res.stdout or "[]")

    def dump(self, snapshot: str, path: str) -> bytes:
        res = sh.run(self._cmd("dump", snapshot, path), env=self._env(), mutating=False)
        return res.stdout.encode() if isinstance(res.stdout, str) else res.stdout

    def manifest(self, snapshot: str, source_root: str = "/") -> dict:
        from .system import LEGACY_MANIFEST_REL, MANIFEST_REL
        last = None
        for rel in (MANIFEST_REL, LEGACY_MANIFEST_REL):
            try:
                return json.loads(self.dump(snapshot, os.path.join(source_root, rel)))
            except sh.CommandError as e:
                last = e
        raise last

    def forget_tm(self, prune=True):
        # bez grupowania po hoście: snapshoty sprzed zmiany nazwy też podlegają retencji
        args = ["forget", *TM_RETENTION, "--group-by", ""]
        if prune:
            args.append("--prune")
        return self.restic(*args)

    def check(self, read_data_subset: str | None = None):
        args = ["check"]
        if read_data_subset:
            args += ["--read-data-subset", read_data_subset]
        return self.restic(*args, mutating=False)

    def restore(self, snapshot: str, source_root: str, target: str):
        source_root = os.path.abspath(source_root)
        spec = snapshot if source_root == "/" else f"{snapshot}:{source_root}"
        return self.restic("restore", spec, "--target", target, capture=not self.live_output)
