import ast
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
import ctypes
from pathlib import Path

# The production dependency is installed by install.bat. Keep these pure
# store tests runnable in a minimal Python environment as well.
try:
    import platformdirs  # noqa: F401
except ModuleNotFoundError:
    platformdirs = types.ModuleType("platformdirs")
    platformdirs.user_data_dir = lambda *_args, **_kwargs: tempfile.gettempdir()
    sys.modules["platformdirs"] = platformdirs

try:
    import httpx  # production dependency; the REST client tests need it
except ModuleNotFoundError:
    httpx = None

from app.api.models import parse_message
from app.agent import serialize
from app.agent.policy import AutoWakeInputs, should_auto_wake
from app.store.db import Database
from app.store.reconcile_core import (
    IncompleteRowIDSnapshot,
    NOTIFICATION_FLOOR_KEY,
    NotificationBaselinePending,
    ROWID_CURSOR_KEY,
    RowIDProtocolError,
    ensure_notification_floor,
    retry_quarantined,
    scan_recent_head,
    scan_rowid_archive_audit,
    scan_rowid_catchup,
)
from app.store.repo import Repo
from app.util.codes import extract_code
from app.util.winfill import keyboard_inputs, input_types


CHAT = "iMessage;-;+15555550100"
NOW = 1_800_000_000_000
ROOT = Path(__file__).parents[1]


def raw_message(guid, rowid, created=NOW, *, text=None, from_me=False,
                attachments=None, chat=CHAT):
    return {
        "guid": guid,
        "originalROWID": rowid,
        "dateCreated": created,
        "isFromMe": from_me,
        "text": text if text is not None else f"message {guid}",
        "chats": [{"guid": chat}],
        "handle": {"address": "+15555550100"},
        "attachments": attachments or [],
    }


