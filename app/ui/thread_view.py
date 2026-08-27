"""Conversation view. A windowed list of bubble widgets inside a scroll
area: the newest page loads first, older pages load on demand, new
messages append live. Images render inline; other attachments open with
the system handler on click."""
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QDesktopServices, QPixmap, QTextDocument
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from .. import constants
from ..store import attach_cache
from ..util.textutil import (TAPBACK_EMOJI, human_size,
                             is_emoji_only, linkify)
from ..util.codes import extract_code
from ..util.timefmt import fmt_clock, fmt_day, fmt_receipt, same_day
from . import theme
from .composer import Composer
from .image_viewer import ImageViewer, load_upright
from .icons import arrow_down, download, people, refresh

log = logging.getLogger(__name__)

IMAGE_MIME = ("image/",)


def _is_image(att) -> bool:
    mime = (att["mime_type"] or "")
    name = (att["file_name"] or "").lower()
    return mime.startswith(IMAGE_MIME) or name.endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif"))


def _fit_text_label(label: QLabel, content: str, is_html: bool, limit: int):
    """Size a message label so the width setting is visible on every
    message: short texts render at a floor of roughly half the chosen
    width, long texts wrap at the full width. Qt's rich-text labels left
    to themselves wrap at their own heuristic and ignore both."""
    doc = QTextDocument()
    doc.setDefaultFont(label.font())
    if is_html:
        doc.setHtml(content)
    else:
        doc.setPlainText(content)
    inner = max(140, limit - 26)
    floor = min(inner, max(120, int(limit * 0.45)))
    ideal = int(doc.idealWidth()) + 6
    label.setFixedWidth(max(floor, min(ideal, inner)))


def _open_path(path: str):
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


class _AttachmentTile(QPushButton):
    def __init__(self, att, on_activate, limit: int):
        name = att["file_name"] or "attachment"
        size = human_size(att["total_bytes"])
        super().__init__(f"\U0001F4C4  {name}" + (f"   {size}" if size else ""))
        self.guid = att["guid"]
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            f"QPushButton {{ background: {theme.PANEL2}; border: 1px solid "
            f"{theme.BORDER}; border-radius: 8px; padding: 8px 12px; "
            "text-align: left; }")
        self.setMaximumWidth(max(200, limit - 8))
        self.clicked.connect(lambda: on_activate(self.guid))


def _round_pixmap(pm: QPixmap, radius: int = 14) -> QPixmap:
    """Rounded corners and a hairline edge, composited once."""
    from PySide6.QtGui import QPainter, QPainterPath, QColor
    out = QPixmap(pm.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0.5, 0.5, pm.width() - 1, pm.height() - 1,
                        radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pm)
    p.setClipping(False)
    p.setPen(QColor(theme.BORDER))
    p.drawPath(path)
    p.end()
    return out


