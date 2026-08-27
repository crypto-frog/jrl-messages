"""Animated connection indicator.

Replaces the small static dot beside the status text. One glanceable
badge with three honest states:

  ok    a comet arc orbiting a filled core: the line is alive and moving
  warn  a slow breathing ring: working, but degraded or catching up
  fail  a static broken ring: not connected, nothing is being checked

The animation only runs while the badge is actually visible and the
state is animated, so a hidden or minimized window costs nothing. Sized
and colored from the theme, so it follows the text-size setting and the
chosen accent's status palette. Clicking it opens the same connection
details as the status text.
"""
import math
import time

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen

from PySide6.QtWidgets import QWidget

from . import theme

_FRAME_MS = 33          # ~30 fps; a 22 px widget, invisible cost
_ORBIT_DEG_PER_S = 260  # comet speed in the ok state
_BREATH_PERIOD_S = 2.4  # warn-state breathing cycle


class ConnectionBadge(QWidget):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "warn"
        self._t0 = time.monotonic()
        # Self-advanced animation clock, stepped by a bounded amount per
        # actual paint. Wall-clock-driven angles made the badge leap wildly
        # whenever the event loop was saturated (it read as "spinning out
        # of control" during the 3.1.1 storms); with a clamped step, heavy
        # load can only slow the motion down, never make it frantic.
        self._angle = 0.0
        self._breath = 0.0
        self._last_paint = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(_FRAME_MS)
        self._timer.timeout.connect(self.update)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName("Connection state")
        self.restyle()

    # ------------------------------------------------ state

    def set_state(self, state: str):
        state = state if state in ("ok", "warn", "fail") else "warn"
        if state != self.state:
            self.state = state
            self._sync_timer()
            self.update()

    def _color(self) -> QColor:
        return QColor({"ok": theme.OK, "warn": theme.WARN,
                       "fail": theme.FAIL}[self.state])

    def restyle(self):
        d = theme.dim(24)
        self.setFixedSize(d, d)
        self._sync_timer()
        self.update()

    # ------------------------------------------------ lifecycle

    def _sync_timer(self):
        want = self.isVisible() and self.state in ("ok", "warn")
        if want and not self._timer.isActive():
            self._timer.start()
        elif not want and self._timer.isActive():
            self._timer.stop()

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_timer()

    def hideEvent(self, e):
        # A hidden badge must never keep a repaint timer alive; that is
        # this app's standing rule for anything animated.
        self._timer.stop()
        super().hideEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(
                e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)

    # ------------------------------------------------ painting

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height())
        stroke = max(2.0, side * 0.11)
        margin = stroke * 0.5 + 1.0
        ring = QRectF(margin, margin, side - 2 * margin, side - 2 * margin)
        color = self._color()
        now = time.monotonic()
        # Bounded step: at most ~2.5 nominal frames of motion per paint,
        # however late this paint is.
        dt = min(max(0.0, now - self._last_paint), _FRAME_MS * 2.5 / 1000.0)
        self._last_paint = now
        self._angle = (self._angle + dt * _ORBIT_DEG_PER_S) % 360.0
        self._breath = (self._breath + dt / _BREATH_PERIOD_S) % 1.0

        if self.state == "ok":
            # Faint full track so the shape reads as a ring at rest.
            track = QColor(color)
            track.setAlpha(52)
            p.setPen(QPen(track, stroke, Qt.SolidLine, Qt.RoundCap))
            p.drawEllipse(ring)
            # The comet: a bright leading arc plus a fading tail.
            head = (-self._angle) % 360.0
            p.setPen(QPen(color, stroke, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(ring, int(head * 16), int(-96 * 16))
            tail = QColor(color)
            tail.setAlpha(110)
            p.setPen(QPen(tail, stroke, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(ring, int((head + 96) * 16), int(-54 * 16))
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawEllipse(ring.center(), side * 0.14, side * 0.14)
        elif self.state == "warn":
            phase = (math.sin(self._breath * 2 * math.pi) + 1.0) / 2.0
            breathing = QColor(color)
            breathing.setAlpha(int(90 + 150 * phase))
            p.setPen(QPen(breathing, stroke, Qt.SolidLine, Qt.RoundCap))
            p.drawEllipse(ring)
            p.setPen(Qt.NoPen)
            core = QColor(color)
            core.setAlpha(int(140 + 100 * phase))
            p.setBrush(core)
            p.drawEllipse(ring.center(), side * 0.13, side * 0.13)
        else:
            # fail: a broken ring, deliberately still.
            p.setPen(QPen(color, stroke, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(ring, 210 * 16, 240 * 16)
            p.setPen(QPen(color, stroke * 0.9, Qt.SolidLine, Qt.RoundCap))
            inset = side * 0.26
            p.drawLine(QRectF(inset, inset, side - 2 * inset,
                              side - 2 * inset).topRight(),
                       QRectF(inset, inset, side - 2 * inset,
                              side - 2 * inset).bottomLeft())
        p.end()

    def sizeHint(self):
        d = theme.dim(24)
        return QSize(d, d)
