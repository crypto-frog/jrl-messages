"""Sync engine. Two modes:
  full     startup load: ping, contacts, chats, gap-fill, then history backfill
  gapfill  after a socket reconnect: pull everything since the local cursor
Backfill is resumable at any interruption via the sync_state table and
walks chats most-recent-first so the conversations you use are ready first."""
import logging
import threading
import time

from PySide6.QtCore import QThread, Signal

from .. import constants
from ..api import models
from ..api.rest import ApiError, BBClient
from .repo import Repo
from .reconcile_core import (IncompleteRowIDSnapshot,
                             NOTIFICATION_FLOOR_KEY, RowIDProtocolError,
                             NotificationBaselinePending,
                             ensure_notification_floor,
                             notification_eligible, refresh_chats,
                             retry_quarantined, scan_chat_recent,
                             scan_messages_after, scan_recent_head,
                             scan_rowid_archive_audit,
                             scan_rowid_catchup, scan_rowid_tail)

log = logging.getLogger(__name__)

class ReconcileThread(QThread):
    """Authoritative completeness worker beneath the low-latency socket."""

    batch_upserted = Signal(object)       # [(message, is_new, changed), ...]
    chats_refreshed = Signal()
    chat_refreshed = Signal(str, object)   # guid, newest server ts
    rescued = Signal(int)
    poll_ok = Signal()
    poll_failed = Signal(str)
    caught_up = Signal()

    def __init__(self, client: BBClient, repo: Repo,
                 interval_s: int = constants.POLL_INTERVAL_S,
                 notify_new: bool = True, parent=None):
        super().__init__(parent)
        self.client = client
        self.repo = repo
        self.interval = interval_s
        self.overlap_ms = constants.GAP_OVERLAP_MS
        self.last_attempt_ts = time.monotonic()
        self.last_success_ts = time.monotonic()
        self.last_cycle_ts = self.last_attempt_ts  # compatibility/tooltips
        self.consecutive_failures = 0
        self.notify_new = notify_new
        self._stop = False
        self._wake = threading.Event()
        self._want_chats = False
        self._want_head = True
        self._chat_queue: list = []
        self._qlock = threading.Lock()
        self._cycle = 0
        self._rowid_supported = None
        self._caught_up_emitted = False
        self._next_head_due = 0.0
        self._next_deep_due = (
            time.monotonic() + constants.DEEP_AUDIT_INTERVAL_S)
        self._next_rowid_tail_due = (
            time.monotonic() + constants.ROWID_TAIL_AUDIT_INTERVAL_S)
        self._next_rowid_archive_due = (
            time.monotonic() + constants.ROWID_ARCHIVE_AUDIT_INTERVAL_S)
        self._wake.set()  # first authoritative check starts immediately

    def poke(self, chats: bool = False, head: bool = False):
        with self._qlock:
            if chats:
                self._want_chats = True
            if head:
                self._want_head = True
        self._wake.set()

    def set_interval(self, seconds: int):
        self.interval = max(3, int(seconds))
        self._wake.set()

    def arm_notifications(self):
        self.notify_new = True

    def poke_chat(self, chat_guid: str):
        with self._qlock:
            if chat_guid not in self._chat_queue:
                self._chat_queue.append(chat_guid)
        self._wake.set()

    def stop(self):
        self._stop = True
        self._wake.set()

    def run(self):
        while not self._stop:
            self._wake.wait(self.interval)
            self._wake.clear()
            if self._stop:
                break
            try:
                self._cycle += 1
                if (not self.notify_new
                        and self.repo.meta(NOTIFICATION_FLOOR_KEY) is None):
                    # Do not ingest anything until the historical boundary is
                    # durably frozen. Otherwise a transient floor failure can
                    # insert the first live text as silent history and make the
                    # second text appear to be the first alert.
                    ensure_notification_floor(
                        self.client, self.repo, self.notify_new)

                with self._qlock:
                    force_head = self._want_head
                    want_chats = self._want_chats
                    targets, self._chat_queue = self._chat_queue, []
                    self._want_head = False
                    self._want_chats = False

                now = time.monotonic()
                if (self._rowid_supported is False
                        and self._cycle % constants.ROWID_REPROBE_EVERY == 0):
                    # A Messages restart can transiently produce incomplete
                    # pages. Compatibility fallback remains active, but the
                    # authoritative ROWID path gets another chance later.
                    self._rowid_supported = None
                head_found = 0
                if force_head or now >= self._next_head_due:
                    head_summary = scan_recent_head(
                        self.client, self.repo, self.batch_upserted.emit,
                        lambda: self._stop, notify_new=self.notify_new)
                    head_found = head_summary.new
                    self._next_head_due = (
                        time.monotonic() + constants.HEAD_AUDIT_INTERVAL_S)

                for g in targets:
                    got, newest = scan_chat_recent(
                        self.client, self.repo, g,
                        self.batch_upserted.emit, lambda: self._stop,
                        notify_new=self.notify_new)
                    if got:
                        log.warning(
                            "Refresh of chat recovered %d message(s)", got)
                        self.rescued.emit(got)
                    self.chat_refreshed.emit(g, newest)

                summary = None
                if self._rowid_supported is not False:
                    try:
                        summary = scan_rowid_catchup(
                            self.client, self.repo, self.batch_upserted.emit,
                            lambda: self._stop, notify_new=self.notify_new)
                        self._rowid_supported = True
                    except (ApiError, RowIDProtocolError) as e:
                        if (isinstance(e, RowIDProtocolError)
                                or e.status_code in (400, 404, 422)):
                            self._rowid_supported = False
                            self.repo.set_meta("rowid_sync_supported", 0)
                            log.warning(
                                "Server rejected ROWID reconciliation; using "
                                "legacy overlapping audit: %s", e)
                        else:
                            raise
                if self._rowid_supported is False:
                    summary = self._legacy_cycle()

                tail_found = 0
                if (self._rowid_supported is True
                        and time.monotonic() >= self._next_rowid_tail_due):
                    tail = scan_rowid_tail(
                        self.client, self.repo, self.batch_upserted.emit,
                        lambda: self._stop, notify_new=self.notify_new)
                    tail_found = tail.new
                    self._next_rowid_tail_due = (
                        time.monotonic()
                        + constants.ROWID_TAIL_AUDIT_INTERVAL_S)

                archive_found = 0
                if (self._rowid_supported is True
                        and summary is not None
                        and summary.cursor >= summary.snapshot
                        and time.monotonic() >= self._next_rowid_archive_due):
                    archive = scan_rowid_archive_audit(
                        self.client, self.repo, self.batch_upserted.emit,
                        lambda: self._stop, notify_new=self.notify_new)
                    archive_found = archive.new
                    self._next_rowid_archive_due = (
                        time.monotonic()
                        + constants.ROWID_ARCHIVE_AUDIT_INTERVAL_S)

                found = head_found + tail_found + archive_found + (
                    summary.new if summary is not None else 0)
                if found:
                    log.warning(
                        "Reconcile recovered %d message(s) missed by push", found)
                    self.rescued.emit(found)
                    self._wake.set()   # drain the burst immediately
                if summary is not None and summary.cursor < summary.snapshot:
                    self._wake.set()   # continue a bounded history sweep

                # ROWID discovers creations.  This small overlapping audit
                # separately reconciles edits, unsends, and receipts.
                if (self._rowid_supported is True
                        and self._cycle % constants.UPDATE_AUDIT_EVERY == 0):
                    cursor = self.repo.max_ts()
                    if cursor:
                        scan_messages_after(
                            self.client, self.repo,
                            cursor - constants.GAP_OVERLAP_MS,
                            self.batch_upserted.emit, lambda: self._stop,
                            notify_new=self.notify_new)
                if (self._rowid_supported is True
                        and time.monotonic() >= self._next_deep_due):
                    # Anchor this repair window to the real clock, never to a
                    # possibly future-dated local maximum.
                    scan_messages_after(
                        self.client, self.repo,
                        int(time.time() * 1000) - constants.DEEP_OVERLAP_MS,
                        self.batch_upserted.emit, lambda: self._stop,
                        notify_new=self.notify_new)
                    self._next_deep_due = (
                        time.monotonic() + constants.DEEP_AUDIT_INTERVAL_S)
                if self._rowid_supported is True and self._cycle % 20 == 0:
                    retry_quarantined(
                        self.client, self.repo, self.batch_upserted.emit,
                        lambda: self._stop, notify_new=self.notify_new)

                if (want_chats
                        or self._cycle % constants.CHAT_REFRESH_EVERY == 0):
                    refresh_chats(self.client, self.repo, lambda: self._stop)
                    self.chats_refreshed.emit()

                if (summary is not None and summary.cursor >= summary.snapshot
                        and not self._caught_up_emitted):
                    self._caught_up_emitted = True
                    self.caught_up.emit()
                self.repo.set_meta(
                    "last_successful_sync_ms", int(time.time() * 1000))
                self.consecutive_failures = 0
                self.last_success_ts = time.monotonic()
                self.poll_ok.emit()
            except NotificationBaselinePending as e:
                log.info("Notification baseline pending: %s", e)
                self.poll_failed.emit("Confirming the Mac message baseline…")
            except IncompleteRowIDSnapshot as e:
                self.consecutive_failures += 1
                log.warning("Incomplete ROWID snapshot: %s", e)
                if self.consecutive_failures >= constants.POLL_FAILURE_RECOVERY:
                    log.warning(
                        "ROWID snapshots remain incomplete; keeping the "
                        "cursor fixed and retrying the authoritative path")
                    self.consecutive_failures = 0
                self.poll_failed.emit(
                    "The Mac returned an incomplete page; retrying")
            except ApiError as e:
                self.consecutive_failures += 1
                self.poll_failed.emit(str(e))
            except Exception:
                self.consecutive_failures += 1
                log.exception("Reconcile cycle failed")
                self.poll_failed.emit("Unexpected reconciliation error")
            finally:
                self.last_attempt_ts = time.monotonic()
                self.last_cycle_ts = self.last_attempt_ts

    def _legacy_cycle(self):
        """Compatibility path for unusually old BlueBubbles servers."""
        cursor = self.repo.max_ts()
        if cursor:
            return scan_messages_after(
                self.client, self.repo, cursor - constants.DEEP_OVERLAP_MS,
                self.batch_upserted.emit, lambda: self._stop,
                notify_new=self.notify_new)

        # An empty cache must still ask for messages; the old implementation
        # returned early forever and could miss the first new conversation.
        batch = self.client.query_messages(limit=100, offset=0, sort="DESC")
        items = []
        found = changed = 0
        for raw in batch:
            parsed = models.parse_message(raw) if isinstance(raw, dict) else None
            if not parsed:
                continue
            result = self.repo.upsert_message(
                parsed, notify_eligible=notification_eligible(
                    self.repo, parsed, self.notify_new))
            found += int(result.is_new)
            changed += int(result.changed and not result.is_new)
            if result.is_new or result.changed or result.event_pending:
                items.append((parsed, result.is_new, result.changed))
        if items:
            self.batch_upserted.emit(items)
        from .reconcile_core import ScanSummary
        return ScanSummary(len(batch), found, changed,
                           cursor=self.repo.max_ts() or 0)


