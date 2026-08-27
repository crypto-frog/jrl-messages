"""Message composer: growing text input, staged-file chips, attach button.
Enter sends, Shift+Enter inserts a newline. Files arrive from the picker,
drag and drop, or Ctrl+V of a clipboard image."""
import time
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .. import constants
from ..util.textutil import human_size
from . import theme
from .icons import arrow_up, plus, smiley

BIG_FILE = 100 * 1024 * 1024


class _Edit(QPlainTextEdit):
    send_requested = Signal()
    files_pasted = Signal(list)

    def __init__(self):
        super().__init__()
        self.setPlaceholderText("Message")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(theme.dim(40))
        self.textChanged.connect(self._grow)

    def _grow(self):
        fm = self.fontMetrics()
        lines = max(1, int(self.document().size().height()))
        h = lines * fm.lineSpacing() + theme.dim(22)
        self.setFixedHeight(max(theme.dim(40), min(h, theme.dim(160))))

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (
                e.modifiers() & Qt.ShiftModifier):
            self.send_requested.emit()
            return
        super().keyPressEvent(e)

    def insertFromMimeData(self, source):
        if source.hasImage():
            img = source.imageData()
            p = constants.CACHE_DIR / f"pasted-{int(time.time() * 1000)}.png"
            try:
                img.save(str(p), "PNG")
                self.files_pasted.emit([str(p)])
                return
            except Exception:
                pass
        if source.hasUrls():
            paths = [u.toLocalFile() for u in source.urls() if u.isLocalFile()]
            if paths:
                self.files_pasted.emit(paths)
                return
        super().insertFromMimeData(source)


