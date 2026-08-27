"""The in-app notification center: a quiet, theme-matched feed of every
alert the app has raised (messages, Mac wakes, line repairs, connection
changes, tests), opened from the bell in the bottom rail.

Storm rules apply here exactly as everywhere else since 3.1.2:

* The panel is a CHILD widget of the main window, never a new top-level
  window and never a popup window type, so the WindowWarden has nothing
  to police and the ghost-window class cannot return through here.
* Teardown uses removeWidget/hide/deleteLater and never orphans a
  visible widget into a parentless top-level.
* The click-outside filter is inert by construction: it is installed
  only while the panel is on screen, examines only MouseButtonPress,
  performs a bounded geometry test, and always returns False so no
  event is ever consumed or re-entered.
"""
import logging

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from ..util.timefmt import fmt_ago
from . import theme
from .icons import eye_off

log = logging.getLogger(__name__)

MAX_ROWS = 60


def kind_color(kind: str) -> str:
    """Dot color for a feed kind, always read live from the theme."""
    return {
        "message": theme.ACCENT,
        "phone": theme.ACCENT_LINE,
        "phone-up": theme.OK,
        "phone-down": theme.FAIL,
        "wake": theme.WARN,
        "repair": theme.OK,
        "link-down": theme.FAIL,
        "link-up": theme.OK,
        "test": theme.MUTED,
    }.get(kind, theme.MUTED)


