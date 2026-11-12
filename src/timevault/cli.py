"""TimeVault — Time Machine dla Ubuntu (CLI silnika)."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import sys

from . import blockdev, disk, restore, sh, system, vm
from .i18n import L as i18n_L
from .repo import Repo

DATA_MOUNT = "/run/timevault/data"


def _password(args) -> tuple[str | None, str | None]:
    if args.password_file:
        return None, args.password_file
    for var in ("TIMEVAULT_PASSWORD", "LINUX_TM_PASSWORD"):
        if os.environ.get(var):
            return os.environ[var], None
    return getpass.getpass("Hasło do backupu: "), None


def _data_dir(args) -> str | None:
    """Katalog na partycji TM-DATA (montowanej automatycznie), albo None dla repo zdalnego."""
    if args.repo:
        return None
    dev = disk.find_data_partition()
    if not dev:
        sys.exit("Nie znaleziono dysku backupu (partycja TM-DATA). Podłącz dysk albo użyj --repo.")
    mounted = {os.path.realpath(d): m for d, m, _ in blockdev.mounts()}
    if os.path.realpath(dev) in mounted:
        return mounted[os.path.realpath(dev)]
    disk.mount(dev, DATA_MOUNT)
    return DATA_MOUNT


def open_repo(args, data_dir=None) -> Repo:
    pw, pwf = _password(args)
    if args.repo:
        cache = args.cache_dir or "/var/lib/timevault/cache"
        return Repo(args.repo, password=pw, password_file=pwf, cache_dir=cache)
    return Repo(os.path.join(data_dir, "restic"), password=pw, password_file=pwf,
                cache_dir=os.path.join(data_dir, "cache"))


def cmd_inspect(args):
    print(json.dumps(system.inspect(args.root), indent=2, ensure_ascii=False))


def cmd_disks(args):
    sysd = disk.system_disks()
    for d in blockdev.list_disks():
        tag = "  [SYSTEM]" if os.path.realpath(d.path) in sysd else ""
        print(disk.describe(d.path) + tag)


def _confirm(args, what: str) -> str:
    if args.confirm:
        return args.confirm
    print(what)
    return input(f"\nAby kontynuować, wpisz {disk.CONFIRM_WORD}: ").strip()


def cmd_prepare_disk(args):
    print("Ten dysk zostanie WYMAZANY i przygotowany jako dysk Time Machine z Recovery:\n")
    word = _confirm(args, disk.describe(args.disk))
    parts = disk.prepare_backup_disk(args.disk, word, erase_existing_timevault=args.erase_existing_timevault)
    print("Gotowe:", json.dumps(parts, indent=2))


def cmd_init(args):
    data = _data_dir(args)
    repo = open_repo(args, data)
    if repo.exists():
        print("Repozytorium już istnieje:", repo.location)
        return
    repo.init()
    print("Utworzono repozytorium:", repo.location)


def cmd_backup(args):
    data = _data_dir(args)
    repo = open_repo(args, data)
    manifest = system.inspect(args.root)
    system.write_manifest(manifest, args.root)
    summary = repo.backup(args.root, tags=["timevault", "full-system"])
    snap = summary.get("snapshot_id", "")
    if data:
        os.makedirs(os.path.join(data, "manifests"), exist_ok=True)
        with open(os.path.join(data, "manifests", f"{snap}.json"), "w") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"Snapshot {snap[:8]}: {summary.get('files_new', 0)} nowych, "
          f"{summary.get('files_changed', 0)} zmienionych plików, "
          f"dodano {summary.get('data_added', 0) / 1e6:.1f} MB")
    if summary.get("warnings"):
        print(f"Ostrzeżeń: {len(summary['warnings'])} (pliki niedostępne w trakcie backupu)")
    if not args.no_forget:
        repo.forget_tm()


def cmd_snapshots(args):
    repo = open_repo(args, _data_dir(args))
    for s in repo.snapshots():
        print(f"{s['short_id']}  {s['time'][:19].replace('T', ' ')}  {s.get('hostname', '')}  "
              f"{','.join(s.get('tags', []))}")


def cmd_check(args):
    repo = open_repo(args, _data_dir(args))
    res = repo.check(args.read_data)
    print(res.stdout)


def cmd_restore(args):
    repo = open_repo(args, _data_dir(args))
    s = _pick_snapshot(repo, args.snapshot)
    source_root = s["paths"][0]
    manifest = repo.manifest(s["id"], source_root)

    if args.disk:
        plan = restore.plan_disk(manifest, args.disk, boot_mode=args.boot_mode)
    else:
        plan = restore.plan_partition(manifest, args.partition, args.esp, boot_mode=args.boot_mode)
    print(f"Snapshot {s['short_id']} z {s['time'][:16].replace('T', ' ')} "
          f"({manifest['os'].get('pretty', '')})\n")
    print(plan.describe())
    if args.plan_only:
        return
    word = _confirm(args, "")
    report = restore.execute(plan, repo, s["id"], manifest, word,
                             skip_bootloader=args.skip_bootloader)
    print("\nOdtworzono:", json.dumps(report, indent=2, ensure_ascii=False))
    print("Możesz uruchomić komputer ponownie.")


def _pick_snapshot(repo, wanted):
    snaps = repo.snapshots()
    if not snaps:
        sys.exit("Brak snapshotów w repozytorium.")
    if wanted == "latest":
        return snaps[-1]
    match = [s for s in snaps if s["id"].startswith(wanted) or s["short_id"] == wanted]
    if not match:
        sys.exit(f"Nie ma snapshotu {wanted}")
    return match[0]


def cmd_vm_test(args):
    if not shutil.which("qemu-system-x86_64") or not vm.find_ovmf():
        sys.exit("Potrzebne QEMU i OVMF: sudo apt install qemu-system-x86 ovmf")
    data = _data_dir(args)
    img_dir = args.image_dir or (os.path.join(data, "vm") if data else None)
    if not img_dir:
        sys.exit("Repozytorium zdalne — podaj --image-dir na dysku z wolnym miejscem.")
    repo = open_repo(args, data)
    s = _pick_snapshot(repo, args.snapshot)
    manifest = repo.manifest(s["id"], s["paths"][0])
    size = vm.image_size_for(manifest)
    need = int(manifest["root"]["used"] * 1.1)
    os.makedirs(img_dir, exist_ok=True)
    if vm.free_bytes(img_dir) < need:
        sys.exit(f"Za mało miejsca w {img_dir}: potrzeba ~{need / 2**30:.0f} GiB.")
    print(f"Snapshot {s['short_id']} z {s['time'][:16].replace('T', ' ')} -> obraz VM "
          f"{size / 2**30:.0f} GiB (rzadki) w {img_dir}")
    print("Partycje laptopa i jego NVRAM nie są dotykane.\n")
    img = vm.create_image(img_dir, size)
    loop = vm.attach(img)
    try:
        plan = restore.plan_disk(manifest, loop, boot_mode="uefi")
        plan.vm_test = True
        report = restore.execute(plan, repo, s["id"], manifest, disk.CONFIRM_WORD,
                                 target="/mnt/timevault-vmtest")
    finally:
        vm.detach(loop)
    runner = vm.write_runner(img_dir, img)
    for line in report["fstab"]:
        print("  fstab:", line)
    print(f"\nGotowe. Uruchom maszynę wirtualną jako zwykły użytkownik (bez sudo):\n  {runner}")
    print("Sukces = ekran logowania Twojego Ubuntu w oknie QEMU. Log pierwszego startu w VM: "
          "/var/log/timevault-firstboot.log")


def cmd_install_recovery(args):
    from . import recovery
    info = recovery.install(args.disk, args.codename)
    print(f"\nRecovery zainstalowane w slocie {info['slot']} ({info['device']}, Ubuntu {info['codename']}).")
    print("Test: uruchom komputer ponownie, w menu startu (F12/F10/Esc) wybierz dysk USB.")


def _stdin_password() -> str:
    pw = sys.stdin.readline().rstrip("\n")
    if not pw:
        sys.exit("Brak hasła na wejściu.")
    return pw


def cmd_auto_backup(args):
    from . import service
    sys.exit(service.run_backup(trigger="manual" if args.manual else args.trigger))


def cmd_verify(args):
    from . import service
    sys.exit(service.run_check())


def cmd_browse(args):
    from . import service
    sys.exit(service.run_browse())


def cmd_setup_disk(args):
    from . import setup
    pw = _stdin_password() if args.password_stdin else getpass.getpass("Nowe hasło do backupu (min. 8 znaków): ")
    if not args.password_stdin and getpass.getpass("Powtórz hasło: ") != pw:
        sys.exit("Hasła się różnią.")
    word = _confirm(args, disk.describe(args.disk))
    setup.new_disk(args.disk, pw, word, codename=args.codename, with_recovery=not args.no_recovery,
                   progress=lambda m: print(m, flush=True),
                   erase_existing_timevault=args.erase_existing_timevault)
    print(i18n_L("GOTOWE", "DONE"), flush=True)


def cmd_setup_existing(args):
    from . import setup
    pw = _stdin_password() if args.password_stdin else getpass.getpass("Hasło do backupu: ")
    setup.existing_disk(pw, progress=lambda m: print(m, flush=True))
    print(i18n_L("GOTOWE", "DONE"), flush=True)


def cmd_config(args):
    from . import service
    cfg = service.load_config()
    changed = False
    for key, val in (("auto", args.auto), ("on_connect", args.on_connect)):
        if val:
            cfg[key], changed = val == "on", True
    if args.interval:
        cfg["interval"], changed = args.interval, True
    if args.language:
        cfg["language"], changed = args.language, True
    if changed:
        service.save_config(cfg)
    print(json.dumps(cfg))


def cmd_doctor(args):
    from . import doctor, service
    rep = doctor.run("/")
    if args.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    else:
        print(f"{rep['os'].get('pretty', '')}\n" + doctor.text(rep["items"]))
    if os.geteuid() == 0:
        service.compat_status("/", st_before=service.read_status())
    sys.exit(2 if rep["worst"] == "error" else 0)


def cmd_cloud_setup(args):
    from . import cloud, service
    token, sep = None, None
    if args.token_stdin:
        data = sys.stdin.read()
        if data.startswith("CLOUDPW:"):            # opcjonalne osobne hasło (pierwsza linia)
            first, data = data.split("\n", 1)
            sep = first[len("CLOUDPW:"):] or None
        token = cloud.parse_authorize_output(data) if args.remote_type == "drive" else None
    info = service.setup_cloud(token, separate_password=sep, progress=lambda m: print(m, flush=True),
                               remote_type=args.remote_type)
    print("CLOUDINFO " + json.dumps(info), flush=True)
    print(i18n_L("GOTOWE", "DONE"), flush=True)


def cmd_cloud_backup(args):
    from . import service
    sys.exit(service.run_cloud_backup(trigger=args.trigger))


def cmd_cloud_disconnect(args):
    from . import service
    service.disconnect_cloud()
    print(i18n_L("Chmura odłączona. Kopie na Google Drive zostały.", "Cloud disconnected. Backups on Google Drive were kept."))


def cmd_migrate_secrets(args):
    from . import secrets
    done = secrets.migrate()
    print(json.dumps({"migrated": done, "protection": secrets.protection("password")}))


def cmd_status(args):
    from . import service
    print(json.dumps(service.read_status(), indent=2, ensure_ascii=False))


def build_parser():
    p = argparse.ArgumentParser(prog="timevault", description="TimeVault — Time Machine dla Ubuntu")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="pokaż polecenia zmieniające stan, nie wykonuj")
    p.add_argument("--repo", help="repozytorium restic (domyślnie dysk TM-DATA), np. rclone:gdrive:timevault")
    p.add_argument("--cache-dir")
    p.add_argument("--password-file")
    p.add_argument("--confirm", help=argparse.SUPPRESS)
    p.add_argument("--lang", choices=["auto", "pl", "en"], help="język komunikatów / message language")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("inspect", help="pokaż manifest systemu")
    s.add_argument("--root", default="/")
    s.set_defaults(fn=cmd_inspect)

    sub.add_parser("disks", help="lista dysków").set_defaults(fn=cmd_disks)

    s = sub.add_parser("prepare-disk", help="przygotuj dysk zewnętrzny (Recovery + backup)")
    s.add_argument("disk")
    s.add_argument("--erase-existing-timevault", action="store_true",
                   help="świadomie wymaż istniejący dysk TimeVault razem z kopiami")
    s.set_defaults(fn=cmd_prepare_disk)

    sub.add_parser("init", help="utwórz repozytorium").set_defaults(fn=cmd_init)

    s = sub.add_parser("backup", help="wykonaj backup całego systemu")
    s.add_argument("--root", default="/")
    s.add_argument("--no-forget", action="store_true")
    s.set_defaults(fn=cmd_backup)

    sub.add_parser("snapshots", help="lista snapshotów").set_defaults(fn=cmd_snapshots)

    s = sub.add_parser("check", help="weryfikacja repozytorium")
    s.add_argument("--read-data", metavar="PROCENT", help="np. 5%% — odczytaj losową część danych")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("restore", help="odtwórz system")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--disk", help="cały dysk (zostanie wymazany)")
    g.add_argument("--partition", help="tylko ta partycja (dual boot)")
    s.add_argument("--esp", help="partycja EFI (tryb --partition)")
    s.add_argument("--snapshot", default="latest")
    s.add_argument("--boot-mode", choices=["uefi", "bios"])
    s.add_argument("--plan-only", action="store_true", help="pokaż plan i zakończ")
    s.add_argument("--skip-bootloader", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("vm-test", help="odtwórz backup do maszyny wirtualnej i przygotuj jej start")
    s.add_argument("--snapshot", default="latest")
    s.add_argument("--image-dir", help="gdzie zapisać obraz (domyślnie TM-DATA/vm)")
    s.set_defaults(fn=cmd_vm_test)

    s = sub.add_parser("install-recovery", help="zbuduj/aktualizuj system Recovery na dysku backupu")
    s.add_argument("--disk", help="dysk backupu (domyślnie wykrywany po etykietach)")
    s.add_argument("--codename", help="wersja Ubuntu dla Recovery (domyślnie jak ten system)")
    s.set_defaults(fn=cmd_install_recovery)

    # --- dla usług systemd i aplikacji
    s = sub.add_parser("auto-backup", help="backup w tle (systemd)")
    s.add_argument("--manual", action="store_true", help="na żądanie — bez limitu częstotliwości")
    s.add_argument("--trigger", choices=["timer", "connect", "manual"], default="timer")
    s.set_defaults(fn=cmd_auto_backup)
    sub.add_parser("verify", help="weryfikacja repozytorium (systemd)").set_defaults(fn=cmd_verify)
    sub.add_parser("browse", help="zamontuj kopie do przeglądania (systemd)").set_defaults(fn=cmd_browse)
    s = sub.add_parser("setup-disk", help="nowy dysk: partycje + hasło + Recovery + automatyczne kopie")
    s.add_argument("disk")
    s.add_argument("--password-stdin", action="store_true")
    s.add_argument("--codename")
    s.add_argument("--no-recovery", action="store_true")
    s.add_argument("--erase-existing-timevault", action="store_true",
                   help="świadomie wymaż istniejący dysk TimeVault razem z kopiami")
    s.set_defaults(fn=cmd_setup_disk)
    s = sub.add_parser("setup-existing", help="połącz z już przygotowanym dyskiem TimeVault")
    s.add_argument("--password-stdin", action="store_true")
    s.set_defaults(fn=cmd_setup_existing)
    s = sub.add_parser("config", help="ustawienia")
    s.add_argument("--auto", choices=["on", "off"], help="kopie według harmonogramu")
    s.add_argument("--interval", choices=["1h", "4h", "24h", "7d"])
    s.add_argument("--on-connect", choices=["on", "off"], help="kopia po podłączeniu dysku")
    s.add_argument("--language", choices=["auto", "pl", "en"])
    s.set_defaults(fn=cmd_config)
    sub.add_parser("status", help="stan (bez roota)").set_defaults(fn=cmd_status)
    sub.add_parser("migrate-secrets", help="zaszyfruj hasła zapisane jawnie przez starsze wersje"
                   ).set_defaults(fn=cmd_migrate_secrets)
    s = sub.add_parser("cloud-setup", help="połącz z Google Drive (token z `rclone authorize` na stdin)")
    s.add_argument("--token-stdin", action="store_true")
    s.add_argument("--remote-type", default="drive", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_cloud_setup)
    s = sub.add_parser("cloud-backup", help="kopia do chmury (systemd)")
    s.add_argument("--trigger", choices=["timer", "manual"], default="timer")
    s.set_defaults(fn=cmd_cloud_backup)
    sub.add_parser("cloud-disconnect", help="odłącz Google Drive (kopie zostają)").set_defaults(fn=cmd_cloud_disconnect)
    s = sub.add_parser("doctor", help="sprawdź zgodność z bieżącym Ubuntu / compatibility check")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_doctor)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    from . import i18n
    if args.lang:
        i18n.set_lang(args.lang)
    else:
        try:
            from . import service
            i18n.set_lang(service.load_config().get("language"))
        except Exception:  # noqa: BLE001
            pass
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    sh.DRY_RUN = args.dry_run
    if os.geteuid() != 0 and args.cmd not in ("disks", "status", "doctor"):
        sys.exit(i18n.L("timevault wymaga uprawnień roota (sudo).", "timevault needs root privileges (sudo)."))
    if not shutil.which("restic") and args.cmd not in ("inspect", "disks", "prepare-disk", "status", "config", "doctor"):
        sys.exit("Brak resticu: sudo apt install restic")
    if os.geteuid() == 0:
        try:
            from . import secrets
            secrets.migrate()          # jawne pliki ze starszych wersji -> sejf
        except Exception:  # noqa: BLE001
            pass
    try:
        args.fn(args)
    except (disk.SafetyError, sh.CommandError, RuntimeError) as e:
        sys.exit(i18n.L("Błąd: ", "Error: ") + str(e))


if __name__ == "__main__":
    main()