class SyncThread(QThread):
    status = Signal(str)            # short line for the footer
    connected_ok = Signal(dict)     # server info on successful ping
    failed = Signal(str)            # human-readable connection failure
    contacts_ready = Signal()
    chats_ready = Signal()
    batch_upserted = Signal(object)      # [(message, is_new, changed), ...]
    backfill_page = Signal(str)     # chat_guid whose history grew
    backfill_done = Signal()
    recovery_audit_done = Signal()

    def __init__(self, client: BBClient, repo: Repo, mode: str = "full",
                 horizon_days: int = 0, notify_new: bool = True, parent=None):
        super().__init__(parent)
        self.client = client
        self.repo = repo
        self.mode = mode
        self.horizon_days = horizon_days
        self.notify_new = notify_new
        self._stop = False

    def stop(self):
        self._stop = True

    # ----------------------------------------------------------

    def run(self):
        try:
            if self.mode == "full":
                self._full()
            else:
                self._gapfill()
        except ApiError as e:
            self.failed.emit(str(e))
        except Exception:
            log.exception("Sync thread crashed")
            self.failed.emit("Sync error; see log")

    def _full(self):
        self.status.emit("Connecting\u2026")
        self.client.ping()
        for attempt in range(4):
            try:
                ensure_notification_floor(
                    self.client, self.repo, self.notify_new)
                break
            except (ApiError, NotificationBaselinePending):
                if attempt >= 3 or self._stop:
                    raise
                time.sleep(1.0 + attempt)
        info = self.client.server_info()
        self.connected_ok.emit(info if isinstance(info, dict) else {})

        self.status.emit("Checking latest messages\u2026")
        scan_recent_head(
            self.client, self.repo, self.batch_upserted.emit,
            lambda: self._stop, notify_new=self.notify_new,
            limit=constants.GLOBAL_HEAD_LIMIT)

        self.status.emit("Loading contacts\u2026")
        try:
            self.repo.upsert_contacts(models.parse_contacts(self.client.get_contacts()))
        except ApiError as e:
            log.warning("Contacts unavailable: %s", e)
        self.contacts_ready.emit()

        self.status.emit("Loading conversations\u2026")
        offset = 0
        while not self._stop:
            batch = self.client.query_chats(limit=100, offset=offset)
            if self._stop:
                return
            for ch in batch:
                if self._stop:
                    return
                parsed = models.parse_chat(ch)
                if parsed:
                    self.repo.upsert_chat(parsed)
            if len(batch) < 100:
                break
            offset += 100
        self.chats_ready.emit()

        self._gapfill()
        if not self._stop:
            self.recovery_audit_done.emit()
            self._backfill()

    def _gapfill(self):
        cursor = self.repo.max_ts()
        if not cursor:
            # A brand-new Windows cache must show the newest traffic first.
            # The ROWID reconciler will still walk the complete archive in
            # the background, but the user must not wait for that history
            # sweep before today's messages become visible.
            self.status.emit("Loading recent messages\u2026")
            batch = self.client.query_messages(
                limit=constants.INITIAL_RECENT_MESSAGES,
                offset=0, sort="DESC")
            items = []
            for raw in reversed(batch):
                if self._stop:
                    return
                parsed = models.parse_message(raw) if isinstance(raw, dict) else None
                if not parsed:
                    continue
                result = self.repo.upsert_message(
                    parsed, notify_eligible=notification_eligible(
                        self.repo, parsed, self.notify_new))
                if result.is_new or result.changed or result.event_pending:
                    items.append((parsed, result.is_new, result.changed))
            if items:
                self.batch_upserted.emit(items)
            self.chats_ready.emit()
            return
        self.status.emit("Catching up\u2026")
        scan_messages_after(
            self.client, self.repo, cursor - constants.DEEP_OVERLAP_MS,
            self.batch_upserted.emit, lambda: self._stop,
            notify_new=self.notify_new)
        self.chats_ready.emit()

    def _backfill(self):
        # Give the authoritative ROWID worker a brief chance to advertise
        # support.  When available it owns complete-history ingestion, without
        # inclusive timestamp-boundary holes.
        # A slow first Tailscale/ROWID request can legitimately exceed two
        # seconds. Do not start a duplicate legacy crawl while the
        # authoritative reconciler is still negotiating support.
        for _ in range(200):
            support = self.repo.meta("rowid_sync_supported")
            if support is not None or self._stop:
                break
            time.sleep(0.1)
        support = self.repo.meta("rowid_sync_supported")
        if support is None:
            self.status.emit(
                "History indexing is waiting for a complete Mac snapshot…")
            return
        if self.repo.meta_int("rowid_sync_supported", 0) == 1:
            self.status.emit("Indexing complete history safely…")
            self.backfill_done.emit()
            return

        floor = None
        if self.horizon_days and self.horizon_days > 0:
            floor = int(time.time() * 1000) - self.horizon_days * 86_400_000

        todo = [r["guid"] for r in self.repo.chats_needing_backfill()]
        total_msgs = 0
        for i, guid in enumerate(todo, 1):
            if self._stop:
                return
            state = self.repo.sync_row(guid)
            oldest = state["oldest_synced"] if state else None
            boundary_offset = 0
            while not self._stop:
                batch = self.client.query_messages(
                    chat_guid=guid,
                    # BlueBubbles' before boundary is inclusive. Keep equal-
                    # timestamp rows in the result and page through that
                    # boundary with OFFSET; subtracting one millisecond could
                    # permanently skip the 101st message sharing a timestamp.
                    before=oldest,
                    offset=boundary_offset,
                    sort="DESC",
                    limit=constants.BACKFILL_PAGE)
                if self._stop:
                    return
                if not batch:
                    self.repo.set_sync(guid, oldest, done=True)
                    break
                page_oldest = None
                page_timestamps = []
                for raw in batch:
                    m = models.parse_message(raw)
                    if not m:
                        continue
                    self.repo.upsert_message(
                        m, notify_eligible=notification_eligible(
                            self.repo, m, self.notify_new))
                    total_msgs += 1
                    ts = m["date_created"]
                    page_timestamps.append(ts)
                    page_oldest = (ts if page_oldest is None
                                   else min(page_oldest, ts))
                if page_oldest is None:
                    # A page of malformed rows must still move through the
                    # bounded server result instead of looping forever.
                    boundary_offset += len(batch)
                elif oldest is None or page_oldest < oldest:
                    oldest = page_oldest
                    boundary_offset = sum(
                        1 for ts in page_timestamps if ts == oldest)
                else:
                    boundary_offset += len(batch)
                done = len(batch) < constants.BACKFILL_PAGE
                if floor and oldest and oldest < floor:
                    done = True
                self.repo.set_sync(guid, oldest, done=done)
                self.backfill_page.emit(guid)
                self.status.emit(
                    f"Syncing history: conversation {i} of {len(todo)} "
                    f"({total_msgs:,} messages)")
                if done:
                    break
                time.sleep(0.05)
        if not self._stop:
            self.status.emit("History synced")
            self.backfill_done.emit()