class _ImageLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class Bubble(QWidget):
    def __init__(self, msg, sender_name: Optional[str], reply_text: Optional[str],
                 attachments: list, view: "ThreadView"):
        super().__init__()
        self.guid = msg["guid"]
        self.view = view
        self.msg = dict(msg)
        self._limit = view.bubble_limit()
        self._text_label = None
        self._text_html = ""
        self._inferred_read_ts = None
        from_me = bool(msg["is_from_me"])

        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 2, 14, 2)
        col = QVBoxLayout()
        col.setSpacing(2)

        if sender_name:
            s = QLabel(sender_name)
            s.setStyleSheet(f"color: {theme.MUTED}; font-size: {theme.fs(8.6)}; margin-left: 6px;")
            col.addWidget(s)

        if reply_text:
            q = QLabel("\u21A9 " + " ".join(reply_text.split())[:80])
            q.setStyleSheet(
                f"color: {theme.MUTED}; font-size: {theme.fs(9)}; border-left: 2px solid "
                f"{theme.ACCENT_BORDER}; padding-left: 6px; margin-left: 6px;")
            col.addWidget(q)

        self.frame = QFrame()
        self.frame.setMaximumWidth(self._limit)
        if from_me:
            self.frame.setStyleSheet(
                f"QFrame {{ background: {theme.BUBBLE_ME_GRAD}; "
                "border-radius: 14px; } "
                "QLabel { background: transparent; }")
        else:
            self.frame.setStyleSheet(
                f"QFrame {{ background: {theme.BUBBLE_THEM}; "
                "border-radius: 14px; } "
                "QLabel { background: transparent; }")
        fl = QVBoxLayout(self.frame)
        fl.setContentsMargins(12, 8, 12, 8)
        fl.setSpacing(6)

        self.att_widgets: dict[str, QWidget] = {}
        for att in attachments:
            fl.addWidget(self._attachment_widget(att))

        if msg["is_retracted"]:
            t = QLabel("Message unsent")
            t.setStyleSheet(f"color: {theme.MUTED}; font-style: italic;")
            fl.addWidget(t)
        elif msg["text"]:
            t = QLabel()
            t.setTextFormat(Qt.RichText)
            if is_emoji_only(msg["text"]):
                self._text_html = (
                    f'<span style="font-size: {theme.fs(24)};">'
                    f'{msg["text"]}</span>')
            else:
                self._text_html = linkify(msg["text"])
            t.setText(self._text_html)
            t.setWordWrap(True)
            t.setTextInteractionFlags(
                Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
            t.setOpenExternalLinks(True)
            t.setStyleSheet("color: white;" if from_me else f"color: {theme.TEXT};")
            self._text_label = t
            _fit_text_label(t, self._text_html, True, self._limit)
            fl.addWidget(t)
            try:
                _sender = msg["sender_address"]
            except Exception:
                _sender = None
            code = extract_code(msg["text"], _sender)
            if code:
                fl.addWidget(self._code_chip(code, from_me), 0, Qt.AlignLeft)

        self.tap_label = QLabel()
        self.tap_label.setStyleSheet(
            f"background: {theme.PANEL2}; border: 1px solid {theme.BORDER}; "
            f"border-radius: 9px; padding: 1px 7px; font-size: {theme.fs(9)};")
        self.tap_label.hide()

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

        self.meta = QLabel()
        self.meta.setStyleSheet(f"color: {theme.MUTED}; font-size: {theme.fs(8.2)}; "
                                "margin-left: 6px; margin-right: 6px;")
        self.update_meta()

        row = QHBoxLayout()
        row.setSpacing(6)
        if from_me:
            col.addWidget(self.frame, 0, Qt.AlignRight)
            row.addStretch(1)
            row.addWidget(self.tap_label)
            row.addWidget(self.meta)
            col.addLayout(row)
            outer.addStretch(1)
            outer.addLayout(col)
        else:
            col.addWidget(self.frame, 0, Qt.AlignLeft)
            row.addWidget(self.meta)
            row.addWidget(self.tap_label)
            row.addStretch(1)
            col.addLayout(row)
            outer.addLayout(col)
            outer.addStretch(1)

    # ----------------------------------------------------------

    def _image_menu(self, label, pos, guid, att):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(label)
        act_view = menu.addAction("View")
        act_copy = menu.addAction("Copy image")
        act_save = menu.addAction("Save As\u2026")
        chosen = menu.exec(label.mapToGlobal(pos))
        if chosen == act_view:
            self.view.view_image(guid, att)
        elif chosen == act_copy:
            self.view.copy_image(guid, att)
        elif chosen == act_save:
            self.view.save_image_as(guid, att)

    def _context_menu(self, pos):
        from PySide6.QtWidgets import QApplication, QMenu
        menu = QMenu(self)
        act = menu.addAction("Copy message")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen == act:
            text = self.msg.get("text") or ""
            if not text and self.att_widgets:
                text = "(attachment)"
            QApplication.clipboard().setText(text)

    def _scaled_thumb(self, guid: str):
        tp = attach_cache.thumb_path(guid)
        if not tp.exists():
            return None
        pm = QPixmap(str(tp))
        inner = max(140, self._limit - 26)
        if pm.width() > inner:
            pm = pm.scaledToWidth(inner, Qt.SmoothTransformation)
        return _round_pixmap(pm)

    def _attachment_widget(self, att) -> QWidget:
        guid = att["guid"]
        if _is_image(att):
            lbl = _ImageLabel()
            lbl.setCursor(Qt.PointingHandCursor)
            pm = self._scaled_thumb(guid)
            if pm is not None:
                lbl.setPixmap(pm)
            else:
                lbl.setText("Loading image\u2026")
                lbl.setStyleSheet(f"color: {theme.MUTED}; padding: 20px;")
                self.view.request_download(guid, att["file_name"])
            lbl.clicked.connect(
                lambda g=guid, a=att: self.view.view_image(g, a))
            lbl.setContextMenuPolicy(Qt.CustomContextMenu)
            lbl.customContextMenuRequested.connect(
                lambda pos, l=lbl, g=guid, a=att:
                self._image_menu(l, pos, g, a))
            self.att_widgets[guid] = lbl
            return lbl
        tile = _AttachmentTile(
            att, lambda g, a=att: self.view.open_attachment(g, a),
            self._limit)
        self.att_widgets[guid] = tile
        return tile

    def refit(self, limit: int):
        self._limit = limit
        self.frame.setMaximumWidth(limit)
        if self._text_label is not None:
            _fit_text_label(self._text_label, self._text_html, True, limit)
        for guid, w in self.att_widgets.items():
            if isinstance(w, _AttachmentTile):
                w.setMaximumWidth(max(200, limit - 8))
            elif isinstance(w, _ImageLabel):
                pm = self._scaled_thumb(guid)
                if pm is not None:
                    w.setPixmap(pm)

    def attachment_ready(self, guid: str):
        w = self.att_widgets.get(guid)
        if isinstance(w, _ImageLabel):
            pm = self._scaled_thumb(guid)
            if pm is not None:
                w.setStyleSheet("")
                w.setPixmap(pm)

    def set_inferred_read(self, read_ts, anchor_created):
        """Mark this bubble read by inference from the conversation's
        receipt: valid for from-me messages sent at or before the message
        Apple actually stamped."""
        m = self.msg
        if (m["is_from_me"] and not m.get("date_read")
                and not m.get("error")
                and m["date_created"] <= anchor_created):
            if self._inferred_read_ts != read_ts:
                self._inferred_read_ts = read_ts
                self.update_meta()

    def _code_chip(self, code: str, from_me: bool):
        from PySide6.QtWidgets import QApplication, QPushButton
        chip = QPushButton(f"Copy {code}")
        chip.setCursor(Qt.PointingHandCursor)
        chip.setFocusPolicy(Qt.NoFocus)
        if from_me:
            fg, edge = "rgba(255,255,255,0.92)", "rgba(255,255,255,0.45)"
        else:
            fg, edge = theme.ACCENT, theme.ACCENT_BORDER
        chip.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {fg}; "
            f"border: 1px solid {edge}; border-radius: 9px; "
            f"padding: 2px 10px; font-size: {theme.fs(8.6)}; "
            "font-weight: 600; } "
            f"QPushButton:hover {{ border-color: {fg}; }}")

        def do_copy():
            QApplication.clipboard().setText(code)
            chip.setText("Copied \u2713")
            QTimer.singleShot(1200, lambda: chip.setText(f"Copy {code}"))
        chip.clicked.connect(do_copy)
        return chip

    def update_meta(self):
        m = self.msg
        bits = [fmt_clock(m["date_created"])]
        tooltip = f"{fmt_day(m['date_created'])} \u00b7 " \
                  f"{fmt_clock(m['date_created'])}"
        if m["is_from_me"]:
            if m.get("error"):
                bits.append("Not delivered")
            elif m.get("date_read"):
                bits.append(f"Read {fmt_receipt(m['date_read'])}")
            elif self._inferred_read_ts:
                bits.append("Read")
                tooltip += ("\nRead by "
                            f"{fmt_receipt(self._inferred_read_ts)} "
                            "(from the conversation's read receipt)")
            elif m.get("date_delivered"):
                bits.append("Delivered")
        if m.get("is_edited"):
            bits.append("Edited")
        if (m.get("service") or "").upper() in ("SMS", "RCS"):
            bits.append(m["service"].upper())
        self.meta.setText("  \u00b7  ".join(bits))
        self.meta.setToolTip(tooltip)

    def apply_update(self, m: dict):
        self.msg.update({k: m[k] for k in
                         ("date_delivered", "date_read", "is_edited",
                          "is_retracted", "error") if k in m})
        self.update_meta()

    def set_tapbacks(self, pairs: list):
        if not pairs:
            self.tap_label.hide()
            return
        parts = []
        for idx, n in sorted(pairs):
            e = TAPBACK_EMOJI.get(idx, "\u2764\ufe0f")
            parts.append(e if n == 1 else f"{e}{n}")
        self.tap_label.setText(" ".join(parts))
        self.tap_label.show()

    def flash(self):
        base = self.frame.styleSheet()
        self.frame.setStyleSheet(
            base.replace("border-radius: 14px;",
                         f"border-radius: 14px; border: 1px solid {theme.ACCENT};"))
        QTimer.singleShot(1600, lambda: self.frame.setStyleSheet(base))


