"""Rich notification popups. Frameless accent-styled cards in the
bottom-right corner that never steal focus. When a verification code is
detected, Copy puts it on the clipboard from the popup itself and Fill
types it into whatever field currently has the cursor: one click,
never zero clicks, so nothing is ever typed without the user asking.

Storm safety (3.1.1): the manager is the single place popup windows are
born, so it enforces physical bounds no caller can bypass: at most MAX
cards on screen, a hard cap on live card widgets even if bookkeeping
drifts, and a circuit breaker on the creation rate. If anything ever
tries to create cards faster than a human could be served, the breaker
holds new cards in the bounded queue, logs loudly, and resumes calmly
after a cooldown. Runaway window creation is structurally impossible.
"""
import logging
import sys
import time
from enum import Enum

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from . import theme
from ..util.winfill import FillResult, foreground_window, type_text

log = logging.getLogger(__name__)


class PresentationResult(Enum):
    SHOWN = "shown"
    QUEUED = "queued"
    UNAVAILABLE = "unavailable"


def play_notification_sound() -> bool:
    """Play one explicit app sound; rich QWidget popups have none by default."""
    if sys.platform.startswith("win"):
        try:
            import winsound
            winsound.PlaySound(
                "SystemNotification",
                winsound.SND_ALIAS | winsound.SND_ASYNC)
            return True
        except Exception:
            # MessageBeep remains available when a custom Windows sound
            # scheme does not define the SystemNotification alias.
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
                return True
            except Exception:
                log.exception("Windows notification sound failed")
    try:
        QApplication.beep()
        return True
    except Exception:
        log.exception("Fallback notification sound failed")
        return False


def _clicked_button(widget):
    """Walk through nested labels/layout containers to an action button."""
    while widget is not None:
        if isinstance(widget, QPushButton):
            return widget
        widget = widget.parentWidget()
    return None


