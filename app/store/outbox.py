"""Send worker. Items enter the outbox table, get pushed onto this thread's
queue, and go out one at a time. A failed send never retries silently:
it turns red in the thread and waits for a click. A duplicate text to a
client is worse than a visible failure."""
import logging
import queue
import time

from PySide6.QtCore import QThread, Signal

from ..api import models
from ..api.rest import ApiError, BBClient
from .repo import Repo

log = logging.getLogger(__name__)


class SendThread(QThread):
    outbox_changed = Signal(str)          # chat_guid
    message_sent = Signal(object)         # parsed message from server response

    def __init__(self, client: BBClient, repo: Repo,
                 recover_sending: bool = True, parent=None):
        super().__init__(parent)
        self.client = client
        self.repo = repo
        self.recover_sending = recover_sending
        self.q: "queue.Queue" = queue.Queue()
        self._stop = False

    def submit(self, outbox_id: int):
        self.q.put(outbox_id)

    def stop(self):
        self._stop = True
        self.q.put(None)

    def run(self):
        # The SQLite outbox is the source of truth, not this in-memory queue.
        # Safe queued work resumes; ambiguous in-flight work becomes a visible
        # failure that requires human verification before retrying.
        for oid in self.repo.recover_outbox(
                mark_sending_uncertain=self.recover_sending):
            self.q.put(oid)
        while not self._stop:
            try:
                oid = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            if oid is None:
                break
            row = self.repo.claim_outbox(oid)
            if row is None:
                pending = self.repo.outbox_row(oid)
                if (not self._stop and pending is not None
                        and pending["state"] == "queued"
                        and self.repo.mac_maintenance_active()):
                    # A prior agent may have crashed during Wake. The durable
                    # lease expires by itself; keep this row alive in the
                    # sender queue until it can be claimed safely.
                    time.sleep(1.0)
                    self.q.put(oid)
                continue
            chat = row["chat_guid"]
            self.outbox_changed.emit(chat)
            try:
                if row["attach_path"]:
                    data = self.client.send_attachment(
                        chat, row["temp_guid"], row["attach_path"])
                else:
                    data = self.client.send_text(
                        chat, row["temp_guid"], row["text"] or "")
                if isinstance(data, dict):
                    # Some BlueBubbles send responses omit the chat relation.
                    # Supply the known target before parsing; the old fallback
                    # ran after parse_message had already rejected the payload.
                    data.setdefault("chatGuid", chat)
                parsed = models.parse_message(data) if isinstance(data, dict) else None
                if parsed:
                    # Store the authoritative message and finish the durable
                    # outbox row in one SQLite transaction. A crash can no
                    # longer leave a sent message paired with a red retry.
                    self.repo.upsert_message(
                        parsed, complete_outbox_id=oid)
                    self.message_sent.emit(parsed)
                else:
                    self.repo.outbox_set(oid, "sent")
                log.info("Sent outbox item %s", oid)
            except ApiError as e:
                log.warning("Send failed for outbox %s: %s", oid, e)
                msg = str(e)
                if "Timeout" in msg or "timeout" in msg:
                    msg = ("Delivery uncertain because the Mac did not answer. "
                           "Check Messages before retrying.")
                self.repo.outbox_set(oid, "failed", error=msg)
            except Exception:
                log.exception("Unexpected send failure for outbox %s", oid)
                self.repo.outbox_set(oid, "failed", error="Unexpected error; see log")
            self.outbox_changed.emit(chat)
