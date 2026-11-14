# Linux Backup TimeVault

**Time Machine–style full-system backup and bare-metal restore for Ubuntu.**

Linux Backup TimeVault (project `linux-backup-timevault`, command `timevault`) backs up your entire Ubuntu installation — apps, settings, users, snaps, files —
to an external disk (optionally Google Drive), keeps an hourly/daily/weekly history, and can
restore the system 1:1 onto the same disk or a brand-new one by booting a Recovery system
that lives on the backup disk itself.

> Current version: **0.7.7** — see the [Changelog](#changelog) below.
> Polish documentation: [README.pl.md](README.pl.md).
> The user interface is available in **English and Polish** (follows the system language).

| | |
|---|---|
| Supported systems | Ubuntu 24.04 LTS and newer (tested up to 26.10), UEFI or BIOS |
| Root filesystem | ext4 (no separate `/boot`, no LUKS/LVM yet) |
| Dual boot | Yes — Windows partitions are never touched |
| Backup engine | [restic](https://restic.net) — deduplicated, compressed, encrypted |
| Off-site copy | Optional second copy on Google Drive (via rclone), restorable from Recovery |
| License | Freeware, no modifications (see `LICENSE`) |

## How it works

```
External disk (GPT)
├── TM-EFI          512 MiB  FAT32  boot loader (shim + signed GRUB, Secure Boot OK)
├── TM-RECOVERY-A   3 GiB    ext4   Recovery system (slot A)
├── TM-RECOVERY-B   3 GiB    ext4   Recovery system (slot B, safe A/B updates)
└── TM-DATA         rest     ext4   restic repository + manifests
```

* **One disk, everything included.** The backup disk is also bootable: pick it in the
  firmware boot menu (F12/F10/Esc) and you get the TimeVault Recovery menu.
* **Backups** cover the whole root filesystem (`--one-file-system`). Caches, trash, `/tmp`,
  swap files and apt `.deb` caches are excluded; mount points are kept so `fstab` still works.
  Snaps are included, so nothing needs to be re-downloaded after a restore.
* **Retention** like Time Machine: everything from the last 24 h, daily for 31 days, then
  weekly — never fewer than the last 3 snapshots.
* **Manifest** stored with every snapshot: partition layout, UUIDs, boot mode, ESP, GPU,
  OS version — everything needed to rebuild the system on an empty disk.

### Restore modes

| Mode | When | What is written |
|---|---|---|
| **Partition** | Same laptop, Ubuntu broken (dual boot) | Only the Ubuntu partition is reformatted; GRUB goes into the existing EFI partition next to Windows Boot Manager |
| **Whole disk** | New or replaced disk / new laptop | The disk is wiped: new EFI + Ubuntu partition; GRUB also installed to the fallback path so it boots without an NVRAM entry |

After writing the data, TimeVault assigns new UUIDs and rewrites `fstab`, recreates swap
files, rebuilds initramfs and GRUB in a chroot, marks disks that no longer exist as
`nofail` (so boot never hangs), and installs a one-shot first-boot service that adapts the
NVIDIA setup to the new hardware.

### Safety rails (each one covered by tests)

* Refuses to wipe the disk the running system boots from, or any disk with mounted partitions.
* Refuses to format NTFS/BitLocker/FAT/LUKS/swap partitions as the Ubuntu target.
* Re-checks the target right before writing; checks free space before wiping anything.
* Every destructive action requires typing `WYMAZ` (Polish for *erase*).
* Backups never mount or write the Recovery/EFI partitions; the laptop's own EFI
  partition is never touched by Recovery installation.
* VM restore tests never write to the host's UEFI NVRAM.

## Installation

Download the newest `timevault_<version>_all.deb` from the [Releases page](https://github.com/jgu-linux-apps/linux-backup-timevault/releases/latest) and install it:

```bash
sudo apt install ./timevault_<version>_all.deb
```

The package installs:

* `timevault-gui` — tray app (autostarts on login) and main window,
* `timevault` — command-line tool,
* systemd units: `timevault-backup.timer` (hourly check), `timevault-backup.service`
  (scheduled), `timevault-backup-connect.service` (disk plugged in),
  `timevault-backup-now.service`, `timevault-verify.service`, `timevault-browse.service`,
* a udev rule that hides TimeVault partitions from the desktop and starts a backup
  when the disk is plugged in,
* a polkit rule that lets a local administrator start/stop *only* the TimeVault units
  without a password.

## Where passwords are stored

| Secret | Location | Protection |
|---|---|---|
| Backup password | `/etc/timevault/password.cred` | encrypted with `systemd-creds`: **TPM 2.0 + system key** (system key only if there is no TPM) |
| Google Drive token | `/etc/timevault/rclone.cred` | same |
| Separate cloud password (optional) | `/etc/timevault/cloud-password.cred` | same |
| Drive credentials in the rescue system | Recovery partition | encrypted with your backup password (OpenSSL AES-256, PBKDF2) |
| The backups themselves | USB disk / Google Drive | encrypted by restic |

* No secret is ever stored in plain text. restic gets the password straight from the
  credential store (`RESTIC_PASSWORD_COMMAND`); rclone gets a temporary config file in
  `/run` (RAM, `0600`) for the duration of an operation, refreshed tokens are re-encrypted.
* A copied `.cred` file is useless on another computer (TPM-bound key). After restoring the
  system on a **different** computer the app asks for the backup password once again.
* Plain-text files written by versions ≤ 0.6.3 are encrypted automatically on upgrade and
  the originals are overwritten with zeros and deleted (`timevault migrate-secrets`).
* Limits: an administrator logged in on the running laptop can still read the password —
  unattended backups need it, and such a user can read all your files anyway. Recovery always
  asks for the password — **do not lose it**: without it the backups cannot be decrypted.

## Using the app

1. Plug in an external disk and open **TimeVault**.
2. The app detects the disk and shows one action:
   * **Use this disk…** — the disk is already a TimeVault disk: enter the backup password,
     nothing is erased;
   * **Prepare this disk…** — a new external disk: type `WYMAZ`, set a password. TimeVault
     partitions the disk, creates the repository, installs Recovery and starts the first
     backup — all in one step. Only external (USB) disks are ever offered.
3. From then on backups run automatically. Choose the **frequency** (every hour, every 4 hours,
   once a day, once a week, or off) and whether to **back up as soon as the disk is
   connected**. A notification tells you when it is safe to unplug. Hover over any button or
   option for an explanation.
4. **Click a backup in the history** to open it in Files (read-only) — your home folder from
   that moment, with the whole system tree above it. **Browse backups** opens the full
   history. Copy any file from any point in time.
5. **Verify backups** checks repository integrity and reads a random 5 % of the data.
6. **Compatibility with Ubuntu** is checked after every backup (filesystem, boot loader,
   tools, rescue-system packages, rescue system vs. installed Ubuntu release). After an Ubuntu
   upgrade (e.g. 26.10 → 27.04) you get a notification to refresh the rescue system; problems
   are reported *before* something stops working. Manual check: **More → Check compatibility
   with Ubuntu** or `timevault doctor`.
7. **More → Refresh rescue system on the disk…** rebuilds Recovery (newer kernel/drivers) —
   worth doing after a major Ubuntu upgrade. The previous version is kept as a fallback.

## Google Drive (off-site copy)

**Settings → Cloud → Connect to Google Drive…** opens the browser; sign in and allow access.
TimeVault only gets access to the files it creates (`drive.file` scope), not to the rest of your
Drive. Backups are compressed, deduplicated and **encrypted on your computer before upload**;
each upload after the first contains only changes.

* Before enabling, the app shows the estimated size of the first upload (≈ the size of the
  backups on the USB disk), the expected time at 20 and 50 Mbit/s, and checks free space on
  Drive (free Google Drive = 15 GB; a full-system backup usually needs 100 GB or more).
* Connecting uploads nothing: automatic uploads start as **manual only**; choose every 6 hours,
  once a day or once a week when you are ready; optionally only on mains power.
* Same password as the USB disk by default, or a separate one.
* The rclone credentials are stored in `/etc/timevault/rclone.conf` (root, 0600) and copied into
  the rescue system **encrypted with the backup password** (OpenSSL AES-256, PBKDF2).
* **Restore from the cloud:** boot Recovery → *Restore* → source **Google Drive** → connect to
  Wi-Fi if needed → password → snapshot → target → plan → `WYMAZ` — same as from the USB disk.
* *Disconnect Google Drive* stops uploads on this computer; backups on Drive are kept.

After connecting Google Drive, refresh the rescue system once (**More → Refresh the rescue
system**) so it contains the tools for restoring from the cloud.

## Restoring the whole system

1. Plug in the TimeVault disk, power on and choose it in the firmware boot menu.
2. Select **TimeVault Recovery** → *Odtworz system z kopii* (restore system).
3. Enter the password, pick a snapshot, pick the target:
   *previous Ubuntu partition* (recommended for dual boot) or *whole disk*.
4. Review the plan, type `WYMAZ`, wait, reboot without the USB disk.

If Google Drive is connected, Recovery first asks for the source: *TimeVault USB disk* (fast)
or *Google Drive* (needs internet, slower).

## Command line

```bash
sudo timevault disks                         # list disks (system disk is marked [SYSTEM])
sudo timevault setup-disk /dev/sdX           # all-in-one: partitions + password + Recovery
sudo timevault setup-existing                # connect to an already prepared disk
sudo timevault backup                        # manual full-system backup
sudo timevault snapshots                     # list snapshots
sudo timevault check --read-data 5%          # verify
sudo timevault install-recovery              # build/update Recovery (A/B)
sudo timevault vm-test                       # restore into a VM image and boot it in QEMU
sudo timevault restore --partition /dev/nvme0n1p5 --plan-only
sudo timevault cloud-backup --trigger manual  # upload to Google Drive now
sudo timevault cloud-disconnect              # stop uploads (backups on Drive are kept)
timevault status                             # state as JSON (no root needed)
timevault doctor                             # compatibility check with the installed Ubuntu
sudo timevault config --interval 4h --on-connect on   # 1h | 4h | 24h | 7d, --auto off
timevault --lang en …                        # message language: pl | en | auto
```

## Repository layout and building

The source code is in `src/`; the `.deb` packages are not kept in git but attached to
[GitHub Releases](https://github.com/jgu-linux-apps/linux-backup-timevault/releases).
Pushing a tag `vX.Y.Z` that matches `__version__` in `src/timevault/__init__.py` makes GitHub
Actions (`.github/workflows/release.yml`) build the package and publish the release.

```bash
cd src
bash packaging/build-deb.sh        # -> dist/timevault_<version>_all.deb
```

## Testing

Run inside `src/`:

```bash
sudo python3 -m pytest tests/      # creates loop devices under /tmp/timevault-test
```

The suite (31 tests) runs real partitioning, formatting, backup and restore on loop devices,
including dual-boot layouts and every safety rail. Release testing additionally covers:
a minimal Ubuntu 24.04 → backup → restore → boot to login in QEMU/UEFI; Recovery booted in
QEMU restoring onto an empty second disk with a real `grub-install`; and the restored disk
booting with an empty NVRAM.

## Known limitations

* Root must be ext4 without a separate `/boot`, LUKS or LVM.
* ext4 has no snapshots: files changing during a backup (e.g. an open browser profile) may be
  captured in an inconsistent state. Close heavy apps for the first backup.
* If the USB disk is lost too, restoring from Google Drive needs a new rescue USB stick made on
  another Ubuntu computer (planned).

## Future Ubuntu releases

| Possible change in Ubuntu | Effect | What you see |
|---|---|---|
| Package renamed in the rescue-system set | Refreshing Recovery fails | Warning in advance; the old Recovery keeps working (A/B) |
| Ubuntu drops GRUB (e.g. systemd-boot) | Restore stops at the boot loader step | Compatibility error in advance; TimeVault update needed |
| New install on btrfs/ZFS/LUKS | Backups refuse to run | Compatibility error + notification |
| Release upgrade (26.10 → 27.04) | Rescue system has an older kernel | Notification: refresh the rescue system |

Restores always run the restored system's own GRUB/initramfs tools in a chroot, so a newer
Ubuntu is restored with its own tools.

## Versioning

TimeVault follows [Semantic Versioning](https://semver.org): `MAJOR.MINOR.PATCH`.
While the major version is `0`, minor versions may change behaviour; on-disk formats
(partition labels, repository layout, manifests) stay backward compatible — disks and
backups created by older versions keep working.

## Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

### [0.7.7] — 2026-11-14

#### Changed
- **Aurora** now also colours the window title bar (it stayed white): the window gets its own bar with the
  title and round minimise / maximise / close buttons, in GNOME's button order. Drag it to move the window,
  double-click to maximise, drag the window edges to resize. **System** keeps the normal title bar.

### [0.7.6] — 2026-11-14

#### Added
- **Aurora** theme (More → Theme), the shared look of the JGu Linux Apps series: lavender / deep navy,
  violet-to-cyan gradients. Light and dark follow the system. **System** keeps the standard Qt look.

### [0.7.5] — 2026-11-12

#### Changed
- License: the app is now published under the Freeware License (No Modifications),
  see `LICENSE`; free to use and share unmodified, modification is not permitted.
  No other changes in the app.

### [0.7.4] — 2026-11-12

#### Changed
- No changes in the app. The repository now keeps the source code in `src/`, and the `.deb`
  is published on the GitHub Releases page (built by GitHub Actions for every version tag).
  README: installation from Releases, repository layout.

### [0.7.3] — 2026-11-06

#### Fixed
- Notifications put a large bell icon into the top bar (Qt's `QSystemTrayIcon.showMessage`
  switches the tray icon to "needs attention" on GNOME) and clicking them did nothing. They are
  now regular GNOME notifications (`notify-send`) with the TimeVault icon: clicking a
  notification opens the app and GNOME removes it. Errors use critical urgency.
- Package depends on `libnotify-bin`.

### [0.7.2] — 2026-11-06

#### Changed
- About dialog shows the credits: *Created by: JGu* / *Supported by Claude*.

### [0.7.1] — 2026-11-06

#### Fixed
- A backup in which a few files could not be fully read (restic exit code 3 — e.g.
  `incomplete metadata … xattr.list` on a Flatpak file updated during the backup) was reported
  as **failed**, left out of the history and skipped retention. It is now a saved backup with a
  warning: the window shows how many files were not fully read (hover for the list) and the
  notification mentions it. Real errors are still reported as errors. File-level errors from
  restic's stderr are now collected too.

### [0.7.0] — 2026-11-05

#### Security
- **Passwords and the Google Drive token are no longer stored in plain text.** They are kept in
  the `systemd-creds` credential store, encrypted with a TPM 2.0–bound key plus the system key
  (system key only without TPM). restic reads the password via `RESTIC_PASSWORD_COMMAND`;
  rclone gets a temporary `0600` config in `/run` only for the duration of an operation.
- Automatic migration of plain-text secrets from earlier versions on upgrade; originals are
  overwritten with zeros and deleted.
- The compatibility check reports how the password is protected.
- If the stored password cannot be decrypted (system restored on another computer), the app
  asks for it again instead of failing silently.

### [0.6.3] — 2026-11-05

#### Fixed
- **Browsing backups failed** with `fusermount: mount failed: Permission denied`: since Ubuntu
  25.04 AppArmor allows `fusermount3` to mount only under `/mnt`, `/media`, `/tmp`, `$HOME` and
  `/run/user/<uid>`. Backups are now mounted at `/mnt/timevault-browse`; the service reports
  "ready" only once the backups are visible and passes mount errors to the app.
- Connecting Google Drive no longer schedules uploads: cloud uploads default to **manual only**
  until you choose a frequency. Nothing is uploaded by connecting.
- Connecting Google Drive hung for minutes while estimating the upload size by walking ~60 GB
  of repository files on the USB disk; the estimate is now read instantly from the partition.

### [0.6.2] — 2026-11-05

#### Added
- Clicking a backup in the history opens it in Files (read-only), starting in your home folder
  from that moment.

#### Fixed
- Google Drive sign-in reported "failed" after a successful login: rclone ≥ 1.59 (Ubuntu's
  1.60) returns the token as a base64 blob instead of JSON. Both formats are parsed now; error
  messages no longer show any part of the token.

### [0.6.1] — 2026-11-05

#### Changed
- Display name changed to **Linux Backup TimeVault** (window, tray, menu, About, Recovery,
  boot menu). Package, command and on-disk formats are unchanged (`timevault`).
- Compatibility dialog: unambiguous symbols (✓ ℹ ⚠ ✗) and fully Polish wording.

#### Fixed
- Google Drive sign-in failed with `decode simple map: illegal base64 data`: rclone expects the
  options blob in base64 **without padding**. Verified with rclone 1.60 (Ubuntu package).

### [0.6.0] — 2026-11-05

#### Added
- **Google Drive** as an optional second, off-site copy: sign-in in the browser (`rclone
  authorize`, `drive.file` scope), encrypted repository `TimeVault/` on Drive, own schedule
  (6 h / daily / weekly), optional mains-power-only uploads, separate or shared password,
  free-space check and upload time estimate before enabling, "Upload now", "Disconnect".
- **Restore from Google Drive in Recovery**: source choice (USB disk / Google Drive), network
  setup via nmtui, credentials stored in the rescue system encrypted with the backup password.
- **Settings** window with tabs *Backups*, *Cloud* and *Language*; the main window shows a
  summary of the schedule and the last cloud backup.
- systemd units `timevault-cloud.timer`, `timevault-cloud.service`, `timevault-cloud-now.service`.

#### Changed
- Backup schedule and language moved from the main window / More menu to **Settings**.
- Package depends on `rclone` and `openssl`; the rescue system includes `openssl`.

### [0.5.1] — 2026-11-05

#### Added
- **Light/dark app icon** that follows the system theme in the window, the dock and the
  application menu (GNOME does not switch app icons by itself — the app keeps a per-user
  `.desktop` copy pointing to the matching icon; `TryExec` hides it if the app is removed).
- Tray icon: white on GNOME (the Ubuntu top bar is dark in both themes), follows the theme
  on desktops with a light panel.
- **Version** shown in the main window, in the tray tooltip and in **More → About**
  (with backup engine version and system).

### [0.5.0] — 2026-11-05

#### Added
- **Compatibility check** (`timevault doctor`, after every backup, **More → Check compatibility
  with Ubuntu**): root filesystem, boot loader (GRUB vs systemd-boot), required tools,
  debootstrap support for the current release, availability of rescue-system packages, and
  the rescue system's Ubuntu release vs the installed one. Warnings shown in the main window
  and as one-time notifications.
- **Ubuntu upgrade detection**: a change of release between backups (or since the last backup)
  triggers "Ubuntu was upgraded — refresh the rescue system".
- Rescue system records the Ubuntu release it was built from.
- **Hard safety lock**: `prepare-disk` / `setup-disk` refuse to erase a disk that already holds
  TimeVault partitions (read directly from GPT/superblocks, not from udev), even with the
  confirmation word. Intentional re-initialisation needs `--erase-existing-timevault`.

#### Fixed
- **Critical (0.4.0):** on Ubuntu 26.10 `lsblk -J` without the `NAME` column returns a flat
  list, so the app did not see the partitions of the TimeVault disk and offered to *prepare*
  (erase) it. The device tree is now rebuilt from `PKNAME`; `/dev/disk/by-partlabel` and
  `by-label` are used as a second, independent source; regression test with the real output.

### [0.4.0] — 2026-11-05

#### Added
- **English language** for the app, tray, notifications, the Recovery menu (language choice on
  start) and engine messages (restore plans, warnings, errors). Follows the system language;
  can be changed under **More → Język / Language**. Commands run via pkexec get the app's
  language (`timevault --lang pl|en`).
- **Backup frequency**: every hour, every 4 hours, once a day, once a week, or off (manual only).
- **Back up as soon as the disk is connected** — separate switch; handled by udev
  (`timevault-backup-connect.service`, at most once per 15 minutes) with the app as a fallback
  when it detects the disk.
- **Tooltips** on every button and option explaining what it does.
- Settings stored in `/var/lib/timevault/config.json` (no secrets, writable by the `sudo`
  group) — administrators change them in the app without a password prompt; values are
  validated by the service.

#### Changed
- Scheduled backups: the hourly timer now checks whether a backup is due for the chosen
  frequency (`auto-backup --trigger timer|connect|manual`).
- Disk setup keeps existing settings instead of resetting them.

### [0.3.1] — 2026-11-05

#### Changed
- First-run screen rewritten: the app detects the connected disk and shows **one** clear
  action — *Use this disk…* for a disk already prepared by TimeVault (only asks for the
  password, nothing is erased), *Prepare this disk…* for a new external disk (with an
  explicit erase warning), or *Plug in an external disk* when none is connected.
- "Update Recovery" moved to **More → Refresh rescue system on the disk…** with a plain
  explanation of what the rescue system is and when to refresh it.

#### Fixed
- The new-disk wizard could list internal non-system disks (e.g. a second NVMe data
  drive). Only external disks (USB / hot-plug / removable) are offered now.

### [0.3.0] — 2026-11-05

#### Added
- **Desktop app** (`timevault-gui`, PyQt6): tray icon with idle / running / error /
  disk-absent states (light and dark variants), main window with last backup, disk usage,
  history, and actions; notifications on completion, failure and disk connection;
  single instance; autostart on login.
- **All-in-one disk setup**: `setup-disk` partitions the disk, stores the password, creates
  the repository, installs Recovery and starts the first backup. `setup-existing` connects to
  a disk prepared by an earlier version.
- **Automatic backups**: hourly systemd timer and udev trigger on disk plug-in; at most one
  automatic backup per ~50 minutes; disk unmounted after each backup.
- **Browse backups** read-only in Files (`restic mount` via `timevault-browse.service`).
- **Verify** action (`timevault-verify.service`, 5 % data read).
- Status (`/var/lib/timevault/status.json`) and settings readable without root; password in
  `/etc/timevault/password` (root, 0600).
- udev rule hiding TimeVault partitions from the desktop; polkit rule allowing local
  administrators to start/stop only TimeVault units.
- `.deb` package (`packaging/build-deb.sh`), launchers pinned to `/usr/bin/python3`.
- English README; Polish README moved to `README.pl.md`.

#### Changed
- Recovery menu: dialogs sized to the screen, numbered entries instead of device tags,
  whole-disk targets show their contents (e.g. `NTFS 'Dane'`) and an explicit
  "everything will be erased" warning, snapshots show the OS version.

### [0.2.0] — 2026-11-05

#### Added
- **Recovery system** on the backup disk: minimal Ubuntu built with debootstrap + apt,
  shim and signed GRUB on `TM-EFI` (Secure Boot compatible), text menu on tty1
  (restore, network, shell, reboot, power off).
- A/B Recovery slots: updates go to the inactive slot, the boot menu switches only after a
  successful build, the previous version stays as a fallback.
- `TM-EFI` written with mtools — never mounted, the laptop's EFI partition never touched.
- `install-recovery` command.

#### Verified
- Recovery booted in QEMU restoring onto an empty disk with a real `grub-install`;
  restored disk boots with an empty NVRAM. Recovery booted on real hardware.

### [0.1.2] — 2026-11-05

#### Changed
- Project renamed from **linux-backup-timemachine** to **linux-backup-timevault**
  (`python3 -m timevault`). Disks, repositories and manifests from 0.1.x keep working
  (legacy manifest path is still read; retention no longer groups by host).

#### Fixed
- Signed Ubuntu GRUB looks for `EFI/ubuntu/grub.cfg`, which `grub-install --removable`
  does not create → GRUB stopped at the `grub>` prompt. A config stub pointing to the new
  root UUID is now always written (verified with GRUB 2.14 from Ubuntu 26.10).
- First-boot service no longer waits for the network before the login screen.

#### Added
- VM test image: os-prober disabled, longer GNOME Shell start timeout (VM images on slow
  USB disks), 8 GB RAM and faster disk cache in `run-vm.sh`.

### [0.1.1] — 2026-11-05

#### Added
- `vm-test`: restore a snapshot into a sparse VM image on the backup disk and boot it in
  QEMU/UEFI without touching the host's partitions or NVRAM.

#### Fixed
- `update-grub` wrote `root=/dev/loopXpY` instead of `root=UUID=…` when the
  `/dev/disk/by-uuid` link was missing; the link is now ensured and the result verified.
- Confirmation word changed to ASCII `WYMAZ`.
- Partition labels with spaces and USB bridge model names displayed correctly.

### [0.1.0] — 2026-11-05

#### Added
- Backup engine on restic: system manifest, full-system backup to an external disk or
  rclone remote, Time Machine–like retention, verification.
- Backup disk layout: `TM-EFI`, `TM-RECOVERY-A/B`, `TM-DATA`.
- Restore in partition mode (dual boot) and whole-disk mode with UUID/`fstab` rewrite,
  swap recreation, chroot GRUB/initramfs, first-boot hardware adaptation.
- Safety rails and an end-to-end test suite on loop devices.

#### Fixed during development
- Retention `--keep-hourly` removed backups made within the same hour; replaced with
  `--keep-within 24h` + `--keep-last 3`.