class _FeedRow(QFrame):
    """One feed entry: kind dot, title, body, relative time, hide."""

    def __init__(self, entry, panel):
        super().__init__(panel._container)
        self._panel = panel
        self._chat_guid = entry["chat_guid"]
        self._feed_id = entry["id"]
        self.setObjectName("feedRow")
        self.setCursor(Qt.PointingHandCursor if self._chat_guid
                       else Qt.ArrowCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(theme.dim(10), theme.dim(7),
                               theme.dim(6), theme.dim(7))
        lay.setSpacing(theme.dim(8))

        dot = QLabel()
        d = max(6, theme.dim(8))
        dot.setFixedSize(d, d)
        dot.setStyleSheet(
            f"background: {kind_color(entry['kind'])}; "
            f"border-radius: {d // 2}px;")
        lay.addWidget(dot, 0, Qt.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(theme.dim(1))
        top = QHBoxLayout()
        top.setSpacing(theme.dim(6))
        title = QLabel(entry["title"] or "Notification")
        title.setStyleSheet(
            f"color: {theme.TEXT}; font-size: {theme.fs(9.2)}; "
            "font-weight: 600; background: transparent;")
        fresh = not entry["seen"]
        if fresh:
            title.setText(title.text() + "  •")
            title.setStyleSheet(
                f"color: {theme.ACCENT}; font-size: {theme.fs(9.2)}; "
                "font-weight: 650; background: transparent;")
        top.addWidget(title, 1)
        when = QLabel(fmt_ago(entry["created_ms"]))
        when.setStyleSheet(
            f"color: {theme.MUTED}; font-size: {theme.fs(8.0)}; "
            "background: transparent;")
        top.addWidget(when, 0, Qt.AlignTop)
        text_col.addLayout(top)
        body = (entry["body"] or "").replace("\n", " ")
        if len(body) > 120:
            body = body[:119] + "…"
        if body:
            body_label = QLabel(body)
            body_label.setWordWrap(True)
            body_label.setStyleSheet(
                f"color: {theme.MUTED}; font-size: {theme.fs(8.6)}; "
                "background: transparent;")
            text_col.addWidget(body_label)
        lay.addLayout(text_col, 1)

        hide_btn = QPushButton()
        hide_btn.setObjectName("feedHide")
        hide_btn.setToolTip("Hide this notification")
        hide_btn.setAccessibleName("Hide this notification")
        hide_btn.setCursor(Qt.PointingHandCursor)
        z = theme.dim(24)
        hide_btn.setFixedSize(z, z)
        hide_btn.setIcon(eye_off(theme.MUTED))
        icon_px = max(12, int(z * 0.72))
        hide_btn.setIconSize(QSize(icon_px, icon_px))
        hide_btn.clicked.connect(
            lambda: self._panel.hide_item(self._feed_id))
        lay.addWidget(hide_btn, 0, Qt.AlignTop)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._chat_guid:
            self._panel.open_entry(self._chat_guid)
            return
        super().mousePressEvent(event)


class _OutsideClickFilter(QObject):
    """Closes the panel on a press outside it. Inert by construction:
    only MouseButtonPress is examined, nothing is consumed, and the
    filter exists only while the panel is visible."""

    def __init__(self, panel):
        super().__init__(panel)
        self._panel = panel

    def eventFilter(self, _obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            self._panel.press_observed(event)
        return False


class AlertCenterPanel(QFrame):
    """Theme-matched overlay listing recent alerts, anchored to the bell."""

    def __init__(self, window, repo, on_open_chat=None, on_changed=None):
        super().__init__(window)
        self._window = window
        self.repo = repo
        self._on_open_chat = on_open_chat
        self._on_changed = on_changed
        self._anchor = None
        self._filter = None
        self.setObjectName("alertCenter")
        self.hide()

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.dim(4), theme.dim(4),
                                theme.dim(4), theme.dim(4))
        root.setSpacing(theme.dim(2))
        header = QHBoxLayout()
        header.setContentsMargins(theme.dim(8), theme.dim(4),
                                  theme.dim(4), 0)
        self._title = QLabel("Notifications")
        header.addWidget(self._title, 1)
        self.clear_btn = QPushButton("Clear all")
        self.clear_btn.setObjectName("ghost")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.setToolTip("Hide every notification in this list")
        self.clear_btn.clicked.connect(self.clear_all)
        header.addWidget(self.clear_btn)
        root.addLayout(header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._rows_layout = QVBoxLayout(self._container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(theme.dim(2))
        self._empty = QLabel("Quiet so far.\nNew alerts land here as "
                             "they happen, even ones you were away for.")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        self._rows_layout.addWidget(self._empty)
        self._rows_layout.addStretch(1)
        self._scroll.setWidget(self._container)
        root.addWidget(self._scroll, 1)
        self._row_widgets: list = []

        esc = QShortcut(QKeySequence(Qt.Key_Escape), self,
                        activated=self.hide_panel)
        esc.setContext(Qt.WidgetWithChildrenShortcut)
        self.restyle()

    # ------------------------------------------------------------ state

    def toggle(self, anchor) -> None:
        if self.isVisible():
            self.hide_panel()
        else:
            self.show_panel(anchor)

    def show_panel(self, anchor) -> None:
        self._anchor = anchor
        self.refresh()
        self.reposition()
        self.show()
        self.raise_()
        # Opening the center is reading it: the bell badge clears now,
        # while rows rendered above keep their fresh markers this once.
        try:
            self.repo.feed_mark_all_seen()
        except Exception:
            log.exception("Could not mark feed seen")
        if self._filter is None:
            self._filter = _OutsideClickFilter(self)
            QApplication.instance().installEventFilter(self._filter)
        if self._on_changed is not None:
            self._on_changed()

    def hide_panel(self) -> None:
        if self._filter is not None:
            QApplication.instance().removeEventFilter(self._filter)
            self._filter = None
        self.hide()
        if self._on_changed is not None:
            self._on_changed()

    def press_observed(self, event) -> None:
        """Called by the inert filter for every mouse press while open."""
        if not self.isVisible():
            return
        try:
            gp = event.globalPosition().toPoint()
        except AttributeError:      # Qt5-style event in tests
            gp = event.globalPos()
        inside_panel = self.rect().contains(self.mapFromGlobal(gp))
        anchor = self._anchor
        inside_anchor = bool(
            anchor is not None
            and anchor.rect().contains(anchor.mapFromGlobal(gp)))
        if not inside_panel and not inside_anchor:
            self.hide_panel()

    # ------------------------------------------------------------ data

    def refresh(self) -> None:
        try:
            entries = self.repo.feed_recent(MAX_ROWS)
        except Exception:
            log.exception("Could not read the notification feed")
            entries = []
        self._container.setUpdatesEnabled(False)
        try:
            for w in self._row_widgets:
                self._rows_layout.removeWidget(w)
                w.hide()
                w.deleteLater()
            self._row_widgets = []
            self._empty.setVisible(not entries)
            insert_at = self._rows_layout.indexOf(self._empty) + 1
            for entry in entries:
                row = _FeedRow(entry, self)
                self._rows_layout.insertWidget(insert_at, row)
                insert_at += 1
                self._row_widgets.append(row)
        finally:
            self._container.setUpdatesEnabled(True)

    def refresh_if_visible(self) -> None:
        if self.isVisible():
            self.refresh()
            self.reposition()

    def open_entry(self, chat_guid: str) -> None:
        self.hide_panel()
        if self._on_open_chat is not None and chat_guid:
            self._on_open_chat(chat_guid)

    def hide_item(self, feed_id: int) -> None:
        try:
            self.repo.feed_hide(feed_id)
        except Exception:
            log.exception("Could not hide feed entry %s", feed_id)
        self.refresh()
        if self._on_changed is not None:
            self._on_changed()

    def clear_all(self) -> None:
        try:
            self.repo.feed_clear()
        except Exception:
            log.exception("Could not clear the notification feed")
        self.refresh()
        if self._on_changed is not None:
            self._on_changed()

    # ------------------------------------------------------------ look

    def reposition(self) -> None:
        """Sit just above the bell, hugging the content, clamped inside
        the window."""
        win = self._window
        width = min(theme.dim(380), max(280, win.width() - theme.dim(24)))
        ceiling = min(theme.dim(430), max(220, win.height() - theme.dim(120)))
        wanted = self._container.sizeHint().height() + theme.dim(56)
        height = max(theme.dim(150), min(wanted, ceiling))
        self.resize(width, height)
        anchor = self._anchor
        if anchor is not None:
            top_left = anchor.mapTo(win, QPoint(0, 0))
            x = min(max(theme.dim(10), top_left.x() - theme.dim(40)),
                    win.width() - width - theme.dim(10))
            y = max(theme.dim(10), top_left.y() - height - theme.dim(8))
        else:
            x = theme.dim(10)
            y = win.height() - height - theme.dim(56)
        self.move(x, y)
        self.raise_()

    def restyle(self) -> None:
        """Re-derive every color and dimension from the live theme."""
        self._title.setStyleSheet(
            f"color: {theme.TEXT}; font-size: {theme.fs(9.6)}; "
            "font-weight: 650; background: transparent;")
        self._empty.setStyleSheet(
            f"color: {theme.MUTED}; font-size: {theme.fs(8.8)}; "
            f"padding: {theme.dim(18)}px; background: transparent;")
        self.setStyleSheet(
            f"QFrame#alertCenter {{ background: {theme.PANEL2}; "
            f"border: 1px solid {theme.ACCENT_BORDER}; "
            f"border-radius: {theme.dim(12)}px; }} "
            "QScrollArea { background: transparent; } "
            f"QFrame#feedRow {{ background: transparent; "
            f"border-radius: {theme.dim(8)}px; }} "
            f"QFrame#feedRow:hover {{ background: {theme.HOVER_BG}; }} "
            "QPushButton#feedHide { background: transparent; border: none; "
            f"border-radius: {theme.dim(12)}px; }} "
            f"QPushButton#feedHide:hover {{ background: {theme.SEL_BG}; }}")
        if self.isVisible():
            self.refresh()
            self.reposition()