class NotificationPopup(QWidget):
    open_requested = Signal(str)
    dismissed = Signal(object)

    WIDTH = 350

    def __init__(self, title: str, body: str, code, chat_guid: str,
                 event_key: str = ""):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.chat_guid = chat_guid
        self.event_key = event_key
        self.code = str(code) if code else None
        self._action_taken = False
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        card = QWidget(self)
        card.setObjectName("card")
        card.setStyleSheet(
            f"QWidget#card {{ background: {theme.PANEL2}; border: 1px solid "
            f"{theme.BORDER}; border-left: 3px solid {theme.ACCENT}; "
            "border-radius: 12px; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(
            theme.dim(14), theme.dim(10), theme.dim(10), theme.dim(10))
        lay.setSpacing(theme.dim(6))

        top = QHBoxLayout()
        t = QLabel(title)
        t.setTextFormat(Qt.PlainText)
        t.setTextInteractionFlags(Qt.NoTextInteraction)
        t.setStyleSheet(f"font-weight: 600; font-size: {theme.fs(10.5)};")
        x = QPushButton("✕")
        x.setObjectName("ghost")
        x.setFixedSize(theme.dim(22), theme.dim(22))
        x.setFocusPolicy(Qt.NoFocus)
        x.setAccessibleName("Dismiss notification")
        x.setToolTip("Dismiss")
        x.clicked.connect(self.close)
        top.addWidget(t, 1)
        top.addWidget(x)
        lay.addLayout(top)

        b = QLabel(body if len(body) <= 220 else body[:217] + "…")
        b.setTextFormat(Qt.PlainText)
        b.setWordWrap(True)
        b.setStyleSheet(f"color: {theme.MUTED}; font-size: {theme.fs(9.4)};")
        lay.addWidget(b)

        if code:
            badge = QLabel(self.code)
            badge.setTextFormat(Qt.PlainText)
            badge.setTextInteractionFlags(Qt.TextSelectableByMouse)
            badge.setAccessibleName(f"Verification code {self.code}")
            badge.setStyleSheet(
                f"background: {theme.BG}; color: {theme.TEXT}; "
                f"border: 1px solid {theme.ACCENT_BORDER}; "
                f"border-radius: 8px; padding: 5px 9px; "
                f"font-family: 'Cascadia Mono', 'Consolas', monospace; "
                f"font-size: {theme.fs(12)}; font-weight: 700;")
            lay.addWidget(badge, 0, Qt.AlignLeft)
            row = QHBoxLayout()
            self.copy_btn = QPushButton("Copy code")
            self.copy_btn.setObjectName("accent")
            self.copy_btn.setFocusPolicy(Qt.NoFocus)
            self.copy_btn.setAccessibleName(
                f"Copy verification code {self.code}")
            self.copy_btn.setToolTip("Copy the verification code")
            self.copy_btn.clicked.connect(self._copy)
            self.fill_btn = QPushButton("Fill code")
            self.fill_btn.setFocusPolicy(Qt.NoFocus)
            self.fill_btn.setAccessibleName(
                f"Fill verification code {self.code}")
            self.fill_btn.setToolTip(
                "Type the code into the field that currently has focus")
            self.fill_btn.clicked.connect(self._fill)
            row.addWidget(self.copy_btn)
            row.addWidget(self.fill_btn)
            row.addStretch(1)
            lay.addLayout(row)

        self.setFixedWidth(theme.dim(self.WIDTH))
        self.adjustSize()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        # Code popups remain long enough to read and act on without haste.
        self._timer.setInterval(35000 if self.code else 9000)
        self._timer.timeout.connect(self.close)
        self._timer.start()
        # absolute failsafe: no popup outlives a minute, hover or not
        self._hard_timer = QTimer(self)
        self._hard_timer.setSingleShot(True)
        self._hard_timer.setInterval(60000)
        self._hard_timer.timeout.connect(self.close)
        self._hard_timer.start()

    # ------------------------------------------------ actions

    def _copy(self):
        if self._action_taken:
            return
        self._action_taken = True
        self._disable_actions()
        QApplication.clipboard().setText(self.code)
        self.copy_btn.setText("Copied ✓")
        QTimer.singleShot(1200, self.close)

    def _fill(self):
        if self._action_taken:
            return
        self._action_taken = True
        self._disable_actions()
        # Use the field that is active when the user deliberately clicks
        # Fill. type_text rechecks that same HWND immediately before sending,
        # closing the focus-race without forcing or stealing focus.
        target_hwnd = foreground_window()
        result = type_text(self.code, target_hwnd)
        if result is FillResult.SUCCESS:
            self.fill_btn.setText("Filled ✓")
            QTimer.singleShot(1200, self.close)
        elif result is FillResult.PARTIAL:
            # Some characters may already be present. Do not silently copy a
            # full code and invite accidental duplication on paste.
            self.fill_btn.setText("Fill incomplete")
            self.fill_btn.setToolTip(
                "Windows accepted only part of the code. Clear the field, "
                "then use Copy code and paste it.")
            self.copy_btn.setText("Copy full code")
            self.copy_btn.setEnabled(True)
            self._action_taken = False
            self._timer.start(15000)
        else:
            QApplication.clipboard().setText(self.code)
            self.fill_btn.setText("Couldn't fill · copied")
            self.fill_btn.setToolTip(
                "Focus changed or Windows blocked typing; the code is on "
                "the clipboard, so press Ctrl+V")
            QTimer.singleShot(1800, self.close)

    def _disable_actions(self):
        self._timer.stop()
        for name in ("copy_btn", "fill_btn"):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(False)

    # ------------------------------------------------ behavior

    def mouseReleaseEvent(self, e):
        child = self.childAt(e.position().toPoint())
        if (e.button() == Qt.LeftButton and self.chat_guid
                and _clicked_button(child) is None):
            self.open_requested.emit(self.chat_guid)
            self.close()
        super().mouseReleaseEvent(e)

    def enterEvent(self, e):
        self._timer.stop()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._timer.start(3000)
        super().leaveEvent(e)

    def closeEvent(self, e):
        self._timer.stop()
        self._hard_timer.stop()
        self.dismissed.emit(self)
        super().closeEvent(e)


