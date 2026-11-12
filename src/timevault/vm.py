"""Test odtwarzania w maszynie wirtualnej: backup -> obraz dysku -> start w QEMU (UEFI).

Obraz powstaje na dysku backupu (TM-DATA), jest rzadki (sparse) i nie trafia do kolejnych
backupów (inny system plików). Odtworzenie w trybie vm_test nie dotyka NVRAM laptopa.
"""
from __future__ import annotations

import os
import pwd
import shlex
import shutil

from . import sh
from .disk import GiB

OVMF_CANDIDATES = [
    ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
    ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd"),
    ("/usr/share/qemu/OVMF_CODE.fd", "/usr/share/qemu/OVMF_VARS.fd"),
]


def find_ovmf() -> tuple[str, str] | None:
    for code, vars_ in OVMF_CANDIDATES:
        if os.path.exists(code) and os.path.exists(vars_):
            return code, vars_
    return None


def image_size_for(manifest: dict) -> int:
    """Obraz trochę większy niż dane, zaokrąglony do GiB, min. 32 GiB."""
    need = int(manifest["root"]["used"] * 1.25) + 2 * GiB
    return max(32 * GiB, -(-need // GiB) * GiB)


def free_bytes(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def create_image(directory: str, size: int) -> str:
    os.makedirs(directory, exist_ok=True)
    img = os.path.join(directory, "restore-test.img")
    if os.path.exists(img):
        os.remove(img)
    with open(img, "wb") as f:
        f.truncate(size)
    return img


def attach(img: str) -> str:
    return sh.run(["losetup", "-P", "-f", "--show", img]).stdout.strip()


def detach(loop: str):
    sh.run(["losetup", "-d", loop], check=False)


def qemu_command(img: str, vars_copy: str, code: str, mem_mib=8192, cpus=4) -> list[str]:
    return ["qemu-system-x86_64", "-enable-kvm", "-machine", "q35", "-cpu", "host",
            "-smp", str(cpus), "-m", str(mem_mib),
            "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
            "-drive", f"if=pflash,format=raw,file={vars_copy}",
            "-drive", f"file={img},format=raw,if=virtio,cache=unsafe,aio=threads",
            "-vga", "virtio", "-display", "gtk",
            "-nic", "user,model=virtio-net-pci",
            "-usb", "-device", "usb-tablet",
            "-name", "timevault restore test"]


def write_runner(directory: str, img: str) -> str:
    """Skrypt run-vm.sh do uruchomienia VM jako zwykły użytkownik (okno GTK w jego sesji)."""
    ovmf = find_ovmf()
    if not ovmf:
        raise RuntimeError("Brak OVMF (firmware UEFI dla QEMU): sudo apt install qemu-system-x86 ovmf")
    code, vars_tpl = ovmf
    vars_copy = os.path.join(directory, "OVMF_VARS.fd")
    shutil.copyfile(vars_tpl, vars_copy)
    script = os.path.join(directory, "run-vm.sh")
    with open(script, "w") as f:
        f.write("#!/bin/sh\n# timevault: start odtworzonego systemu w maszynie wirtualnej\n")
        f.write("exec " + shlex.join(qemu_command(img, vars_copy, code)) + "\n")
    os.chmod(script, 0o755)
    user = os.environ.get("SUDO_USER")
    if user:
        pw = pwd.getpwnam(user)
        for p in (directory, img, vars_copy, script):
            os.chown(p, pw.pw_uid, pw.pw_gid)
    return script
