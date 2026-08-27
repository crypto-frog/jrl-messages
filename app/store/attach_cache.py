"""Attachment cache. Bytes are lazy: the thread view requests a download
when a tile enters the window or is clicked. Images get a PNG thumbnail
(HEIC decoded via pillow-heif). Files live at cache/attachments/<guid>/."""
import logging
import queue
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .. import constants
from ..api.rest import ApiError, BBClient
from ..util.textutil import safe_filename
from .repo import Repo

log = logging.getLogger(__name__)

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    pass


def attachment_path(guid: str, file_name: str) -> Path:
    return constants.ATTACH_DIR / guid / safe_filename(file_name)


def thumb_path(guid: str) -> Path:
    # v2: EXIF orientation honored; old unversioned thumbs are ignored
    return constants.THUMB_DIR / f"{guid}_v2.png"


def make_thumbnail(src: Path, guid: str) -> Path | None:
    try:
        from PIL import Image
        tp = thumb_path(guid)
        if tp.exists():
            return tp
        from PIL import ImageOps
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im
            im.thumbnail((constants.THUMB_MAX, constants.THUMB_MAX))
            tp.parent.mkdir(parents=True, exist_ok=True)
            im.save(tp, "PNG")
        return tp
    except Exception:
        log.warning("Thumbnail failed for %s", guid)
        return None


class DownloadThread(QThread):
    ready = Signal(str, str)      # guid, local path
    failed = Signal(str, str)     # guid, error text

    def __init__(self, client: BBClient, repo: Repo, parent=None):
        super().__init__(parent)
        self.client = client
        self.repo = repo
        self.q: "queue.Queue" = queue.Queue()
        self._seen = set()
        self._stop = False

    def request(self, guid: str, file_name: str):
        if guid in self._seen:
            return
        self._seen.add(guid)
        self.q.put((guid, file_name))

    def stop(self):
        self._stop = True
        self.q.put(None)

    def run(self):
        while not self._stop:
            try:
                item = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            guid, file_name = item
            dest = attachment_path(guid, file_name)
            try:
                if not dest.exists():
                    self.repo.set_attachment_local(guid, None, "pending")
                    self.client.download_attachment(guid, dest)
                row = self.repo.attachment(guid)
                mime = (row["mime_type"] or "") if row else ""
                if mime.startswith("image/") or dest.suffix.lower() in (
                        ".heic", ".heif", ".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    make_thumbnail(dest, guid)
                self.repo.set_attachment_local(guid, str(dest), "done")
                self.ready.emit(guid, str(dest))
            except ApiError as e:
                self._seen.discard(guid)
                self.repo.set_attachment_local(guid, None, "failed")
                self.failed.emit(guid, str(e))
            except Exception:
                log.exception("Download crashed for %s", guid)
                self._seen.discard(guid)
                self.repo.set_attachment_local(guid, None, "failed")
                self.failed.emit(guid, "Download error")
