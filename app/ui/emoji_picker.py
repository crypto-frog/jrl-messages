# -*- coding: utf-8 -*-
"""Emoji picker. Flat purpose-built cells (no button chrome, no
inherited padding, no clipping): the emoji is drawn full size on a
clean field with an accent-tinted hover. Search filters live, Enter
takes the first match, clicks insert and the panel stays open, recents
persist in their own small file."""
import json
import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QScrollArea, QVBoxLayout, QWidget)

from .. import constants
from . import emoji_data, theme

log = logging.getLogger(__name__)

_RECENT_PATH = constants.DATA_DIR / "emoji_recent.json"
_RECENT_MAX = 16
_COLS = 8


def _load_recent() -> list:
    try:
        data = json.loads(_RECENT_PATH.read_text(encoding="utf-8"))
        return [c for c in data if isinstance(c, str)][:_RECENT_MAX]
    except Exception:
        return []


def _save_recent(chars: list):
    try:
        _RECENT_PATH.write_text(json.dumps(chars[:_RECENT_MAX]),
                                encoding="utf-8")
    except Exception:
        log.exception("Could not save emoji recents")


def record_recent(char: str):
    chars = _load_recent()
    if char in chars:
        chars.remove(char)
    chars.insert(0, char)
    _save_recent(chars)


class _EmojiCell(QLabel):
    """A single emoji tile: full-size glyph, zero chrome, soft hover."""

    def __init__(self, char: str, name: str, on_pick, font: QFont):
        super().__init__(char)
        self._on_pick = on_pick
        self.setFont(font)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(theme.dim(40), theme.dim(40))
        self.setCursor(Qt.PointingHandCursor)
        if name:
            self.setToolTip(name)
        self._style(False)

    def _style(self, hover: bool):
        bg = theme.SEL_BG if hover else "transparent"
        self.setStyleSheet(
            f"background: {bg}; border-radius: 10px; padding: 0px;")

    def enterEvent(self, e):
        self._style(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._style(False)
        super().leaveEvent(e)

    def activate(self):
        self._on_pick(self.text())

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.activate()
        super().mouseReleaseEvent(e)


class EmojiPicker(QWidget):
    picked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setFixedSize(theme.dim(384), theme.dim(420))
        self.setStyleSheet(
            f"EmojiPicker {{ background: {theme.PANEL2}; border: 1px solid "
            f"{theme.BORDER}; border-radius: 14px; }}")

        self._emoji_font = QFont("Segoe UI Emoji")
        self._emoji_font.setPointSizeF(15.0 * theme.scale())

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search emoji")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._pick_first)
        root.addWidget(self.search)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        self.body_lay = QVBoxLayout(body)
        self.body_lay.setContentsMargins(0, 0, 6, 0)
        self.body_lay.setSpacing(4)
        self.scroll.setWidget(body)
        root.addWidget(self.scroll, 1)

        # recents
        self.recent_header = self._header("Recent")
        self.body_lay.addWidget(self.recent_header)
        self.recent_host = QWidget()
        self.recent_lay = QHBoxLayout(self.recent_host)
        self.recent_lay.setContentsMargins(0, 0, 0, 0)
        self.recent_lay.setSpacing(4)
        self.recent_lay.addStretch(1)
        self.body_lay.addWidget(self.recent_host)

        # sections, built once; search toggles visibility
        self._buttons = []          # (cell, keywords, section header)
        self._headers = []
        for cat_name, entries in emoji_data.CATEGORIES:
            header = self._header(cat_name)
            self._headers.append(header)
            self.body_lay.addWidget(header)
            grid_host = QWidget()
            grid = QGridLayout(grid_host)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(4)
            for i, (char, words) in enumerate(entries):
                cell = _EmojiCell(char, words.split()[0] if words else "",
                                  self._pick, self._emoji_font)
                grid.addWidget(cell, i // _COLS, i % _COLS)
                self._buttons.append((cell, words, header))
            self.body_lay.addWidget(grid_host)

        self.no_results = QLabel("No emoji found")
        self.no_results.setAlignment(Qt.AlignCenter)
        self.no_results.setStyleSheet(
            f"color: {theme.MUTED}; font-size: {theme.fs(9.4)}; "
            "margin-top: 24px;")
        self.no_results.hide()
        self.body_lay.addWidget(self.no_results)
        self.body_lay.addStretch(1)

        self._reload_recent()
        self.search.setFocus()

    # ------------------------------------------------ pieces

    def _header(self, text: str) -> QLabel:
        lbl = QLabel()
        lbl.setTextFormat(Qt.RichText)
        lbl.setText(
            f'<span style="color: {theme.ACCENT}; '
            f'font-size: {theme.fs(6.5)};">\u25CF</span>'
            f'&nbsp;&nbsp;<span style="font-weight: 600;">'
            f'{text.upper()}</span>')
        lbl.setStyleSheet(
            f"color: {theme.MUTED}; font-size: {theme.fs(8.2)}; "
            "margin-top: 8px;")
        return lbl

    def _reload_recent(self):
        while self.recent_lay.count() > 1:
            item = self.recent_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        chars = _load_recent()
        show = bool(chars) and not self.search.text().strip()
        self.recent_header.setVisible(show)
        self.recent_host.setVisible(show)
        if show:
            lookup = dict(emoji_data.ALL)
            for char in chars[:_COLS]:
                name = (lookup.get(char, "") or "").split()
                cell = _EmojiCell(char, name[0] if name else "",
                                  self._pick, self._emoji_font)
                self.recent_lay.insertWidget(self.recent_lay.count() - 1,
                                             cell)

    # ------------------------------------------------ behavior

    def _filter(self, text: str):
        q = (text or "").strip().lower()
        self._reload_recent()
        if not q:
            for cell, _w, _h in self._buttons:
                cell.setVisible(True)
            for h in self._headers:
                h.setVisible(True)
            self.no_results.hide()
            return
        visible_by_header = {h: 0 for h in self._headers}
        any_hit = False
        for cell, words, header in self._buttons:
            hit = (q in words) or (q == cell.text())
            cell.setVisible(hit)
            if hit:
                any_hit = True
                visible_by_header[header] += 1
        for h in self._headers:
            h.setVisible(visible_by_header[h] > 0)
        self.no_results.setVisible(not any_hit)
        self.scroll.verticalScrollBar().setValue(0)

    def _pick_first(self):
        results = emoji_data.search(self.search.text())
        if results:
            self._pick(results[0][0])

    def _pick(self, char: str):
        record_recent(char)
        self.picked.emit(char)
        if not self.search.text().strip():
            self._reload_recent()

    def open_at(self, global_pos):
        self.move(global_pos)
        self.show()
        self.search.setFocus()
        self.search.selectAll()
