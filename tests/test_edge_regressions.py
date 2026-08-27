"""Focused regressions for cross-thread durability handoffs.

These tests deliberately exercise the awkward boundaries that ordinary happy
path tests miss: concurrent first-run floor responses, cancellation while the
Wake REST worker is still on the wire, asynchronous popup queue failure, and
the local-agent version handshake.  GUI-heavy classes are executed with tiny
fakes where the Linux test host cannot load QtGui.
"""
from __future__ import annotations

import ast
import enum
import logging
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path


# Keep the store regressions runnable before install.bat has installed the
# production dependency, matching tests/test_reliability.py.
try:
    import platformdirs  # noqa: F401
except ModuleNotFoundError:
    platformdirs = types.ModuleType("platformdirs")
    platformdirs.user_data_dir = lambda *_args, **_kwargs: tempfile.gettempdir()
    sys.modules["platformdirs"] = platformdirs


from app.store.db import Database
from app.store.reconcile_core import (
    ensure_notification_floor,
    scan_recent_head,
    scan_rowid_catchup,
)
from app.store.repo import Repo


ROOT = Path(__file__).parents[1]
CHAT = "iMessage;-;+15555550100"
NOW = 1_800_000_000_000


def raw_message(guid: str, rowid: int, text: str) -> dict:
    return {
        "guid": guid,
        "originalROWID": rowid,
        "dateCreated": NOW + rowid,
        "isFromMe": False,
        "text": text,
        "chats": [{"guid": CHAT}],
        "handle": {"address": "+15555550100"},
        "attachments": [],
    }


class _RepoCase(unittest.TestCase):
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


