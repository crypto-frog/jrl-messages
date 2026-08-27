"""Central, low-noise tooltip policy.

Qt already supplies an appropriate platform hover delay.  This controller
adds the part Qt does not: a learnable two-show limit, a real Off switch,
and suppression while a mouse button is down.  Stable tip IDs are
persisted; dynamic widgets are limited only for the current run so the
settings file never grows with message content.

Design rule, learned from the 3.1.0 window-storm freeze: an application
wide event filter must be provably inert.  This filter therefore touches
exactly one event type (QEvent.ToolTip) and its only possible actions are
returning True or False.  It never calls into Qt's tooltip machinery, it
never hides, shows, creates, or destroys any window, and it never tracks
Destroy, Leave, or mouse events.  Interfering with native tooltip windows
from inside the global filter is what let turning tips off spawn runaway
windows and freeze the app; a filter that can only consume an event
cannot do any of that, on any platform, in any state.  The single
QToolTip call left in this module runs once from set_mode(), outside the
filter, on an explicit user action.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QToolTip

from .. import config

# One physical hover produces a stream of ToolTip help events while the
# cursor rests and micro-moves.  Events for the same object within this
# window count as one appearance for the two-show limit.
SAME_HOVER_WINDOW_S = 3.0


class TooltipController(QObject):
    MODES = ("limited", "always", "off")

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.mode = self._clean_mode(
            getattr(settings, "tooltip_mode", "limited"))
        # obj id -> (count, last event monotonic).  Session-only by design:
        # bubble metadata and attachment names must never become config keys.
        self._session_seen: dict[int, tuple[int, float]] = {}
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save_now)

    def _save_now(self):
        try:
            config.save(self.settings)
        except Exception:
            # A failed settings write must never disturb the UI; the learned
            # counts simply persist on the next successful save.
            pass

    @classmethod
    def _clean_mode(cls, mode: str) -> str:
        return mode if mode in cls.MODES else "limited"

    def set_mode(self, mode: str) -> None:
        self.mode = self._clean_mode(mode)
        self.settings.tooltip_mode = self.mode
        # Outside the event filter, once, on an explicit user action: hide
        # a tooltip that may be on screen at the moment Off is chosen.
        if self.mode == "off":
            try:
                QToolTip.hideText()
            except Exception:
                pass

    def reset_learned(self) -> None:
        self.settings.tooltip_seen = {}
        self._session_seen.clear()
        self._save_timer.start()

    @staticmethod
    def _stable_id(obj) -> str:
        try:
            value = obj.property("jrlTipId")
        except Exception:
            value = None
        return str(value).strip() if value else ""

    def _seen_count(self, obj, obj_id: int) -> int:
        stable = self._stable_id(obj)
        if stable:
            seen = self.settings.tooltip_seen
            if not isinstance(seen, dict):
                seen = {}
                self.settings.tooltip_seen = seen
            try:
                return max(0, int(seen.get(stable, 0)))
            except (TypeError, ValueError):
                return 0
        entry = self._session_seen.get(obj_id)
        if entry is None:
            return 0
        return entry[0]

    def _record_shown(self, obj, obj_id: int, now: float, count: int) -> None:
        stable = self._stable_id(obj)
        if stable:
            self.settings.tooltip_seen[stable] = count + 1
            self._save_timer.start()
        if len(self._session_seen) > 1000:
            # Dynamic bubble widgets are short-lived.  Keep the session policy
            # bounded even after hours of opening long conversations.
            self._session_seen.clear()
        self._session_seen[obj_id] = (count + 1, now)

    def eventFilter(self, obj, event):
        # Absolute rule: no event other than ToolTip is ever examined, and
        # the only possible side effects here are bookkeeping and the
        # boolean return.  See the module docstring for why.
        if event.type() != QEvent.Type.ToolTip:
            return False
        if self.mode == "off":
            return True
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            # Tooltips during a click or drag are the exact distraction this
            # policy exists to remove.
            return True
        if self.mode == "always":
            return False

        now = time.monotonic()
        obj_id = id(obj)
        entry = self._session_seen.get(obj_id)
        if entry is not None and now - entry[1] < SAME_HOVER_WINDOW_S:
            # A repeated help event during one continuous hover is still one
            # appearance.  Refresh the window and keep showing it.
            self._session_seen[obj_id] = (entry[0], now)
            return False
        count = self._seen_count(obj, obj_id)
        if count >= 2:
            return True
        self._record_shown(obj, obj_id, now, count)
        return False
