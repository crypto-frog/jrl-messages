"""Pure reconciliation primitives shared by the Qt workers and tests.

Socket.IO is deliberately treated as a low-latency hint.  Completeness comes
from a durable, monotonically committed walk of the Mac Messages database's
``ROWID`` (serialized by BlueBubbles as ``originalROWID``).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

from .. import constants
from ..api import models

log = logging.getLogger(__name__)

ROWID_CURSOR_KEY = "source_rowid_cursor"
ROWID_SUPPORTED_KEY = "rowid_sync_supported"
NOTIFICATION_FLOOR_KEY = "notification_baseline_rowid"
ROWID_LOWER_CANDIDATE_KEY = "rowid_lower_snapshot_candidate"
ROWID_LOWER_COUNT_KEY = "rowid_lower_snapshot_count"
ROWID_ARCHIVE_AUDIT_CURSOR_KEY = "rowid_archive_audit_cursor"
EMPTY_BASELINE_FIRST_SEEN_KEY = "notification_empty_baseline_first_seen_ms"


class RowIDProtocolError(RuntimeError):
    """The server accepted a ROWID query but did not honor its bounds."""


class IncompleteRowIDSnapshot(RuntimeError):
    """A successful response omitted the row used to freeze its snapshot.

    This is treated as a transient/incomplete response, not as evidence that
    the BlueBubbles server lacks ROWID support.  The durable cursor must stay
    put so a fresh connection can retry the same interval.
    """


class NotificationBaselinePending(RuntimeError):
    """A new installation must confirm that an empty Mac is really empty."""


@dataclass(frozen=True)
class ScanSummary:
    examined: int = 0
    new: int = 0
    changed: int = 0
    snapshot: int = 0
    cursor: int = 0


def _rowid(raw: dict) -> int:
    try:
        return int(raw.get("originalROWID") or raw.get("ROWID") or 0)
    except (TypeError, ValueError):
        return 0


def _query_rowid_window(client, low: int, high: int) -> list[dict]:
    """Read one bounded ROWID window with a defensive second pass.

    Some server/database races can return a short but otherwise successful
    page.  Since a numeric window has a strict maximum size, unioning a second
    response is cheap and closes the common transient-omission hole.  Genuine
    deleted SQLite ROWIDs simply produce the same short set twice.
    """
    merged: dict[int, dict] = {}

    def completeness(raw: dict) -> int:
        chat_context = bool(raw.get("chatGuid") or raw.get("chat")
                            or raw.get("chats"))
        # Required parse context dominates optional enrichment. A later sparse
        # retry must never overwrite a complete first response for one ROWID.
        return (20 * bool(raw.get("guid"))
                + 20 * bool(raw.get("dateCreated") is not None)
                + 20 * chat_context
                + 4 * bool("isFromMe" in raw)
                + 2 * bool(raw.get("handle"))
                + 2 * bool("text" in raw)
                + min(5, len(raw)))

    possible = max(0, int(high) - int(low))
    for _attempt in range(max(1, constants.ROWID_RECHECK_PASSES)):
        batch = client.query_messages_rowid_range(low, high)
        for raw in batch:
            if not isinstance(raw, dict):
                raise RowIDProtocolError(
                    "ROWID query returned a non-message record")
            rid = _rowid(raw)
            if rid <= low or rid > high:
                raise RowIDProtocolError(
                    f"ROWID query returned {rid} outside ({low},{high}]")
            old = merged.get(rid)
            if old is None or completeness(raw) > completeness(old):
                merged[rid] = raw
        if len(merged) >= possible:
            break
    return [merged[rid] for rid in sorted(merged)]


def ensure_notification_floor(client, repo, notify_new: bool):
    """Freeze the pre-existing Mac history boundary on first installation.

    Rows above this boundary remain notification-eligible even while a large
    historical archive is still being indexed.
    """
    if notify_new:
        return None
    existing = repo.meta(NOTIFICATION_FLOOR_KEY)
    try:
        if existing is not None:
            return int(existing)
    except (TypeError, ValueError):
        pass
    snapshot = max(0, int(client.max_message_rowid()))
    if snapshot == 0 and repo.message_count() == 0:
        now_ms = int(time.time() * 1000)
        first_ms = repo.meta_int(EMPTY_BASELINE_FIRST_SEEN_KEY, 0)
        if first_ms <= 0:
            repo.set_meta(EMPTY_BASELINE_FIRST_SEEN_KEY, now_ms)
            raise NotificationBaselinePending(
                "Mac reported an empty message database; confirming stability")
        if now_ms - first_ms < constants.EMPTY_BASELINE_STABLE_MS:
            raise NotificationBaselinePending(
                "Mac message database is still in the empty confirmation window")
    elif repo.meta_int(EMPTY_BASELINE_FIRST_SEEN_KEY, 0):
        repo.set_meta(EMPTY_BASELINE_FIRST_SEEN_KEY, 0)
    return repo.set_meta_min(NOTIFICATION_FLOOR_KEY, snapshot)


def notification_eligible(repo, message: dict, notify_new: bool) -> bool:
    rid = int(message.get("source_rowid") or 0)
    raw_floor = repo.meta(NOTIFICATION_FLOOR_KEY)
    if notify_new:
        # Once a first-install floor exists, a later rolling archive repair
        # must not turn a pre-install historical omission into a fresh alert.
        # A genuinely late iCloud insertion has a newer ROWID and remains
        # eligible even when Apple preserved an old dateCreated value.
        try:
            if rid and raw_floor is not None and rid <= int(raw_floor):
                return False
        except (TypeError, ValueError):
            pass
        return True
    if raw_floor is None:
        return False
    try:
        floor = int(raw_floor)
    except (TypeError, ValueError):
        return False
    # Zero is a valid frozen floor for an empty Mac. The first later ROWID
    # must still be eligible even before the historical baseline is armed.
    return bool(rid and rid > floor)


def _upsert_reconciled(repo, message: dict, notify_new: bool):
    """Upsert an authoritative read and repair a missing delivery event.

    Two first-start workers can observe different max ROWIDs before
    ``set_meta_min`` settles on the earliest floor. A row briefly classified
    as baseline history must gain its event when a later authoritative read
    proves its ROWID is above the final durable floor. Requiring a concrete
    ROWID/floor pair prevents legacy or pre-baseline history from alerting.
    """
    eligible = notification_eligible(repo, message, notify_new)
    repair_existing = False
    rid = int(message.get("source_rowid") or 0)
    raw_floor = repo.meta(NOTIFICATION_FLOOR_KEY)
    if eligible and rid and raw_floor is not None:
        try:
            repair_existing = rid > int(raw_floor)
        except (TypeError, ValueError):
            repair_existing = False
    return repo.upsert_message(
        message, notify_eligible=eligible,
        allow_existing_event=repair_existing)


def scan_rowid_catchup(client, repo, emit_batch: Callable,
                       stop_check: Callable[[], bool], *,
                       notify_new: bool, max_windows: int = 4) -> ScanSummary:
    """Import every Mac row in a frozen ``(cursor, snapshot]`` interval.

    Each numeric sub-range is at most ``ROWID_WINDOW`` wide, therefore it can
    contain at most that many SQLite rows and requires no mutable OFFSET
    pagination.  The cursor advances only after every row in the sub-range is
    either committed or durably quarantined.
    """
    cursor = max(0, repo.meta_int(ROWID_CURSOR_KEY, 0))
    snapshot = max(0, int(client.max_message_rowid()))

    if snapshot < cursor:
        candidate = repo.meta_int(ROWID_LOWER_CANDIDATE_KEY, -1)
        sightings = repo.meta_int(ROWID_LOWER_COUNT_KEY, 0)
        sightings = sightings + 1 if candidate == snapshot else 1
        repo.set_meta(ROWID_LOWER_CANDIDATE_KEY, snapshot)
        repo.set_meta(ROWID_LOWER_COUNT_KEY, sightings)
        if sightings < 3:
            raise IncompleteRowIDSnapshot(
                f"Mac reported lower max ROWID {snapshot} below cursor "
                f"{cursor} ({sightings}/3 confirmations)")
        log.warning("Mac message ROWID reset detected (%d -> %d); re-indexing",
                    cursor, snapshot)
        cursor = 0
        repo.set_meta(ROWID_CURSOR_KEY, 0)
        repo.set_meta(ROWID_LOWER_COUNT_KEY, 0)
    else:
        # A single empty/lower result during a Messages relaunch is common.
        # One normal snapshot cancels the pending database-reset hypothesis.
        if repo.meta_int(ROWID_LOWER_COUNT_KEY, 0):
            repo.set_meta(ROWID_LOWER_COUNT_KEY, 0)

    # Never shrink the historical notification boundary on an unconfirmed
    # transient low/empty max while Messages is relaunching.
    if not notify_new:
        repo.set_meta_min(NOTIFICATION_FLOOR_KEY, snapshot)

    examined = new_count = changed_count = 0
    windows = 0
    while cursor < snapshot and not stop_check():
        high = min(snapshot, cursor + constants.ROWID_WINDOW)
        batch = _query_rowid_window(client, cursor, high)
        returned_rowids = {
            _rowid(raw) for raw in batch if isinstance(raw, dict)
        }
        if high == snapshot and snapshot not in returned_rowids:
            raise IncompleteRowIDSnapshot(
                f"Message ROWID {snapshot} was omitted from its snapshot "
                "response; retrying without advancing the cursor")
        changed_items = []
        for raw in batch:
            if stop_check():
                break
            rid = _rowid(raw)
            examined += 1
            parsed = models.parse_message(raw)
            if parsed is None:
                repo.record_sync_failure(
                    rid, raw.get("guid"), raw, "Incomplete message payload")
                continue
            result = _upsert_reconciled(repo, parsed, notify_new)
            repo.clear_sync_failure(rid)
            new_count += int(result.is_new)
            changed_count += int(result.changed and not result.is_new)
            if result.is_new or result.changed or result.event_pending:
                changed_items.append((parsed, result.is_new, result.changed))

        if stop_check():
            break
        if changed_items:
            emit_batch(changed_items)
        # The bounded interval is now fully accounted for.  Commit progress
        # only after its rows and UI-delivery ledger are durable.
        cursor = high
        repo.set_meta(ROWID_CURSOR_KEY, cursor)
        windows += 1
        if windows >= max(1, max_windows):
            break

    repo.set_meta(ROWID_SUPPORTED_KEY, 1)
    return ScanSummary(examined, new_count, changed_count, snapshot, cursor)


def scan_rowid_tail(client, repo, emit_batch: Callable,
                    stop_check: Callable[[], bool], *,
                    notify_new: bool,
                    span: int = constants.ROWID_TAIL_SPAN) -> ScanSummary:
    """Continuously verify the newest database ROWIDs without trusting the
    durable catch-up cursor or message timestamps.

    An iCloud message can be inserted today with last week's timestamp.  It is
    then absent from a date-sorted head page, but it is necessarily near the
    ROWID tail.  Re-reading this small tail makes a transiently incomplete old
    response self-heal on a later pass.
    """
    snapshot = max(0, int(client.max_message_rowid()))
    cursor = max(0, snapshot - max(1, int(span)))
    examined = new_count = changed_count = 0
    while cursor < snapshot and not stop_check():
        high = min(snapshot, cursor + constants.ROWID_WINDOW)
        batch = _query_rowid_window(client, cursor, high)
        items = []
        for raw in batch:
            if stop_check():
                break
            rid = _rowid(raw)
            parsed = models.parse_message(raw)
            if parsed is None:
                repo.record_sync_failure(
                    rid, raw.get("guid"), raw,
                    "Incomplete message payload from tail audit")
                continue
            result = _upsert_reconciled(repo, parsed, notify_new)
            repo.clear_sync_failure(rid)
            examined += 1
            new_count += int(result.is_new)
            changed_count += int(result.changed and not result.is_new)
            if result.is_new or result.changed or result.event_pending:
                items.append((parsed, result.is_new, result.changed))
        if items:
            emit_batch(items)
        cursor = high
    return ScanSummary(examined, new_count, changed_count, snapshot, cursor)


def scan_rowid_archive_audit(client, repo, emit_batch: Callable,
                             stop_check: Callable[[], bool], *,
                             notify_new: bool) -> ScanSummary:
    """Re-read one rolling archive window so no omission is permanent.

    The immediate second pass and 200-row tail cover common server races.
    This slower durable cursor eventually revisits every older ROWID too,
    including a row omitted twice after it has moved outside the tail.
    """
    snapshot = max(0, int(client.max_message_rowid()))
    if snapshot <= 0:
        return ScanSummary(snapshot=snapshot, cursor=0)
    cursor = max(0, repo.meta_int(ROWID_ARCHIVE_AUDIT_CURSOR_KEY, 0))
    if cursor >= snapshot:
        cursor = 0
    high = min(snapshot, cursor + constants.ROWID_WINDOW)
    batch = _query_rowid_window(client, cursor, high)
    examined = new_count = changed_count = 0
    items = []
    for raw in batch:
        if stop_check():
            break
        rid = _rowid(raw)
        parsed = models.parse_message(raw)
        if parsed is None:
            repo.record_sync_failure(
                rid, raw.get("guid"), raw,
                "Incomplete message payload from archive audit")
            continue
        result = _upsert_reconciled(repo, parsed, notify_new)
        repo.clear_sync_failure(rid)
        examined += 1
        new_count += int(result.is_new)
        changed_count += int(result.changed and not result.is_new)
        if result.is_new or result.changed or result.event_pending:
            items.append((parsed, result.is_new, result.changed))
    if stop_check():
        return ScanSummary(
            examined, new_count, changed_count, snapshot, cursor)
    if items:
        emit_batch(items)
    repo.set_meta(ROWID_ARCHIVE_AUDIT_CURSOR_KEY, high)
    return ScanSummary(examined, new_count, changed_count, snapshot, high)


def scan_recent_head(client, repo, emit_batch: Callable,
                     stop_check: Callable[[], bool], *, notify_new: bool,
                     limit: int = constants.GLOBAL_HEAD_LIMIT) -> ScanSummary:
    """Reconcile a bounded newest-message page without trusting any cursor.

    This independent safety net repairs a transiently incomplete ROWID range,
    a poisoned timestamp maximum, and messages that moved between sibling
    iMessage/SMS chat GUIDs.  Upserts are idempotent, so frequent audits are
    cheap locally and cannot duplicate a message or its delivery event.
    """
    batch = client.query_messages(
        limit=max(1, int(limit)), offset=0, sort="DESC")
    examined = new_count = changed_count = 0
    items = []
    # BlueBubbles returns DESC; commit/emit oldest-to-newest for stable UI
    # ordering when several messages arrive in the same audit.
    for raw in reversed(batch):
        if stop_check():
            break
        parsed = models.parse_message(raw) if isinstance(raw, dict) else None
        if not parsed:
            continue
        examined += 1
        result = _upsert_reconciled(repo, parsed, notify_new)
        new_count += int(result.is_new)
        changed_count += int(result.changed and not result.is_new)
        if result.is_new or result.changed or result.event_pending:
            items.append((parsed, result.is_new, result.changed))
    if items:
        emit_batch(items)
    return ScanSummary(examined, new_count, changed_count,
                       cursor=repo.max_ts() or 0)


def retry_quarantined(client, repo, emit_batch: Callable,
                      stop_check: Callable[[], bool], *, notify_new: bool) -> int:
    """Retry rows that once arrived without enough chat/date context."""
    recovered = 0
    items = []
    for failure in repo.sync_failures():
        if stop_check():
            break
        raw = None
        guid = failure["guid"]
        if guid:
            rows = client.query_message_guid(guid)
            raw = rows[0] if rows else None
        if raw is None:
            try:
                raw = json.loads(failure["raw"])
            except Exception:
                raw = None
        parsed = models.parse_message(raw) if isinstance(raw, dict) else None
        if parsed is None:
            if isinstance(raw, dict):
                repo.record_sync_failure(
                    failure["source_rowid"], guid, raw,
                    "Retry still returned an incomplete payload")
            continue
        result = _upsert_reconciled(repo, parsed, notify_new)
        repo.clear_sync_failure(failure["source_rowid"])
        recovered += 1
        if result.is_new or result.changed or result.event_pending:
            items.append((parsed, result.is_new, result.changed))
    if items:
        emit_batch(items)
    return recovered


def scan_messages_after(client, repo, after_ms: int, emit_batch: Callable,
                        stop_check: Callable[[], bool], *,
                        notify_new: bool) -> ScanSummary:
    """Secondary date audit for edits, retractions, and read receipts.

    ROWID reconciliation owns message completeness; this deliberately remains
    an overlapping audit, so mutable timestamp pagination cannot create a
    permanent hole.
    """
    examined = new_count = changed_count = 0
    offset = 0
    while not stop_check():
        batch = client.query_messages(
            after=after_ms, sort="ASC", limit=constants.BACKFILL_PAGE,
            offset=offset)
        if stop_check():
            break
        items = []
        for raw in batch:
            if stop_check():
                break
            parsed = models.parse_message(raw) if isinstance(raw, dict) else None
            if not parsed:
                continue
            examined += 1
            result = _upsert_reconciled(repo, parsed, notify_new)
            new_count += int(result.is_new)
            changed_count += int(result.changed and not result.is_new)
            if result.is_new or result.changed or result.event_pending:
                items.append((parsed, result.is_new, result.changed))
        if items:
            emit_batch(items)
        if len(batch) < constants.BACKFILL_PAGE:
            break
        offset += constants.BACKFILL_PAGE
    return ScanSummary(examined, new_count, changed_count,
                       cursor=repo.max_ts() or 0)


def scan_chat_recent(client, repo, chat_guid: str, emit_batch: Callable,
                     stop_check: Callable[[], bool],
                     limit: int = constants.BACKFILL_PAGE,
                     *, notify_new: bool = True):
    found = 0
    newest = None
    items = []
    batch = client.query_messages(
        chat_guid=chat_guid, limit=limit, offset=0, sort="DESC")
    for raw in batch:
        if stop_check():
            break
        if isinstance(raw, dict):
            raw.setdefault("chatGuid", chat_guid)
        parsed = models.parse_message(raw) if isinstance(raw, dict) else None
        if not parsed:
            continue
        newest = max(newest or 0, parsed["date_created"])
        result = _upsert_reconciled(repo, parsed, notify_new)
        found += int(result.is_new)
        if result.is_new or result.changed or result.event_pending:
            items.append((parsed, result.is_new, result.changed))
    if items:
        emit_batch(items)
    return found, newest


def refresh_chats(client, repo, stop_check: Callable[[], bool]) -> None:
    offset = 0
    while not stop_check():
        batch = client.query_chats(limit=100, offset=offset)
        if stop_check():
            break
        for chat in batch:
            if stop_check():
                break
            parsed = models.parse_chat(chat)
            if parsed:
                repo.upsert_chat(parsed)
        if len(batch) < 100:
            break
        offset += 100
