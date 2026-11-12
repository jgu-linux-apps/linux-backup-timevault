"""TimeVault — aplikacja (tray + okno). Działa jako zwykły użytkownik.

Operacje wymagające roota idą przez:
  * systemctl start/stop usług TimeVault (polkit pozwala bez hasła),
  * pkexec timevault … (przygotowanie dysku, hasło, system ratunkowy) — okno hasła systemu.
Stan: /var/lib/timevault/status.json, ustawienia: /var/lib/timevault/config.json
(administrator — grupa sudo — może je zmieniać bez hasła; bez sekretów).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys

from PyQt6.QtCore import QFileSystemWatcher, QProcess, QSettings, Qt, QTimer
from PyQt6.QtGui import QAction, QActionGroup, QGuiApplication, QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
                             QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit,
                             QProgressBar, QPushButton, QRadioButton, QSystemTrayIcon, QTabWidget,
                             QToolButton, QVBoxLayout, QWidget)

from . import APP_NAME, __version__, i18n
from .i18n import L
from .userdisks import (_children, candidate_disks, disk_name, human,  # noqa: F401
                        is_timevault_disk, lsblk, timevault_disk, timevault_disk_connected)

APP_ID = "timevault-gui"
STATUS_FILE = os.environ.get("TIMEVAULT_STATUS", "/var/lib/timevault/status.json")
CONFIG_FILE = os.path.join(os.path.dirname(STATUS_FILE), "config.json")
BROWSE_DIR = "/mnt/timevault-browse"
ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
TIMEVAULT = os.environ.get("TIMEVAULT_BIN", "/usr/bin/timevault")
DEFAULT_CONFIG = {"auto": True, "interval": "1h", "on_connect": True, "language": "auto",
                  "cloud": False, "cloud_interval": "24h", "cloud_ac_only": True}


def cloud_intervals():
    return [("off", L("Tylko ręcznie (przyciskiem)", "Manual only (button)")),
            ("6h", L("Co 6 godzin", "Every 6 hours")), ("24h", L("Raz dziennie", "Once a day")),
            ("7d", L("Raz w tygodniu", "Once a week"))]


def intervals():
    return [("1h", L("Co godzinę", "Every hour")), ("4h", L("Co 4 godziny", "Every 4 hours")),
            ("24h", L("Raz dziennie", "Once a day")), ("7d", L("Raz w tygodniu", "Once a week")),
            ("off", L("Wyłączone (tylko ręcznie)", "Off (manual only)"))]


# --------------------------------------------------------------------------- pomocnicze
def is_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        return False


def icon(name: str) -> QIcon:
    return QIcon(os.path.join(ICONS, name))


def app_icon_path() -> str:
    """Ikona aplikacji dopasowana do motywu: jasna lub ciemna."""
    return os.path.join(ICONS, "timevault-dark.svg" if is_dark() else "timevault.svg")


def app_icon() -> QIcon:
    return QIcon(app_icon_path())


USER_DESKTOP = os.path.expanduser("~/.local/share/applications/timevault.desktop")
SYSTEM_DESKTOP = "/usr/share/applications/timevault.desktop"


def sync_launcher_icon():
    """Dock i menu aplikacji biorą ikonę z pliku .desktop — GNOME sam nie przełącza ikon
    aplikacji z motywem. Trzymamy więc kopię .desktop użytkownika ze ścieżką do ikony jasnej
    albo ciemnej; GNOME od razu odświeża ikonę w docku i menu."""
    if not os.path.exists(SYSTEM_DESKTOP):
        return
    want = app_icon_path()
    try:
        lines = open(SYSTEM_DESKTOP).read().splitlines()
        content = "\n".join(f"Icon={want}" if l.startswith("Icon=") else l for l in lines) + "\n"
        if os.path.exists(USER_DESKTOP) and open(USER_DESKTOP).read() == content:
            return
        os.makedirs(os.path.dirname(USER_DESKTOP), exist_ok=True)
        with open(USER_DESKTOP + ".tmp", "w") as f:
            f.write(content)
        os.replace(USER_DESKTOP + ".tmp", USER_DESKTOP)
    except OSError:
        pass


def tray_icon(state: str) -> QIcon:
    # Panel GNOME jest ciemny w obu motywach -> jasna ikona; w innych środowiskach wg motywu.
    gnome = "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "")
    variant = "light" if (is_dark() or gnome) else "dark"
    st = {"running": "running", "checking": "running", "error": "error",
          "disk-absent": "absent", "not-configured": "absent"}.get(state, "idle")
    return icon(f"tray-{st}-{variant}.svg")


def read_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def read_status() -> dict:
    return read_json(STATUS_FILE)


def read_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in read_json(CONFIG_FILE).items() if k in DEFAULT_CONFIG})
    return cfg


def parse_time(s: str | None):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def ago(t) -> str:
    if not t:
        return L("nigdy", "never")
    secs = (dt.datetime.now().astimezone() - t).total_seconds()
    if secs < 90:
        return L("przed chwilą", "just now")
    if secs < 3600:
        return L(f"{int(secs // 60)} min temu", f"{int(secs // 60)} min ago")
    if t.date() == dt.date.today():
        return L(f"dziś {t:%H:%M}", f"today {t:%H:%M}")
    if t.date() == dt.date.today() - dt.timedelta(days=1):
        return L(f"wczoraj {t:%H:%M}", f"yesterday {t:%H:%M}")
    return f"{t:%d.%m.%Y %H:%M}" if i18n.lang() == "pl" else f"{t:%Y-%m-%d %H:%M}"


def cloud_summary(st: dict, cfg: dict) -> str:
    cs = st.get("cloud_state")
    if not cfg.get("cloud") or cs in (None, "not-configured"):
        return L("wyłączona", "off")
    names = {"running": L("trwa wysyłanie…", "uploading…"),
             "offline": L("czeka na internet", "waiting for internet"),
             "waiting-power": L("czeka na zasilanie z sieci", "waiting for mains power"),
             "error": L("błąd — szczegóły w Ustawieniach", "error — see Settings")}
    if cs in names:
        return names[cs]
    last = parse_time(st.get("cloud_last_success"))
    if not last:
        return L("połączona, nic jeszcze nie wysłano", "connected, nothing uploaded yet")
    return "<b>" + ago(last) + "</b>" + (L(" (wysyłanie ręczne)", " (manual uploads)")
                                          if cfg.get("cloud_interval") == "off" else "")


def compat_issues(st: dict) -> list[tuple[str, str, str]]:
    """Ostrzeżenia zgodności do pokazania: (poziom, tekst, klucz do powiadomień)."""
    from . import doctor
    issues = []
    for it in st.get("compat") or []:
        if it.get("level") in ("warn", "error"):
            issues.append((it["level"], L(it["pl"], it["en"]), f"{it['id']}:{it['en']}"))
    seen = (st.get("os") or {}).get("version_id")
    cur = doctor.os_info()
    if seen and cur.get("version_id") and cur["version_id"] != seen:
        # Ubuntu zaktualizowane, a od tego czasu nie było kopii (usługa jeszcze nie sprawdziła).
        issues.append(("warn", L(f"Ubuntu zaktualizowano do {cur['pretty']}. Utwórz kopię i odśwież system "
                                 "ratunkowy (Więcej → Odśwież system ratunkowy).",
                                 f"Ubuntu was upgraded to {cur['pretty']}. Make a backup and refresh the rescue "
                                 "system (More → Refresh the rescue system)."), f"os:{cur['version_id']}"))
    return issues


def systemctl(action: str, unit: str) -> bool:
    return subprocess.run(["systemctl", action, "--no-block", unit],
                          capture_output=True).returncode == 0


def root_args(args: list[str]) -> list[str]:
    """Polecenia uruchamiane przez pkexec dostają język aplikacji (pkexec czyści środowisko)."""
    return [TIMEVAULT, "--lang", i18n.lang(), *args]


# --------------------------------------------------------------------------- powiadomienia
class Notifier:
    """Zwykłe powiadomienia GNOME (notify-send) zamiast QSystemTrayIcon.showMessage:
    Qt przy showMessage przełącza ikonę traya w stan „wymaga uwagi” (duży dzwonek w panelu),
    a kliknięcie w takie powiadomienie nic nie robi. Tu: ikona TimeVault, kliknięcie otwiera
    aplikację, GNOME zamyka kliknięte powiadomienie."""

    MAX_PENDING = 5

    def __init__(self, on_open, fallback):
        self.on_open = on_open
        self.fallback = fallback          # QSystemTrayIcon.showMessage, gdy brak notify-send
        self.pending: list[QProcess] = []

    def notify(self, title: str, body: str, error: bool = False):
        if not shutil.which("notify-send"):
            self.fallback(title, body, error)
            return
        args = ["-a", APP_NAME, "-i", app_icon_path(),
                "-h", "string:desktop-entry:timevault",
                "-u", "critical" if error else "normal",
                "-A", "default=" + L("Otwórz", "Open"),
                title, body]
        proc = QProcess()
        proc.finished.connect(lambda *_a, p=proc: self._done(p))
        proc.start("notify-send", args)
        self.pending.append(proc)
        while len(self.pending) > self.MAX_PENDING:      # stare, niekliknięte — nie trzymamy procesów
            old = self.pending.pop(0)
            old.kill()

    def _done(self, proc: QProcess):
        out = bytes(proc.readAllStandardOutput()).decode(errors="replace").strip()
        if proc in self.pending:
            self.pending.remove(proc)
        if out == "default":                 # kliknięcie w powiadomienie (lub przycisk „Otwórz”)
            self.on_open()


# --------------------------------------------------------------------------- okna dialogowe
class ProcessDialog(QDialog):
    """Uruchamia pkexec timevault … i pokazuje postęp na żywo."""

    def __init__(self, parent, title, args, stdin_data: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 380)
        lay = QVBoxLayout(self)
        self.label = QLabel(L("Trwa… (system może poprosić o hasło administratora)",
                              "Working… (the system may ask for the administrator password)"))
        self.log = QPlainTextEdit(readOnly=True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.close_btn = QPushButton(L("Zamknij", "Close"))
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        for w in (self.label, self.bar, self.log, self.close_btn):
            lay.addWidget(w)
        self.ok = False
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._done)
        self.proc.start("pkexec", root_args(args))
        if stdin_data is not None:
            self.proc.write((stdin_data + "\n").encode())
            self.proc.closeWriteChannel()

    def _read(self):
        self.log.appendPlainText(bytes(self.proc.readAllStandardOutput()).decode(errors="replace").rstrip())

    def _done(self, code, _status):
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self.ok = code == 0
        if self.ok:
            self.label.setText(L("Gotowe.", "Done."))
        elif code in (126, 127):
            self.label.setText(L("Anulowano.", "Cancelled."))
        else:
            self.label.setText(L(f"Nie powiodło się (kod {code}).", f"Failed (code {code})."))
        self.close_btn.setEnabled(True)


class PasswordDialog(QDialog):
    def __init__(self, parent, title, text, confirm=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        lay = QVBoxLayout(self)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        self.pw = QLineEdit(echoMode=QLineEdit.EchoMode.Password, placeholderText=L("Hasło", "Password"))
        lay.addWidget(self.pw)
        self.pw2 = None
        if confirm:
            self.pw2 = QLineEdit(echoMode=QLineEdit.EchoMode.Password,
                                 placeholderText=L("Powtórz hasło", "Repeat password"))
            lay.addWidget(self.pw2)
        self.err = QLabel("")
        self.err.setStyleSheet("color:#dc2626")
        lay.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _ok(self):
        if self.pw2 is not None:
            if len(self.pw.text()) < 8:
                self.err.setText(L("Hasło musi mieć co najmniej 8 znaków.", "The password must have at least 8 characters."))
                return
            if self.pw.text() != self.pw2.text():
                self.err.setText(L("Hasła się różnią.", "The passwords do not match."))
                return
        elif not self.pw.text():
            return
        self.accept()


class NewDiskDialog(QDialog):
    """Kreator: wybór dysku -> ostrzeżenie -> WYMAZ -> hasło."""

    def __init__(self, parent, preselect: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(L("Przygotuj dysk TimeVault", "Prepare a TimeVault disk"))
        self.resize(560, 420)
        lay = QVBoxLayout(self)
        intro = QLabel(L("Wybierz dysk zewnętrzny. Zostanie <b>całkowicie wymazany</b> i przygotowany jako "
                         "dysk TimeVault: system ratunkowy (start komputera z tego dysku) + kopie zapasowe — "
                         "wszystko na jednym dysku.",
                         "Choose an external disk. It will be <b>completely erased</b> and set up as a TimeVault "
                         "disk: a rescue system (boot the computer from this disk) + backups — all on one disk."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.list = QListWidget()
        self.list.setToolTip(L("Pokazywane są tylko dyski zewnętrzne (USB). Dysk wewnętrzny komputera "
                               "nigdy nie jest proponowany.",
                               "Only external (USB) disks are shown. The computer's internal disk "
                               "is never offered."))
        for d in candidate_disks():
            parts = ", ".join(filter(None, [(c.get("label") or c.get("partlabel") or c.get("fstype") or "")
                                            for c in _children(d)])) or L("pusty", "empty")
            txt = (f"{d['path']}   {(d.get('model') or '').strip()}   {human(int(d['size']))}   "
                   f"{(d.get('tran') or '').upper()}\n   " + L("zawiera: ", "contains: ") + parts)
            it = QListWidgetItem(txt)
            it.setData(Qt.ItemDataRole.UserRole, d["path"])
            self.list.addItem(it)
            if d["path"] == preselect:
                self.list.setCurrentItem(it)
        if self.list.count() == 0:
            self.list.addItem(L("Nie znaleziono dysku zewnętrznego (USB). Podłącz dysk i otwórz to okno ponownie.",
                                "No external (USB) disk found. Plug in a disk and open this window again."))
        lay.addWidget(self.list)
        self.confirm = QLineEdit(placeholderText=L("Wpisz WYMAZ, aby potwierdzić", "Type WYMAZ to confirm"))
        self.confirm.setToolTip(L("Zabezpieczenie przed przypadkowym wymazaniem dysku.",
                                  "Protection against erasing a disk by accident. (WYMAZ = 'erase' in Polish.)"))
        lay.addWidget(self.confirm)
        self.recovery = QCheckBox(L("Zainstaluj system ratunkowy (zalecane, ok. 5 min, pobiera ~1 GB)",
                                    "Install the rescue system (recommended, ~5 min, downloads ~1 GB)"))
        self.recovery.setToolTip(L("Pozwala uruchomić komputer z tego dysku i odtworzyć cały system, "
                                   "gdy Ubuntu nie startuje albo masz nowy dysk.",
                                   "Lets you boot the computer from this disk and restore the whole system "
                                   "when Ubuntu does not start or you have a new disk."))
        self.recovery.setChecked(True)
        lay.addWidget(self.recovery)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(L("Dalej", "Next"))
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.disk = None

    def _ok(self):
        it = self.list.currentItem()
        if not it or not it.data(Qt.ItemDataRole.UserRole):
            QMessageBox.warning(self, APP_NAME, L("Wybierz dysk.", "Choose a disk."))
            return
        if self.confirm.text().strip() != "WYMAZ":
            QMessageBox.warning(self, APP_NAME, L("Wpisz WYMAZ, aby potwierdzić usunięcie danych z dysku.",
                                                     "Type WYMAZ to confirm erasing the disk."))
            return
        self.disk = it.data(Qt.ItemDataRole.UserRole)
        self.accept()


# --------------------------------------------------------------------------- ustawienia
class SettingsDialog(QDialog):
    def __init__(self, app: "TimeVaultApp"):
        super().__init__(app.window)
        self.app = app
        self._loading = True
        self.setWindowTitle(L("Ustawienia", "Settings"))
        self.resize(560, 520)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._backups_tab(), L("Kopie", "Backups"))
        tabs.addTab(self._cloud_tab(), L("Chmura", "Cloud"))
        tabs.addTab(self._lang_tab(), L("Język", "Language"))
        lay.addWidget(tabs)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.button(QDialogButtonBox.StandardButton.Close).setText(L("Zamknij", "Close"))
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.refresh()
        self._loading = False

    # -- zakładka Kopie
    def _backups_tab(self):
        w = QWidget()
        l = QVBoxLayout(w)
        row = QHBoxLayout()
        lbl = QLabel(L("Automatyczne kopie na dysk USB:", "Automatic backups to the USB disk:"))
        self.interval = QComboBox()
        for key, text in intervals():
            self.interval.addItem(text, key)
        tip = L("Jak często tworzyć kopię, gdy dysk TimeVault jest podłączony. Kopia zawiera tylko zmiany "
                "od poprzedniej, więc zwykle trwa kilka minut.",
                "How often to back up while the TimeVault disk is connected. Each backup stores only the "
                "changes since the previous one, so it usually takes a few minutes.")
        lbl.setToolTip(tip)
        self.interval.setToolTip(tip)
        row.addWidget(lbl)
        row.addWidget(self.interval, 1)
        l.addLayout(row)
        self.on_connect = QCheckBox(L("Utwórz kopię zaraz po podłączeniu dysku", "Back up as soon as the disk is connected"))
        self.on_connect.setToolTip(L("Kopia rusza sama kilka sekund po podłączeniu dysku TimeVault (najwyżej raz "
                                     "na 15 minut). Po zakończeniu przyjdzie powiadomienie, że dysk można odłączyć.",
                                     "A backup starts by itself a few seconds after the TimeVault disk is plugged in "
                                     "(at most once per 15 minutes). You get a notification when it can be unplugged."))
        l.addWidget(self.on_connect)
        l.addStretch(1)
        self.interval.currentIndexChanged.connect(self._save_backups)
        self.on_connect.toggled.connect(self._save_backups)
        return w

    def _save_backups(self, *_):
        if self._loading:
            return
        key = self.interval.currentData()
        ch = {"on_connect": self.on_connect.isChecked()}
        ch.update({"auto": False} if key == "off" else {"auto": True, "interval": key})
        self.app.save_settings(ch)

    # -- zakładka Chmura
    def _cloud_tab(self):
        w = QWidget()
        l = QVBoxLayout(w)
        self.cloud_info = QLabel()
        self.cloud_info.setWordWrap(True)
        self.cloud_info.setTextFormat(Qt.TextFormat.RichText)
        l.addWidget(self.cloud_info)
        # niepołączona
        self.cloud_off = QWidget()
        ol = QVBoxLayout(self.cloud_off)
        ol.setContentsMargins(0, 0, 0, 0)
        self.sep_pw = QCheckBox(L("Osobne hasło do kopii w chmurze", "Separate password for cloud backups"))
        self.sep_pw.setToolTip(L("Domyślnie kopie w chmurze są szyfrowane tym samym hasłem co na dysku USB — "
                                 "łatwiej zapamiętać jedno.",
                                 "By default cloud backups are encrypted with the same password as the USB disk — "
                                 "one password is easier to remember."))
        ol.addWidget(self.sep_pw)
        self.btn_connect = QPushButton(L("Połącz z Google Drive…", "Connect to Google Drive…"))
        self.btn_connect.setToolTip(L("Otworzy się przeglądarka: zaloguj się do Google i zezwól TimeVault na dostęp "
                                      "do plików, które sam utworzy (bez wglądu w resztę Twojego Drive).",
                                      "A browser opens: sign in to Google and allow TimeVault to access the files "
                                      "it creates itself (no access to the rest of your Drive)."))
        self.btn_connect.clicked.connect(self.connect_cloud)
        ol.addWidget(self.btn_connect, 0, Qt.AlignmentFlag.AlignLeft)
        l.addWidget(self.cloud_off)
        # połączona
        self.cloud_on = QWidget()
        nl = QVBoxLayout(self.cloud_on)
        nl.setContentsMargins(0, 0, 0, 0)
        self.cloud_quota = QProgressBar()
        self.cloud_quota.setFormat(L("Google Drive: %p% zajęte", "Google Drive: %p% used"))
        nl.addWidget(self.cloud_quota)
        row = QHBoxLayout()
        lbl = QLabel(L("Wysyłaj kopię:", "Upload a backup:"))
        self.cloud_interval = QComboBox()
        for key, text in cloud_intervals():
            self.cloud_interval.addItem(text, key)
        tip = L("Jak często wysyłać kopię na Google Drive. Wysyłane są tylko zmiany. "
                "„Tylko ręcznie” — nic nie idzie samo, wysyłasz przyciskiem.",
                "How often to upload a backup to Google Drive. Only changes are uploaded. "
                "“Manual only” — nothing is sent by itself, you upload with the button.")
        lbl.setToolTip(tip)
        self.cloud_interval.setToolTip(tip)
        row.addWidget(lbl)
        row.addWidget(self.cloud_interval, 1)
        nl.addLayout(row)
        self.cloud_ac = QCheckBox(L("Tylko przy zasilaniu z sieci (laptop)", "Only on mains power (laptop)"))
        self.cloud_ac.setToolTip(L("Na baterii automatyczna wysyłka czeka na podłączenie ładowarki.",
                                   "On battery, automatic uploads wait until the charger is connected."))
        nl.addWidget(self.cloud_ac)
        brow = QHBoxLayout()
        self.btn_cloud_now = QPushButton(L("Wyślij kopię teraz", "Upload a backup now"))
        self.btn_cloud_now.setToolTip(L("Wysyła kopię na Google Drive od razu (także na baterii).",
                                        "Uploads a backup to Google Drive right away (also on battery)."))
        self.btn_cloud_now.clicked.connect(lambda: systemctl("start", "timevault-cloud-now.service"))
        self.btn_cloud_off = QPushButton(L("Odłącz Google Drive…", "Disconnect Google Drive…"))
        self.btn_cloud_off.setToolTip(L("Wyłącza wysyłanie na tym komputerze. Kopie na Drive zostają.",
                                        "Stops uploads on this computer. Backups on Drive are kept."))
        self.btn_cloud_off.clicked.connect(self.disconnect_cloud)
        brow.addWidget(self.btn_cloud_now)
        brow.addWidget(self.btn_cloud_off)
        brow.addStretch(1)
        nl.addLayout(brow)
        l.addWidget(self.cloud_on)
        l.addStretch(1)
        self.cloud_interval.currentIndexChanged.connect(self._save_cloud)
        self.cloud_ac.toggled.connect(self._save_cloud)
        return w

    def _save_cloud(self, *_):
        if self._loading:
            return
        self.app.save_settings({"cloud_interval": self.cloud_interval.currentData(),
                                "cloud_ac_only": self.cloud_ac.isChecked()})

    def _estimate_html(self, st) -> str:
        used = (st.get("disk") or {}).get("used") or 0
        est = st.get("cloud_estimate") or int(used)
        if not est:
            return ""
        h20 = est * 8 / 20e6 / 3600
        h50 = est * 8 / 50e6 / 3600
        return L(f"Pierwsza wysyłka: ok. <b>{human(est)}</b> (tyle co kopie na dysku USB) — przy wysyłaniu "
                 f"20 Mb/s ok. <b>{h20:.0f} h</b>, przy 50 Mb/s ok. <b>{h50:.0f} h</b>. Kolejne wysyłki to tylko "
                 "zmiany (zwykle kilkadziesiąt–kilkaset MB).",
                 f"First upload: about <b>{human(est)}</b> (as much as the backups on the USB disk) — at 20 Mbit/s "
                 f"about <b>{h20:.0f} h</b>, at 50 Mbit/s about <b>{h50:.0f} h</b>. Later uploads contain only "
                 "changes (usually tens to hundreds of MB).")

    def refresh(self):
        self._loading = True
        cfg, st = read_config(), read_status()
        self.interval.setCurrentIndex(max(0, self.interval.findData(cfg["interval"] if cfg["auto"] else "off")))
        self.on_connect.setChecked(cfg["on_connect"])
        connected = cfg.get("cloud") and st.get("cloud_state") not in (None, "not-configured")
        self.cloud_off.setVisible(not connected)
        self.cloud_on.setVisible(bool(connected))
        if not connected:
            self.cloud_info.setText(L(
                "<b>Druga kopia poza domem.</b> Chroni, gdy zginie i laptop, i dysk USB (kradzież, pożar). "
                "Kopie są <b>kompresowane i szyfrowane na Twoim komputerze</b> — Google widzi tylko "
                "zaszyfrowane dane. Z chmury odtworzysz cały system w systemie ratunkowym "
                "(źródło: Google Drive).<br><br>",
                "<b>A second, off-site copy.</b> Protects you when both the laptop and the USB disk are gone "
                "(theft, fire). Backups are <b>compressed and encrypted on your computer</b> — Google only sees "
                "encrypted data. You can restore the whole system from the cloud in the rescue system "
                "(source: Google Drive).<br><br>")
                + self._estimate_html(st) + "<br><br>" + L(
                "<span style='color:#b45309'><b>Uwaga na miejsce:</b> darmowy Google Drive ma 15 GB — kopia "
                "całego systemu zwykle potrzebuje planu 100 GB lub większego. Przed włączeniem sprawdzę "
                "wolne miejsce.</span>",
                "<span style='color:#b45309'><b>Mind the space:</b> free Google Drive has 15 GB — a full-system "
                "backup usually needs a 100 GB plan or larger. Free space is checked before enabling.</span>"))
        else:
            q = st.get("cloud_quota") or {}
            last = parse_time(st.get("cloud_last_success"))
            err = st.get("cloud_error") if st.get("cloud_state") == "error" else ""
            self.cloud_info.setText(
                L("<b>Google Drive połączony.</b><br>", "<b>Google Drive connected.</b><br>")
                + L("Ostatnia kopia w chmurze: ", "Last cloud backup: ") + f"<b>{ago(last)}</b>"
                + (f" (+{human(st.get('cloud_last_added', 0))})" if last else "")
                + L(f"<br>Kopii w chmurze: {len(st.get('cloud_snapshots') or [])}",
                    f"<br>Backups in the cloud: {len(st.get('cloud_snapshots') or [])}")
                + (f"<br><span style='color:#dc2626'>{err}</span>" if err else ""))
            self.cloud_quota.setVisible(bool(q.get("total")))
            if q.get("total"):
                self.cloud_quota.setValue(int(100 * (q.get("used") or 0) / q["total"]))
                self.cloud_quota.setToolTip(L(f"Wolne: {human(q.get('free') or 0)} z {human(q['total'])}",
                                              f"Free: {human(q.get('free') or 0)} of {human(q['total'])}"))
            self.cloud_interval.setCurrentIndex(max(0, self.cloud_interval.findData(cfg["cloud_interval"])))
            self.cloud_ac.setChecked(cfg["cloud_ac_only"])
            self.btn_cloud_now.setEnabled(st.get("cloud_state") != "running")
        self._loading = False

    def connect_cloud(self):
        cloud_pw = None
        if self.sep_pw.isChecked():
            pw = PasswordDialog(self, L("Hasło do chmury", "Cloud password"),
                                L("Ustal osobne hasło do kopii w chmurze (min. 8 znaków). Zapamiętaj je — "
                                  "system ratunkowy zapyta o nie przy odtwarzaniu z Google Drive.",
                                  "Set a separate password for cloud backups (min. 8 characters). Remember it — "
                                  "the rescue system asks for it when restoring from Google Drive."), confirm=True)
            if pw.exec() != QDialog.DialogCode.Accepted:
                return
            cloud_pw = pw.pw.text()
        from . import cloud
        auth = AuthDialog(self, cloud.authorize_command())
        if auth.exec() != QDialog.DialogCode.Accepted or not auth.output:
            return
        stdin = (f"CLOUDPW:{cloud_pw}\n" if cloud_pw else "") + auth.output
        dlg = ProcessDialog(self, L("Łączenie z Google Drive", "Connecting to Google Drive"),
                            ["cloud-setup", "--token-stdin"], stdin)
        dlg.exec()
        info = None
        for line in dlg.log.toPlainText().splitlines():
            if line.startswith("CLOUDINFO "):
                info = json.loads(line[len("CLOUDINFO "):])
        if dlg.ok and info:
            q, est = info.get("quota") or {}, info.get("estimate") or 0
            text = L(f"Google Drive połączony. Wolne miejsce: {human(q.get('free') or 0)}, "
                     f"pierwsza kopia: ok. {human(est)}.",
                     f"Google Drive connected. Free space: {human(q.get('free') or 0)}, "
                     f"first backup: about {human(est)}.")
            if not info.get("fits"):
                QMessageBox.warning(self, APP_NAME, text + "\n\n" + L(
                    "Kopia prawdopodobnie się NIE zmieści. Powiększ plan Google Drive albo odłącz chmurę.",
                    "The backup will probably NOT fit. Upgrade your Google Drive plan or disconnect the cloud."))
            else:
                QMessageBox.information(self, APP_NAME, text + "\n\n" + L(
                    "Nic nie zostało jeszcze wysłane. Wyślij pierwszą kopię przyciskiem „Wyślij kopię teraz” "
                    "albo wybierz automatyczne wysyłanie (np. raz dziennie).",
                    "Nothing has been uploaded yet. Send the first backup with “Upload a backup now” "
                    "or choose automatic uploads (e.g. once a day)."))
        self.app.refresh()
        self.refresh()

    def disconnect_cloud(self):
        if QMessageBox.question(self, APP_NAME, L(
                "Odłączyć Google Drive na tym komputerze? Kopie już wysłane zostaną na Drive i nadal będzie "
                "można z nich odtworzyć system.",
                "Disconnect Google Drive on this computer? Backups already uploaded stay on Drive and can still "
                "be used to restore the system.")) == QMessageBox.StandardButton.Yes:
            self.app.run_root(L("Odłączanie Google Drive", "Disconnecting Google Drive"), ["cloud-disconnect"])
            self.refresh()

    # -- zakładka Język
    def _lang_tab(self):
        w = QWidget()
        l = QVBoxLayout(w)
        grp = QButtonGroup(w)
        cur = read_config()["language"]
        for code, text in (("auto", L("Jak w systemie", "System default")), ("pl", "Polski"), ("en", "English")):
            rb = QRadioButton(text)
            rb.setChecked(cur == code)
            rb.toggled.connect(lambda on, c=code: on and not self._loading and self.app.set_language(c))
            grp.addButton(rb)
            l.addWidget(rb)
        note = QLabel(L("Zmiana języka uruchamia aplikację ponownie.", "Changing the language restarts the app."))
        note.setStyleSheet("color:gray")
        l.addWidget(note)
        l.addStretch(1)
        return w


class AuthDialog(QDialog):
    """`rclone authorize drive` w sesji użytkownika: otwiera przeglądarkę, czeka na zgodę."""

    def __init__(self, parent, cmd):
        super().__init__(parent)
        self.setWindowTitle(L("Logowanie do Google", "Google sign-in"))
        lay = QVBoxLayout(self)
        lbl = QLabel(L("W przeglądarce otworzyła się strona Google. Zaloguj się i kliknij „Zezwól”. "
                       "To okno zamknie się samo.",
                       "A Google page opened in your browser. Sign in and click “Allow”. "
                       "This window closes by itself."))
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        bar = QProgressBar()
        bar.setRange(0, 0)
        lay.addWidget(bar)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        bb.rejected.connect(self._cancel)
        lay.addWidget(bb)
        self.output = ""
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(
            lambda: setattr(self, "output", self.output + bytes(self.proc.readAllStandardOutput()).decode(errors="replace")))
        self.proc.finished.connect(self._done)
        self.proc.start(cmd[0], cmd[1:])

    def _done(self, code, _st):
        from . import cloud
        try:
            if code == 0:
                cloud.parse_authorize_output(self.output)   # token w JSON albo base64 (nowe rclone)
                self.accept()
                return
        except ValueError:
            pass
        # Szczegóły tylko z linii z komunikatem — bez tokenu (to dane dostępowe).
        details = "\n".join(l for l in self.output.splitlines()
                            if "NOTICE" in l or "ERROR" in l or "Fatal" in l)[-500:]
        QMessageBox.warning(self, APP_NAME, L("Logowanie nie powiodło się.", "Sign-in failed.")
                            + ("\n\n" + details if details else ""))
        self.reject()

    def _cancel(self):
        self.proc.kill()
        self.reject()


# --------------------------------------------------------------------------- okno główne
class MainWindow(QWidget):
    def __init__(self, app: "TimeVaultApp"):
        super().__init__()
        self.app = app
        self._loading = False
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(580, 680)
        lay = QVBoxLayout(self)

        head = QHBoxLayout()
        self.logo = QLabel()
        self.logo.setPixmap(app_icon().pixmap(56, 56))
        head.addWidget(self.logo)
        titles = QVBoxLayout()
        titles.addWidget(QLabel(f"<span style='font-size:20pt;font-weight:600'>{APP_NAME}</span>"))
        self.subtitle = QLabel("")
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        lay.addLayout(head)

        # Pierwsze uruchomienie: jedna jasna akcja zależna od podłączonego dysku.
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        self.banner.setStyleSheet("#banner{border:1px solid #2563eb;border-radius:8px;"
                                  "background:rgba(37,99,235,0.08)}")
        bl = QVBoxLayout(self.banner)
        self.banner_text = QLabel()
        self.banner_text.setWordWrap(True)
        self.banner_btn = QPushButton()
        self.banner_btn.setDefault(True)
        self.banner_btn.clicked.connect(self._banner_action)
        bl.addWidget(self.banner_text)
        bl.addWidget(self.banner_btn, 0, Qt.AlignmentFlag.AlignRight)
        lay.addWidget(self.banner)
        self.banner_kind = None
        self.banner_disk = None

        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        cl = QVBoxLayout(card)
        self.last = QLabel()
        self.last.setToolTip(L("Kiedy powstała ostatnia udana kopia i ile nowych danych dodała.",
                               "When the last successful backup was made and how much new data it added."))
        self.diskinfo = QLabel()
        self.space = QProgressBar()
        self.space.setFormat(L("%p% zajęte", "%p% used"))
        self.err = QLabel()
        self.err.setWordWrap(True)
        self.err.setStyleSheet("color:#dc2626")
        self.compat = QLabel()
        self.compat.setWordWrap(True)
        self.compat.setStyleSheet("color:#b45309")
        self.compat.setToolTip(L("Zgodność TimeVault z Twoim Ubuntu — sprawdzana po każdej kopii. "
                                 "Ostrzeżenie pojawia się, zanim coś przestanie działać (np. po aktualizacji "
                                 "Ubuntu do nowej wersji).",
                                 "TimeVault compatibility with your Ubuntu — checked after every backup. "
                                 "A warning appears before something stops working (e.g. after upgrading "
                                 "Ubuntu to a new release)."))
        for w in (self.last, self.diskinfo, self.space, self.err, self.compat):
            cl.addWidget(w)

        self.sched = QLabel()
        self.sched.setToolTip(L("Kiedy powstają kopie. Zmiana: Ustawienia → Kopie.",
                                "When backups are made. Change it in Settings → Backups."))
        self.cloud_line = QLabel()
        self.cloud_line.setToolTip(L("Druga kopia na Google Drive (poza domem). Ustawienia → Chmura.",
                                     "Second, off-site backup on Google Drive. Settings → Cloud."))
        cl.addWidget(self.sched)
        cl.addWidget(self.cloud_line)
        lay.addWidget(card)

        row = QHBoxLayout()
        self.btn_backup = QPushButton(L("Utwórz kopię teraz", "Back up now"))
        self.btn_backup.setToolTip(L("Tworzy kopię od razu, niezależnie od harmonogramu. "
                                     "Zapisywane są tylko zmiany od poprzedniej kopii.",
                                     "Makes a backup right away, regardless of the schedule. "
                                     "Only changes since the previous backup are stored."))
        self.btn_backup.clicked.connect(app.backup_now)
        self.btn_browse = QPushButton(L("Przeglądaj kopie", "Browse backups"))
        self.btn_browse.clicked.connect(app.toggle_browse)
        self.btn_verify = QPushButton(L("Sprawdź kopie", "Verify backups"))
        self.btn_verify.setToolTip(L("Sprawdza, czy kopie na dysku są kompletne i nieuszkodzone "
                                     "(odczytuje losowe 5% danych). Nic nie zmienia.",
                                     "Checks that the backups on the disk are complete and undamaged "
                                     "(reads a random 5% of the data). Changes nothing."))
        self.btn_verify.clicked.connect(app.verify)
        for b in (self.btn_backup, self.btn_browse, self.btn_verify):
            row.addWidget(b)
        lay.addLayout(row)

        hl = QLabel("<b>" + L("Historia kopii", "Backup history") + "</b>")
        lay.addWidget(hl)
        self.history = QListWidget()
        self.history.itemClicked.connect(app.open_snapshot)
        self.history.setToolTip(L("Kliknij kopię, aby otworzyć ją w Plikach (tylko do odczytu) i przejrzeć "
                                  "foldery i pliki z tamtej chwili.\n\n"
                                  "Wszystkie kopie na dysku, najnowsza na górze. Starsze są automatycznie "
                                  "przerzedzane: z ostatniej doby zostają wszystkie, potem jedna dziennie "
                                  "(31 dni), dalej jedna tygodniowo.",
                                  "Click a backup to open it in Files (read-only) and browse the folders and "
                                  "files from that moment.\n\n"
                                  "All backups on the disk, newest first. Older ones are thinned out "
                                  "automatically: all from the last 24 hours, then one per day (31 days), "
                                  "then one per week."))
        lay.addWidget(self.history, 1)

        bottom = QHBoxLayout()
        self.version = QLabel(f"{APP_NAME} {__version__}")
        self.version.setStyleSheet("color:gray")
        self.version.setToolTip(L("Wersja aplikacji. Szczegóły: Więcej → O programie.",
                                  "App version. Details: More → About."))
        bottom.addWidget(self.version)
        bottom.addStretch(1)
        self.btn_settings = QPushButton(L("Ustawienia…", "Settings…"))
        self.btn_settings.setToolTip(L("Częstotliwość kopii, kopia po podłączeniu dysku, Google Drive i język.",
                                       "Backup frequency, backup on disk connection, Google Drive and language."))
        self.btn_settings.clicked.connect(app.open_settings)
        bottom.addWidget(self.btn_settings)
        self.more = QToolButton()
        self.more.setText(L("Więcej", "More"))
        self.more.setToolTip(L("Rzadziej używane opcje i język.", "Less frequently used options and language."))
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more = QMenu(self.more)
        more.setToolTipsVisible(True)
        self.act_recovery = more.addAction(L("Odśwież system ratunkowy na dysku…", "Refresh the rescue system on the disk…"))
        self.act_recovery.setToolTip(L("Buduje od nowa system ratunkowy (nowsze jądro i sterowniki). "
                                       "Warto po dużej aktualizacji Ubuntu.",
                                       "Rebuilds the rescue system (newer kernel and drivers). "
                                       "Worth doing after a major Ubuntu upgrade."))
        self.act_recovery.triggered.connect(app.update_recovery)
        self.act_doctor = more.addAction(L("Sprawdź zgodność z Ubuntu", "Check compatibility with Ubuntu"))
        self.act_doctor.setToolTip(L("Sprawdza system plików, bootloader, programy i pakiety potrzebne "
                                     "systemowi ratunkowemu w tej wersji Ubuntu.",
                                     "Checks the filesystem, boot loader, programs and the packages the "
                                     "rescue system needs in this Ubuntu release."))
        self.act_doctor.triggered.connect(app.check_compat)
        self.act_new = more.addAction(L("Przygotuj kolejny dysk zewnętrzny…", "Prepare another external disk…"))
        self.act_new.setToolTip(L("Wymazuje wybrany dysk USB i robi z niego dodatkowy dysk TimeVault.",
                                  "Erases a chosen USB disk and turns it into an additional TimeVault disk."))
        self.act_new.triggered.connect(lambda: app.setup_new_disk())
        more.addSeparator()
        act_about = more.addAction(L("O programie", "About"))
        act_about.setToolTip(L("Wersja, silnik kopii i informacje o programie.",
                               "Version, backup engine and information about the app."))
        act_about.triggered.connect(app.about)
        self.more.setMenu(more)
        bottom.addWidget(self.more)
        lay.addLayout(bottom)

    # -- baner pierwszego uruchomienia -------------------------------------------
    def _banner_action(self):
        if self.banner_kind == "existing":
            self.app.setup_existing()
        elif self.banner_kind == "new":
            self.app.setup_new_disk(self.banner_disk)

    def update_banner(self, configured: bool):
        if configured:
            self.banner.hide()
            return
        tv = timevault_disk()
        cands = candidate_disks()
        self.banner.show()
        self.banner_btn.show()
        if tv:
            self.banner_kind = "existing"
            self.banner_text.setText(L(
                f"<b>Wykryto dysk TimeVault: {disk_name(tv)}</b><br>Ten dysk jest już przygotowany "
                "(system ratunkowy i kopie). Podaj hasło do kopii, a aplikacja zacznie z niego "
                "korzystać i będzie robić kopie automatycznie.<br><b>Nic nie zostanie usunięte.</b>",
                f"<b>TimeVault disk detected: {disk_name(tv)}</b><br>This disk is already set up "
                "(rescue system and backups). Enter the backup password and the app will start using it "
                "and back up automatically.<br><b>Nothing will be erased.</b>"))
            self.banner_btn.setText(L("Użyj tego dysku…", "Use this disk…"))
            self.banner_btn.setToolTip(L("Pyta tylko o hasło do kopii. Dysk i kopie zostają bez zmian.",
                                         "Only asks for the backup password. The disk and backups stay as they are."))
        elif cands:
            d = cands[0]
            self.banner_kind, self.banner_disk = "new", d["path"]
            self.banner_text.setText(L(
                f"<b>Podłączony dysk: {disk_name(d)}</b><br>To nie jest jeszcze dysk TimeVault. "
                "Aplikacja może go przygotować: system ratunkowy (start komputera z tego dysku) "
                "i automatyczne kopie — wszystko na jednym dysku.<br>"
                "<span style='color:#dc2626'><b>Uwaga: wszystkie dane na tym dysku zostaną usunięte.</b></span>",
                f"<b>Connected disk: {disk_name(d)}</b><br>This is not a TimeVault disk yet. "
                "The app can set it up: a rescue system (boot the computer from this disk) and automatic "
                "backups — all on one disk.<br>"
                "<span style='color:#dc2626'><b>Warning: all data on this disk will be erased.</b></span>"))
            self.banner_btn.setText(L("Przygotuj ten dysk…", "Prepare this disk…"))
            self.banner_btn.setToolTip(L("Otwiera kreator: potwierdzenie, hasło do kopii, przygotowanie dysku "
                                         "i pierwsza kopia.",
                                         "Opens the wizard: confirmation, backup password, disk setup "
                                         "and the first backup."))
        else:
            self.banner_kind = None
            self.banner_text.setText(L("<b>Podłącz dysk zewnętrzny (USB)</b>, na który mają trafiać kopie.<br>"
                                       "Aplikacja sama go wykryje.",
                                       "<b>Plug in an external (USB) disk</b> for your backups.<br>"
                                       "The app will detect it automatically."))
            self.banner_btn.hide()

    # -- odświeżanie ------------------------------------------------------------
    def refresh(self, st: dict, connected: bool):
        state = st.get("state", "not-configured" if not st else "idle")
        configured = bool(st) and state != "not-configured"
        names = {"idle": L("Gotowe", "Ready"), "running": L("Trwa tworzenie kopii…", "Backing up…"),
                 "checking": L("Trwa sprawdzanie kopii…", "Verifying backups…"),
                 "error": L("Ostatnia operacja nie powiodła się", "The last operation failed"),
                 "disk-absent": L("Dysk odłączony", "Disk not connected"),
                 "not-configured": L("Nie skonfigurowano", "Not set up")}
        self.subtitle.setText(names.get(state, state))
        last = parse_time(st.get("last_success"))
        extra = f" (+{human(st['last_added'])})" if st.get("last_added") is not None and last else ""
        self.last.setText(L("Ostatnia kopia: ", "Last backup: ") + f"<b>{ago(last)}</b>{extra}")
        self.diskinfo.setText(L("Dysk TimeVault: ", "TimeVault disk: ")
                              + ("<b>" + L("podłączony", "connected") + "</b>" if connected
                                 else L("odłączony", "not connected")))
        dk = st.get("disk")
        self.space.setVisible(bool(dk))
        if dk:
            self.space.setValue(int(100 * dk["used"] / max(1, dk["total"])))
            self.space.setToolTip(L(f"Wolne: {human(dk['free'])} z {human(dk['total'])}. Gdy miejsca zabraknie, "
                                    "najstarsze kopie są usuwane automatycznie.",
                                    f"Free: {human(dk['free'])} of {human(dk['total'])}. When space runs out, "
                                    "the oldest backups are removed automatically."))
        self.err.setText((st.get("error") or "") if state == "error" else "")
        self.err.setVisible(state == "error")
        issues = compat_issues(st)
        lines = [("✗ " if lvl == "error" else "⚠ ") + txt for lvl, txt, _ in issues]
        skipped = st.get("last_skipped") or []
        if st.get("last_incomplete") and state != "error":
            lines.insert(0, "⚠ " + L(f"Ostatnia kopia zapisana, ale {st.get('warnings') or len(skipped)} plik(ów) nie dało "
                                     "się w pełni odczytać (najczęściej zmieniały się w trakcie kopii). Najedź, "
                                     "aby zobaczyć które.",
                                     f"The last backup was saved, but {st.get('warnings') or len(skipped)} file(s) could "
                                     "not be fully read (usually they changed during the backup). Hover to see which."))
        self.compat.setText("<br>".join(lines))
        self.compat.setVisible(bool(lines))
        if skipped and st.get("last_incomplete"):
            self.compat.setToolTip(L("Pliki odczytane niepełnie w ostatniej kopii:\n", "Files not fully read in the last backup:\n")
                                   + "\n".join(skipped[:15]))
        cfg = read_config()
        sched = dict(intervals()).get(cfg["interval"]) if cfg["auto"] else L("tylko ręcznie", "manual only")
        if cfg["auto"] and cfg["on_connect"]:
            sched += L(" + po podłączeniu dysku", " + when the disk is connected")
        elif not cfg["auto"] and cfg["on_connect"]:
            sched = L("po podłączeniu dysku", "when the disk is connected")
        self.sched.setText(L("Kopie: ", "Backups: ") + sched)
        self.cloud_line.setText(L("Kopia w chmurze: ", "Cloud backup: ") + cloud_summary(st, cfg))
        self.sched.setVisible(configured)
        self.cloud_line.setVisible(configured)
        busy = state in ("running", "checking")
        for b in (self.btn_backup, self.btn_verify):
            b.setEnabled(configured and connected and not busy)
        self.btn_browse.setEnabled(configured and connected)
        browsing = bool(st.get("browse"))
        self.btn_browse.setText(L("Zakończ przeglądanie", "Stop browsing") if browsing
                                else L("Przeglądaj kopie", "Browse backups"))
        self.btn_browse.setToolTip(
            L("Zamyka podgląd kopii, żeby dysk można było odłączyć.", "Closes the backup view so the disk can be unplugged.")
            if browsing else
            L("Otwiera kopie w Plikach (tylko do odczytu): każda kopia to folder z datą. Skopiuj z niego "
              "dowolny plik z dowolnego momentu.",
              "Opens the backups in Files (read-only): each backup is a folder named by date. Copy any "
              "file from any point in time."))
        self.act_recovery.setEnabled(configured and connected and not busy)
        self.more.setVisible(True)
        self.act_recovery.setVisible(configured)
        self.update_banner(configured)
        self.history.clear()
        for s in reversed(st.get("snapshots", [])):
            t = parse_time(s.get("time"))
            when = (f"{t:%d.%m.%Y  %H:%M}" if i18n.lang() == "pl" else f"{t:%Y-%m-%d  %H:%M}") if t else ""
            it = QListWidgetItem(f"{when}   ·   {s['id']}   📂")
            it.setData(Qt.ItemDataRole.UserRole, s["id"])
            self.history.addItem(it)
        if not st.get("snapshots"):
            self.history.addItem(L("Brak kopii", "No backups yet") if configured
                                 else L("Kopie pojawią się tutaj.", "Backups will appear here."))

    def apply_theme(self):
        self.setWindowIcon(app_icon())
        self.logo.setPixmap(app_icon().pixmap(56, 56))

    def closeEvent(self, e):  # zamknięcie okna = schowanie do traya
        e.ignore()
        self.hide()


# --------------------------------------------------------------------------- aplikacja
class TimeVaultApp:
    def __init__(self, qapp: QApplication, start_hidden: bool):
        self.qapp = qapp
        self.prev: dict = {}
        self.prev_connected = None
        self.window = MainWindow(self)
        self.tray = QSystemTrayIcon(tray_icon("idle"))
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        self.status_action = QAction(APP_NAME)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        menu.addSeparator()
        for text, fn in ((L("Utwórz kopię teraz", "Back up now"), self.backup_now),
                         (L("Przeglądaj kopie", "Browse backups"), self.toggle_browse),
                         (L("Otwórz ", "Open ") + APP_NAME, self.show_window)):
            a = QAction(text, menu)
            a.triggered.connect(fn)
            menu.addAction(a)
        menu.addSeparator()
        quit_a = QAction(L("Zakończ", "Quit"), menu)
        quit_a.triggered.connect(qapp.quit)
        menu.addAction(quit_a)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.show_window()
                                    if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()
        self.notifier = Notifier(self.show_window, self._tray_message)

        self.watcher = QFileSystemWatcher()
        if os.path.isdir(os.path.dirname(STATUS_FILE)):
            self.watcher.addPath(os.path.dirname(STATUS_FILE))
        self.watcher.directoryChanged.connect(lambda _p: self.refresh())
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(5000)
        try:
            QGuiApplication.styleHints().colorSchemeChanged.connect(lambda _s: self.theme_changed())
        except AttributeError:
            pass
        sync_launcher_icon()
        self.refresh()
        if not start_hidden:
            self.show_window()

    # -- stan ------------------------------------------------------------------
    def refresh(self):
        st = read_status()
        connected = timevault_disk_connected()
        state = st.get("state", "not-configured")
        if state in ("idle", "disk-absent") and st:
            state = "idle" if connected else "disk-absent"
        self.tray.setIcon(tray_icon(state))
        last = ago(parse_time(st.get("last_success")))
        self.status_action.setText(L("Ostatnia kopia: ", "Last backup: ") + last if st
                                   else APP_NAME + ": " + L("nie skonfigurowano", "not set up"))
        self.tray.setToolTip(f"{APP_NAME} {__version__} — " + L("ostatnia kopia: ", "last backup: ") + last)
        self.notify_transitions(st, connected)
        self.notify_compat(st)
        self.window.refresh(st, connected)
        self.prev, self.prev_connected = st, connected

    def notify_transitions(self, st, connected):
        p = self.prev
        if p.get("state") == "running" and st.get("state") == "idle":
            extra = ""
            if st.get("last_incomplete"):
                extra = L(f" {st.get('warnings', 0)} plik(ów) odczytano niepełnie — szczegóły w oknie.",
                          f" {st.get('warnings', 0)} file(s) were not fully read — details in the window.")
            self.notify(L("Kopia zapasowa gotowa", "Backup complete"),
                        L(f"Dodano {human(st.get('last_added', 0))}. Dysk można bezpiecznie odłączyć.",
                          f"Added {human(st.get('last_added', 0))}. The disk can be safely unplugged.") + extra)
        elif p.get("state") in ("running", "checking") and st.get("state") == "error":
            self.notify(L("Kopia nie powiodła się", "Backup failed"), (st.get("error") or "")[:200], error=True)
        if p.get("cloud_state") == "running" and st.get("cloud_state") == "idle":
            self.notify(L("Kopia w chmurze gotowa", "Cloud backup complete"),
                        L(f"Wysłano {human(st.get('cloud_last_added', 0))} na Google Drive.",
                          f"Uploaded {human(st.get('cloud_last_added', 0))} to Google Drive."))
        elif p.get("cloud_state") == "running" and st.get("cloud_state") == "error":
            self.notify(L("Kopia w chmurze nie powiodła się", "Cloud backup failed"),
                        (st.get("cloud_error") or "")[:200], error=True)
        if p.get("state") == "checking" and st.get("state") == "idle" and st.get("last_check_ok"):
            self.notify(L("Kopie sprawdzone", "Backups verified"), L("Wszystko w porządku.", "Everything is fine."))
        if self.prev_connected is False and connected and st:
            cfg = read_config()
            if cfg["on_connect"]:
                # Zapas dla reguły udev: usługa sama pilnuje blokady i odstępu 15 min.
                systemctl("start", "timevault-backup-connect.service")
                self.notify(L("Dysk TimeVault podłączony", "TimeVault disk connected"),
                            L("Kopia zaraz się rozpocznie.", "A backup will start shortly."))
            else:
                self.notify(L("Dysk TimeVault podłączony", "TimeVault disk connected"),
                            L("Kopia powstanie według harmonogramu.", "A backup will be made on schedule."))

    def open_settings(self):
        SettingsDialog(self).exec()
        self.refresh()

    def theme_changed(self):
        """Motyw jasny/ciemny: ikona okna, docku, menu aplikacji i traya."""
        self.qapp.setWindowIcon(app_icon())
        self.window.apply_theme()
        sync_launcher_icon()
        self.refresh()

    def about(self):
        try:
            restic = subprocess.run(["restic", "version"], capture_output=True, text=True,
                                    timeout=5).stdout.split(" compiled")[0].strip()
        except Exception:  # noqa: BLE001
            restic = "restic"
        from . import doctor
        osi = doctor.os_info().get("pretty", "")
        box = QMessageBox(self.window)
        box.setWindowTitle(L("O programie", "About"))
        box.setIconPixmap(app_icon().pixmap(72, 72))
        box.setText(f"<b style='font-size:15pt'>{APP_NAME} {__version__}</b><br>"
                    "linux-backup-timevault")
        box.setInformativeText(L(
            "Kopie całego systemu jak Time Machine: automatyczne, szyfrowane kopie na dysk zewnętrzny, "
            "system ratunkowy na tym samym dysku i odtworzenie 1:1 na ten sam albo nowy dysk.",
            "Time Machine–style full-system backups: automatic, encrypted backups to an external disk, "
            "a rescue system on the same disk and 1:1 restore onto the same or a new disk.")
            + f"<br><br>{L('Silnik kopii', 'Backup engine')}: {restic}<br>{L('System', 'System')}: {osi}"
            + f"<br>{L('Dokumentacja', 'Documentation')}: /usr/share/doc/timevault/"
            + "<br><br>Created by: <b>JGu</b><br>Supported by Claude")
        box.exec()

    def notify_compat(self, st):
        """Każde nowe ostrzeżenie zgodności — jedno powiadomienie (pamiętane między uruchomieniami)."""
        settings = QSettings("TimeVault", "gui")
        done = set(settings.value("notified_compat", [], type=list) or [])
        for level, text, key in compat_issues(st):
            if key not in done:
                self.notify(APP_NAME + ": " + L("wymagana uwaga", "attention needed"), text,
                            error=level == "error")
                done.add(key)
        settings.setValue("notified_compat", sorted(done))

    def check_compat(self):
        from . import doctor
        rep = doctor.run("/", with_recovery=False)
        rec = [it for it in read_status().get("compat") or [] if it.get("id") == "recovery"]
        txt = rep["os"].get("pretty", "") + "\n\n" + doctor.text(rep["items"] + rec)
        QMessageBox.information(self.window, L("Zgodność z Ubuntu", "Compatibility with Ubuntu"), txt)

    def notify(self, title, text, error=False):
        self.notifier.notify(title, text, error)

    def _tray_message(self, title, text, error=False):
        kind = QSystemTrayIcon.MessageIcon.Critical if error else QSystemTrayIcon.MessageIcon.Information
        self.tray.showMessage(title, text, kind, 8000)

    # -- ustawienia ----------------------------------------------------------------
    def save_settings(self, changes: dict):
        cfg = read_config()
        cfg.update(changes)
        try:
            # Plik jest zapisywalny dla grupy sudo (katalog nie) — zapis w miejscu.
            with open(CONFIG_FILE, "r+") as f:
                f.truncate(0)
                json.dump(cfg, f, indent=2)
            return True
        except OSError:
            pass
        # Brak prawa zapisu (np. użytkownik spoza grupy sudo) — przez pkexec.
        args = ["config", "--auto", "on" if cfg["auto"] else "off", "--interval", cfg["interval"],
                "--on-connect", "on" if cfg["on_connect"] else "off", "--language", cfg["language"]]
        ok = subprocess.run(["pkexec", *root_args(args)], capture_output=True).returncode == 0
        self.refresh()
        return ok

    def set_language(self, code: str):
        if read_config()["language"] != code and self.save_settings({"language": code}):
            QMessageBox.information(self.window, APP_NAME,
                                    "Język zostanie zmieniony po ponownym uruchomieniu aplikacji.\n"
                                    "The language will change after the app restarts.")
            # Nowa instancja startuje po zamknięciu tej (jedna instancja naraz).
            QProcess.startDetached("/bin/sh", ["-c", 'sleep 1; exec "$0" -m timevault.gui', sys.executable])
            self.qapp.quit()

    # -- akcje -----------------------------------------------------------------
    def show_window(self):
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def backup_now(self):
        if not systemctl("start", "timevault-backup-now.service"):
            self.notify(APP_NAME, L("Nie udało się uruchomić kopii.", "Could not start the backup."), error=True)

    def verify(self):
        systemctl("start", "timevault-verify.service")

    def toggle_browse(self):
        if read_status().get("browse"):
            systemctl("stop", "timevault-browse.service")
            return
        systemctl("start", "timevault-browse.service")
        self._browse_tries = 0
        QTimer.singleShot(1000, self._open_browse)

    def _open_browse(self, snapshot: str | None = None):
        st = read_status()
        if st.get("browse_error") and not st.get("browse_starting") and self._browse_tries > 1:
            self.notify(APP_NAME, L("Nie udało się otworzyć kopii: ", "Could not open the backup: ")
                        + st["browse_error"][-160:], error=True)
            return
        if snapshot:
            base = os.path.join(BROWSE_DIR, "ids", snapshot)
            home = os.path.join(base, os.path.expanduser("~").lstrip("/"))
            target = home if os.path.isdir(home) else base
            ready = os.path.isdir(base)
        else:
            target = os.path.join(BROWSE_DIR, "snapshots")
            ready = os.path.isdir(target)
        if ready:
            subprocess.Popen(["xdg-open", target])
            return
        self._browse_tries += 1
        if self._browse_tries < 180:            # pierwsze wczytanie dużego repo z USB bywa wolne
            QTimer.singleShot(1000, lambda: self._open_browse(snapshot))
        else:
            self.notify(APP_NAME, L("Nie udało się otworzyć kopii do przeglądania.",
                                    "Could not open the backups for browsing."), error=True)

    def open_snapshot(self, item):
        """Kliknięcie kopii w historii: Pliki z katalogiem domowym z tamtej chwili (tylko odczyt)."""
        sid = item.data(Qt.ItemDataRole.UserRole)
        if not sid:
            return
        if not timevault_disk_connected():
            QMessageBox.information(self.window, APP_NAME, L("Podłącz dysk TimeVault, aby przejrzeć kopię.",
                                                             "Connect the TimeVault disk to browse the backup."))
            return
        if not read_status().get("browse"):
            systemctl("start", "timevault-browse.service")
            self.notify(APP_NAME, L("Otwieram kopię… (pierwsze otwarcie trwa kilka sekund)",
                                    "Opening the backup… (the first time takes a few seconds)"))
        self._browse_tries = 0
        QTimer.singleShot(500, lambda: self._open_browse(sid))

    def run_root(self, title, args, stdin=None) -> bool:
        dlg = ProcessDialog(self.window, title, args, stdin)
        dlg.exec()
        self.refresh()
        return dlg.ok

    def setup_new_disk(self, preselect: str | None = None):
        dlg = NewDiskDialog(self.window, preselect)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        pw = PasswordDialog(self.window, L("Hasło do kopii", "Backup password"),
                            L("Ustal hasło szyfrujące kopie (min. 8 znaków). <b>Zapamiętaj je</b> — "
                              "bez niego kopii nie da się odtworzyć, także z systemu ratunkowego.",
                              "Set the password that encrypts your backups (min. 8 characters). "
                              "<b>Remember it</b> — without it the backups cannot be restored, "
                              "not even from the rescue system."), confirm=True)
        if pw.exec() != QDialog.DialogCode.Accepted:
            return
        args = ["--confirm", "WYMAZ", "setup-disk", dlg.disk, "--password-stdin"]
        if not dlg.recovery.isChecked():
            args.append("--no-recovery")
        self.run_root(L("Przygotowanie dysku TimeVault", "Setting up the TimeVault disk"), args, stdin=pw.pw.text())

    def setup_existing(self):
        pw = PasswordDialog(self.window, L("Użyj dysku TimeVault", "Use the TimeVault disk"),
                            L("Podaj hasło do kopii — to samo, które ustawiłeś przy pierwszej kopii "
                              "na tym dysku. Nic nie zostanie usunięte.",
                              "Enter the backup password — the one you set for the first backup on this "
                              "disk. Nothing will be erased."))
        if pw.exec() == QDialog.DialogCode.Accepted:
            self.run_root(L("Użycie dysku TimeVault", "Using the TimeVault disk"),
                          ["setup-existing", "--password-stdin"], stdin=pw.pw.text())

    def update_recovery(self):
        text = L("System ratunkowy to małe Ubuntu na dysku TimeVault. Uruchamiasz z niego komputer, gdy "
                 "Twoje Ubuntu nie wstaje albo masz nowy dysk, i odtwarzasz system z kopii.\n\n"
                 "Odświeżenie buduje go od nowa (nowsze jądro i sterowniki) — warto po dużej aktualizacji "
                 "Ubuntu albo przed odtwarzaniem na nowym komputerze. Poprzednia wersja zostaje jako zapas.\n\n"
                 "Trwa ok. 5 minut i pobiera ok. 1 GB. Kopie na dysku nie są zmieniane.\n\nOdświeżyć teraz?",
                 "The rescue system is a small Ubuntu on the TimeVault disk. You boot the computer from it "
                 "when your Ubuntu does not start or you have a new disk, and restore the system from a backup.\n\n"
                 "Refreshing rebuilds it (newer kernel and drivers) — worth doing after a major Ubuntu upgrade "
                 "or before restoring onto a new computer. The previous version is kept as a fallback.\n\n"
                 "Takes about 5 minutes and downloads about 1 GB. Backups on the disk are not changed.\n\n"
                 "Refresh now?")
        if QMessageBox.question(self.window, L("System ratunkowy", "Rescue system"), text) \
                == QMessageBox.StandardButton.Yes:
            self.run_root(L("Odświeżanie systemu ratunkowego", "Refreshing the rescue system"), ["install-recovery"])


def main():
    i18n.set_lang(read_config().get("language"))
    qapp = QApplication(sys.argv)
    qapp.setApplicationName(APP_NAME)
    qapp.setDesktopFileName("timevault")
    qapp.setWindowIcon(app_icon())
    qapp.setQuitOnLastWindowClosed(False)
    start_hidden = "--tray" in sys.argv
    sock = QLocalSocket()
    sock.connectToServer(APP_ID)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(APP_ID)
    server = QLocalServer()
    server.listen(APP_ID)
    app = TimeVaultApp(qapp, start_hidden)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), app.show_window()))
    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())
