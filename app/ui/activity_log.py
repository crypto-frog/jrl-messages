"""On-screen activity log and the window warden.

The activity log answers "what is the app doing right now" without
digging through files: every connection attempt, status change, wake,
recovery, refusal, error, and warning of this session is collected in a
bounded in-memory ring and shown in a live, dark-styled, non-modal panel
opened from the connection chip. A logging bridge mirrors every WARNING
or worse from any module into the same ring, so things like the popup
circuit breaker report themselves here too.

The window warden is the flight recorder for the ghost-window storms
reported on Windows: a cheap 4 Hz scan of the application's top-level
widgets. Anything visible that is not an expected window class is
recorded in the activity log with its exact class, size, and flags, and
is hidden on the spot (rate-limited so the warden itself can never
loop). If a storm ever recurs, the log names the culprit and the screen
stays clean.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections import deque

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                               QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QVBoxLayout)

from .. import constants
from . import theme

log = logging.getLogger(__name__)

RING_SIZE = 600


class ActivityRecorder(QObject):
    entry_added = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries: deque[str] = deque(maxlen=RING_SIZE)
        self._last_line = ""
        self._repeat = 0

    def record(self, kind: str, text: str):
        text = " ".join(str(text).split())
        if not text:
            return
        base = f"{kind.upper():7s} {text}"
        if base == self._last_line:
            # Collapse repeats so a chatty condition cannot bury the story.
            self._repeat += 1
            if self._repeat in (5, 25) or self._repeat % 100 == 0:
                self._emit(f"{base}  (repeated ×{self._repeat})")
            return
        self._last_line = base
        self._repeat = 0
        self._emit(base)

    def _emit(self, base: str):
        stamp = time.strftime("%H:%M:%S")
        line = f"{stamp}  {base}"
        self.entries.append(line)
        self.entry_added.emit(line)

    def text(self) -> str:
        return "\n".join(self.entries)


class ActivityLogHandler(logging.Handler):
    """Mirror warnings and errors from every module into the recorder."""

    def __init__(self, recorder: ActivityRecorder):
        super().__init__(level=logging.WARNING)
        self.recorder = recorder

    def emit(self, record):
        try:
            kind = "error" if record.levelno >= logging.ERROR else "warn"
            source = record.name.rsplit(".", 1)[-1]
            self.recorder.record(kind, f"[{source}] {record.getMessage()}")
        except Exception:
            pass


class ActivityPanel(QDialog):
    """Non-modal live view over the recorder. Styled by the app theme."""

    def __init__(self, recorder: ActivityRecorder, details_provider,
                 parent=None):
        super().__init__(parent)
        self.recorder = recorder
        self.details_provider = details_provider
        self.setWindowTitle("Activity")
        self.setModal(False)
        self.resize(theme.dim(640), theme.dim(460))
        self.setMinimumSize(theme.dim(430), theme.dim(300))

        root = QVBoxLayout(self)
        self.details = QLabel("")
        self.details.setTextFormat(Qt.PlainText)
        self.details.setWordWrap(True)
        self.details.setStyleSheet(
            f"background: {theme.PANEL2}; border: 1px solid {theme.BORDER}; "
            f"border-radius: {theme.dim(9)}px; color: {theme.TEXT}; "
            f"padding: {theme.dim(8)}px; font-size: {theme.fs(9.2)};")
        root.addWidget(self.details)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setStyleSheet(
            f"QPlainTextEdit {{ background: {theme.BG}; "
            f"color: {theme.TEXT}; border: 1px solid {theme.BORDER}; "
            f"border-radius: {theme.dim(9)}px; "
            f"font-family: 'Cascadia Mono', 'Consolas', monospace; "
            f"font-size: {theme.fs(8.8)}; }}")
        self.view.setPlainText(recorder.text())
        root.addWidget(self.view, 1)

        buttons = QHBoxLayout()
        hint = QLabel("Live · connection attempts, errors, refusals, "
                      "resets, and recoveries appear here as they happen")
        hint.setStyleSheet(
            f"color: {theme.MUTED}; font-size: {theme.fs(8.4)};")
        hint.setWordWrap(True)
        buttons.addWidget(hint, 1)
        copy_btn = QPushButton("Copy all")
        copy_btn.clicked.connect(self._copy_all)
        buttons.addWidget(copy_btn)
        folder_btn = QPushButton("Open log folder")
        folder_btn.setToolTip("Full on-disk logs for the window, the "
                              "background agent, and its supervisor")
        folder_btn.clicked.connect(self._open_folder)
        buttons.addWidget(folder_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        buttons.addWidget(close_btn)
        root.addLayout(buttons)

        recorder.entry_added.connect(self._append)
        self._refresh_details()
        self._details_timer = QTimer(self)
        self._details_timer.setInterval(2000)
        self._details_timer.timeout.connect(self._refresh_details)
        self._details_timer.start()
        self._scroll_to_end()

    def _refresh_details(self):
        try:
            self.details.setText(self.details_provider())
        except Exception:
            pass

    def _append(self, line: str):
        bar = self.view.verticalScrollBar()
        stick = bar.value() >= bar.maximum() - 4
        self.view.appendPlainText(line)
        if stick:
            self._scroll_to_end()

    def _scroll_to_end(self):
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _copy_all(self):
        QApplication.clipboard().setText(self.recorder.text())

    def _open_folder(self):
        path = str(constants.LOG_DIR)
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            log.exception("Could not open the log folder")

    def closeEvent(self, e):
        self._details_timer.stop()
        try:
            self.recorder.entry_added.disconnect(self._append)
        except Exception:
            pass
        super().closeEvent(e)


class WindowWarden(QObject):
    """Detect, name, and neutralize unexpected top-level windows.

    Expected windows: the main window itself, dialogs (all of the app's
    dialogs are QDialog subclasses, as are Qt's message boxes), menus,
    tooltips and combo popups (Qt internal classes), and the app's own
    frameless notification cards. Anything else visible at the top level
    is a bug showing itself; the warden records exactly what it was and
    hides it, so a misbehaving code path produces log lines instead of a
    screenful of ghost windows.
    """

    QT_INTERNAL = {"QTipLabel", "QComboBoxPrivateContainer", "QMenu",
                   "QToolTip", "QWhatsThat", "QSystemTrayIconSys",
                   "QShapedPixmapWidget", "QSplashScreen"}
    MAX_HIDES_PER_MINUTE = 20

    def __init__(self, main_window, recorder: ActivityRecorder,
                 extra_expected=(), parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self.recorder = recorder
        self.extra_expected = tuple(extra_expected)
        self._hide_times: deque[float] = deque(maxlen=64)
        self._seen_ids: set[int] = set()
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.sweep)

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _expected(self, widget) -> bool:
        from PySide6.QtWidgets import QMainWindow
        if widget is self.main_window or isinstance(widget, QMainWindow):
            return True
        if isinstance(widget, (QDialog, QMenu, QMessageBox)):
            return True
        try:
            # Popups and tooltips are legitimate transient windows: menus,
            # combo dropdowns, the emoji picker, Qt's own tip labels.
            if widget.windowType() in (Qt.WindowType.Popup,
                                       Qt.WindowType.ToolTip):
                return True
        except Exception:
            pass
        if type(widget).__name__ in self.QT_INTERNAL:
            return True
        if self.extra_expected and isinstance(widget, self.extra_expected):
            return True
        return False

    def sweep(self):
        try:
            for widget in QApplication.topLevelWidgets():
                try:
                    if not widget.isVisible() or self._expected(widget):
                        continue
                    ident = id(widget)
                    flags = widget.windowFlags()
                    flags_text = (hex(int(flags.value))
                                  if hasattr(flags, "value")
                                  else str(int(flags)))
                    if ident not in self._seen_ids:
                        self._seen_ids.add(ident)
                        self.recorder.record(
                            "error",
                            "Unexpected window detected and hidden: "
                            f"class={type(widget).__name__} "
                            f"name={widget.objectName() or '-'} "
                            f"size={widget.width()}x{widget.height()} "
                            f"flags={flags_text}")
                        log.critical(
                            "Window warden: unexpected top-level %s "
                            "(name=%r, %dx%d, flags=%s)",
                            type(widget).__name__, widget.objectName(),
                            widget.width(), widget.height(), flags_text)
                    now = time.monotonic()
                    while (self._hide_times
                           and now - self._hide_times[0] > 60.0):
                        self._hide_times.popleft()
                    if len(self._hide_times) < self.MAX_HIDES_PER_MINUTE:
                        self._hide_times.append(now)
                        widget.hide()
                except RuntimeError:
                    continue
        except Exception:
            log.exception("Window warden sweep failed")