class OutboxBubble(QWidget):
    def __init__(self, row, on_retry, limit: int):
        super().__init__()
        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 2, 14, 2)
        outer.addStretch(1)
        col = QVBoxLayout()
        failed = row["state"] == "failed"
        frame = QFrame()
        frame.setMaximumWidth(limit)
        frame.setStyleSheet(
            f"QFrame {{ background: {theme.BUBBLE_ME}; border-radius: 14px; "
            + (f"border: 1px solid {theme.FAIL}; " if failed else "opacity: 0.7; ")
            + "} QLabel { background: transparent; color: white; }")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(12, 8, 12, 8)
        if row["attach_path"]:
            from pathlib import Path
            fl.addWidget(QLabel("\U0001F4CE " + Path(row["attach_path"]).name))
        if row["text"]:
            t = QLabel(row["text"])
            t.setWordWrap(True)
            _fit_text_label(t, row["text"], False, limit)
            fl.addWidget(t)
        col.addWidget(frame, 0, Qt.AlignRight)
        meta = QLabel()
        if failed:
            err = row["last_error"] or "Send failed"
            meta.setText(f"{err}. Click to retry.")
            meta.setStyleSheet(f"color: {theme.FAIL}; font-size: {theme.fs(8.6)};")
            meta.setCursor(Qt.PointingHandCursor)
            oid = row["id"]
            meta.mouseReleaseEvent = lambda e, i=oid: on_retry(i)
        else:
            meta.setText("Sending\u2026")
            meta.setStyleSheet(f"color: {theme.MUTED}; font-size: {theme.fs(8.6)};")
        col.addWidget(meta, 0, Qt.AlignRight)
        outer.addLayout(col)


