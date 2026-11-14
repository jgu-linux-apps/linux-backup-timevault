"""Motyw „Aurora” dla Qt (wspólny wygląd serii JGu Linux Apps): jasny = lawendowe szkło, ciemny = granat
z zorzą, gradient fiolet → cyjan. Jasny/ciemny według systemu. „system” = wygląd Qt bez zmian.

Wybór motywu to ustawienie użytkownika (QSettings), nie /var/lib/timevault/config.json, który należy
do usługi systemowej. UWAGA: 8-cyfrowe kolory w Qt to #AARRGGBB."""
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QColor, QPalette

THEMES = ("system", "aurora")

PAL = {
    False: dict(bg="#F7F4FF", panel="#FFFFFF", text="#1F1A3A", sub="#6B6590", border="#E2DCFA", hover="#EFEBFF",
                field="#FFFFFF", accent="#7C5CFF", accent2="#22B8E6", trough="#ECE8FA"),
    True: dict(bg="#0C0E1A", panel="#14172A", text="#ECEBFF", sub="#A3A1C8", border="#2A2F4D", hover="#1E2340",
               field="#11142A", accent="#A855F7", accent2="#22D3EE", trough="#1C2038"),
}

QSS = """
QWidget#tvMain {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {bg}, stop:0.6 {bg2}, stop:1 {bg3}); }}
QDialog, QMessageBox {{ background: {bg}; }}
QFrame#tvCard {{ background: {panel}; border: 1px solid {border}; border-radius: 14px; }}
QFrame#banner {{ background: {hover}; border: 1px solid {accent}; border-radius: 12px; }}
QPushButton {{ background: {panel}; color: {text}; border: 1px solid {border}; border-radius: 9px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: {accent}; background: {hover}; }}
QPushButton:pressed {{ background: {border}; }}
QPushButton:disabled {{ color: {sub}; background: {bg}; }}
QPushButton#tvPrimary, QFrame#banner QPushButton {{ color: white; border: 1px solid {accent};
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {accent}, stop:1 {accent2}); font-weight: 600; }}
QPushButton#tvPrimary:hover, QFrame#banner QPushButton:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {accent2}, stop:1 {accent}); }}
QPushButton#tvPrimary:disabled {{ background: {border}; color: {sub}; border-color: {border}; }}
QToolButton {{ background: transparent; color: {text}; border: 1px solid {border}; border-radius: 9px; padding: 5px 12px; }}
QToolButton:hover {{ background: {hover}; border-color: {accent}; }}
QToolButton::menu-indicator {{ image: none; }}
QProgressBar {{ background: {trough}; border: none; border-radius: 6px; text-align: center; color: {text}; min-height: 16px; }}
QProgressBar::chunk {{ border-radius: 6px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {accent}, stop:1 {accent2}); }}
QListWidget {{ background: {panel}; border: 1px solid {border}; border-radius: 12px; color: {text}; padding: 4px; outline: 0; }}
QListWidget::item {{ padding: 5px 6px; border-radius: 7px; }}
QListWidget::item:hover:!selected {{ background: {hover}; }}
QListWidget::item:selected {{ color: white;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {accent}, stop:1 {accent2}); }}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {{ background: {field}; color: {text}; border: 1px solid {border};
    border-radius: 8px; padding: 4px 8px; selection-background-color: {accent}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border: 1px solid {accent}; }}
QComboBox QAbstractItemView {{ background: {panel}; color: {text}; selection-background-color: {accent}; border: 1px solid {border}; }}
QMenu {{ background: {panel}; color: {text}; border: 1px solid {border}; border-radius: 10px; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
QMenu::item:selected {{ color: white; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {accent}, stop:1 {accent2}); }}
QMenu::item:disabled {{ color: {sub}; }}
QMenu::separator {{ height: 1px; background: {border}; margin: 4px 8px; }}
QCheckBox, QRadioButton, QLabel {{ color: {text}; }}
QToolTip {{ background: {panel}; color: {text}; border: 1px solid {border}; border-radius: 6px; padding: 4px; }}
"""

_orig = {}


def saved() -> str:
    v = QSettings("timevault", "gui").value("theme", "system", str)
    return v if v in THEMES else "system"


def save(name: str):
    QSettings("timevault", "gui").setValue("theme", name if name in THEMES else "system")


def apply(qapp, name: str, dark: bool):
    if not _orig:
        _orig.update(style=qapp.style().name(), palette=qapp.palette(), qss=qapp.styleSheet())
    if name != "aurora":
        qapp.setStyle(_orig["style"])
        qapp.setPalette(_orig["palette"])
        qapp.setStyleSheet(_orig["qss"])
        return
    c = PAL[bool(dark)]
    qapp.setStyle("Fusion")
    p = QPalette()
    for role, key in ((QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.WindowText, "text"),
                      (QPalette.ColorRole.Base, "field"), (QPalette.ColorRole.AlternateBase, "hover"),
                      (QPalette.ColorRole.Text, "text"), (QPalette.ColorRole.Button, "panel"),
                      (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.Highlight, "accent"),
                      (QPalette.ColorRole.ToolTipBase, "panel"), (QPalette.ColorRole.ToolTipText, "text"),
                      (QPalette.ColorRole.PlaceholderText, "sub"), (QPalette.ColorRole.Link, "accent")):
        p.setColor(role, QColor(c[key]))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["sub"]))
    qapp.setPalette(p)
    bg2, bg3 = ("#0F1426", "#170F2A") if dark else ("#EFF8FF", "#FFF4F8")
    qapp.setStyleSheet(QSS.format(bg2=bg2, bg3=bg3, **c))
