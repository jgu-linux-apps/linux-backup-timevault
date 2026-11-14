"""Aurora title bar for Qt windows (shared look of the JGu Linux Apps series).

On GNOME/Wayland Qt draws the window title bar itself with a decoration plugin that application style
sheets cannot reach, so under Aurora it stays white. When Aurora is on, the window becomes frameless and
gets this bar instead: themed background, centred title, the window buttons in GNOME's button-layout order,
drag to move, double-click to maximise, and thin grips on the edges for resizing (the compositor does the
actual move/resize through startSystemMove/startSystemResize, so it works on Wayland and X11).
Under the System theme the window keeps its normal frame. The same file lives in linux-icloud-calendar."""
import subprocess

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QWidget

HEIGHT = 34
GRIP = 6

COLORS = {
    False: dict(bg="#F1EDFF", fg="#1F1A3A", line="#E2DCFA", hover="#E3DBFF", press="#D6CBFF"),
    True: dict(bg="#0E1020", fg="#ECEBFF", line="#252A45", hover="#1E2342", press="#2A2F55"),
}


def _button_layout():
    """GNOME's order of window buttons, e.g. ':minimize,maximize,close' -> ([], [min, max, close])."""
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.wm.preferences", "button-layout"],
                             capture_output=True, text=True, timeout=2).stdout.strip().strip("'")
    except Exception:  # noqa: BLE001
        out = ""
    if ":" not in out:
        out = ":minimize,maximize,close"
    left, right = out.split(":", 1)
    keep = ("minimize", "maximize", "close")
    return ([b for b in left.split(",") if b in keep], [b for b in right.split(",") if b in keep])


class _WinButton(QAbstractButton):
    def __init__(self, kind, bar):
        super().__init__(bar)
        self.kind, self.bar = kind, bar
        self.setFixedSize(24, 24)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def paintEvent(self, _e):
        c = self.bar.colors
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = c["press"] if self.isDown() else c["hover"]
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(bg))
        p.drawEllipse(QRectF(0, 0, 24, 24))
        fg = QColor(c["fg"])
        if not self.bar.window().isActiveWindow():
            fg.setAlphaF(0.55)
        pen = QPen(fg, 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.kind == "close":
            p.drawLine(QPointF(8.5, 8.5), QPointF(15.5, 15.5))
            p.drawLine(QPointF(15.5, 8.5), QPointF(8.5, 15.5))
        elif self.kind == "minimize":
            p.drawLine(QPointF(8, 15.5), QPointF(16, 15.5))
        elif self.bar.window().isMaximized():
            p.drawRect(QRectF(8, 10, 6, 6))
            p.drawLine(QPointF(10, 8), QPointF(16, 8))
            p.drawLine(QPointF(16, 8), QPointF(16, 14))
        else:
            p.drawRect(QRectF(8, 8, 8, 8))


class _Grip(QWidget):
    """Invisible strip on a window edge/corner that starts a compositor-driven resize."""
    def __init__(self, win, edges, cursor):
        super().__init__(win)
        self.edges = edges
        self.setCursor(cursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.window().windowHandle():
            self.window().windowHandle().startSystemResize(self.edges)


class TitleBar(QWidget):
    """Frameless window + Aurora bar. Call set_active(on, dark) whenever the theme changes."""

    def __init__(self, win, title):
        super().__init__(win)
        self.win = win
        self.active = False
        self.colors = COLORS[False]
        self.setObjectName("auroraTitleBar")
        self.setFixedHeight(HEIGHT)
        self.title = QLabel(title, self)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        left, right = _button_layout()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.setSpacing(10)
        self.buttons = []

        def add(kinds):
            for k in kinds:
                b = _WinButton(k, self)
                b.clicked.connect(lambda _=False, k=k: self._action(k))
                lay.addWidget(b)
                self.buttons.append(b)

        add(left)
        lay.addStretch(1)
        add(right)
        # title centred over the whole bar, not just the space between the buttons
        self.title.lower()
        E, C = Qt.Edge, Qt.CursorShape
        self.grips = [_Grip(win, e, c) for e, c in (
            (E.TopEdge, C.SizeVerCursor), (E.BottomEdge, C.SizeVerCursor),
            (E.LeftEdge, C.SizeHorCursor), (E.RightEdge, C.SizeHorCursor),
            (E.TopEdge | E.LeftEdge, C.SizeFDiagCursor), (E.BottomEdge | E.RightEdge, C.SizeFDiagCursor),
            (E.TopEdge | E.RightEdge, C.SizeBDiagCursor), (E.BottomEdge | E.LeftEdge, C.SizeBDiagCursor))]
        self._margins = win.contentsMargins()
        win.installEventFilter(self)
        self.hide()
        for g in self.grips:
            g.hide()

    # -- theme -------------------------------------------------------------------
    def set_active(self, on, dark):
        self.colors = COLORS[bool(dark)]
        self.title.setStyleSheet(f"color:{self.colors['fg']};font-weight:600;background:transparent;")
        if on != self.active:
            self.active = on
            win = self.win
            visible = win.isVisible()
            geo = win.geometry()
            win.setWindowFlag(Qt.WindowType.FramelessWindowHint, on)
            m = self._margins
            win.setContentsMargins(m.left(), m.top() + (HEIGHT if on else 0), m.right(), m.bottom())
            self.setVisible(on)
            if visible:
                win.setGeometry(geo)
                win.show()
        self._layout()
        self.update()

    # -- geometry ----------------------------------------------------------------
    def _layout(self):
        w, h = self.win.width(), self.win.height()
        self.setGeometry(0, 0, w, HEIGHT)
        self.title.setGeometry(0, 0, w, HEIGHT)
        self.raise_()
        show = self.active and not (self.win.isMaximized() or self.win.isFullScreen())
        g, c = GRIP, GRIP * 2
        rects = ((c, 0, w - 2 * c, g), (c, h - g, w - 2 * c, g), (0, c, g, h - 2 * c), (w - g, c, g, h - 2 * c),
                 (0, 0, c, c), (w - c, h - c, c, c), (w - c, 0, c, c), (0, h - c, c, c))
        for grip, r in zip(self.grips, rects):
            grip.setGeometry(*r)
            grip.setVisible(show)
            if show:
                grip.raise_()
        # the top grips must not swallow the window buttons
        for b in self.buttons:
            b.raise_()

    def eventFilter(self, obj, e):
        t = e.type()
        if obj is self.win and t in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.WindowStateChange):
            self._layout()
            for b in self.buttons:
                b.update()
        elif obj is self.win and t == QEvent.Type.ActivationChange:
            self.update()
            for b in self.buttons:
                b.update()
        return False

    # -- painting & mouse ---------------------------------------------------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(self.colors["bg"]))
        p.setPen(QColor(self.colors["line"]))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.win.windowHandle():
            self.win.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._action("maximize")

    def _action(self, kind):
        w = self.win
        if kind == "close":
            w.close()
        elif kind == "minimize":
            w.showMinimized()
        elif w.isMaximized():
            w.showNormal()
        else:
            w.showMaximized()
