"""Built-in image viewer: the photo large on a dark backdrop with Copy,
Save As, and Open. Esc or a click on the image dismisses. Reads the
original file (EXIF orientation honored), falling back to the cached
thumbnail if the original is not downloaded yet."""
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import (QDesktopServices, QGuiApplication, QImage,
                           QKeySequence, QPixmap, QShortcut)
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog,
                               QHBoxLayout, QLabel, QPushButton, QVBoxLayout)

from . import theme

log = logging.getLogger(__name__)


def load_upright(path: Path) -> QPixmap:
    """Load an image with EXIF orientation applied."""
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGBA")
            qimg = QImage(im.tobytes("raw", "RGBA"), im.width, im.height,
                          QImage.Format_RGBA8888)
            return QPixmap.fromImage(qimg.copy())
    except Exception:
        return QPixmap(str(path))


class ImageViewer(QDialog):
    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.setWindowTitle(self.path.name)
        self.setWindowModality(Qt.WindowModal)
        self.setStyleSheet("QDialog { background: #0b0d11; }")

        self._pix = load_upright(self.path)

        screen = (parent.screen() if parent is not None
                  else QGuiApplication.primaryScreen())
        avail = screen.availableGeometry()
        max_w, max_h = int(avail.width() * 0.82), int(avail.height() * 0.82)

        shown = self._pix
        if shown.width() > max_w or shown.height() > max_h - 60:
            shown = self._pix.scaled(max_w, max_h - 60, Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 10)
        root.setSpacing(10)

        self.img = QLabel()
        self.img.setPixmap(shown)
        self.img.setAlignment(Qt.AlignCenter)
        self.img.setCursor(Qt.PointingHandCursor)
        self.img.setToolTip("Click to close")
        self.img.mouseReleaseEvent = lambda e: self.accept()
        root.addWidget(self.img, 1, Qt.AlignCenter)

        bar = QHBoxLayout()
        name = QLabel(self.path.name)
        name.setStyleSheet(f"color: {theme.MUTED}; "
                           f"font-size: {theme.fs(8.8)};")
        bar.addWidget(name, 1)
        for label, fn in (("Copy", self._copy),
                          ("Save As\u2026", self._save_as),
                          ("Open", self._open_external)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            bar.addWidget(b)
        close = QPushButton("Close")
        close.setObjectName("accent")
        close.clicked.connect(self.accept)
        bar.addWidget(close)
        root.addLayout(bar)

        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.accept)
        self.adjustSize()

    def _copy(self):
        QApplication.clipboard().setPixmap(self._pix)

    def _save_as(self):
        dest, _f = QFileDialog.getSaveFileName(
            self, "Save image as", str(Path.home() / self.path.name))
        if dest:
            try:
                Path(dest).write_bytes(self.path.read_bytes())
            except Exception:
                log.exception("Save As failed")

    def _open_external(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path)))