class PopupManager:
    MAX = 3
    MAX_PENDING = 100
    # Physical bound on live card widgets, independent of list bookkeeping.
    HARD_CAP = 8
    # Circuit breaker: more creations than this within the window means a
    # runaway caller, not a person reading alerts.
    BREAKER_WINDOW_S = 45.0
    BREAKER_MAX_CREATIONS = 12
    BREAKER_COOLDOWN_S = 20.0

    def __init__(self, on_open, anchor_widget=None, on_presented=None,
                 on_rejected=None):
        self.on_open = on_open
        self.anchor = anchor_widget
        self.on_presented = on_presented
        self.on_rejected = on_rejected
        self.active: list[NotificationPopup] = []
        self.pending: list[tuple] = []
        self._event_keys: set[str] = set()
        self._creation_times: list[float] = []
        self._breaker_until = 0.0
        self._breaker_logged = False
        self._resume_scheduled = False

    # ------------------------------------------------ storm safety

    def _live_card_count(self) -> int:
        """Count actual card widgets, bypassing our own accounting so a
        bookkeeping bug can never translate into unbounded windows."""
        count = 0
        for widget in QApplication.topLevelWidgets():
            try:
                if isinstance(widget, NotificationPopup) and widget.isVisible():
                    count += 1
            except RuntimeError:
                continue
        return count

    def _breaker_engaged(self, now: float) -> bool:
        if now < self._breaker_until:
            return True
        cutoff = now - self.BREAKER_WINDOW_S
        self._creation_times = [t for t in self._creation_times if t > cutoff]
        if len(self._creation_times) >= self.BREAKER_MAX_CREATIONS:
            self._breaker_until = now + self.BREAKER_COOLDOWN_S
            if not self._breaker_logged:
                self._breaker_logged = True
                log.critical(
                    "Notification circuit breaker engaged: %d cards created "
                    "within %.0f s. New cards are queued, not shown. This "
                    "protects against runaway window creation; please send "
                    "the log.", len(self._creation_times),
                    self.BREAKER_WINDOW_S)
            self._schedule_resume(self.BREAKER_COOLDOWN_S)
            return True
        self._breaker_logged = False
        return False

    def _schedule_resume(self, delay_s: float):
        if self._resume_scheduled:
            return
        self._resume_scheduled = True

        def resume():
            self._resume_scheduled = False
            self._drain_pending()
        QTimer.singleShot(int(max(0.25, delay_s) * 1000) + 250, resume)

    def _queue(self, request) -> PresentationResult:
        event_key = request[4]
        if len(self.pending) >= self.MAX_PENDING:
            return PresentationResult.UNAVAILABLE
        self.pending.append(request)
        if event_key:
            self._event_keys.add(event_key)
        return PresentationResult.QUEUED

    # ------------------------------------------------ presentation

    def show(self, title: str, body: str, code, chat_guid: str,
             event_key: str = "") -> PresentationResult:
        """Accept, queue, or refuse a card without silently dropping it.

        A durable delivery event is acknowledged by ``on_presented`` only
        after its card is actually constructed and shown.  Queued cards remain
        durable across this in-memory wait and are deduplicated on ledger
        sweeps.
        """
        if event_key and event_key in self._event_keys:
            return PresentationResult.QUEUED
        request = (title, body, code, chat_guid, event_key)
        if len(self.active) >= self.MAX:
            return self._queue(request)
        return self._show_now(request)

    def _show_now(self, request, *, notify_rejected: bool = False
                  ) -> PresentationResult:
        title, body, code, chat_guid, event_key = request
        now = time.monotonic()
        if self._breaker_engaged(now):
            queued = self._queue(request)
            if (queued is PresentationResult.UNAVAILABLE and notify_rejected
                    and event_key and self.on_rejected is not None):
                QTimer.singleShot(
                    0, lambda key=event_key: self.on_rejected(key))
            return queued
        if self._live_card_count() >= self.HARD_CAP:
            # Accounting can never be trusted more than the screen itself.
            log.critical(
                "Popup hard cap reached (%d live cards); refusing creation",
                self.HARD_CAP)
            if event_key:
                self._event_keys.discard(event_key)
            if (notify_rejected and event_key
                    and self.on_rejected is not None):
                QTimer.singleShot(
                    0, lambda key=event_key: self.on_rejected(key))
            return PresentationResult.UNAVAILABLE
        p = None
        try:
            p = NotificationPopup(title, body, code, chat_guid, event_key)
            p.open_requested.connect(self.on_open)
            p.dismissed.connect(self._gone)
            self.active.append(p)
            if event_key:
                self._event_keys.add(event_key)
            self._restack()
            p.show()
            p.raise_()
            self._creation_times.append(now)
        except Exception:
            log.exception("Could not show notification popup")
            if p is not None:
                self.active = [item for item in self.active if item is not p]
            if event_key:
                self._event_keys.discard(event_key)
            if p is not None:
                p.deleteLater()
            if (notify_rejected and event_key
                    and self.on_rejected is not None):
                QTimer.singleShot(
                    0, lambda key=event_key: self.on_rejected(key))
            return PresentationResult.UNAVAILABLE
        if event_key and self.on_presented is not None:
            # Queue to the GUI event loop so QWidget.show() has been accepted
            # before the durable ledger is acknowledged.
            QTimer.singleShot(
                0, lambda key=event_key: self.on_presented(key))
        return PresentationResult.SHOWN

    def _gone(self, popup):
        self.active = [p for p in self.active if p is not popup]
        if popup.event_key:
            self._event_keys.discard(popup.event_key)
        popup.deleteLater()
        self._restack()
        self._drain_pending()

    def _drain_pending(self):
        while self.pending and len(self.active) < self.MAX:
            request = self.pending.pop(0)
            event_key = request[4]
            if event_key:
                self._event_keys.discard(event_key)
            result = self._show_now(request, notify_rejected=True)
            if result is PresentationResult.QUEUED:
                # The breaker re-queued this request and scheduled a resume.
                # Stop draining; showing more right now is exactly what the
                # breaker exists to prevent.
                break
            if result is PresentationResult.UNAVAILABLE:
                # The failed request was rejected back to the durable ledger.
                # Continue so later queued cards are not stranded waiting for
                # a dismissal that may never occur.
                continue

    def _restack(self):
        screen = QGuiApplication.screenAt(QCursor.pos())
        if self.anchor is not None:
            if screen is None:
                try:
                    screen = self.anchor.screen()
                except Exception:
                    screen = None
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        y = geo.bottom() - 16
        for p in reversed(self.active):
            y = max(geo.top() + 12, y - p.height())
            p.move(geo.right() - p.width() - 16, y)
            y -= 10