class FakeRowIDClient:
    def __init__(self, rows):
        self.rows = list(rows)

    def max_message_rowid(self):
        return max(
            (int(r.get("originalROWID") or 0) for r in self.rows),
            default=0,
        )

    def query_messages_rowid_range(self, low, high):
        return [
            r for r in self.rows
            if low < int(r.get("originalROWID") or 0) <= high
        ]

    def query_messages(self, chat_guid=None, limit=100, offset=0,
                       after=None, before=None, sort="DESC", where=None):
        rows = list(self.rows)
        if chat_guid:
            rows = [
                r for r in rows
                if any(c.get("guid") == chat_guid
                       for c in r.get("chats") or [])
            ]
        rows.sort(key=lambda r: int(r.get("dateCreated") or 0),
                  reverse=sort.upper() == "DESC")
        return rows[offset:offset + limit]


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "messages.db")
        self.repo = Repo(self.db)

    def tearDown(self):
        connection = getattr(self.db._local, "c", None)
        if connection is not None:
            connection.close()
            self.db._local.c = None
        self.tmp.cleanup()

    def parsed(self, *args, **kwargs):
        value = parse_message(raw_message(*args, **kwargs))
        self.assertIsNotNone(value)
        return value

    def test_socket_can_claim_delivery_event_after_boot_wins_insert_race(self):
        message = self.parsed("race-guid", 1)
        boot = self.repo.upsert_message(message, notify_eligible=False)
        self.assertTrue(boot.is_new)
        self.assertEqual([], self.repo.pending_delivery_events())

        push = self.repo.upsert_message(
            message, notify_eligible=True, allow_existing_event=True)
        self.assertFalse(push.is_new)
        events = self.repo.pending_delivery_events()
        self.assertEqual(1, len(events))

        self.assertTrue(
            self.repo.apply_unread_event("race-guid", chat_is_open=False))
        self.assertFalse(
            self.repo.apply_unread_event("race-guid", chat_is_open=False))
        chat = self.db.one("SELECT unread FROM chats WHERE guid=?", (CHAT,))
        self.assertEqual(1, chat["unread"])

        self.repo.finish_notification_event("race-guid")
        self.assertEqual([], self.repo.pending_delivery_events())

    def test_mark_read_acknowledges_pending_unread_event(self):
        message = self.parsed("read-race", 1)
        self.repo.upsert_message(message, notify_eligible=True)
        self.repo.mark_read(CHAT)
        self.assertFalse(
            self.repo.apply_unread_event("read-race", chat_is_open=False))
        self.assertEqual(0, self.repo.unread_total())

    def test_mark_all_read_acknowledges_pending_unread_events(self):
        for n in range(2):
            message = self.parsed(f"read-all-{n}", n + 1)
            self.repo.upsert_message(message, notify_eligible=True)
        self.repo.mark_all_read()
        for n in range(2):
            self.assertFalse(self.repo.apply_unread_event(
                f"read-all-{n}", chat_is_open=False))
        self.assertEqual(0, self.repo.unread_total())

    def test_outbox_active_count_ignores_failed_and_sent_rows(self):
        queued = self.repo.enqueue(CHAT, "one", None)
        sending = self.repo.enqueue(CHAT, "two", None)
        failed = self.repo.enqueue(CHAT, "three", None)
        finished = self.repo.enqueue(CHAT, "four", None)
        self.assertNotEqual(queued, sending)
        self.repo.outbox_set(sending, "sending")
        self.repo.outbox_set(failed, "failed", error="boom")
        self.repo.outbox_set(finished, "sent")
        # Only work that could still reach the wire blocks a Messages
        # restart; failed rows wait for a human and finished rows are done.
        self.assertEqual(2, self.repo.outbox_active_count())

    def test_only_one_concurrent_sender_can_claim_an_outbox_row(self):
        oid = self.repo.enqueue(CHAT, "exactly once", None)
        barrier = threading.Barrier(12)
        claims = []
        guard = threading.Lock()

        def claim():
            barrier.wait()
            row = self.repo.claim_outbox(oid)
            with guard:
                claims.append(row)

        workers = [threading.Thread(target=claim) for _ in range(12)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(1, sum(row is not None for row in claims))
        stored = self.repo.outbox_row(oid)
        self.assertEqual("sending", stored["state"])
        self.assertEqual(1, stored["attempts"])

    def test_mac_maintenance_lease_fences_late_send_claims(self):
        self.assertTrue(self.repo.try_begin_mac_maintenance())
        self.assertTrue(self.repo.mac_maintenance_active())
        oid = self.repo.enqueue(CHAT, "queued during Wake", None)
        self.assertIsNone(self.repo.claim_outbox(oid))
        self.assertEqual("queued", self.repo.outbox_row(oid)["state"])
        self.repo.end_mac_maintenance()
        self.assertFalse(self.repo.mac_maintenance_active())
        self.assertIsNotNone(self.repo.claim_outbox(oid))

    def test_mac_maintenance_refuses_when_send_is_already_queued(self):
        oid = self.repo.enqueue(CHAT, "already queued", None)
        self.assertFalse(self.repo.try_begin_mac_maintenance())
        # A refused Wake must remove its tentative fence immediately.
        self.assertIsNotNone(self.repo.claim_outbox(oid))

    def test_concurrent_push_and_poll_commit_one_row_and_one_event(self):
        message = self.parsed("concurrent-guid", 3)
        results = []
        result_lock = threading.Lock()

        def ingest():
            outcome = self.repo.upsert_message(
                message, notify_eligible=True, allow_existing_event=True)
            with result_lock:
                results.append(outcome)

        workers = [threading.Thread(target=ingest) for _ in range(12)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual(1, sum(int(r.is_new) for r in results))
        count = self.db.one(
            "SELECT COUNT(*) AS n FROM messages WHERE guid=?",
            ("concurrent-guid",),
        )
        self.assertEqual(1, count["n"])
        self.assertEqual(1, len(self.repo.pending_delivery_events()))

    def test_fixed_rowid_windows_resume_without_holes(self):
        rows = [
            raw_message("r1", 1),
            raw_message("r100", 100),
            raw_message("r101", 101),
            raw_message("r205", 205),
        ]
        client = FakeRowIDClient(rows)
        emitted = []

        first = scan_rowid_catchup(
            client, self.repo, emitted.extend, lambda: False,
            notify_new=False, max_windows=1)
        self.assertEqual(100, first.cursor)
        second = scan_rowid_catchup(
            client, self.repo, emitted.extend, lambda: False,
            notify_new=False, max_windows=1)
        self.assertEqual(200, second.cursor)
        third = scan_rowid_catchup(
            client, self.repo, emitted.extend, lambda: False,
            notify_new=False, max_windows=1)
        self.assertEqual(205, third.cursor)
        self.assertEqual(
            {"r1", "r100", "r101", "r205"},
            {r["guid"] for r in self.db.query("SELECT guid FROM messages")},
        )

    def test_rowid_second_pass_repairs_middle_omission_and_keeps_full_copy(self):
        full_one = raw_message("r1", 1, text="complete payload")
        row_two = raw_message("r2", 2)
        row_three = raw_message("r3", 3)

        class FlakyWindowClient(FakeRowIDClient):
            def __init__(self):
                super().__init__([full_one, row_two, row_three])
                self.calls = 0

            def query_messages_rowid_range(self, low, high):
                self.calls += 1
                if self.calls == 1:
                    return [full_one, row_three]
                # The retry both restores the omitted middle row and returns
                # a worse duplicate for row 1. The merge must keep the full.
                return [
                    {"guid": "r1", "originalROWID": 1},
                    row_two, row_three,
                ]

        client = FlakyWindowClient()
        result = scan_rowid_catchup(
            client, self.repo, lambda _items: None, lambda: False,
            notify_new=False)
        self.assertEqual(3, result.cursor)
        self.assertGreaterEqual(client.calls, 2)
        self.assertEqual(
            {"r1", "r2", "r3"},
            {r["guid"] for r in self.db.query("SELECT guid FROM messages")})
        self.assertEqual(
            "complete payload",
            self.db.one("SELECT text FROM messages WHERE guid='r1'")["text"])

    def test_rolling_archive_audit_repairs_old_hole_without_stale_alert(self):
        self.repo.set_meta(NOTIFICATION_FLOOR_KEY, 300)
        client = FakeRowIDClient([
            raw_message("old-omission", 50),
            raw_message("snapshot", 301),
        ])
        summary = scan_rowid_archive_audit(
            client, self.repo, lambda _items: None, lambda: False,
            notify_new=True)
        self.assertEqual(100, summary.cursor)
        self.assertIsNotNone(self.db.one(
            "SELECT 1 FROM messages WHERE guid='old-omission'"))
        self.assertEqual([], self.repo.pending_delivery_events())

    def test_recent_head_repairs_a_known_edited_message(self):
        original = self.parsed("edited-head", 1, text="old text")
        self.repo.upsert_message(original, notify_eligible=False)
        edited = raw_message("edited-head", 1, text="corrected text")
        edited["isEdited"] = True
        batches = []
        summary = scan_recent_head(
            FakeRowIDClient([edited]), self.repo, batches.extend,
            lambda: False, notify_new=True)
        self.assertEqual(1, summary.changed)
        self.assertEqual(
            "corrected text",
            self.db.one(
                "SELECT text FROM messages WHERE guid='edited-head'")["text"])
        self.assertEqual(1, len(batches))

    def test_late_icloud_row_with_old_date_is_still_discovered(self):
        client = FakeRowIDClient([raw_message("newer", 1, NOW)])
        scan_rowid_catchup(
            client, self.repo, lambda _items: None, lambda: False,
            notify_new=False)

        # It appears in chat.db later, but keeps a date far outside the old
        # 24-hour overlap. A timestamp cursor skipped this forever; ROWID 2
        # must still be imported and produce a running-session delivery event.
        old_date = NOW - 90 * 24 * 60 * 60 * 1000
        client.rows.append(raw_message("late-old", 2, old_date))
        result = scan_rowid_catchup(
            client, self.repo, lambda _items: None, lambda: False,
            notify_new=True)
        self.assertEqual(1, result.new)
        stored = self.db.one(
            "SELECT source_rowid,date_created FROM messages WHERE guid=?",
            ("late-old",),
        )
        self.assertEqual(2, stored["source_rowid"])
        self.assertEqual(old_date, stored["date_created"])
        self.assertEqual(1, len(self.repo.pending_delivery_events()))

    def test_new_row_alerts_while_initial_history_is_still_indexing(self):
        client = FakeRowIDClient([
            raw_message("historical-1", 1),
            raw_message("historical-2", 2),
        ])
        self.assertEqual(
            2, ensure_notification_floor(client, self.repo, notify_new=False))
        scan_rowid_catchup(
            client, self.repo, lambda _items: None, lambda: False,
            notify_new=False)
        self.assertEqual([], self.repo.pending_delivery_events())

        # Alerting must not wait for a potentially large archive to finish.
        client.rows.append(raw_message("arrived-during-index", 3))
        scan_rowid_catchup(
            client, self.repo, lambda _items: None, lambda: False,
            notify_new=False)
        events = self.repo.pending_delivery_events()
        self.assertEqual(
            ["arrived-during-index"], [e["message_guid"] for e in events])

    def test_global_head_respects_first_run_notification_floor(self):
        client = FakeRowIDClient([
            raw_message("historical-head", 1),
            raw_message("floor-head", 2),
        ])
        ensure_notification_floor(client, self.repo, notify_new=False)
        client.rows.append(raw_message("new-head", 3, NOW + 1000))
        scan_recent_head(
            client, self.repo, lambda _items: None, lambda: False,
            notify_new=False)
        self.assertEqual(
            ["new-head"],
            [e["message_guid"] for e in self.repo.pending_delivery_events()],
        )

    def test_zero_notification_floor_allows_first_later_message(self):
        client = FakeRowIDClient([])
        with self.assertRaises(NotificationBaselinePending):
            ensure_notification_floor(
                client, self.repo, notify_new=False)
        self.repo.set_meta(
            "notification_empty_baseline_first_seen_ms",
            int(time.time() * 1000) - 10_000)
        self.assertEqual(
            0, ensure_notification_floor(
                client, self.repo, notify_new=False))
        client.rows.append(raw_message("first-ever", 1))
        scan_recent_head(
            client, self.repo, lambda _items: None, lambda: False,
            notify_new=False)
        self.assertEqual(
            ["first-ever"],
            [e["message_guid"] for e in self.repo.pending_delivery_events()],
        )

    def test_transient_empty_first_snapshot_does_not_arm_old_history(self):
        client = FakeRowIDClient([])
        with self.assertRaises(NotificationBaselinePending):
            ensure_notification_floor(client, self.repo, notify_new=False)
        self.assertIsNone(self.repo.meta(NOTIFICATION_FLOOR_KEY))

        # Messages finishes relaunching and exposes its existing history
        # during the confirmation window. It must become the quiet floor.
        client.rows.append(raw_message("existing-after-relaunch", 1))
        self.assertEqual(
            1, ensure_notification_floor(
                client, self.repo, notify_new=False))
        scan_rowid_catchup(
            client, self.repo, lambda _items: None, lambda: False,
            notify_new=False)
        self.assertEqual([], self.repo.pending_delivery_events())

    def test_lower_max_requires_three_confirmations_before_reset(self):
        self.repo.set_meta(ROWID_CURSOR_KEY, 100)
        self.repo.set_meta(NOTIFICATION_FLOOR_KEY, 100)
        lower = FakeRowIDClient([raw_message("reset-row", 50)])
        for _ in range(2):
            with self.assertRaises(IncompleteRowIDSnapshot):
                scan_rowid_catchup(
                    lower, self.repo, lambda _items: None, lambda: False,
                    notify_new=False)
            self.assertEqual(100, self.repo.meta_int(ROWID_CURSOR_KEY))
            self.assertEqual(100, self.repo.meta_int(NOTIFICATION_FLOOR_KEY))

        result = scan_rowid_catchup(
            lower, self.repo, lambda _items: None, lambda: False,
            notify_new=False)
        self.assertEqual(50, result.cursor)
        self.assertEqual(50, self.repo.meta_int(NOTIFICATION_FLOOR_KEY))
        self.assertEqual([], self.repo.pending_delivery_events())

    def test_rowid_cursor_does_not_advance_if_server_ignores_bounds(self):
        class BrokenClient(FakeRowIDClient):
            def query_messages_rowid_range(self, low, high):
                return [raw_message("outside", high + 1)]

        with self.assertRaises(RowIDProtocolError):
            scan_rowid_catchup(
                BrokenClient([raw_message("max", 250)]),
                self.repo, lambda _items: None, lambda: False,
                notify_new=False)
        self.assertEqual(0, self.repo.meta_int("source_rowid_cursor", 0))

    def test_incomplete_final_snapshot_is_retried_without_cursor_advance(self):
        rows = [raw_message("first", 1), raw_message("snapshot", 2)]

        class IncompleteClient(FakeRowIDClient):
            omit = True

            def query_messages_rowid_range(self, low, high):
                result = super().query_messages_rowid_range(low, high)
                if self.omit:
                    return [r for r in result
                            if int(r.get("originalROWID") or 0) != high]
                return result

        client = IncompleteClient(rows)
        with self.assertRaises(IncompleteRowIDSnapshot):
            scan_rowid_catchup(
                client, self.repo, lambda _items: None, lambda: False,
                notify_new=False)
        self.assertEqual(0, self.repo.meta_int("source_rowid_cursor", 0))
        # A transient omission keeps the authoritative transport undecided;
        # only a real protocol rejection may force compatibility mode.
        self.assertEqual(-1, self.repo.meta_int("rowid_sync_supported", -1))
        self.assertEqual([], self.db.query("SELECT guid FROM messages"))

        client.omit = False
        result = scan_rowid_catchup(
            client, self.repo, lambda _items: None, lambda: False,
            notify_new=False)
        self.assertEqual(2, result.cursor)
        self.assertEqual(
            {"first", "snapshot"},
            {r["guid"] for r in self.db.query("SELECT guid FROM messages")},
        )

    def test_global_head_repairs_a_cursor_and_timestamp_blind_spot(self):
        # Simulate bad durable cursors already beyond the missing message.
        self.repo.set_meta("source_rowid_cursor", 999)
        self.repo.upsert_message(
            self.parsed("future-local", 999, NOW + 7 * 86_400_000))
        sibling = "SMS;-;+15555550100"
        missing = raw_message(
            "globally-recovered", 10, NOW, chat=sibling)
        client = FakeRowIDClient([missing])
        emitted = []

        result = scan_recent_head(
            client, self.repo, emitted.extend, lambda: False,
            notify_new=True)
        self.assertEqual(1, result.new)
        stored = self.db.one(
            "SELECT chat_guid FROM messages WHERE guid=?",
            ("globally-recovered",),
        )
        self.assertEqual(sibling, stored["chat_guid"])
        self.assertEqual(1, len(self.repo.pending_delivery_events()))

        # Repeating the independent scan is idempotent.
        again = scan_recent_head(
            client, self.repo, emitted.extend, lambda: False,
            notify_new=True)
        self.assertEqual(0, again.new)
        self.assertEqual(1, len(self.repo.pending_delivery_events()))

    def test_hide_acknowledges_pending_unread_event(self):
        message = self.parsed("hide-race", 1)
        self.repo.upsert_message(message, notify_eligible=True)
        self.repo.hide_chat(CHAT)
        self.assertFalse(
            self.repo.apply_unread_event("hide-race", chat_is_open=False))
        row = self.db.one(
            "SELECT hidden,unread FROM chats WHERE guid=?", (CHAT,))
        self.assertEqual((1, 0), (row["hidden"], row["unread"]))

    def test_incomplete_row_is_quarantined_then_retried_by_guid(self):
        incomplete = raw_message("repair-me", 1)
        incomplete.pop("dateCreated")

        class RepairClient(FakeRowIDClient):
            def query_message_guid(self, guid):
                return [raw_message(guid, 1)]

        client = RepairClient([incomplete])
        result = scan_rowid_catchup(
            client, self.repo, lambda _items: None, lambda: False,
            notify_new=False)
        self.assertEqual(1, result.cursor)
        self.assertEqual(1, len(self.repo.sync_failures()))
        self.assertEqual(
            1,
            retry_quarantined(
                client, self.repo, lambda _items: None, lambda: False,
                notify_new=False),
        )
        self.assertIsNotNone(
            self.db.one("SELECT guid FROM messages WHERE guid='repair-me'"))
        self.assertEqual([], self.repo.sync_failures())

    def test_equal_timestamp_local_pages_do_not_drop_boundary_rows(self):
        for n in range(70):
            self.repo.upsert_message(
                self.parsed(f"same-{n:03d}", n + 1, NOW))
        page1 = self.repo.messages_window(CHAT, None, 60)
        key = (page1[0]["date_created"], page1[0]["guid"])
        page2 = self.repo.messages_window(CHAT, key, 60)
        guids = {r["guid"] for r in page1 + page2}
        self.assertEqual(70, len(guids))

    def test_complete_payload_repairs_attachment_and_marks_changed(self):
        initial = self.repo.upsert_message(self.parsed("attach-guid", 1))
        self.assertTrue(initial.changed)
        attachment = {
            "guid": "attachment-1",
            "mimeType": "image/jpeg",
            "transferName": "photo.jpg",
            "totalBytes": 1234,
            "width": 640,
            "height": 480,
        }
        repaired = self.repo.upsert_message(
            self.parsed(
                "attach-guid", 1, attachments=[attachment]))
        self.assertFalse(repaired.is_new)
        self.assertTrue(repaired.changed)
        self.assertIsNotNone(self.repo.attachment("attachment-1"))

    def test_sparse_update_does_not_clear_omitted_authoritative_flags(self):
        full = raw_message("sparse-merge", 7, from_me=True)
        full["itemType"] = 1
        full["error"] = 9
        self.repo.upsert_message(parse_message(full))

        sparse = raw_message("sparse-merge", 7, text="updated")
        sparse.pop("isFromMe")
        sparse.pop("itemType", None)
        sparse.pop("error", None)
        self.repo.upsert_message(parse_message(sparse))

        stored = self.db.one(
            "SELECT is_from_me,item_type,error,text FROM messages WHERE guid=?",
            ("sparse-merge",),
        )
        self.assertEqual(1, stored["is_from_me"])
        self.assertEqual(1, stored["item_type"])
        self.assertEqual(9, stored["error"])
        self.assertEqual("updated", stored["text"])

    def test_persisted_outbox_recovers_only_safe_queued_work(self):
        safe = self.repo.enqueue(CHAT, "safe", None)
        uncertain = self.repo.enqueue(CHAT, "uncertain", None)
        self.repo.outbox_set(uncertain, "sending")
        self.assertEqual([safe], self.repo.recover_outbox())
        row = self.repo.outbox_row(uncertain)
        self.assertEqual("failed", row["state"])
        self.assertIn("uncertain", row["last_error"].lower())

    def test_live_retired_sender_is_not_reclassified_during_recovery(self):
        uncertain = self.repo.enqueue(CHAT, "still in flight", None)
        self.repo.outbox_set(uncertain, "sending")
        self.repo.recover_outbox(mark_sending_uncertain=False)
        self.assertEqual("sending", self.repo.outbox_row(uncertain)["state"])

    def test_kick_outbox_semantics_resubmit_queued_only(self):
        # The window enqueues while the agent channel is down; on reconnect
        # the agent submits queued rows and must leave in-flight rows alone.
        queued = self.repo.enqueue(CHAT, "waiting", None)
        flying = self.repo.enqueue(CHAT, "on the wire", None)
        self.repo.outbox_set(flying, "sending")
        resubmitted = self.repo.recover_outbox(mark_sending_uncertain=False)
        self.assertEqual([queued], resubmitted)
        self.assertEqual("sending", self.repo.outbox_row(flying)["state"])

    def test_sent_message_and_outbox_completion_commit_together(self):
        oid = self.repo.enqueue(CHAT, "send", None)
        self.repo.outbox_set(oid, "sending")
        message = self.parsed("server-guid", 50, from_me=True)
        self.repo.upsert_message(message, complete_outbox_id=oid)
        outbox = self.repo.outbox_row(oid)
        self.assertEqual("sent", outbox["state"])
        self.assertEqual("server-guid", outbox["server_guid"])
        self.assertIsNotNone(
            self.db.one("SELECT guid FROM messages WHERE guid='server-guid'"))

    def test_parser_rejects_sparse_message_instead_of_inventing_now(self):
        raw = raw_message("sparse", 1)
        raw.pop("dateCreated")
        self.assertIsNone(parse_message(raw))


class AutoWakePolicyTests(unittest.TestCase):
    """Every gate of the automatic Wake Mac, in isolation."""

    def base(self, **overrides):
        values = dict(
            now=10_000.0, interval_minutes=30, connected=True,
            poll_healthy=True, outbox_active=0, busy=False,
            last_incoming_ts=0.0, last_wake_ts=0.0)
        values.update(overrides)
        return AutoWakeInputs(**values)

    def test_wakes_after_full_quiet_interval(self):
        self.assertTrue(should_auto_wake(self.base()))

    def test_disabled_when_interval_is_zero(self):
        self.assertFalse(should_auto_wake(self.base(interval_minutes=0)))

    def test_recent_incoming_message_prevents_wake(self):
        self.assertFalse(should_auto_wake(
            self.base(last_incoming_ts=10_000.0 - 29 * 60)))

    def test_at_most_one_wake_per_quiet_interval(self):
        self.assertFalse(should_auto_wake(
            self.base(last_wake_ts=10_000.0 - 29 * 60)))
        self.assertTrue(should_auto_wake(
            self.base(last_wake_ts=10_000.0 - 31 * 60,
                      last_incoming_ts=10_000.0 - 31 * 60)))

    def test_active_send_blocks_wake(self):
        # Quitting Messages mid-send could interrupt a delivery to a client.
        self.assertFalse(should_auto_wake(self.base(outbox_active=1)))

    def test_busy_recovery_or_wake_blocks_wake(self):
        self.assertFalse(should_auto_wake(self.base(busy=True)))

    def test_unreachable_or_unconfigured_backend_blocks_wake(self):
        self.assertFalse(should_auto_wake(self.base(connected=False)))
        self.assertFalse(should_auto_wake(self.base(poll_healthy=False)))


class AgentChannelSerializationTests(unittest.TestCase):
    def test_feed_reassembles_split_and_concatenated_lines(self):
        buf = bytearray()
        first = serialize.encode({"event": "a"})
        second = serialize.encode({"event": "b"})
        blob = first + second
        got = []
        got += serialize.feed(buf, blob[:3])
        got += serialize.feed(buf, blob[3:len(first) + 2])
        got += serialize.feed(buf, blob[len(first) + 2:])
        self.assertEqual([{"event": "a"}, {"event": "b"}], got)
        self.assertEqual(0, len(buf))

    def test_feed_drops_garbage_without_losing_later_lines(self):
        buf = bytearray()
        blob = b"not json\n" + serialize.encode({"event": "ok"})
        self.assertEqual([{"event": "ok"}], serialize.feed(buf, blob))

    def test_slim_batch_carries_reaction_fields_and_never_raw(self):
        parsed = parse_message(raw_message("slim-1", 4, text="hello"))
        slim = serialize.slim_batch([(parsed, True, True)])
        self.assertEqual(1, len(slim))
        item = slim[0]
        self.assertEqual(
            {"guid", "chat_guid", "date_created", "is_from_me", "text",
             "is_new", "changed"}, set(item))
        self.assertEqual("slim-1", item["guid"])
        self.assertEqual(CHAT, item["chat_guid"])
        self.assertTrue(item["is_new"])
        # raw payloads and internal underscore fields must never cross the
        # channel; the window reads full rows from the shared database.
        self.assertNotIn("raw", item)
        self.assertNotIn("_present_fields", item)

    def test_slim_batch_survives_malformed_items(self):
        parsed = parse_message(raw_message("slim-2", 5))
        slim = serialize.slim_batch(
            [None, ("bad",), (parsed, False), 7, (None, True, True)])
        self.assertEqual(1, len(slim))
        self.assertFalse(slim[0]["is_new"])
        self.assertTrue(slim[0]["changed"])


class ResponsiveBubbleTests(unittest.TestCase):
    def setUp(self):
        from app.ui import theme
        self.theme = theme
        self._scale = theme._SCALE

    def tearDown(self):
        self.theme._SCALE = self._scale

    def test_bubbles_track_the_pane_between_floor_and_ceiling(self):
        limit = self.theme.responsive_bubble_limit
        self.assertEqual(240, limit(0))          # floor for tiny panes
        mid = limit(800)
        self.assertEqual(int((800 - 36) * self.theme.BUBBLE_PANE_FRAC), mid)
        self.assertLess(limit(700), limit(900))  # grows with the window
        self.assertEqual(self.theme.BUBBLE_MAX_BASE_PX, limit(3000))

    def test_readability_ceiling_scales_with_text_size(self):
        limit = self.theme.responsive_bubble_limit
        self.theme._SCALE = 1.5
        self.assertEqual(
            int(round(self.theme.BUBBLE_MAX_BASE_PX * 1.5)), limit(3000))
        # A larger font also deserves a larger cap than the default scale.
        self.theme._SCALE = 1.0
        self.assertLess(limit(3000),
                        int(round(self.theme.BUBBLE_MAX_BASE_PX * 1.5)))

    def test_manual_bubble_width_setting_is_fully_removed(self):
        theme_src = (ROOT / "app" / "ui" / "theme.py").read_text(
            encoding="utf-8")
        dialog_src = (ROOT / "app" / "ui" / "settings_dialog.py").read_text(
            encoding="utf-8")
        thread_src = (ROOT / "app" / "ui" / "thread_view.py").read_text(
            encoding="utf-8")
        config_src = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
        for source in (theme_src, dialog_src, thread_src, config_src):
            self.assertNotIn("BUBBLE_WIDTHS", source)
            self.assertNotIn("bubble_width", source)
        self.assertNotIn("Bubble width", dialog_src)
        self.assertNotIn("get_bubble_px", dialog_src)
        self.assertIn("responsive_bubble_limit", thread_src)
        # The live refit path that reflows bubbles on resize must remain.
        self.assertIn("def resizeEvent", thread_src)
        self.assertIn("_refit_all", thread_src)
        self.assertIn("def refit", thread_src)

    def test_stale_config_with_retired_key_loads_cleanly(self):
        import json
        from app import config, constants
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "config.json"
            fake.write_text(json.dumps({
                "server_url": "http://mac.test:1234",
                "bubble_width": "Extra wide",
                "accent": "Teal",
            }), encoding="utf-8")
            original = constants.CONFIG_PATH
            constants.CONFIG_PATH = fake
            try:
                settings = config.load()
            finally:
                constants.CONFIG_PATH = original
        self.assertEqual("http://mac.test:1234", settings.server_url)
        self.assertEqual("Teal", settings.accent)
        self.assertFalse(hasattr(settings, "bubble_width"))
        self.assertEqual(constants.AUTO_WAKE_DEFAULT_MIN,
                         settings.auto_wake_minutes)


class UpgradeMigrationTests(unittest.TestCase):
    def test_v1_database_gets_new_reliability_columns_and_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            c = sqlite3.connect(path)
            c.executescript(
                """
                CREATE TABLE chats (
                  guid TEXT PRIMARY KEY, display_name TEXT,
                  is_group INTEGER NOT NULL DEFAULT 0, participants TEXT,
                  last_activity INTEGER, unread INTEGER NOT NULL DEFAULT 0,
                  archived INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE messages (
                  guid TEXT PRIMARY KEY, chat_guid TEXT NOT NULL,
                  sender_address TEXT, is_from_me INTEGER NOT NULL DEFAULT 0,
                  text TEXT, subject TEXT, service TEXT,
                  date_created INTEGER NOT NULL, date_delivered INTEGER,
                  date_read INTEGER, is_edited INTEGER NOT NULL DEFAULT 0,
                  is_retracted INTEGER NOT NULL DEFAULT 0,
                  thread_originator_guid TEXT, associated_guid TEXT,
                  associated_type INTEGER, item_type INTEGER NOT NULL DEFAULT 0,
                  error INTEGER NOT NULL DEFAULT 0, raw TEXT
                );
                INSERT INTO chats(guid,participants) VALUES(
                  'iMessage;-;+15555550100','["+15555550100"]');
                INSERT INTO messages(
                  guid,chat_guid,is_from_me,text,date_created,
                  is_edited,is_retracted,item_type,error
                ) VALUES(
                  'old-searchable','iMessage;-;+15555550100',0,
                  'needleword',1800000000000,0,0,0,0);
                """
            )
            c.commit()
            c.close()

            db = Database(path)
            message_columns = {
                row["name"] for row in db.query("PRAGMA table_info(messages)")
            }
            chat_columns = {
                row["name"] for row in db.query("PRAGMA table_info(chats)")
            }
            tables = {
                row["name"] for row in db.query(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertIn("source_rowid", message_columns)
            self.assertIn("first_seen_ms", message_columns)
            self.assertIn("hidden", chat_columns)
            self.assertIn("delivery_events", tables)
            self.assertIn("sync_failures", tables)
            self.assertEqual(1, len(Repo(db).search('"needleword"')))
            db.conn().close()


class VerificationCodeTests(unittest.TestCase):
    def test_win32_input_layout_matches_x86_and_x64_abi(self):
        Input32, _Keyboard32 = input_types(ctypes.c_uint32)
        Input64, _Keyboard64 = input_types(ctypes.c_uint64)
        self.assertEqual(28, ctypes.sizeof(Input32))
        self.assertEqual(40, ctypes.sizeof(Input64))

    def test_fill_builds_only_unicode_down_up_pairs(self):
        Input, events = keyboard_inputs("123456", ctypes.c_uint64)
        self.assertEqual(40, ctypes.sizeof(Input))
        self.assertEqual(12, len(events))
        scans = [event.u.ki.wScan for event in events]
        self.assertEqual([ord(c) for c in "112233445566"], scans)
        flags = [event.u.ki.dwFlags for event in events]
        self.assertEqual([0x0004, 0x0006] * 6, flags)
        self.assertNotIn(13, scans)  # Fill never submits Enter.

    def test_bare_code(self):
        self.assertEqual("123456", extract_code("123456", "+15555550100"))

    def test_spaced_code_is_normalized(self):
        self.assertEqual(
            "774210", extract_code("Your verification code is 774 210"))

    def test_explicit_code_wins_over_other_numbers(self):
        self.assertEqual(
            "482913",
            extract_code(
                "Expires in 10 minutes. Your login code is 482913.",
                "+15555550100"))

    def test_code_after_unrelated_order_number_wins(self):
        self.assertEqual(
            "9876",
            extract_code("Order 123456; code 9876", "+15555550100"))

    def test_code_before_keyword_and_unicode_spacing(self):
        self.assertEqual(
            "774210",
            extract_code("774 210 is your verification code", "28849"))

    def test_short_code_sender_is_enough_context(self):
        self.assertEqual(
            "53721", extract_code("Use 53721 to continue", "28849"))

    def test_normal_conversation_number_is_not_code(self):
        self.assertIsNone(
            extract_code("The settlement proposal is 125000 dollars.",
                         "+15555550100"))

    def test_reference_number_is_not_promoted_to_code(self):
        self.assertIsNone(
            extract_code("Your confirmation number is 847261.",
                         "+15555550100"))


class StaticUiCompletenessTests(unittest.TestCase):
    def test_thread_view_exposes_every_photo_action_once(self):
        path = ROOT / "app" / "ui" / "thread_view.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef)
                   and node.name == "ThreadView")
        methods = [node.name for node in cls.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for name in ("view_image", "copy_image", "save_image_as"):
            self.assertEqual(1, methods.count(name))
        self.assertEqual(1, methods.count("dragEnterEvent"))
        self.assertEqual(1, methods.count("dropEvent"))

    def test_notification_ui_contains_copy_fill_and_plain_text_guards(self):
        path = ROOT / "app" / "ui" / "notify.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn('QPushButton("Copy code")', source)
        self.assertIn('QPushButton("Fill code")', source)
        self.assertGreaterEqual(source.count("setTextFormat(Qt.PlainText)"), 3)
        self.assertIn("Qt.WindowDoesNotAcceptFocus", source)

    def test_primary_controls_are_labelled_scaled_and_accent_aware(self):
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        thread = (ROOT / "app" / "ui" / "thread_view.py").read_text(
            encoding="utf-8")
        for declaration in (
                'QPushButton("New")', 'QPushButton("Hidden")',
                'QPushButton("Recover")'):
            self.assertIn(declaration, main)
        apply_theme = main[
            main.index("    def apply_theme("):
            main.index("    # ------------------------------------------------ recover and wake")
        ]
        self.assertIn("self._style_left_actions()", apply_theme)
        self.assertGreaterEqual(main.count("setAccessibleName"), 4)
        self.assertIn("setIconSize(QSize(icon_px, icon_px))", main)
        self.assertIn('QPushButton("Most recent", self)', thread)
        self.assertIn('arrow_down("#ffffff")', thread)
        self.assertIn("border-radius: {h // 2}px", thread)
        self.assertNotIn("↓  Newest", thread)

    def test_manual_recovery_rebuilds_transport_and_forces_global_audit(self):
        source = (ROOT / "app" / "agent" / "core.py").read_text(
            encoding="utf-8")
        sync_source = (ROOT / "app" / "store" / "sync.py").read_text(
            encoding="utf-8")
        recovery = source[
            source.index("    def recover_messages(self):"):
            source.index("    def _kick_recovery_checks(self):")
        ]
        refresh_block = source[
            source.index("    def _cmd_refresh_chat("):
            source.index("    def reload_settings(")
        ]
        self.assertIn("self.stop_backend()", recovery)
        self.assertIn("self.start_backend()", recovery)
        self.assertIn("poke(chats=True, head=True)",
                      source[source.index("    def _kick_recovery_checks"):
                             source.index("    def _maybe_finish_manual_recovery")])
        self.assertIn("poke(chats=True, head=True)", refresh_block)
        self.assertIn("self._backend_generation += 1", source)
        self.assertIn("self._manual_poll_ok", recovery)
        self.assertIn("self._manual_sync_audit_done", recovery)
        self.assertIn("self._maybe_finish_manual_recovery()", source)
        self.assertIn("recovery_audit_done.emit()", sync_source)


@unittest.skipIf(httpx is None, "httpx is not installed in this environment")
class WakeMacClientTests(unittest.TestCase):
    """The wake call must hit the documented BlueBubbles route, carry the
    password, and surface a too-old server as a clean, actionable error."""

    def _client_with(self, handler):
        from app.api.rest import BBClient
        client = BBClient("http://mac.test", "secret")
        client._c.close()
        client._c = httpx.Client(transport=httpx.MockTransport(handler))
        return client

    def test_restart_uses_documented_route_and_password(self):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["password"] = request.url.params.get("password")
            return httpx.Response(
                200, json={"status": 200, "data": {"message": "ok"}})

        client = self._client_with(handler)
        try:
            data = client.restart_messages_app()
        finally:
            client.close()
        self.assertEqual("POST", seen["method"])
        self.assertEqual("/api/v1/mac/imessage/restart", seen["path"])
        self.assertEqual("secret", seen["password"])
        self.assertEqual({"message": "ok"}, data)

    def test_missing_route_maps_to_apierror_with_status(self):
        from app.api.rest import ApiError

        def handler(request):
            return httpx.Response(
                404, json={"status": 404,
                           "error": {"message": "Not found"}})

        client = self._client_with(handler)
        try:
            with self.assertRaises(ApiError) as ctx:
                client.restart_messages_app()
        finally:
            client.close()
        self.assertEqual(404, ctx.exception.status_code)


class StaticWakeAndComposerTests(unittest.TestCase):
    def test_wake_mac_machinery_lives_in_the_agent_with_its_guards(self):
        core = (ROOT / "app" / "agent" / "core.py").read_text(
            encoding="utf-8")
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        rest = (ROOT / "app" / "api" / "rest.py").read_text(encoding="utf-8")
        # The window keeps the button and shortcut and delegates the work.
        self.assertIn('QPushButton("Wake Mac")', main)
        self.assertIn('"Ctrl+Shift+M"', main)
        self.assertIn('{"cmd": "wake"}', main)
        self.assertIn('"/api/v1/mac/imessage/restart"', rest)
        wake = core[
            core.index("    def wake_mac(self"):
            core.index("    @Slot(bool, str, int)")
        ]
        # The agent must refuse to quit Messages while a send is active,
        # and must trigger the Mac restart rather than another local scan.
        self.assertIn("try_begin_mac_maintenance", wake)
        self.assertIn("_WakeWorker(self.client)", wake)
        self.assertIn("_wake_poll_verified", core)
        self.assertIn("verified post-restart scan", core)
        poke = core[
            core.index("    def _wake_poke("):
            core.index("    def _finish_wake(")
        ]
        self.assertIn("poke(chats=True, head=True)", poke)
        recover = core[
            core.index("    def _recover("):
            core.index("    def _kick_recovery_checks(")
        ]
        self.assertIn("self._cancel_wake()", recover)
        teardown = core[
            core.index("    def stop_backend(self):"):
            core.index("    @Slot()\n    def _clean_retired_backends(")
        ]
        self.assertIn("self._wake_worker", teardown)

    def test_auto_wake_is_wired_gated_and_configurable(self):
        core = (ROOT / "app" / "agent" / "core.py").read_text(
            encoding="utf-8")
        dialog = (ROOT / "app" / "ui" / "settings_dialog.py").read_text(
            encoding="utf-8")
        self.assertIn("should_auto_wake", core)
        self.assertIn("_auto_wake_timer", core)
        self.assertIn('origin="auto"', core)
        # The agent, not the window, resurrects hidden chats on new
        # activity, so it also happens while no window is open.
        self.assertIn("_resurrect_if_hidden", core)
        self.assertIn("Auto Wake Mac", dialog)
        self.assertIn("auto_wake_minutes", dialog)

    def test_composer_action_icons_are_large_and_scale_with_text(self):
        source = (ROOT / "app" / "ui" / "composer.py").read_text(
            encoding="utf-8")
        self.assertIn("_style_round_icon", source)
        self.assertIn("QSize(theme.dim(22), theme.dim(22))", source)
        self.assertIn("theme.dim(38)", source)
        # The stamp-sized 16 and 17 pixel icons must never return.
        self.assertNotIn("theme.dim(16)", source)
        self.assertNotIn("theme.dim(17)", source)

    def test_no_plain_string_double_braces_in_ui_stylesheets(self):
        """A '}}' inside a plain string mixed into an f-string chain renders
        as a literal doubled brace; Qt then silently drops every stylesheet
        rule after it. This shipped in 2.2.0 and disabled Recover's
        disabled/focus styling without any visible error."""
        import re
        pattern = re.compile(r'^\s*"[^"\n]*\}\}', re.MULTILINE)
        ui_dir = ROOT / "app" / "ui"
        for path in sorted(ui_dir.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            self.assertIsNone(
                pattern.search(source),
                f"{path.name}: plain string containing '}}}}' inside a "
                "stylesheet chain")


class StaticAgentSplitTests(unittest.TestCase):
    """The architectural contract of 3.0.0: the agent owns every worker
    and every reliability mechanism; the window owns none of them."""

    def test_window_process_hosts_no_workers_or_watchdog(self):
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        window_entry = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        for forbidden in ("SocketThread", "ReconcileThread", "SendThread",
                          "DownloadThread", "SyncThread", "QThread",
                          "_watchdog", "start_backend", "stop_backend"):
            self.assertNotIn(forbidden, main)
            self.assertNotIn(forbidden, window_entry)
        self.assertIn("AgentLink", main)
        # Durable sending: enqueue first, then ask the agent.
        send_block = main[
            main.index("    def _on_send("):
            main.index("    def _on_need_download(")
        ]
        self.assertLess(send_block.index("self.repo.enqueue"),
                        send_block.index('{"cmd": "submit_outbox"'))

    def test_agent_owns_every_worker_and_the_watchdog(self):
        core = (ROOT / "app" / "agent" / "core.py").read_text(
            encoding="utf-8")
        for required in ("SocketThread(", "ReconcileThread(", "SendThread(",
                         "DownloadThread(", "SyncThread(",
                         "def _watchdog_tick", "system resume",
                         "polling stalled", "worker dead",
                         "notification_baseline_complete"):
            self.assertIn(required, core)

    def test_worker_signal_wiring_uses_named_queued_receivers(self):
        source = (ROOT / "app" / "agent" / "core.py").read_text(
            encoding="utf-8")
        start_backend = source[
            source.index("    def start_backend(self):"):
            source.index("    def stop_backend(self):")
        ]
        full_sync = source[
            source.index("    def _start_full_sync(self):"):
            source.index("    # ------------------------------------------------ watchdog")
        ]
        self.assertNotIn("lambda", start_backend)
        self.assertNotIn("lambda", full_sync)
        self.assertGreaterEqual(
            (start_backend + full_sync).count("type=queued"), 20)

    def test_agent_entry_is_single_instance_and_supervised(self):
        agent_main = (ROOT / "app" / "agent" / "main.py").read_text(
            encoding="utf-8")
        supervisor = (ROOT / "agent_supervisor.pyw").read_text(
            encoding="utf-8")
        self.assertIn("QLockFile", agent_main)
        self.assertIn("AGENT_EXIT_DUPLICATE", agent_main)
        self.assertIn("--stop", agent_main)
        self.assertIn("setStaleLockTime(0)", agent_main)
        self.assertIn("def _wait_ready(timeout_s: float = 60.0)", agent_main)
        self.assertIn("Retiring agent version", agent_main)
        self.assertIn("_launch_installed_supervisor()", agent_main)
        self.assertIn("DUPLICATE_EXIT = 3", supervisor)
        self.assertIn("rc == DUPLICATE_EXIT", supervisor)
        self.assertIn("rc == 0", supervisor)   # clean stop ends supervision

    def test_notification_ledger_is_periodic_sound_and_never_silent_ack(self):
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        notify = (ROOT / "app" / "ui" / "notify.py").read_text(
            encoding="utf-8")
        config_src = (ROOT / "app" / "config.py").read_text(
            encoding="utf-8")
        self.assertIn("NOTIFICATION_SWEEP_MS", main)
        self.assertIn("PresentationResult.UNAVAILABLE", main)
        self.assertIn("play_notification_sound", main)
        self.assertIn("notification_sound", config_src)
        self.assertIn("PresentationResult.QUEUED", notify)
        self.assertNotIn("code_items[:-3]", main)
        window_entry = (ROOT / "app" / "main.py").read_text(
            encoding="utf-8")
        self.assertIn("setQuitOnLastWindowClosed(False)", window_entry)
        self.assertIn("QSystemTrayIcon.isSystemTrayAvailable()", main)

    def test_tooltips_are_limited_and_can_be_disabled(self):
        tips = (ROOT / "app" / "ui" / "tooltips.py").read_text(
            encoding="utf-8")
        chat_list = (ROOT / "app" / "ui" / "chat_list.py").read_text(
            encoding="utf-8")
        self.assertIn('("limited", "always", "off")', tips)
        self.assertIn("count >= 2", tips)
        self.assertIn("QApplication.mouseButtons()", tips)
        self.assertNotIn(
            "Click a conversation to open it. Hover the right edge",
            chat_list)

    def test_runtime_dependencies_include_socketio_polling_transport(self):
        requirements = (ROOT / "requirements.txt").read_text(
            encoding="utf-8").lower()
        self.assertIn("requests>=", requirements)

    def test_installers_register_agent_and_mac_package_is_complete(self):
        install = (ROOT / "install.bat").read_text(encoding="utf-8")
        self.assertIn("make_startup_launcher.py", install)
        self.assertIn("run_agent.py --stop", install)
        launcher = (ROOT / "tools" / "make_startup_launcher.py").read_text(
            encoding="utf-8")
        self.assertIn("agent_supervisor.pyw", launcher)
        keepalive = (ROOT / "mac" / "jrl-keepalive.sh").read_text(
            encoding="utf-8")
        installer = (ROOT / "mac" / "install-jrl-keepalive.sh").read_text(
            encoding="utf-8")
        uninstaller = (ROOT / "mac" / "uninstall-jrl-keepalive.sh").read_text(
            encoding="utf-8")
        self.assertIn("--restart-messages", keepalive)
        self.assertIn("wait_for_exit", keepalive)
        self.assertIn("wait_for_messages \"$old_pid\"", keepalive)
        self.assertIn("acquire_lock 90", keepalive)
        self.assertIn('last-$ACTION-error', keepalive)
        self.assertIn("NSAppSleepDisabled", installer)
        self.assertIn("id of app \"Messages\"", installer)
        self.assertIn("plutil -lint", installer)
        self.assertIn("launchctl kickstart", installer)
        self.assertIn("<integer>120</integer>", installer)
        for label in ("com.jrl.messages.keepalive",
                      "com.jrl.messages.dailyrestart"):
            self.assertIn(label, installer)
            self.assertIn(label, uninstaller)
        self.assertIn("StartInterval", installer)
        self.assertIn("StartCalendarInterval", installer)
        self.assertTrue((ROOT / "MAC-SETUP.md").exists())

    def test_channel_commands_cover_every_window_action(self):
        core = (ROOT / "app" / "agent" / "core.py").read_text(
            encoding="utf-8")
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        for cmd in ("submit_outbox", "kick_outbox", "download", "poke",
                    "poke_chat", "refresh_chat", "group_changed", "recover",
                    "wake", "settings_changed", "stop_agent"):
            self.assertIn(f'"{cmd}"', core)
        for sent in ('"cmd": "submit_outbox"', '"cmd": "download"',
                     '"cmd": "poke"', '"cmd": "refresh_chat"',
                     '"cmd": "recover"', '"cmd": "wake"',
                     '"cmd": "settings_changed"'):
            self.assertIn(sent, main)
        link = (ROOT / "app" / "ui" / "agent_link.py").read_text(
            encoding="utf-8")
        self.assertIn('{"cmd": "kick_outbox"}', link)

    def test_processes_log_to_separate_files(self):
        logging_src = (ROOT / "app" / "logging_setup.py").read_text(
            encoding="utf-8")
        agent_main = (ROOT / "app" / "agent" / "main.py").read_text(
            encoding="utf-8")
        self.assertIn('filename: str = "jrl-messages.log"', logging_src)
        self.assertIn('setup_logging(filename="jrl-agent.log")', agent_main)


if __name__ == "__main__":
    unittest.main()