class ThreadView(QWidget):
    send_message = Signal(str, str, list)     # chat_guid, text, files
    need_download = Signal(str, str)          # attachment guid, file name
    retry_outbox = Signal(int)
    refresh_requested = Signal(str)           # chat_guid
    group_details = Signal(str)               # chat_guid

    def __init__(self, repo, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.handles: dict = {}
        self.chat_guid: Optional[str] = None
        self.chat_row = None
        self.oldest_key = None
        self.by_guid: dict[str, Bubble] = {}
        self._outbox_widgets: list[QWidget] = []
        self._open_after_download: set[str] = set()
        self._image_action_after_download: dict[str, str] = {}
        # Per-conversation drafts: unsent composer text and staged
        # files, keyed by chat guid. Without this, switching threads
        # LEAKED a half-typed message into whichever conversation was
        # opened next, the exact way a wrong-recipient send begins.
        self._drafts: dict[str, tuple] = {}
        # New arrivals below the fold while the user reads history.
        self._jump_new = 0
        self.setAcceptDrops(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.header = QWidget()
        self.header.setStyleSheet(f"background: {theme.PANEL};")
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(18, 10, 14, 10)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel("")
        self.title.setTextFormat(Qt.PlainText)
        self.title.setStyleSheet(f"font-size: {theme.fs(12)}; font-weight: 600;")
        self.subtitle = QLabel("")
        self.subtitle.setTextFormat(Qt.PlainText)
        self.subtitle.setStyleSheet(f"color: {theme.MUTED}; font-size: {theme.fs(8.8)};")
        titles.addWidget(self.title)
        self.title_tick = QFrame()
        self.title_tick.setFixedSize(theme.dim(46), max(2, theme.dim(3)))
        self.title_tick.setStyleSheet(
            f"background: {theme.ACCENT}; border-radius: 1px;")
        titles.addWidget(self.title_tick)
        titles.addWidget(self.subtitle)
        hl.addLayout(titles, 1)
        self.people_btn = QPushButton()
        self.people_btn.setObjectName("ghost")
        self.people_btn.setToolTip("Group members")
        self.people_btn.setAccessibleName("Show group members")
        self.people_btn.setCursor(Qt.PointingHandCursor)
        self.people_btn.setIcon(people(theme.ACCENT))
        self.people_btn.setIconSize(
            QSize(theme.dim(20), theme.dim(20)))
        self.people_btn.setFixedSize(theme.dim(36), theme.dim(36))
        self.people_btn.clicked.connect(
            lambda: self.chat_guid and self.group_details.emit(self.chat_guid))
        self.people_btn.hide()
        hl.addWidget(self.people_btn, 0, Qt.AlignVCenter)
        self.export_btn = QPushButton()
        self.export_btn.setObjectName("ghost")
        self.export_btn.setToolTip(
            "Save this conversation as a text file (Ctrl+E)")
        self.export_btn.setAccessibleName("Save conversation transcript")
        self.export_btn.setCursor(Qt.PointingHandCursor)
        self.export_btn.setIcon(download(theme.ACCENT))
        self.export_btn.setIconSize(
            QSize(theme.dim(20), theme.dim(20)))
        self.export_btn.setFixedSize(theme.dim(36), theme.dim(36))
        self.export_btn.clicked.connect(
            lambda: self.export_conversation())
        hl.addWidget(self.export_btn, 0, Qt.AlignVCenter)
        self.refresh_btn = QPushButton()
        self.refresh_btn.setObjectName("ghost")
        self.refresh_btn.setToolTip(
            "Refresh this conversation and check all recent messages (F5)")
        self.refresh_btn.setAccessibleName(
            "Refresh conversation and recent messages")
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.setIcon(refresh(theme.ACCENT))
        self.refresh_btn.setIconSize(
            QSize(theme.dim(20), theme.dim(20)))
        self.refresh_btn.setFixedSize(theme.dim(36), theme.dim(36))
        self.refresh_btn.clicked.connect(
            lambda: self.chat_guid and self.refresh_requested.emit(self.chat_guid))
        hl.addWidget(self.refresh_btn, 0, Qt.AlignVCenter)
        root.addWidget(self.header)

        self.sep = QFrame()
        self.sep.setFixedHeight(1)
        self.sep.setStyleSheet(f"background: {theme.BORDER};")
        root.addWidget(self.sep)

        self._stick_until = 0.0
        self._stick_hard = 0.0
        self._refit_timer = QTimer(self)
        self._refit_timer.setSingleShot(True)
        self._refit_timer.setInterval(140)
        self._refit_timer.timeout.connect(self._refit_all)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.host = QWidget()
        self.vbox = QVBoxLayout(self.host)
        self.vbox.setContentsMargins(0, 8, 0, 8)
        self.vbox.setSpacing(2)
        self.vbox.addStretch(1)
        self.scroll.setWidget(self.host)
        bar = self.scroll.verticalScrollBar()
        bar.rangeChanged.connect(self._on_range_changed)
        bar.sliderPressed.connect(self._break_stick)
        self.scroll.viewport().installEventFilter(self)
        root.addWidget(self.scroll, 1)

        self.older_btn = QPushButton("Load earlier messages")
        self.older_btn.clicked.connect(self.load_older)
        self.older_btn.hide()

        self.comp_sep = QFrame()
        self.comp_sep.setFixedHeight(1)
        self.comp_sep.setStyleSheet(f"background: {theme.BORDER};")
        root.addWidget(self.comp_sep)

        self.composer = Composer()
        self.composer.submit.connect(self._on_submit)
        root.addWidget(self.composer)

        self.jump_btn = QPushButton("Most recent", self)
        self.jump_btn.setCursor(Qt.PointingHandCursor)
        self.jump_btn.setToolTip("Go to the most recent messages")
        self.jump_btn.setAccessibleName("Go to most recent messages")
        self.jump_btn.hide()
        self.jump_btn.clicked.connect(lambda: self._stick(0.4))
        self.setAcceptDrops(True)
        self._style_jump()
        self.scroll.verticalScrollBar().valueChanged.connect(
            self._update_jump)

        self.empty = QLabel("Select a conversation")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet(f"color: {theme.MUTED}; font-size: {theme.fs(12)};")
        self.vbox.addWidget(self.empty)
        self.header.hide()
        self.composer.hide()

    # ------------------------------------------------ loading

    def bubble_limit(self) -> int:
        """The width bubbles may actually use, derived purely from the
        conversation pane: a fixed fraction of the viewport with a
        readability ceiling that scales with the text size. Bubbles
        follow the window on their own; there is no width setting. The
        resize handler below refits every visible bubble when the window
        or splitter changes."""
        vw = self.scroll.viewport().width() or self.width() or 900
        return theme.responsive_bubble_limit(vw)

    def dragEnterEvent(self, e):
        if self.chat_guid and e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls()
                 if u.isLocalFile()]
        self._stage_dropped(paths)
        e.acceptProposedAction()

    def _stage_dropped(self, paths):
        import os as _os
        paths = [p for p in paths if p and _os.path.isfile(p)]
        if paths and self.chat_guid:
            self.composer.stage_files(paths)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refit_timer.start()
        self._update_jump()

    def _refit_all(self):
        limit = self.bubble_limit()
        dead = []
        for guid, b in self.by_guid.items():
            try:
                b.refit(limit)
            except RuntimeError:
                dead.append(guid)
        for guid in dead:
            self.by_guid.pop(guid, None)

    def show_empty(self):
        """Clear the pane back to the select-a-conversation state."""
        self._stash_draft()
        self.chat_guid = None
        self.chat_row = None
        self.oldest_key = None
        self.by_guid.clear()
        self._outbox_widgets.clear()
        self._clear_layout()
        self._set_jump_count(0)
        self.header.hide()
        self.composer.hide()
        self.empty.show()

    def _style_jump(self):
        h = theme.dim(38)
        self.jump_btn.setIcon(arrow_down("#ffffff"))
        self.jump_btn.setIconSize(QSize(theme.dim(17), theme.dim(17)))
        self.jump_btn.setFixedHeight(h)
        self.jump_btn.setMinimumWidth(theme.dim(136))
        self.jump_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.ACCENT}; color: white; "
            f"border: none; border-radius: {h // 2}px; "
            f"padding: 0 {theme.dim(16)}px; "
            f"font-size: {theme.fs(9.2)}; font-weight: 600; }} "
            f"QPushButton:hover {{ background: {theme.ACCENT_DOWN}; }} "
            f"QPushButton:pressed {{ background: {theme.ACCENT_DOWN}; }} "
            "QPushButton:focus { border: 2px solid white; }")
        self.jump_btn.adjustSize()
        self._update_jump()

    def _set_jump_count(self, count: int):
        """The jump pill doubles as an arrivals counter. While the user
        reads history, messages landing below accumulate as '3 new'
        instead of yanking the view down or passing in silence."""
        self._jump_new = max(0, count)
        if self._jump_new:
            label = ("1 new message" if self._jump_new == 1
                     else f"{self._jump_new} new messages")
            self.jump_btn.setText(label)
            self.jump_btn.setToolTip(
                "Go to the most recent messages "
                f"({label.replace(' message', ' arrived message', 1)})")
        else:
            self.jump_btn.setText("Most recent")
            self.jump_btn.setToolTip("Go to the most recent messages")
        self.jump_btn.adjustSize()
        self._update_jump()

    def _update_jump(self, *_):
        show = bool(self.chat_guid) and not self.near_bottom()
        if not show and self._jump_new:
            # Back at the bottom by any route: the count served its
            # purpose. Reset directly (no recursion through the setter).
            self._jump_new = 0
            self.jump_btn.setText("Most recent")
            self.jump_btn.setToolTip("Go to the most recent messages")
            self.jump_btn.adjustSize()
        self.jump_btn.setVisible(show)
        if show:
            x = (self.width() - self.jump_btn.width()) // 2
            y = self.composer.y() - self.jump_btn.height() - theme.dim(10)
            self.jump_btn.move(x, max(0, y))
            self.jump_btn.raise_()

    def restyle(self):
        self.sep.setStyleSheet(f"background: {theme.BORDER};")
        self.comp_sep.setStyleSheet(f"background: {theme.BORDER};")
        self.title_tick.setFixedSize(theme.dim(46), max(2, theme.dim(3)))
        self.title_tick.setStyleSheet(
            f"background: {theme.ACCENT}; border-radius: 1px;")
        self._style_jump()
        self.header.setStyleSheet(f"background: {theme.PANEL};")
        self.title.setStyleSheet(
            f"font-size: {theme.fs(12)}; font-weight: 600;")
        self.subtitle.setStyleSheet(
            f"color: {theme.MUTED}; font-size: {theme.fs(8.8)};")
        self.empty.setStyleSheet(
            f"color: {theme.MUTED}; font-size: {theme.fs(12)};")
        self.refresh_btn.setFixedSize(theme.dim(36), theme.dim(36))
        self.people_btn.setFixedSize(theme.dim(36), theme.dim(36))
        self.export_btn.setFixedSize(theme.dim(36), theme.dim(36))
        action_icon = theme.dim(20)
        self.refresh_btn.setIcon(refresh(theme.ACCENT))
        self.refresh_btn.setIconSize(QSize(action_icon, action_icon))
        self.people_btn.setIcon(people(theme.ACCENT))
        self.people_btn.setIconSize(QSize(action_icon, action_icon))
        self.export_btn.setIcon(download(theme.ACCENT))
        self.export_btn.setIconSize(QSize(action_icon, action_icon))
        self.composer.restyle()

    def set_handles(self, handles: dict):
        self.handles = handles

    def _stash_draft(self):
        """Remember the open conversation's unsent composer content, or
        forget its entry when the composer is effectively empty."""
        if not self.chat_guid:
            return
        text, files = self.composer.draft_state()
        if text.strip() or files:
            self._drafts[self.chat_guid] = (text, files)
        else:
            self._drafts.pop(self.chat_guid, None)

    def load_chat(self, chat_row, focus_guid: Optional[str] = None,
                  preserve_scroll: bool = False):
        same_chat = self.chat_guid == chat_row["guid"]
        if not same_chat:
            self._stash_draft()
        bar_before = self.scroll.verticalScrollBar()
        was_near_bottom = self.near_bottom() if same_chat else True
        distance_from_bottom = (bar_before.maximum() - bar_before.value()
                                if same_chat else 0)
        self.chat_row = chat_row
        self.chat_guid = chat_row["guid"]
        self.by_guid.clear()
        self._outbox_widgets.clear()
        self._clear_layout()
        self.empty.hide()
        self.header.show()
        self.composer.show()

        title = self.repo.chat_title(chat_row, self.handles)
        self.title.setText(title)
        import json as _json
        try:
            parts = _json.loads(chat_row["participants"] or "[]")
        except Exception:
            parts = []
        svc = (self.chat_guid.split(";")[0] if ";" in self.chat_guid else "")
        if chat_row["is_group"]:
            names = ", ".join(self.repo.name_for(p, self.handles) for p in parts)
            self.subtitle.setText(f"{len(parts)} people   \u00b7   {names}")
            self.people_btn.show()
        else:
            self.people_btn.hide()
            self.subtitle.setText((parts[0] if parts else "") +
                                  (f"  \u00b7  {svc}" if svc else ""))

        if focus_guid:
            ts = self.repo.message_ts(focus_guid)
            rows = (self.repo.messages_around(self.chat_guid, ts, 30)
                    if ts else self.repo.messages_window(
                        self.chat_guid, None, constants.PAGE_SIZE))
        else:
            rows = self.repo.messages_window(self.chat_guid, None,
                                             constants.PAGE_SIZE)
        self.oldest_key = ((rows[0]["date_created"], rows[0]["guid"])
                           if rows else None)
        self._render(rows)
        self.older_btn.setVisible(bool(rows) and len(rows) >= 30)
        self._apply_watermark()
        self.refresh_outbox()
        if not same_chat:
            # A different conversation: its own draft (possibly none)
            # replaces whatever was in the composer, and the jump pill
            # starts counting from zero.
            self._set_jump_count(0)
            draft_text, draft_files = self._drafts.get(
                self.chat_guid, ("", []))
            self.composer.restore_draft(draft_text, draft_files)
        if preserve_scroll and same_chat and not was_near_bottom:
            self._stick_until = 0.0
            def restore_position():
                bar = self.scroll.verticalScrollBar()
                bar.setValue(max(0, bar.maximum() - distance_from_bottom))
            QTimer.singleShot(0, restore_position)
            QTimer.singleShot(80, restore_position)
        elif focus_guid and focus_guid in self.by_guid:
            self._stick_until = 0.0
            QTimer.singleShot(60, lambda: self._focus(focus_guid))
        else:
            self._stick(1.2)
        if not preserve_scroll:
            self.composer.focus()

    def refresh_from_repo(self, preserve_scroll: bool = True):
        """Rebuild the open thread from committed state without reopening it.

        A rebuild is intentionally used for live changes: it is less clever
        than patching individual widgets, but it cannot leave an out-of-order,
        edited, retracted, or newly-attached message stale until restart.
        """
        if not self.chat_guid:
            return
        row = self.repo.db.one("SELECT * FROM chats WHERE guid=?",
                               (self.chat_guid,))
        if row is not None:
            self.load_chat(row, preserve_scroll=preserve_scroll)

    def _clear_layout(self):
        # The empty-state label and the load-earlier button live for the
        # lifetime of the view: pull them out of the layout but never
        # destroy them. Everything else (bubbles, separators, outbox) dies.
        #
        # Rule (3.1.2, after the Windows ghost-window storms): a dying
        # widget is hidden and deleted WITHOUT ever being orphaned. Calling
        # setParent(None) turns a widget into a top-level window object,
        # and on Windows a reparent/teardown race can flash such orphans as
        # bare white frames. Keeping the host as parent until deleteLater
        # runs makes that flash structurally impossible.
        keep = (self.empty, self.older_btn)
        while self.vbox.count() > 1:
            item = self.vbox.takeAt(1)
            w = item.widget()
            if w is None:
                continue
            if w in keep:
                w.setParent(self.host)
                w.hide()
            else:
                w.hide()
                w.deleteLater()

    def _render(self, rows, prepend=False):
        guids = [r["guid"] for r in rows]
        atts = self.repo.attachments_for(guids)
        taps = self.repo.tapbacks_for(guids)
        # Build and insert as one batch with painting paused. A large
        # conversation would otherwise relayout and repaint once per
        # bubble, which is exactly the churn that saturated the event loop
        # during hide/unhide storms; one batched pass paints once.
        self.host.setUpdatesEnabled(False)
        try:
            widgets = []
            prev_ts = None
            for r in rows:
                if prev_ts is None or not same_day(prev_ts, r["date_created"]):
                    widgets.append(self._day_sep(r["date_created"]))
                prev_ts = r["date_created"]
                widgets.append(self._make_bubble(r, atts.get(r["guid"], []),
                                                 taps.get(r["guid"], [])))
            insert_at = 1
            if prepend:
                for w in reversed(widgets):
                    self.vbox.insertWidget(insert_at, w)
            else:
                for w in widgets:
                    self.vbox.insertWidget(self.vbox.count(), w)
            # keep the load-earlier button pinned to the top
            self.vbox.insertWidget(1, self.older_btn)
        finally:
            self.host.setUpdatesEnabled(True)
            self.host.update()

    def _make_bubble(self, r, attachments, taps) -> Bubble:
        sender = None
        if self.chat_row is not None and self.chat_row["is_group"] and not r["is_from_me"]:
            sender = self.repo.name_for(r["sender_address"], self.handles)
        reply = None
        if r["thread_originator_guid"]:
            reply = self.repo.message_text(r["thread_originator_guid"])
        b = Bubble(r, sender, reply, attachments, self)
        b.set_tapbacks(taps)
        self.by_guid[r["guid"]] = b
        return b

    def _day_sep(self, ts) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 10, 0, 10)
        lbl = QLabel()
        lbl.setTextFormat(Qt.RichText)
        lbl.setText(f'<span style="color:{theme.ACCENT};">\u2022</span>'
                    f'&nbsp;&nbsp;{fmt_day(ts)}')
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(
            f"color: {theme.MUTED}; font-size: {theme.fs(8.6)}; "
            f"background: {theme.PANEL}; border: 1px solid {theme.BORDER}; "
            "border-radius: 9px; padding: 2px 12px;")
        lay.addStretch(1)
        lay.addWidget(lbl)
        lay.addStretch(1)
        return wrap

    def load_older(self):
        if not self.chat_guid or self.oldest_key is None:
            return
        self._stick_until = 0.0
        rows = self.repo.messages_window(self.chat_guid, self.oldest_key,
                                         constants.PAGE_SIZE)
        if not rows:
            self.older_btn.hide()
            return
        bar = self.scroll.verticalScrollBar()
        old_max, old_val = bar.maximum(), bar.value()
        self.oldest_key = (rows[0]["date_created"], rows[0]["guid"])
        self._render(rows, prepend=True)

        def restore():
            bar2 = self.scroll.verticalScrollBar()
            bar2.setValue(bar2.maximum() - old_max + old_val)
        QTimer.singleShot(0, restore)

    # ------------------------------------------------ live updates

    def _stick(self, seconds: float = 1.2, hard: float = 4.0):
        """Pin the view to the newest message. The pin renews itself each
        time content grows, releases after the layout goes quiet, and a
        hard ceiling guarantees it can never hold forever."""
        self._set_jump_count(0)
        now = time.monotonic()
        self._stick_until = now + seconds
        self._stick_hard = now + max(seconds, hard)
        self.scroll_bottom()
        for delay in (0, 200, 550):
            QTimer.singleShot(delay, self._pin_now)

    def _pin_now(self):
        if time.monotonic() < self._stick_until:
            self.scroll_bottom()

    def _break_stick(self, *_):
        self._stick_until = 0.0

    def eventFilter(self, obj, e):
        if (obj is self.scroll.viewport() and e.type() == QEvent.Wheel
                and e.angleDelta().y() > 0):
            self._break_stick()
        return super().eventFilter(obj, e)

    def _on_range_changed(self, _mn, mx):
        now = time.monotonic()
        if now < self._stick_until:
            self.scroll.verticalScrollBar().setValue(mx)
            self._stick_until = min(self._stick_hard, now + 0.45)

    def near_bottom(self) -> bool:
        bar = self.scroll.verticalScrollBar()
        return bar.maximum() - bar.value() < 220

    def scroll_bottom(self):
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def apply_message(self, m: dict):
        if m["chat_guid"] != self.chat_guid:
            return True
        at = m.get("associated_type")
        if at and 1000 <= at <= 3999:
            target = self.by_guid.get(m.get("associated_guid") or "")
            if target:
                taps = self.repo.tapbacks_for([target.guid])
                target.set_tapbacks(taps.get(target.guid, []))
            return True
        if m.get("item_type"):
            return True
        if m["guid"] in self.by_guid:
            self.by_guid[m["guid"]].apply_update(m)
            return True
        stick = self.near_bottom()
        row = self.repo.db.one("SELECT * FROM messages WHERE guid=?", (m["guid"],))
        if row is None:
            return True
        prev = self._last_bubble_ts()
        if prev is not None and row["date_created"] < prev:
            return False  # caller must perform a scroll-preserving rebuild
        atts = self.repo.attachments_for([m["guid"]]).get(m["guid"], [])
        if prev is None or not same_day(prev, row["date_created"]):
            self.vbox.insertWidget(self.vbox.count(),
                                   self._day_sep(row["date_created"]))
        self.vbox.insertWidget(self.vbox.count(), self._make_bubble(row, atts, []))
        self.refresh_outbox()
        if stick or m["is_from_me"]:
            QTimer.singleShot(30, self.scroll_bottom)
        else:
            # Reading history while a message lands below: count it on
            # the jump pill instead of moving the view or saying nothing.
            self._set_jump_count(self._jump_new + 1)
        return True

    def _last_bubble_ts(self):
        latest = None
        for b in self.by_guid.values():
            ts = b.msg["date_created"]
            latest = ts if latest is None else max(latest, ts)
        return latest

    def update_message(self, m: dict):
        b = self.by_guid.get(m["guid"])
        if b:
            b.apply_update(m)
        else:
            self.apply_message(m)
        if m.get("date_read"):
            self._apply_watermark()

    def _apply_watermark(self):
        if not self.chat_guid:
            return
        wm = self.repo.read_watermark(self.chat_guid)
        if not wm:
            return
        read_ts, anchor = wm
        for b in self.by_guid.values():
            try:
                b.set_inferred_read(read_ts, anchor)
            except RuntimeError:
                pass

    # ------------------------------------------------ outbox

    def refresh_outbox(self):
        for w in self._outbox_widgets:
            # Hide and delete in place; never orphan a visible widget into a
            # top-level window object (see _clear_layout).
            w.hide()
            w.deleteLater()
        self._outbox_widgets.clear()
        if not self.chat_guid:
            return
        for row in self.repo.outbox_pending(self.chat_guid):
            w = OutboxBubble(row, self.retry_outbox.emit,
                             self.bubble_limit())
            self._outbox_widgets.append(w)
            self.vbox.insertWidget(self.vbox.count(), w)
        if self._outbox_widgets:
            QTimer.singleShot(30, self.scroll_bottom)

    # ------------------------------------------------ attachments

    def request_download(self, guid: str, file_name: str):
        self.need_download.emit(guid, file_name or "attachment")

    def open_attachment(self, guid: str, att):
        row = self.repo.attachment(guid) or att
        path = row["local_path"] if row and row["local_path"] else None
        if path and os.path.exists(path):
            _open_path(path)
            return
        self._open_after_download.add(guid)
        self.request_download(guid, row["file_name"] if row else "attachment")

    def view_image(self, guid: str, att):
        path = self._attachment_local_path(guid, att)
        if path:
            viewer = ImageViewer(Path(path), self)
            viewer.exec()
            return
        self._image_action_after_download[guid] = "view"
        self.request_download(guid, att["file_name"] or "image")

    def copy_image(self, guid: str, att):
        path = self._attachment_local_path(guid, att)
        if path:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setPixmap(load_upright(Path(path)))
            return
        self._image_action_after_download[guid] = "copy"
        self.request_download(guid, att["file_name"] or "image")

    def save_image_as(self, guid: str, att):
        path = self._attachment_local_path(guid, att)
        if path:
            self._save_image_path(Path(path))
            return
        self._image_action_after_download[guid] = "save"
        self.request_download(guid, att["file_name"] or "image")

    def _attachment_local_path(self, guid: str, att):
        row = self.repo.attachment(guid) or att
        path = row["local_path"] if row and row["local_path"] else None
        return path if path and os.path.exists(path) else None

    def _save_image_path(self, source: Path):
        from PySide6.QtWidgets import QFileDialog
        dest, _kind = QFileDialog.getSaveFileName(
            self, "Save image as", str(Path.home() / source.name))
        if dest:
            try:
                from shutil import copy2
                copy2(source, dest)
            except Exception:
                log.exception("Save image failed")

    @Slot(str, str)
    def on_attachment_ready(self, guid: str, path: str):
        keep_bottom = self.near_bottom()
        b = None
        for bub in self.by_guid.values():
            if guid in bub.att_widgets:
                b = bub
                break
        if b:
            b.attachment_ready(guid)
        if guid in self._open_after_download:
            self._open_after_download.discard(guid)
            _open_path(path)
        action = self._image_action_after_download.pop(guid, None)
        if action == "view":
            ImageViewer(Path(path), self).exec()
        elif action == "copy":
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setPixmap(load_upright(Path(path)))
        elif action == "save":
            self._save_image_path(Path(path))
        if keep_bottom:
            self._stick(0.6)

    # ------------------------------------------------ input

    def _on_submit(self, text: str, files: list):
        if self.chat_guid:
            # Sent content is no longer a draft.
            self._drafts.pop(self.chat_guid, None)
            self.send_message.emit(self.chat_guid, text, files)

    # ------------------------------------------------ transcript

    def export_conversation(self, path: Optional[str] = None):
        """Write the whole open conversation to a plain text file.

        Plain text on purpose: a transcript that opens anywhere, prints
        cleanly, and drops into a matter file without a special viewer.
        Returns the path written, or None when nothing was open or the
        save dialog was cancelled."""
        if not self.chat_guid or self.chat_row is None:
            return None
        title = self.repo.chat_title(self.chat_row, self.handles)
        if path is None:
            safe = "".join(c if (c.isalnum() or c in " -_.") else "_"
                           for c in title).strip() or "conversation"
            suggested = f"{safe} transcript.txt"
            path, _ = QFileDialog.getSaveFileName(
                self, "Save conversation as text", suggested,
                "Text files (*.txt)")
            if not path:
                return None
        rows: list = []
        before_key = None
        while True:
            page = self.repo.messages_window(self.chat_guid,
                                             before_key, 500)
            if not page:
                break
            rows = page + rows
            before_key = (page[0]["date_created"], page[0]["guid"])
            if len(page) < 500:
                break
        atts = self.repo.attachments_for([r["guid"] for r in rows])
        lines = [title,
                 f"Exported {datetime.now().strftime('%Y-%m-%d %H:%M')} "
                 f"from JRL Messages ({len(rows)} messages)",
                 ""]
        for r in rows:
            stamp = datetime.fromtimestamp(
                (r["date_created"] or 0) / 1000).strftime("%Y-%m-%d %H:%M")
            sender = ("Me" if r["is_from_me"]
                      else self.repo.name_for(r["sender_address"],
                                              self.handles))
            text = (r["text"] or "").strip()
            if r["is_retracted"]:
                text = "[message retracted]"
            elif r["is_edited"] and text:
                text += "  [edited]"
            row_atts = atts.get(r["guid"], [])
            if text:
                lines.append(f"[{stamp}] {sender}: {text}")
            elif row_atts:
                lines.append(f"[{stamp}] {sender}:")
            else:
                lines.append(f"[{stamp}] {sender}: [no text]")
            for att in row_atts:
                keys = att.keys() if hasattr(att, "keys") else []
                name = ("file_name" in keys and att["file_name"]) or "file"
                lines.append(f"    [attachment: {name}]")
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info("Transcript exported: %d messages", len(rows))
        return str(path)

    def _focus(self, guid: str):
        b = self.by_guid.get(guid)
        if not b:
            return
        self.scroll.ensureWidgetVisible(b, 0, 120)
        b.flash()
