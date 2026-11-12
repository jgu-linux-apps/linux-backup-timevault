"""Menu tekstowe systemu Recovery (tty1, whiptail). Teksty bez polskich znakow —
konsola tekstowa ich nie wyswietli."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from . import blockdev, disk, restore, sh
from .i18n import L, set_lang
from .repo import Repo

TITLE = "Linux Backup TimeVault - Recovery"
DATA_MOUNT = "/run/timevault/data"
MIN_TARGET = 8 * 2**30          # mniejsze dyski (pendrive itp.) nie są celem odtworzenia


def screen(h, w):
    """Okno dopasowane do ekranu (konsola laptopa ma zwykle 100-240 kolumn)."""
    cols, rows = shutil.get_terminal_size((80, 25))
    return min(max(h, rows - 6), rows - 2), max(min(cols - 4, 120), w)


def wt(*args, h=20, w=76):
    """whiptail: interfejs na terminalu, wynik na stderr."""
    res = subprocess.run(["whiptail", "--title", TITLE, *args[:1], *args[1:2], str(h), str(w), *args[2:]],
                         stderr=subprocess.PIPE, text=True)
    return res.returncode, res.stderr.strip()


def msg(text, h=14, w=76):
    wt("--msgbox", text, h=h, w=w)


def yesno(text, h=20, w=76) -> bool:
    h, w = screen(h, w)
    return wt("--yesno", text, h=h, w=w)[0] == 0


def menu(text, items, h=22, w=76):
    """items: [(wartość, opis)]. Na ekranie zwykłe numery zamiast technicznych wartości."""
    h, w = screen(h, w)
    flat = []
    for i, (_, label) in enumerate(items, 1):
        flat += [str(i), label[: w - 12]]
    res = subprocess.run(["whiptail", "--title", TITLE, "--menu", text, str(h), str(w),
                          str(min(len(items), h - 8)), *flat], stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        return res.returncode, None
    return 0, items[int(res.stderr.strip()) - 1][0]


def password(text) -> str | None:
    code, out = wt("--passwordbox", text, h=10, w=60)
    return out if code == 0 else None


def inputbox(text) -> str | None:
    code, out = wt("--inputbox", text, h=10, w=70)
    return out if code == 0 else None


def ascii_only(s: str) -> str:
    tr = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ—…", "acelnoszzACELNOSZZ-.")
    return s.translate(tr)


# --------------------------------------------------------------------------- kroki
def mount_data() -> str:
    dev = disk.find_data_partition()
    if not dev:
        raise RuntimeError(L("Nie znaleziono partycji TM-DATA z kopiami.", "TM-DATA partition with backups not found."))
    if not blockdev.is_mounted(dev):
        disk.mount(dev, DATA_MOUNT)
    return dev


def open_repo() -> Repo | None:
    while True:
        pw = password(L("Haslo do kopii:", "Backup password:"))
        if pw is None:
            return None
        repo = Repo(os.path.join(DATA_MOUNT, "restic"), password=pw,
                    cache_dir="/tmp/timevault-cache")
        try:
            repo.snapshots()
            return repo
        except sh.CommandError:
            msg(L("Nie udalo sie otworzyc kopii. Zle haslo?", "Could not open the backups. Wrong password?"), h=8)


def snapshot_os(s) -> str:
    """Wersja systemu z manifestu zapisanego obok repozytorium (bez odczytu z kopii)."""
    try:
        with open(os.path.join(DATA_MOUNT, "manifests", s["id"] + ".json")) as f:
            return json.load(f)["os"]["pretty"]
    except (OSError, KeyError, json.JSONDecodeError):
        return ""


def choose_snapshot(repo):
    snaps = list(reversed(repo.snapshots()))
    items = [(s, ascii_only(f"{s['time'][:16].replace('T', ' ')}   {snapshot_os(s) or s.get('hostname', '')}"
                            f"   ({s['short_id']})")) for s in snaps]
    code, s = menu(L("Wybierz kopie do odtworzenia (najnowsza na gorze):",
                     "Choose the backup to restore (newest first):"), items)
    return s if code == 0 else None


def describe_contents(d) -> str:
    """Co jest na dysku — żeby było jasne, co zostanie skasowane."""
    kinds = []
    for p in d.parts:
        if p.fstype in ("ntfs", "BitLocker"):
            kinds.append(f"NTFS '{p.label}'" if p.label else "Windows (NTFS)")
        elif p.fstype == "ext4":
            kinds.append(f"Linux ext4 {p.size / 2**30:.0f} GB")
        elif p.fstype == "vfat" and p.parttype == blockdev.ESP_GUID:
            kinds.append("EFI")
        elif p.fstype:
            kinds.append(p.fstype)
    uniq = []
    for k in kinds:
        if k not in uniq:
            uniq.append(k)
    return ", ".join(uniq) or L("pusty", "empty")


def target_options(manifest, backup_disk):
    """Cele odtworzenia — najbezpieczniejszy pierwszy: poprzednie Ubuntu (dual boot)."""
    opts = []
    old = blockdev.find_by("UUID", manifest["root"]["uuid"])
    if old and blockdev.parent_disk(old) != backup_disk:
        size = blockdev.size_bytes(old) / 2**30
        opts.append((("P", old), L(f"Nadpisz poprzednie Ubuntu {os.path.basename(old)} ({size:.0f} GB)"
                                   " - zalecane, Windows i reszta dysku zostaja",
                                   f"Overwrite the previous Ubuntu {os.path.basename(old)} ({size:.0f} GB)"
                                   " - recommended, Windows and the rest of the disk stay")))
    for d in blockdev.list_disks():
        if os.path.realpath(d.path) == os.path.realpath(backup_disk) or d.size < MIN_TARGET:
            continue
        for p in d.parts:
            if p.fstype == "ext4" and ("P", p.path) not in [o[0] for o in opts]:
                opts.append((("P", p.path), L(f"Partycja Linux {os.path.basename(p.path)} ({p.size / 2**30:.0f} GB)"
                                              " - tylko ta partycja zostanie nadpisana",
                                              f"Linux partition {os.path.basename(p.path)} ({p.size / 2**30:.0f} GB)"
                                              " - only this partition is overwritten")))
        opts.append((("D", d.path), L("CALY DYSK ", "WHOLE DISK ") + f"{os.path.basename(d.path)} {d.model[:18].strip()} "
                                    f"{d.size / 2**30:.0f} GB [{describe_contents(d)}] - "
                                    + L("WSZYSTKO ZOSTANIE USUNIETE", "EVERYTHING WILL BE ERASED")))
    return [(v, ascii_only(t)) for v, t in opts]


CLOUD_SECRET = "/etc/timevault-recovery/cloud.conf.enc"
CLOUD_CONF = "/tmp/timevault-rclone.conf"


def boot_disk() -> str | None:
    """Dysk, z którego wystartował system ratunkowy — nigdy nie jest celem odtworzenia."""
    for dev, mnt, _ in blockdev.mounts():
        if mnt == "/" and dev.startswith("/dev/"):
            return blockdev.parent_disk(dev)
    return None


def choose_source() -> str | None:
    if not os.path.exists(CLOUD_SECRET):
        return "usb"
    code, src = menu(L("Skad odtworzyc system?", "Where to restore the system from?"), [
        ("usb", L("Dysk USB TimeVault (szybko)", "TimeVault USB disk (fast)")),
        ("cloud", L("Google Drive (wymaga internetu, znacznie wolniej)", "Google Drive (needs internet, much slower)")),
    ], h=14)
    return src if code == 0 else None


def open_cloud_repo():
    from . import cloud
    while True:
        pw = password(L("Haslo do kopii w chmurze:", "Cloud backup password:"))
        if pw is None:
            return None
        if not cloud.decrypt_from_recovery(CLOUD_SECRET, pw, CLOUD_CONF):
            msg(L("Zle haslo.", "Wrong password."), h=8)
            continue
        while not cloud.online(CLOUD_CONF):
            if not yesno(L("Brak polaczenia z Google Drive.\n\nPolaczyc z siecia (Wi-Fi / kabel)?",
                           "Cannot reach Google Drive.\n\nConnect to a network (Wi-Fi / cable)?"), h=10):
                return None
            os.system("nmtui-connect || nmtui")
        repo = cloud.open_repo(CLOUD_CONF, password=pw)
        repo.cache_dir = "/tmp/timevault-cloud-cache"
        try:
            repo.snapshots()
            return repo
        except sh.CommandError:
            msg(L("Nie udalo sie otworzyc kopii w chmurze.", "Could not open the cloud backups."), h=8)
            return None


def do_restore():
    src = choose_source()
    if not src:
        return
    if src == "cloud":
        backup_disk = boot_disk() or ""
        repo = open_cloud_repo()
    else:
        try:
            mount_data()
        except Exception as e:
            msg(ascii_only(str(e)))
            return
        backup_disk = blockdev.parent_disk(disk.find_data_partition())
        repo = open_repo()
    if not repo:
        return
    s = choose_snapshot(repo)
    if not s:
        return
    manifest = repo.manifest(s["id"], s["paths"][0])
    opts = target_options(manifest, backup_disk)
    if not opts:
        msg(L("Nie znaleziono dysku docelowego.", "No target disk found."), h=8)
        return
    code, choice = menu(L("Gdzie odtworzyc system z ", "Where to restore the system from ")
                        + s['time'][:16].replace('T', ' ') + "?", opts)
    if code != 0:
        return
    kind, dev = choice
    try:
        plan = (restore.plan_partition(manifest, dev) if kind == "P"
                else restore.plan_disk(manifest, dev))
    except Exception as e:
        msg(L("Nie mozna:\n\n", "Not possible:\n\n") + ascii_only(str(e)))
        return
    if not yesno(L("PLAN ODTWORZENIA\n\n", "RESTORE PLAN\n\n") + ascii_only(plan.describe())
                 + L("\n\nKontynuowac?", "\n\nContinue?"), h=22):
        return
    word = inputbox(L(f"Ostatnie potwierdzenie. Dane na celu zostana usuniete.\nWpisz {disk.CONFIRM_WORD}:",
                      f"Final confirmation. Data on the target will be erased.\nType {disk.CONFIRM_WORD}:"))
    if word != disk.CONFIRM_WORD:
        msg(L("Anulowano - nic nie zostalo zmienione.", "Cancelled - nothing was changed."), h=8)
        return
    os.system("clear")
    print(L("TimeVault: odtwarzanie systemu. Nie wylaczaj komputera.\n",
            "TimeVault: restoring the system. Do not turn off the computer.\n"))
    repo.live_output = True
    try:
        report = restore.execute(plan, repo, s["id"], manifest, word,
                                 progress=lambda m: print("==>", ascii_only(m), flush=True))
    except Exception as e:
        print(L("\nBLAD:", "\nERROR:"), ascii_only(str(e)))
        input(L("\nEnter - powrot do menu", "\nEnter - back to the menu"))
        return
    msg(L("Gotowe. System odtworzony na ", "Done. System restored to ") + report["root_part"] +
        L(".\n\nWyjmij dysk USB i wybierz 'Uruchom ponownie'.", ".\n\nRemove the USB disk and choose 'Reboot'."), h=10)


def choose_language():
    code, choice = menu("Jezyk / Language", [("pl", "Polski"), ("en", "English")], h=12)
    set_lang(choice if code == 0 else "en")


def main():
    os.environ.setdefault("TERM", "linux")
    choose_language()
    while True:
        code, choice = menu(L("Co chcesz zrobic?", "What do you want to do?"), [
            ("1", L("Odtworz system z kopii", "Restore the system from a backup")),
            ("2", L("Siec / Wi-Fi (potrzebne tylko dla Google Drive)", "Network / Wi-Fi (only needed for Google Drive)")),
            ("3", L("Wiersz polecen", "Command line")),
            ("4", L("Uruchom ponownie", "Reboot")),
            ("5", L("Wylacz komputer", "Power off")),
            ("6", "Jezyk / Language"),
        ], h=17)
        if code != 0:
            continue
        if choice == "1":
            do_restore()
        elif choice == "2":
            os.system("nmtui")
        elif choice == "3":
            os.system("clear; echo '" + L("Powrot do menu: exit", "Back to the menu: exit") + "'; bash --noprofile")
        elif choice == "4":
            os.system("systemctl reboot")
        elif choice == "5":
            os.system("systemctl poweroff")
        elif choice == "6":
            choose_language()


if __name__ == "__main__":
    sys.exit(main())