class _Chip(QFrame):
    removed = Signal(str)

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        p = Path(path)
        self.setStyleSheet(
            f"QFrame {{ background: {theme.PANEL2}; border: 1px solid {theme.BORDER};"
            "border-radius: 8px; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 4, 4)
        lay.setSpacing(6)
        size = ""
        try:
            size = human_size(p.stat().st_size)
        except OSError:
            pass
        label = QLabel(f"\U0001F4CE {p.name}" + (f"  \u00b7 {size}" if size else ""))
        label.setStyleSheet("border: none;")
        lay.addWidget(label)
        self.close_btn = QPushButton("\u2715")
        self.close_btn.setObjectName("ghost")
        self.close_btn.setToolTip(f"Remove {p.name}")
        self.close_btn.setAccessibleName(f"Remove attachment {p.name}")
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.clicked.connect(lambda: self.removed.emit(self.path))
        lay.addWidget(self.close_btn)
        self.restyle()

    def restyle(self):
        self.close_btn.setFixedSize(theme.dim(28), theme.dim(28))


class Composer(QWidget):
    submit = Signal(str, list)   # text, [file paths]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.files: list[str] = []

        root = QVBoxLayout(self)
        self.root_layout = root
        root.setContentsMargins(14, 6, 14, 12)
        root.setSpacing(6)

        self.chips_area = QScrollArea()
        self.chips_area.setWidgetResizable(True)
        self.chips_area.setFixedHeight(theme.dim(44))
        self.chips_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.chips_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chips_host = QWidget()
        self.chips_lay = QHBoxLayout(self.chips_host)
        self.chips_lay.setContentsMargins(0, 0, 0, 0)
        self.chips_lay.setSpacing(6)
        self.chips_lay.addStretch(1)
        self.chips_area.setWidget(self.chips_host)
        self.chips_area.hide()
        root.addWidget(self.chips_area)

        row = QHBoxLayout()
        self.action_row = row
        row.setSpacing(8)
        self.attach_btn = QPushButton()
        self.attach_btn.setToolTip("Attach files")
        self.attach_btn.setAccessibleName("Attach files")
        self.attach_btn.setCursor(Qt.PointingHandCursor)
        self.attach_btn.clicked.connect(self._pick)
        row.addWidget(self.attach_btn, 0, Qt.AlignBottom)

        self.emoji_btn = QPushButton()
        self.emoji_btn.setToolTip("Emoji")
        self.emoji_btn.setAccessibleName("Open emoji picker")
        self.emoji_btn.setCursor(Qt.PointingHandCursor)
        self.emoji_btn.setFocusPolicy(Qt.StrongFocus)
        self.emoji_btn.clicked.connect(self._open_emoji)
        self._picker = None
        row.addWidget(self.emoji_btn, 0, Qt.AlignBottom)

        self.edit = _Edit()
        self.edit.send_requested.connect(self._send)
        self.edit.files_pasted.connect(self.stage_files)
        row.addWidget(self.edit, 1)

        self.send_btn = QPushButton()
        self.send_btn.setIcon(arrow_up("#ffffff"))
        self.send_btn.setToolTip("Send message")
        self.send_btn.setAccessibleName("Send message")
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self._style_send()
        self.send_btn.clicked.connect(self._send)
        row.addWidget(self.send_btn, 0, Qt.AlignBottom)
        root.addLayout(row)
        self.edit.textChanged.connect(self._update_send_enabled)
        self._update_send_enabled()
        self.restyle()

    # ----------------------------------------------------------

    def _pick(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach files")
        self.stage_files(paths)

    def stage_files(self, paths: list, confirm_large: bool = True):
        for p in paths or []:
            path = str(p)
            if not path or path in self.files or not Path(path).is_file():
                continue
            try:
                if (confirm_large
                        and Path(path).stat().st_size > BIG_FILE):
                    ok = QMessageBox.question(
                        self, "Large file",
                        f"{Path(path).name} is over 100 MB and may fail to send. "
                        "Attach anyway?") == QMessageBox.Yes
                    if not ok:
                        continue
            except OSError:
                continue
            self.files.append(path)
            chip = _Chip(path)
            chip.removed.connect(self._remove)
            self.chips_lay.insertWidget(self.chips_lay.count() - 1, chip)
        self.chips_area.setVisible(bool(self.files))
        self._update_send_enabled()

    def _remove(self, path: str):
        self.files = [f for f in self.files if f != path]
        for i in reversed(range(self.chips_lay.count())):
            w = self.chips_lay.itemAt(i).widget()
            if isinstance(w, _Chip) and w.path == path:
                # Hide and delete in place; never orphan a visible widget
                # into a top-level window object.
                self.chips_lay.removeWidget(w)
                w.hide()
                w.deleteLater()
        self.chips_area.setVisible(bool(self.files))
        self._update_send_enabled()

    def _open_emoji(self):
        from .emoji_picker import EmojiPicker
        if self._picker is None:
            self._picker = EmojiPicker(self)
            self._picker.picked.connect(self._insert_emoji)
        p = self._picker
        pos = self.emoji_btn.mapToGlobal(self.emoji_btn.rect().topLeft())
        pos.setY(pos.y() - p.height() - theme.dim(8))
        pos.setX(max(0, pos.x() - theme.dim(12)))
        p.open_at(pos)

    def _insert_emoji(self, char: str):
        self.edit.insertPlainText(char)
        self.edit.setFocus()

    def _update_send_enabled(self):
        self.send_btn.setEnabled(
            bool(self.edit.toPlainText().strip()) or bool(self.files))

    def _send(self):
        text = self.edit.toPlainText().strip()
        if not text and not self.files:
            return
        files = list(self.files)
        self.edit.clear()
        for f in files:
            self._remove(f)
        self.submit.emit(text, files)

    def focus(self):
        self.edit.setFocus()

    # ------------------------------------------------ drafts

    def draft_state(self) -> tuple:
        """The unsent content, for per-conversation draft keeping."""
        return self.edit.toPlainText(), list(self.files)

    def clear_content(self):
        """Empty the text and every staged file chip, in place."""
        self.edit.clear()
        for f in list(self.files):
            self._remove(f)

    def restore_draft(self, text: str, files: list):
        """Replace the composer's content with a stored draft, or clear
        it when the draft is empty. The large-file confirmation is
        skipped on restore: these files were approved when first
        staged, and a question box on a mere conversation switch would
        be baffling. A staged file deleted from disk since then simply
        drops out (stage_files verifies existence)."""
        self.clear_content()
        if text:
            self.edit.setPlainText(text)
            cursor = self.edit.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.edit.setTextCursor(cursor)
        if files:
            self.stage_files(files, confirm_large=False)
        self._update_send_enabled()

    def _style_send(self):
        """One owner for the send button's geometry and paint: the radius
        is computed from the true pixel size every time, so the circle
        cannot degrade into a square at any text scale."""
        d = theme.dim(38)
        self.send_btn.setFixedSize(d, d)
        self.send_btn.setIconSize(QSize(theme.dim(19), theme.dim(19)))
        self.send_btn.setIcon(arrow_up("#ffffff"))
        self.send_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.ACCENT}; border: none; "
            f"border-radius: {d // 2}px; }} "
            f"QPushButton:hover {{ background: {theme.ACCENT_DOWN}; }} "
            f"QPushButton:pressed {{ background: {theme.ACCENT_DOWN}; }} "
            f"QPushButton:disabled {{ background: {theme.SEL_BG}; }} "
            "QPushButton:focus { border: 2px solid white; }")

    def _style_round_icon(self, btn: QPushButton):
        """Large, legible circular action button. The icon is 22/38 of the
        control and every dimension goes through theme.dim, so both grow
        with the chosen text size instead of staying stamp-sized."""
        d = theme.dim(38)
        btn.setFixedSize(d, d)
        btn.setIconSize(QSize(theme.dim(22), theme.dim(22)))
        btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; "
            f"border-radius: {d // 2}px; }} "
            f"QPushButton:hover {{ background: {theme.HOVER_BG}; }} "
            f"QPushButton:pressed {{ background: {theme.SEL_BG}; }} "
            f"QPushButton:focus {{ background: {theme.HOVER_BG}; "
            f"border: 1px solid {theme.ACCENT}; }}")

    def restyle(self):
        """Re-measure every fixed dimension after a text-size or accent
        change; fonts update via the stylesheet, these must follow."""
        self.root_layout.setContentsMargins(
            theme.dim(14), theme.dim(6), theme.dim(14), theme.dim(12))
        self.root_layout.setSpacing(theme.dim(6))
        self.action_row.setSpacing(theme.dim(8))
        self.chips_lay.setSpacing(theme.dim(6))
        self.chips_area.setFixedHeight(theme.dim(44))
        self._style_send()
        self._style_round_icon(self.attach_btn)
        self.attach_btn.setIcon(plus(theme.ACCENT))
        self._style_round_icon(self.emoji_btn)
        self.emoji_btn.setIcon(smiley(theme.ACCENT))
        for i in range(self.chips_lay.count()):
            chip = self.chips_lay.itemAt(i).widget()
            if isinstance(chip, _Chip):
                chip.restyle()
        self._picker = None   # rebuilt at next open with the new scale
        QTimer.singleShot(0, self.edit._grow)