class FirstRunFloorRaceTests(_RepoCase):
    def test_authoritative_rescan_claims_event_above_concurrent_floor(self):
        """A delayed lower floor response must not silence an already stored row.

        Both callers observe no durable floor before issuing their requests.
        The response containing ROWID 2 finishes first, so a head scan stores
        row 2 as history.  The older ROWID-1 response then wins set_meta_min.
        The authoritative ROWID rescan must notice that existing row 2 is now
        above the final floor and create its one missing delivery event.
        """
        high_entered = threading.Event()
        low_entered = threading.Event()
        release_high = threading.Event()
        release_low = threading.Event()
        errors = []

        class FloorClient:
            def __init__(self, value, entered, release):
                self.value = value
                self.entered = entered
                self.release = release

            def max_message_rowid(self):
                self.entered.set()
                if not self.release.wait(2.0):
                    raise TimeoutError("floor response was not released")
                return self.value

        def freeze(client):
            try:
                ensure_notification_floor(client, self.repo, notify_new=False)
            except Exception as exc:  # surfaced with useful thread context
                errors.append(exc)

        high = threading.Thread(
            target=freeze,
            args=(FloorClient(2, high_entered, release_high),),
            daemon=True,
        )
        low = threading.Thread(
            target=freeze,
            args=(FloorClient(1, low_entered, release_low),),
            daemon=True,
        )
        high.start()
        low.start()
        try:
            self.assertTrue(high_entered.wait(1.0))
            self.assertTrue(low_entered.wait(1.0))

            release_high.set()
            high.join(2.0)
            self.assertFalse(high.is_alive())
            self.assertEqual(2, self.repo.meta_int("notification_baseline_rowid"))

            row_two = raw_message("arrived-during-floor-race", 2, "live row")

            class HeadClient:
                def query_messages(self, **_kwargs):
                    return [row_two]

            scan_recent_head(
                HeadClient(), self.repo, lambda _items: None, lambda: False,
                notify_new=False,
            )
            self.assertEqual([], self.repo.pending_delivery_events())

            release_low.set()
            low.join(2.0)
            self.assertFalse(low.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(1, self.repo.meta_int("notification_baseline_rowid"))

            row_one = raw_message("pre-install-history", 1, "old row")

            class AuthoritativeClient:
                def max_message_rowid(self):
                    return 2

                def query_messages_rowid_range(self, low_rowid, high_rowid):
                    return [
                        row for row in (row_one, row_two)
                        if low_rowid < row["originalROWID"] <= high_rowid
                    ]

            scan_rowid_catchup(
                AuthoritativeClient(), self.repo, lambda _items: None,
                lambda: False, notify_new=False,
            )
            self.assertEqual(
                ["arrived-during-floor-race"],
                [event["message_guid"]
                 for event in self.repo.pending_delivery_events()],
            )
        finally:
            release_high.set()
            release_low.set()
            high.join(2.0)
            low.join(2.0)

    def test_pruned_completed_event_is_not_recreated_by_rescan(self):
        """The message marker must outlive a completed ledger row.

        Reconciliation is deliberately allowed to repair an existing row
        whose first ingest missed its event.  Once both side effects completed
        and pruning removed that event, however, an authoritative replay must
        distinguish it from a never-recorded event and stay silent.
        """
        message = raw_message("completed-and-pruned", 1, "already alerted")

        class Client:
            def max_message_rowid(self):
                return 1

            def query_messages_rowid_range(self, low_rowid, high_rowid):
                return ([message]
                        if low_rowid < 1 <= high_rowid else [])

        self.repo.set_meta("notification_baseline_rowid", 0)
        scan_rowid_catchup(
            Client(), self.repo, lambda _items: None, lambda: False,
            notify_new=False,
        )
        self.assertEqual(
            ["completed-and-pruned"],
            [event["message_guid"]
             for event in self.repo.pending_delivery_events()],
        )

        self.repo.apply_unread_event(
            "completed-and-pruned", chat_is_open=True)
        self.repo.finish_notification_event("completed-and-pruned")
        self.assertEqual(
            1, self.repo.prune_delivery_events(int(time.time() * 1000) + 1))
        marker = self.db.one(
            "SELECT delivery_event_recorded FROM messages WHERE guid=?",
            ("completed-and-pruned",))
        self.assertEqual(1, marker["delivery_event_recorded"])

        # Rewind only the authoritative scan cursor, simulating a full audit
        # or ROWID generation replay of this still-present message.
        self.repo.set_meta("source_rowid_cursor", 0)
        scan_rowid_catchup(
            Client(), self.repo, lambda _items: None, lambda: False,
            notify_new=False,
        )
        self.assertEqual([], self.repo.pending_delivery_events())
        self.assertIsNone(self.db.one(
            "SELECT 1 FROM delivery_events WHERE message_guid=?",
            ("completed-and-pruned",)))


try:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
except (ImportError, ModuleNotFoundError):
    QCoreApplication = None
    QLocalSocket = None


@unittest.skipIf(QCoreApplication is None, "PySide6 QtCore is unavailable")
class WakeMaintenanceLeaseTests(_RepoCase):
    def test_cancel_keeps_lease_until_old_wake_worker_finishes(self):
        from app import config
        from app.agent.core import AgentCore

        app = QCoreApplication.instance() or QCoreApplication([])

        class BlockingWakeClient:
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()

            def restart_messages_app(self):
                self.started.set()
                if not self.release.wait(4.0):
                    raise TimeoutError("test Wake worker was not released")
                return {"success": True}

            def close(self):
                pass

        client = BlockingWakeClient()
        core = AgentCore(self.repo, config.Settings())
        core.client = client
        worker = None
        try:
            self.assertTrue(core.wake_mac(origin="manual"))
            worker = core._wake_worker
            self.assertTrue(client.started.wait(2.0))
            self.assertTrue(self.repo.mac_maintenance_active())

            core._cancel_wake(kick=False)
            self.assertEqual("idle", core._wake_state)
            self.assertTrue(
                self.repo.mac_maintenance_active(),
                "cancellation exposed the outbox while the old Wake request "
                "could still restart Messages",
            )
            self.assertFalse(
                core.wake_mac(origin="manual"),
                "a second Wake replaced a canceled worker whose REST call "
                "was still in flight",
            )
            self.assertIs(worker, core._wake_worker)

            oid = self.repo.enqueue(CHAT, "must remain fenced", None)
            self.assertIsNone(self.repo.claim_outbox(oid))

            client.release.set()
            self.assertTrue(worker.wait(4000))
            deadline = time.monotonic() + 2.0
            while (self.repo.mac_maintenance_active()
                   and time.monotonic() < deadline):
                app.processEvents()
                time.sleep(0.01)
            self.assertFalse(self.repo.mac_maintenance_active())
            self.assertIsNotNone(self.repo.claim_outbox(oid))
        finally:
            client.release.set()
            if worker is not None and worker.isRunning():
                worker.wait(5000)
            for _ in range(3):
                app.processEvents()
            core.shutdown()


class PopupQueueFailureTests(unittest.TestCase):
    @staticmethod
    def _popup_namespace():
        """Execute the production manager without importing QtGui/EGL."""
        tree = ast.parse((ROOT / "app" / "ui" / "notify.py").read_text(
            encoding="utf-8"))
        selected = [
            node for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name in {"PresentationResult", "PopupManager"}
        ]

        class Signal:
            def __init__(self):
                self.callbacks = []

            def connect(self, callback):
                self.callbacks.append(callback)

        class PlaceholderPopup:
            pass

        class Timer:
            @staticmethod
            def singleShot(_delay, callback):
                callback()

        class GuiApplication:
            screenAt = staticmethod(lambda _position: None)
            primaryScreen = staticmethod(lambda: None)

        class Cursor:
            pos = staticmethod(lambda: None)

        class ApplicationStub:
            # The 3.1.1 hard cap counts real card widgets on screen; in this
            # widgetless harness there are none.
            topLevelWidgets = staticmethod(lambda: [])

        namespace = {
            "Enum": enum.Enum,
            "NotificationPopup": PlaceholderPopup,
            "QTimer": Timer,
            "QGuiApplication": GuiApplication,
            "QCursor": Cursor,
            "QApplication": ApplicationStub,
            "log": logging.getLogger("popup-regression"),
            "Signal": Signal,
            "time": time,
        }
        module = ast.fix_missing_locations(
            ast.Module(body=selected, type_ignores=[]))
        exec(compile(module, "notify-popup-subset", "exec"), namespace)
        namespace["SignalFake"] = Signal
        return namespace

    def test_later_queued_constructor_or_show_failure_rejects_event(self):
        for failure in ("constructor", "show"):
            with self.subTest(failure=failure):
                namespace = self._popup_namespace()
                rejected = []
                manager = namespace["PopupManager"](
                    lambda _guid: None,
                    on_presented=lambda _key: None,
                    on_rejected=rejected.append,
                )

                class ExistingPopup:
                    event_key = ""

                    def deleteLater(self):
                        pass

                    def height(self):
                        return 10

                    def width(self):
                        return 10

                    def move(self, *_args):
                        pass

                manager.active = [ExistingPopup() for _ in range(manager.MAX)]
                result = manager.show(
                    "Client", "queued text", None, CHAT,
                    event_key="durable-ledger-key",
                )
                self.assertIs(
                    result, namespace["PresentationResult"].QUEUED)

                if failure == "constructor":
                    class FailedPopup:
                        def __init__(self, *_args, **_kwargs):
                            raise RuntimeError("constructor failed")
                else:
                    Signal = namespace["SignalFake"]

                    class FailedPopup:
                        def __init__(self, *_args, **_kwargs):
                            self.event_key = "durable-ledger-key"
                            self.open_requested = Signal()
                            self.dismissed = Signal()

                        def show(self):
                            raise RuntimeError("show failed")

                        def raise_(self):
                            pass

                        def deleteLater(self):
                            pass

                namespace["NotificationPopup"] = FailedPopup
                with self.assertLogs("popup-regression", level="ERROR"):
                    manager._gone(manager.active[0])

                self.assertEqual(["durable-ledger-key"], rejected)
                self.assertEqual([], manager.pending)
                self.assertNotIn("durable-ledger-key", manager._event_keys)

        # Exercise the production MainWindow callback as a small, unbound
        # method.  Importing the full QWidget graph is not possible on the
        # headless CI image, but this still verifies the real cleanup logic
        # rather than duplicating it in a fake callback.
        tree = ast.parse((ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8"))
        window_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MainWindow")
        callback_node = next(
            node for node in window_class.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_on_popup_rejected")

        scheduled = []

        class Timer:
            @staticmethod
            def singleShot(delay, callback):
                scheduled.append((delay, callback))

        callback_namespace = {"QTimer": Timer}
        exec(compile(ast.fix_missing_locations(ast.Module(
            body=[callback_node], type_ignores=[])),
            "main-window-popup-rejection", "exec"), callback_namespace)

        drains = []
        window = types.SimpleNamespace(
            _popup_ack_groups={
                "durable-ledger-key": ["message-a", "message-b"]},
            _popup_pending_guids={"message-a", "message-b", "unrelated"},
            _popup_signalled_keys={"durable-ledger-key", "unrelated"},
            _drain_delivery_events=lambda: drains.append(True),
        )
        callback_namespace["_on_popup_rejected"](
            window, "durable-ledger-key")
        self.assertNotIn("durable-ledger-key", window._popup_ack_groups)
        self.assertEqual({"unrelated"}, window._popup_pending_guids)
        self.assertEqual({"unrelated"}, window._popup_signalled_keys)
        self.assertEqual(1, len(scheduled))
        self.assertEqual(5000, scheduled[0][0])
        scheduled[0][1]()
        self.assertEqual([True], drains)

    def test_failed_queued_popup_does_not_strand_the_next_request(self):
        namespace = self._popup_namespace()
        rejected = []
        presented = []
        manager = namespace["PopupManager"](
            lambda _guid: None,
            on_presented=presented.append,
            on_rejected=rejected.append,
        )

        class ExistingPopup:
            event_key = ""

            def deleteLater(self):
                pass

        manager.active = [ExistingPopup() for _ in range(manager.MAX)]
        first = manager.show(
            "First", "will fail", None, CHAT, event_key="failed-key")
        second = manager.show(
            "Second", "must continue", None, CHAT, event_key="next-key")
        self.assertIs(first, namespace["PresentationResult"].QUEUED)
        self.assertIs(second, namespace["PresentationResult"].QUEUED)

        Signal = namespace["SignalFake"]

        class SelectivePopup:
            def __init__(self, _title, _body, _code, _chat_guid, event_key):
                if event_key == "failed-key":
                    raise RuntimeError("first queued constructor failed")
                self.event_key = event_key
                self.open_requested = Signal()
                self.dismissed = Signal()

            def show(self):
                pass

            def raise_(self):
                pass

            def deleteLater(self):
                pass

        namespace["NotificationPopup"] = SelectivePopup
        with self.assertLogs("popup-regression", level="ERROR"):
            manager._gone(manager.active[0])

        self.assertEqual(["failed-key"], rejected)
        self.assertEqual(["next-key"], presented)
        self.assertEqual([], manager.pending)
        self.assertNotIn("failed-key", manager._event_keys)
        self.assertIn("next-key", manager._event_keys)
        self.assertEqual(3, len(manager.active))

    def test_toast_burst_uses_one_bounded_collection_deadline(self):
        source = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        self.assertIn("self._toast_timer.setInterval(900)", source)
        tree = ast.parse(source)
        window_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MainWindow")
        queue_node = next(
            node for node in window_class.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_queue_toast")
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(
            body=[queue_node], type_ignores=[])),
            "main-window-toast-queue", "exec"), namespace)

        class Timer:
            def __init__(self):
                self.active = False
                self.starts = 0

            def isActive(self):
                return self.active

            def start(self):
                self.starts += 1
                self.active = True

        timer = Timer()
        window = types.SimpleNamespace(
            _queued_event_guids=set(), _toast_queue=[], _toast_timer=timer)
        queue = namespace["_queue_toast"]
        for index in range(5):
            queue(
                window, f"event-{index}", "popup", "Title", "Body",
                None, CHAT)

        self.assertEqual(1, timer.starts)
        self.assertEqual(5, len(window._toast_queue))
        self.assertEqual(
            {f"event-{index}" for index in range(5)},
            window._queued_event_guids)

    def test_windows_sound_alias_failure_uses_message_beep(self):
        tree = ast.parse((ROOT / "app" / "ui" / "notify.py").read_text(
            encoding="utf-8"))
        sound_node = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "play_notification_sound")
        calls = []

        class Application:
            @staticmethod
            def beep():
                calls.append(("application-beep",))

        namespace = {
            "sys": types.SimpleNamespace(platform="win32"),
            "QApplication": Application,
            "log": logging.getLogger("sound-regression"),
        }
        exec(compile(ast.fix_missing_locations(ast.Module(
            body=[sound_node], type_ignores=[])),
            "notify-sound-subset", "exec"), namespace)

        winsound = types.ModuleType("winsound")
        winsound.SND_ALIAS = 0x1
        winsound.SND_ASYNC = 0x2
        winsound.SND_NODEFAULT = 0x4
        winsound.MB_ICONASTERISK = 0x40

        def play_sound(alias, flags):
            calls.append(("alias", alias, flags))
            raise RuntimeError("sound scheme alias missing")

        def message_beep(kind):
            calls.append(("message-beep", kind))

        winsound.PlaySound = play_sound
        winsound.MessageBeep = message_beep
        previous = sys.modules.get("winsound")
        sys.modules["winsound"] = winsound
        try:
            self.assertTrue(namespace["play_notification_sound"]())
        finally:
            if previous is None:
                sys.modules.pop("winsound", None)
            else:
                sys.modules["winsound"] = previous

        self.assertEqual(
            ("alias", "SystemNotification",
             winsound.SND_ALIAS | winsound.SND_ASYNC),
            calls[0])
        self.assertEqual(
            ("message-beep", winsound.MB_ICONASTERISK), calls[1])
        self.assertNotIn(("application-beep",), calls)


@unittest.skipIf(QCoreApplication is None, "PySide6 QtCore is unavailable")
class AgentHandshakeTests(unittest.TestCase):
    def test_outbox_and_buffer_wait_for_matching_hello(self):
        from app import constants
        from app.agent import serialize
        from app.ui.agent_link import AgentLink

        app = QCoreApplication.instance() or QCoreApplication([])

        class Socket:
            def __init__(self):
                self.writes = []

            def state(self):
                return QLocalSocket.ConnectedState

            def write(self, payload):
                self.writes.append(bytes(payload))
                return len(payload)

            def flush(self):
                return True

            def disconnectFromServer(self):
                pass

        sock = Socket()
        link = AgentLink()
        link._sock = sock
        link._pending = [
            {"cmd": "poke", "head": True},
            {"cmd": "recover"},
        ]

        def written_commands():
            buffer = bytearray()
            payloads = []
            for chunk in sock.writes:
                payloads.extend(serialize.feed(buffer, chunk))
            return [payload.get("cmd") for payload in payloads]

        try:
            link._on_connected()
            self.assertNotIn("kick_outbox", written_commands())
            self.assertNotIn("poke", written_commands())
            self.assertNotIn("recover", written_commands())
            self.assertEqual(2, len(link._pending))

            link._dispatch({"event": "hello", "version": "older-agent"})
            self.assertNotIn("kick_outbox", written_commands())
            self.assertNotIn("poke", written_commands())
            self.assertNotIn("recover", written_commands())
            self.assertEqual(2, len(link._pending))

            link._dispatch({"event": "hello", "version": constants.VERSION})
            commands = written_commands()
            self.assertIn("kick_outbox", commands)
            self.assertIn("poke", commands)
            self.assertIn("recover", commands)
            self.assertEqual([], link._pending)
        finally:
            link.stop()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
