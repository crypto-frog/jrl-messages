"""The ANCS protocol layer and its app wiring (3.3.0).

The radio itself cannot run in a container, so the protocol is held to
byte-exact correctness here, and the wiring tests pin the properties
that make the feature safe: lazy Bluetooth imports, off by default, all
alerts through the existing guarded pipeline, and the agent untouched.
"""
from __future__ import annotations

import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path

try:
    import platformdirs  # noqa: F401
except ModuleNotFoundError:
    platformdirs = types.ModuleType("platformdirs")
    platformdirs.user_data_dir = lambda *_a, **_k: tempfile.gettempdir()
    sys.modules["platformdirs"] = platformdirs

ROOT = Path(__file__).parents[1]

from app.phone import ancs  # noqa: E402


def event_bytes(event_id=0, flags=0, category=4, count=1, uid=42):
    return bytes([event_id, flags, category, count]) + struct.pack("<I", uid)


def tlv(attr_id, text):
    raw = text.encode("utf-8")
    return bytes([attr_id]) + struct.pack("<H", len(raw)) + raw


def full_response(uid, app_id="com.burbn.instagram", title="anna_k",
                  subtitle="", message="liked your photo",
                  date="20260819T151005"):
    return (bytes([ancs.CMD_GET_NOTIFICATION_ATTRIBUTES])
            + struct.pack("<I", uid)
            + tlv(ancs.ATTR_APP_IDENTIFIER, app_id)
            + tlv(ancs.ATTR_TITLE, title)
            + tlv(ancs.ATTR_SUBTITLE, subtitle)
            + tlv(ancs.ATTR_MESSAGE, message)
            + tlv(ancs.ATTR_DATE, date))


class SourceEventTests(unittest.TestCase):
    def test_parses_a_valid_added_event(self):
        e = ancs.parse_source_event(event_bytes(uid=0xDEADBEEF, flags=2))
        self.assertEqual(ancs.EVENT_ADDED, e.event_id)
        self.assertEqual(0xDEADBEEF, e.uid)
        self.assertEqual("Social", e.category_name)
        self.assertFalse(e.silent)
        self.assertFalse(e.pre_existing)

    def test_rejects_malformed_packets(self):
        self.assertIsNone(ancs.parse_source_event(b""))
        self.assertIsNone(ancs.parse_source_event(b"\x00" * 7))
        self.assertIsNone(ancs.parse_source_event(b"\x00" * 9))
        self.assertIsNone(ancs.parse_source_event(
            bytes([9, 0, 0, 0]) + struct.pack("<I", 1)))

    def test_alert_gate_only_fresh_audible_added(self):
        self.assertTrue(ancs.should_alert(
            ancs.parse_source_event(event_bytes())))
        self.assertFalse(ancs.should_alert(ancs.parse_source_event(
            event_bytes(event_id=ancs.EVENT_MODIFIED))))
        self.assertFalse(ancs.should_alert(ancs.parse_source_event(
            event_bytes(event_id=ancs.EVENT_REMOVED))))
        self.assertFalse(ancs.should_alert(ancs.parse_source_event(
            event_bytes(flags=ancs.FLAG_SILENT))))
        # The backlog the iPhone replays on every connect never alerts:
        # this is the anti-storm rule for reconnections.
        self.assertFalse(ancs.should_alert(ancs.parse_source_event(
            event_bytes(flags=ancs.FLAG_PRE_EXISTING))))

    def test_messages_app_is_ignored_by_default(self):
        self.assertTrue(ancs.app_id_ignored("com.apple.MobileSMS"))
        self.assertTrue(ancs.app_id_ignored("com.apple.mobilesms"))
        self.assertFalse(ancs.app_id_ignored("com.burbn.instagram"))

    def test_user_ignore_list_matches_substrings_case_insensitively(self):
        extra = "Instagram, com.zhiliaoapp"
        self.assertTrue(ancs.app_id_ignored("com.burbn.Instagram", extra))
        self.assertTrue(ancs.app_id_ignored(
            "com.zhiliaoapp.musically", extra))
        self.assertFalse(ancs.app_id_ignored("com.apple.news", extra))
        self.assertFalse(ancs.app_id_ignored("com.apple.news", ""))


class CommandBytesTests(unittest.TestCase):
    def test_get_notification_attributes_is_byte_exact(self):
        payload = ancs.build_get_notification_attributes(0x01020304)
        expected = (
            bytes([0])                       # CommandID
            + struct.pack("<I", 0x01020304)  # UID little-endian
            + bytes([0])                     # AppIdentifier, no param
            + bytes([1]) + struct.pack("<H", ancs.TITLE_MAX)
            + bytes([2]) + struct.pack("<H", ancs.SUBTITLE_MAX)
            + bytes([3]) + struct.pack("<H", ancs.MESSAGE_MAX)
            + bytes([5])                     # Date, no param
        )
        self.assertEqual(expected, payload)

    def test_get_app_attributes_is_nul_terminated(self):
        payload = ancs.build_get_app_attributes("com.apple.news")
        self.assertEqual(
            bytes([1]) + b"com.apple.news\x00" + bytes([0]), payload)


class ReassemblyTests(unittest.TestCase):
    def test_single_chunk_response_completes(self):
        r = ancs.NotificationAttributesResponse(42)
        self.assertTrue(r.feed(full_response(42)))
        out = r.result()
        self.assertEqual("com.burbn.instagram", out["app_id"])
        self.assertEqual("anna_k", out["title"])
        self.assertEqual("liked your photo", out["message"])
        self.assertEqual(
            ancs.parse_ancs_date("20260819T151005"), out["when_ms"])
        self.assertFalse(r.overflowed)

    def test_fragmented_response_reassembles(self):
        blob = full_response(7, message="a longer message body " * 8)
        r = ancs.NotificationAttributesResponse(7)
        for i in range(0, len(blob), 20):     # 20-byte GATT chunks
            done = r.feed(blob[i:i + 20])
        self.assertTrue(done)
        self.assertIn("longer message", r.result()["message"])

    def test_unicode_survives_fragmentation_mid_codepoint(self):
        blob = full_response(9, title="Café ☕", message="très bien 👍")
        r = ancs.NotificationAttributesResponse(9)
        for i in range(0, len(blob), 3):      # brutal 3-byte chunks
            r.feed(blob[i:i + 3])
        self.assertTrue(r.done)
        self.assertEqual("Café ☕", r.result()["title"])
        self.assertEqual("très bien 👍", r.result()["message"])

    def test_incomplete_stream_stays_pending(self):
        blob = full_response(11)
        r = ancs.NotificationAttributesResponse(11)
        self.assertFalse(r.feed(blob[:len(blob) - 4]))
        self.assertFalse(r.done)
        self.assertTrue(r.feed(blob[len(blob) - 4:]))

    def test_wrong_uid_or_command_is_rejected_not_wedged(self):
        r = ancs.NotificationAttributesResponse(1)
        self.assertTrue(r.feed(full_response(2)))    # different UID
        self.assertTrue(r.overflowed)
        r2 = ancs.NotificationAttributesResponse(1)
        self.assertTrue(r2.feed(b"\x07" + b"\x00" * 8))
        self.assertTrue(r2.overflowed)

    def test_runaway_buffer_is_capped(self):
        r = ancs.NotificationAttributesResponse(1)
        junk = (bytes([0]) + struct.pack("<I", 1)
                + bytes([ancs.ATTR_TITLE]) + struct.pack("<H", 0xFFFF))
        r.feed(junk)
        for _ in range(600):
            if r.feed(b"x" * 20):
                break
        self.assertTrue(r.overflowed)

    def test_empty_attributes_still_complete(self):
        blob = (bytes([0]) + struct.pack("<I", 5)
                + tlv(ancs.ATTR_APP_IDENTIFIER, "com.apple.news")
                + tlv(ancs.ATTR_TITLE, "")
                + tlv(ancs.ATTR_SUBTITLE, "")
                + tlv(ancs.ATTR_MESSAGE, "")
                + tlv(ancs.ATTR_DATE, ""))
        r = ancs.NotificationAttributesResponse(5)
        self.assertTrue(r.feed(blob))
        self.assertEqual("", r.result()["title"])
        self.assertIsNone(r.result()["when_ms"])

    def test_app_display_name_response(self):
        blob = (bytes([1]) + b"com.burbn.instagram\x00"
                + tlv(ancs.APP_ATTR_DISPLAY_NAME, "Instagram"))
        r = ancs.AppAttributesResponse("com.burbn.instagram")
        for i in range(0, len(blob), 6):
            r.feed(blob[i:i + 6])
        self.assertTrue(r.done)
        self.assertEqual("Instagram", r.display_name)

    def test_app_response_for_a_different_app_is_rejected(self):
        blob = (bytes([1]) + b"com.other.app\x00"
                + tlv(ancs.APP_ATTR_DISPLAY_NAME, "Other"))
        r = ancs.AppAttributesResponse("com.burbn.instagram")
        r.feed(blob)
        self.assertTrue(r.overflowed)


class PresentationTests(unittest.TestCase):
    def test_prettify_falls_back_from_bundle_id(self):
        self.assertEqual("Instagram",
                         ancs.prettify_app_id("com.burbn.instagram"))
        self.assertEqual("iPhone app", ancs.prettify_app_id(""))

    def test_presentation_composes_title_and_body(self):
        shaped = ancs.presentation_of(
            {"app_id": "com.burbn.instagram", "title": "anna_k",
             "subtitle": "", "message": "liked your photo"},
            display_name="Instagram")
        self.assertEqual("Instagram", shaped["app_name"])
        self.assertEqual("anna_k: liked your photo", shaped["body"])
        lone = ancs.presentation_of(
            {"app_id": "com.apple.news", "title": "Breaking",
             "subtitle": "", "message": ""})
        self.assertEqual("Breaking", lone["body"])
        empty = ancs.presentation_of({"app_id": "x", "title": "",
                                      "subtitle": "", "message": ""})
        self.assertEqual("New notification", empty["body"])


class DiscoveryTests(unittest.TestCase):
    """3.3.1: the field bug where the iPhone never appeared in the scan.

    iPhones broadcast with no name and an anonymous rotating address,
    so the original named-devices-only scan could show every speaker in
    the house and never the phone. Discovery now leans on the Windows
    paired list (real name, stable identity address) and keeps
    anonymous Apple broadcasters labeled instead of dropping them."""

    def test_address_formatting_is_canonical(self):
        from app.phone.link import format_ble_address
        self.assertEqual("AA:BB:CC:DD:EE:FF",
                         format_ble_address(0xAABBCCDDEEFF))
        self.assertEqual("00:00:00:00:00:01", format_ble_address(1))
        # Anything past 48 bits is hardware nonsense; masked, not shown.
        self.assertEqual("AA:BB:CC:DD:EE:FF",
                         format_ble_address(0x1AABBCCDDEEFF))

    def test_paired_devices_lead_and_dedupe_wins_for_paired(self):
        from app.phone.link import merge_device_rows
        rows = merge_device_rows([
            ("Bose SoundLink", "11:11:11:11:11:11", "scan", -60),
            ("Apple device", "22:22:22:22:22:22", "apple", -70),
            ("Jonathan's iPhone", "aa:aa:aa:aa:aa:aa", "paired", None),
            # The same phone also seen anonymously in the scan: one row.
            ("Apple device", "AA:AA:AA:AA:AA:AA", "apple", -50),
        ])
        self.assertEqual("Jonathan's iPhone", rows[0][0])
        self.assertEqual("paired", rows[0][2])
        self.assertEqual(3, len(rows))
        addresses = [r[1].lower() for r in rows]
        self.assertEqual(len(addresses), len(set(addresses)))

    def test_proof_beats_pairing_beats_names_beats_anonymity(self):
        from app.phone.link import merge_device_rows
        rows = merge_device_rows([
            ("Apple device", "33:33:33:33:33:33", "apple", -80),
            ("Bose SoundLink", "11:11:11:11:11:11", "scan", -40),
            ("iPhone 15 Pro", "44:44:44:44:44:44", "scan", -90),
            ("Jonathan's iPhone", "55:55:55:55:55:55", "paired-voice",
             None),
            ("Your iPhone (verified: serves iPhone notifications to "
             "this PC)", "66:66:66:66:66:66", "verified", -55),
            ("Apple device", "77:77:77:77:77:77", "apple", -45),
        ])
        self.assertEqual(["verified", "paired-voice", "scan", "scan",
                          "apple", "apple"], [r[2] for r in rows])
        self.assertEqual("iPhone 15 Pro", rows[2][0],
                         "a named iPhone outranks other named devices")
        self.assertEqual("77:77:77:77:77:77", rows[4][1],
                         "anonymous devices sort strongest signal first")

    def test_aep_id_address_extraction(self):
        from app.phone.link import address_from_aep_id
        self.assertEqual(
            "04:52:C7:BB:79:0E", address_from_aep_id(
                "Bluetooth#Bluetooth48:51:c5:aa:bb:cc-04:52:c7:bb:79:0e"))
        self.assertEqual(
            "C0:9A:D0:11:22:33", address_from_aep_id(
                "BluetoothLE#BluetoothLE48:51:c5:aa:bb:cc-"
                "c0:9a:d0:11:22:33"))
        self.assertEqual("", address_from_aep_id("garbage"))
        self.assertEqual("", address_from_aep_id(""))

    def test_closeness_buckets_read_naturally(self):
        from app.phone.link import closeness
        self.assertEqual("very close", closeness(-40))
        self.assertEqual("nearby", closeness(-60))
        self.assertEqual("in range", closeness(-85))
        self.assertEqual("", closeness(None))

    def test_apple_company_id_is_the_registered_one(self):
        from app.phone.link import APPLE_COMPANY_ID
        self.assertEqual(0x004C, APPLE_COMPANY_ID)

    def test_discovery_and_picker_are_wired_to_the_paired_list(self):
        link = (ROOT / "app" / "phone" / "link.py").read_text(
            encoding="utf-8")
        self.assertIn("get_device_selector_from_pairing_state", link)
        self.assertIn("return_adv=True", link)
        self.assertIn("manufacturer_data", link)
        # The worker resolves the paired identity before scanning, and
        # only auto-picks an anonymous Apple device when it is alone.
        self.assertIn("_paired_rows", link)
        self.assertIn("len(apple_only) == 1", link)
        dialog = (ROOT / "app" / "ui" / "settings_dialog.py").read_text(
            encoding="utf-8")
        self.assertIn("discover_phones", dialog)
        self.assertNotIn("scan_for_phones", dialog)
        probe = (ROOT / "tools" / "phone_link_probe.py").read_text(
            encoding="utf-8")
        self.assertIn("_paired_rows", probe)

    def test_identification_by_proof_is_wired_everywhere(self):
        """3.3.2: after the field report where nothing was pickable,
        the picker identifies the phone by asking each Apple device
        whether it serves ANCS to this PC (only a phone that trusts
        this PC answers yes), and every enumeration failure is
        surfaced as user-visible notes rather than a debug log."""
        link = (ROOT / "app" / "phone" / "link.py").read_text(
            encoding="utf-8")
        for needed in ("async def probe_ancs", "def find_iphones",
                       "_paired_rows_with_notes", "class AncsMissing",
                       "_probe_candidates", "learned = Signal",
                       "ASSOCIATION_ENDPOINT", "_AQS_LE_PAIRED",
                       "_AQS_CLASSIC_PAIRED"):
            self.assertIn(needed, link)
        # The probe never pairs: proof must stay a harmless glance.
        probe_fn = link[link.index("async def probe_ancs"):
                        link.index("def find_iphones")]
        self.assertNotIn("pair(", probe_fn)
        # Self-heal probing is bounded per cycle.
        self.assertIn("apple_only[:3]", link)
        dialog = (ROOT / "app" / "ui" / "settings_dialog.py").read_text(
            encoding="utf-8")
        for needed in ("Connect my iPhone", "setup_iphone",
                       "_on_wizard_done", "closeness", "phone_pause",
                       "plainPicker", "itemDoubleClicked"):
            self.assertIn(needed, dialog)
        theme_src = (ROOT / "app" / "ui" / "theme.py").read_text(
            encoding="utf-8")
        # Plain pickers show their selection; the delegate-painted chat
        # list is deliberately excluded from these rules.
        self.assertIn("plainPicker::item:selected", theme_src)
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        self.assertIn("_on_phone_learned", main)
        self.assertIn("config.save(self.settings)", main)
        self.assertIn("_pause_phone_for_setup", main)
        # Cancel must hand the radio back to the worker too.
        cancel_block = main[main.index("if not dlg.exec():"):
                            main.index("if not dlg.exec():") + 400]
        self.assertIn("_apply_phone_link_settings", cancel_block)


class FakeRadioSessionTests(unittest.TestCase):
    """The whole worker session run against a scripted fake bleak.

    No container has a radio, so this is the strongest proof available
    for MY half of the radio code: subscribe order, the ANCS-presence
    check, command serialization, Data Source reassembly wiring, the
    app-name lookup, the learned-address signal, and the AncsMissing
    diagnosis all execute for real; only bleak itself is scripted."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication
        cls.qt_app = QCoreApplication.instance() or QCoreApplication([])

    def _fake_bleak(self, with_ancs=True,
                    connected_address="BB:BB:BB:BB:BB:BB",
                    subscribe_denied=False):
        calls = {"notifies": {}, "written": [], "paired": False}

        class FakeService:
            def __init__(self, uuid):
                self.uuid = uuid

        class FakeClient:
            def __init__(self, target, timeout=None,
                         disconnected_callback=None, **_kw):
                self.address = connected_address
                calls["client"] = self
                calls["winrt"] = _kw.get("winrt")

            async def connect(self):
                calls["connected"] = True

            async def disconnect(self):
                calls["disconnected"] = True

            async def pair(self):
                calls["paired"] = True

            @property
            def services(self):
                base = [FakeService(
                    "0000180a-0000-1000-8000-00805f9b34fb")]
                if with_ancs:
                    base.append(FakeService(ancs.SERVICE_UUID.upper()))
                return base

            async def start_notify(self, uuid, cb):
                if subscribe_denied:
                    raise PermissionError("insufficient authentication")
                calls["notifies"][str(uuid).lower()] = cb

            async def write_gatt_char(self, uuid, payload, response=True):
                calls["written"].append(bytes(payload))
                ds = calls["notifies"].get(ancs.DATA_SOURCE_UUID)
                if ds is None:
                    return
                payload = bytes(payload)
                if payload[0] == ancs.CMD_GET_NOTIFICATION_ATTRIBUTES:
                    uid = struct.unpack_from("<I", payload, 1)[0]
                    ds(None, full_response(uid))
                else:
                    app_id = payload[1:payload.index(0, 1)].decode()
                    ds(None, bytes([1]) + app_id.encode() + b"\x00"
                       + tlv(ancs.APP_ATTR_DISPLAY_NAME, "Instagram"))

        class FakeScanner:
            @staticmethod
            async def find_device_by_address(addr, timeout=None):
                return types.SimpleNamespace(address=addr, name="iPhone")

            @staticmethod
            async def discover(timeout=None, return_adv=False):
                return {} if return_adv else []

        fake = types.ModuleType("bleak")
        fake.BleakClient = FakeClient
        fake.BleakScanner = FakeScanner
        return fake, calls

    def _run_session(self, fake, worker, stop_after=0.35):
        import asyncio
        from unittest import mock

        async def scenario():
            worker._loop = asyncio.get_running_loop()
            task = asyncio.create_task(
                worker._session("AA:AA:AA:AA:AA:AA", "iPhone"))
            await asyncio.sleep(0.1)      # subscription settles
            ns = fake[1]["notifies"].get(ancs.NOTIFICATION_SOURCE_UUID)
            if ns is not None:
                ns(None, event_bytes(uid=77))
            await asyncio.sleep(stop_after)
            worker._stop.set()
            await asyncio.wait_for(task, 8)

        with mock.patch.dict(sys.modules, {"bleak": fake[0]}):
            asyncio.run(scenario())

    def test_a_notification_flows_end_to_end(self):
        from app import config
        from app.phone.link import PhoneLinkWorker
        settings = config.Settings(phone_ble_address="AA:AA:AA:AA:AA:AA",
                                   phone_ble_name="iPhone")
        worker = PhoneLinkWorker(settings)
        got, statuses, learned = [], [], []
        worker.notification.connect(got.append)
        worker.status.connect(lambda lvl, txt: statuses.append((lvl, txt)))
        worker.learned.connect(lambda n, a: learned.append((n, a)))
        fake = self._fake_bleak()
        self._run_session(fake, worker)
        self.assertEqual(1, len(got), statuses)
        self.assertEqual("Instagram", got[0]["app_name"])
        self.assertIn("liked your photo", got[0]["body"])
        self.assertEqual(77, got[0]["uid"])
        # Command order: notification attributes, then the app name.
        self.assertEqual(ancs.CMD_GET_NOTIFICATION_ATTRIBUTES,
                         fake[1]["written"][0][0])
        self.assertEqual(ancs.CMD_GET_APP_ATTRIBUTES,
                         fake[1]["written"][1][0])
        self.assertTrue(any(lvl == "up" for lvl, _ in statuses))
        # Connected at a different address than stored: learned fires.
        self.assertEqual([("iPhone", "BB:BB:BB:BB:BB:BB")], learned)
        self.assertTrue(fake[1].get("disconnected"))
        # 3.5.2: the Windows GATT cache is never trusted; every session
        # forces live service discovery, or a stale snapshot makes a
        # real iPhone fail the notification-service check forever.
        self.assertEqual({"use_cached_services": False},
                         fake[1].get("winrt"))

    def test_messages_app_notifications_stay_quiet_end_to_end(self):
        from app import config
        from app.phone.link import PhoneLinkWorker
        import asyncio
        from unittest import mock
        settings = config.Settings(phone_ble_address="AA:AA:AA:AA:AA:AA",
                                   phone_ble_name="iPhone")
        worker = PhoneLinkWorker(settings)
        got = []
        worker.notification.connect(got.append)
        fake, calls = self._fake_bleak()

        original_write = fake.BleakClient.write_gatt_char

        async def write(self, uuid, payload, response=True):
            payload = bytes(payload)
            calls["written"].append(payload)
            ds = calls["notifies"].get(ancs.DATA_SOURCE_UUID)
            if payload[0] == ancs.CMD_GET_NOTIFICATION_ATTRIBUTES:
                uid = struct.unpack_from("<I", payload, 1)[0]
                ds(None, full_response(uid, app_id="com.apple.MobileSMS"))
            else:
                await original_write(self, uuid, payload, response)
        fake.BleakClient.write_gatt_char = write
        self._run_session((fake, calls), worker)
        self.assertEqual([], got, "a mirrored text would double-alert")

    def test_the_link_test_round_trips_while_connected(self):
        """3.3.3: the Test link button asks the phone a real question
        over the live session and answers with timing."""
        import asyncio
        from unittest import mock
        from app import config
        from app.phone.link import PhoneLinkWorker
        settings = config.Settings(phone_ble_address="AA:AA:AA:AA:AA:AA",
                                   phone_ble_name="iPhone")
        worker = PhoneLinkWorker(settings)
        results = []
        worker.test_result.connect(lambda ok, txt: results.append((ok, txt)))
        fake = self._fake_bleak()

        async def scenario():
            worker._loop = asyncio.get_running_loop()
            task = asyncio.create_task(
                worker._session("AA:AA:AA:AA:AA:AA", "iPhone"))
            await asyncio.sleep(0.1)
            self.assertTrue(worker.is_connected())
            worker.request_link_test()
            await asyncio.sleep(0.5)
            worker._stop.set()
            await asyncio.wait_for(task, 8)

        with mock.patch.dict(sys.modules, {"bleak": fake[0]}):
            asyncio.run(scenario())
        self.assertEqual(1, len(results))
        ok, text = results[0]
        self.assertTrue(ok, text)
        self.assertIn("answered in", text)
        self.assertIn("Instagram", text)   # the scripted display name
        self.assertFalse(worker.is_connected(),
                         "state clears when the session ends")

    def test_missing_bond_is_reported_never_paired_in_background(self):
        """3.4.2: the worker must NEVER pair on its own. A refused
        subscription raises PairingNeeded; the guided setup is the only
        place a pairing prompt may be triggered."""
        import asyncio
        from unittest import mock
        from app import config
        from app.phone.link import PairingNeeded, PhoneLinkWorker
        settings = config.Settings(phone_ble_address="AA:AA:AA:AA:AA:AA",
                                   phone_ble_name="iPhone")
        worker = PhoneLinkWorker(settings)
        fake = self._fake_bleak(subscribe_denied=True)

        async def scenario():
            worker._loop = asyncio.get_running_loop()
            with self.assertRaises(PairingNeeded):
                await worker._session("AA:AA:AA:AA:AA:AA", "iPhone")

        with mock.patch.dict(sys.modules, {"bleak": fake[0]}):
            asyncio.run(scenario())
        self.assertFalse(fake[1]["paired"],
                         "background code must never trigger pairing")
        subscribe_body = self._method_source("_subscribe")
        self.assertNotIn("pair(", subscribe_body)
        main_body = self._method_source("_main")
        self.assertIn("Connect my iPhone", main_body)

    @staticmethod
    def _method_source(method_name):
        import ast
        path = ROOT / "app" / "phone" / "link.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == method_name:
                return ast.get_source_segment(source, node) or ""
        return ""

    def test_a_device_without_the_service_is_diagnosed(self):
        from app import config
        from app.phone.link import AncsMissing, PhoneLinkWorker
        import asyncio
        from unittest import mock
        settings = config.Settings(phone_ble_address="AA:AA:AA:AA:AA:AA",
                                   phone_ble_name="iPhone")
        worker = PhoneLinkWorker(settings)
        fake = self._fake_bleak(with_ancs=False)

        async def scenario():
            worker._loop = asyncio.get_running_loop()
            with self.assertRaises(AncsMissing):
                await worker._session("AA:AA:AA:AA:AA:AA", "iPhone")

        with mock.patch.dict(sys.modules, {"bleak": fake[0]}):
            asyncio.run(scenario())
        self.assertTrue(fake[1].get("disconnected"),
                        "even a misdiagnosed device is disconnected")


class ConnectWizardTests(unittest.TestCase):
    """3.4.0: the guided ceremony against a scripted radio. Two Apple
    devices are nearby: AirPods-like (no ANCS) and the phone (ANCS,
    but subscription requires the pairing the user must approve). The
    wizard must skip the first WITHOUT ever pairing at it, pair the
    second, subscribe, prove with a round trip, and report notes."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication
        cls.qt_app = QCoreApplication.instance() or QCoreApplication([])

    def _fake_radio(self):
        calls = {"paired": [], "disconnected": [], "notifies": {}}
        AIRPODS, PHONE = "CC:00:00:00:00:01", "CC:00:00:00:00:02"

        class FakeService:
            def __init__(self, uuid):
                self.uuid = uuid

        class FakeClient:
            def __init__(self, target, timeout=None, **_kw):
                self.address = getattr(target, "address", target)

            async def connect(self):
                return None

            async def disconnect(self):
                calls["disconnected"].append(self.address)

            async def pair(self):
                calls["paired"].append(self.address)

            @property
            def services(self):
                base = [FakeService(
                    "0000180a-0000-1000-8000-00805f9b34fb")]
                if self.address == PHONE:
                    base.append(FakeService(ancs.SERVICE_UUID))
                return base

            async def start_notify(self, uuid, cb):
                if (self.address == PHONE
                        and self.address not in calls["paired"]):
                    raise PermissionError("insufficient authentication")
                calls["notifies"][str(uuid).lower()] = cb

            async def write_gatt_char(self, uuid, payload, response=True):
                ds = calls["notifies"].get(ancs.DATA_SOURCE_UUID)
                payload = bytes(payload)
                if ds and payload[0] == ancs.CMD_GET_APP_ATTRIBUTES:
                    app_id = payload[1:payload.index(0, 1)].decode()
                    ds(None, bytes([1]) + app_id.encode() + b"\x00"
                       + tlv(ancs.APP_ATTR_DISPLAY_NAME, "Settings"))

        class FakeScanner:
            @staticmethod
            async def discover(timeout=None, return_adv=False):
                def dev(addr):
                    return types.SimpleNamespace(address=addr, name=None)

                def adv(rssi):
                    return types.SimpleNamespace(
                        local_name=None, rssi=rssi,
                        manufacturer_data={0x004C: b"\x10"})
                return {AIRPODS: (dev(AIRPODS), adv(-45)),
                        PHONE: (dev(PHONE), adv(-55))}

        fake = types.ModuleType("bleak")
        fake.BleakClient = FakeClient
        fake.BleakScanner = FakeScanner
        return fake, calls, AIRPODS, PHONE

    def test_ceremony_pairs_only_the_phone_and_proves_it(self):
        from unittest import mock
        from app.phone import link
        fake, calls, airpods, phone = self._fake_radio()
        said = []
        with mock.patch.dict(sys.modules, {"bleak": fake}), \
                mock.patch.object(link, "PAIR_RETRY_WAITS", (0.02,)):
            result, notes = link.setup_iphone(
                progress=said.append, scan_timeout=0.05,
                advanced_pairing=True)
        self.assertIsNotNone(result, notes)
        self.assertEqual(phone, result["address"])
        self.assertIsNotNone(result["ms"])
        # The stronger-signal AirPods were examined first, skipped for
        # lacking the service, and never subjected to a pairing prompt.
        self.assertEqual([phone], calls["paired"])
        self.assertTrue(any("not an iPhone" in n for n in notes), notes)
        # Both candidates were released; the worker owns the session.
        self.assertIn(airpods, calls["disconnected"])
        self.assertIn(phone, calls["disconnected"])
        # The user was coached to look at the phone at pairing time.
        self.assertTrue(any("ACTION NEEDED" in s for s in said), said)

    def test_already_paired_returns_pending_without_any_unpair(self):
        """3.4.4 (field report #9): the automatic 'stale bond reset'
        left half-destroyed remnants on both sides, including a ghost
        phone-side entry with no (i) page, unfixable from the PC. The
        ceremony must NEVER unpair: already-paired plus a refused
        subscription is the paired-pending win, immediately, with no
        pairing attempt and no destruction."""
        from unittest import mock
        from app.phone import link
        fake, calls, _airpods, phone = self._fake_radio()

        async def start_notify(self, uuid, cb):
            if self.address == phone:
                raise PermissionError("insufficient authorization")
            calls["notifies"][str(uuid).lower()] = cb
        fake.BleakClient.start_notify = start_notify

        class FakePairing:
            is_paired = True

            async def unpair_async(self):
                self.fail("the ceremony must never unpair")

        async def fake_lookup(address):
            return FakePairing() if address == phone else None

        with mock.patch.dict(sys.modules, {"bleak": fake}), \
                mock.patch.object(link, "PAIR_RETRY_WAITS", (0.02,)), \
                mock.patch.object(link, "_windows_pairing", fake_lookup):
            result, notes = link.setup_iphone(scan_timeout=0.05,
                                              advanced_pairing=True)
        self.assertIsNotNone(result, notes)
        self.assertTrue(result.get("paired_pending"), notes)
        self.assertEqual(phone, result["address"])
        self.assertEqual([], calls["paired"],
                         "no pairing attempt when already paired")
        self.assertTrue(any("already paired" in n for n in notes), notes)

    def test_default_mode_never_pairs_and_routes_to_phone_link(self):
        """3.5.0: by default the flow is attach-only. An unpaired
        iPhone (found by proof, subscription refused) produces
        needs_phone_link with ZERO pairing attempts; pairing is Phone
        Link's job, whose QR runs Microsoft's own app on the phone."""
        from unittest import mock
        from app.phone import link
        fake, calls, _airpods, phone = self._fake_radio()

        async def start_notify(self, uuid, cb):
            if self.address == phone:
                raise PermissionError("insufficient authentication")
            calls["notifies"][str(uuid).lower()] = cb
        fake.BleakClient.start_notify = start_notify
        said = []
        with mock.patch.dict(sys.modules, {"bleak": fake}), \
                mock.patch.object(link, "PAIR_RETRY_WAITS", (0.02,)):
            result, notes = link.setup_iphone(
                progress=said.append, scan_timeout=0.05)
        self.assertIsNotNone(result, notes)
        self.assertTrue(result.get("needs_phone_link"), notes)
        self.assertEqual(phone, result["address"])
        self.assertEqual([], calls["paired"],
                         "default mode must never pair")
        self.assertTrue(any("Phone Link" in n for n in notes), notes)

    def test_phone_link_launcher_and_wiring(self):
        src = (ROOT / "app" / "phone" / "link.py").read_text(
            encoding="utf-8")
        for needed in ("def open_phone_link", "ms-phone:",
                       "ms-settings:mobile-devices", "advanced_pairing",
                       "needs_phone_link"):
            self.assertIn(needed, src)
        dialog = (ROOT / "app" / "ui" / "settings_dialog.py").read_text(
            encoding="utf-8")
        for needed in ("Phone Link pairing", "open_phone_link",
                       "needs_phone_link"):
            self.assertIn(needed, dialog)

    def test_no_unpair_doctrine_and_forget_coaching(self):
        from app.phone import link as link_mod
        src = (ROOT / "app" / "phone" / "link.py").read_text(
            encoding="utf-8")
        for needed in ("_windows_pairing", "def forget_coaching",
                       "Forget This Device", "NEVER UNPAIRS"):
            self.assertIn(needed, src)
        self.assertNotIn("unpair_async", src,
                         "automatic unpairing is banned; it destroyed "
                         "bonds and left ghost phone-side entries")
        recipe = link_mod.forget_coaching()
        self.assertIn("WINDOWS SETTINGS", recipe)
        self.assertIn("Forget This Device", recipe)

    def test_paired_but_blocked_keeps_the_bond_and_coaches_the_switch(self):
        """3.4.3 (field report #7): he pairs, the phone says paired,
        but the subscription stays refused because iOS's Share System
        Notifications switch is off. The old code called that a stale
        bond and DESTROYED it, looping pair-and-destroy forever. Now:
        one reset maximum, never after a fresh pairing, and the
        paired-but-refused state ends the ceremony as a WIN that saves
        the phone and coaches the switch."""
        from unittest import mock
        from app.phone import link
        fake, calls, _airpods, phone = self._fake_radio()
        calls["unpairs"] = 0

        async def start_notify(self, uuid, cb):
            if self.address == phone:
                # Sharing switch off: refused forever, even paired.
                raise PermissionError("insufficient authorization")
            calls["notifies"][str(uuid).lower()] = cb
        fake.BleakClient.start_notify = start_notify

        class FakePairing:
            def __init__(self):
                self.is_paired = False

            async def unpair_async(self):
                calls["unpairs"] += 1
                self.is_paired = False
                return types.SimpleNamespace(status="unpaired")

        record = FakePairing()

        async def fake_lookup(_address):
            return record

        async def pair(self):
            record.is_paired = True     # the user tapped Pair
            calls["paired"].append(self.address)
        fake.BleakClient.pair = pair

        with mock.patch.dict(sys.modules, {"bleak": fake}), \
                mock.patch.object(link, "PAIR_RETRY_WAITS", (0.02,)), \
                mock.patch.object(link, "_windows_pairing", fake_lookup):
            result, notes = link.setup_iphone(scan_timeout=0.05,
                                              advanced_pairing=True)
        self.assertIsNotNone(result, notes)
        self.assertTrue(result.get("paired_pending"), notes)
        self.assertEqual(phone, result["address"])
        self.assertEqual(0, calls["unpairs"],
                         "a bond made in this ceremony must never be "
                         "destroyed by the stale-bond logic")
        self.assertTrue(any("permission switch" in n for n in notes),
                        notes)

    def test_worker_distinguishes_paired_but_blocked(self):
        """The background link says exactly which side needs fixing."""
        import asyncio
        from unittest import mock
        from app import config
        from app.phone import link
        from app.phone.link import PairingNeeded, PhoneLinkWorker
        settings = config.Settings(phone_ble_address="AA:AA:AA:AA:AA:AA",
                                   phone_ble_name="iPhone")
        worker = PhoneLinkWorker(settings)
        statuses = []
        worker.status.connect(lambda lvl, txt: statuses.append(txt))
        fake = FakeRadioSessionTests._fake_bleak(
            self, subscribe_denied=True)

        class PairedRecord:
            is_paired = True

        async def fake_lookup(_address):
            return PairedRecord()

        async def scenario():
            worker._loop = asyncio.get_running_loop()
            worker._stop.clear()
            task = asyncio.create_task(worker._main())
            await asyncio.sleep(1.2)     # one session + PairingNeeded
            worker._stop.set()
            await asyncio.wait_for(task, 8)

        with mock.patch.dict(sys.modules, {"bleak": fake[0]}), \
                mock.patch.object(link, "_windows_pairing", fake_lookup):
            asyncio.run(scenario())
        self.assertTrue(any("Share System Notifications" in s
                            for s in statuses), statuses)
        self.assertFalse(fake[1]["paired"],
                         "the worker still never pairs on its own")

    def test_unapproved_pairing_fails_with_honest_notes(self):
        from unittest import mock
        from app.phone import link
        fake, calls, _airpods, phone = self._fake_radio()

        async def never(_self):      # the user never approves: bleak
            raise RuntimeError(      # raises, exactly like the field
                "Could not pair with device: AuthenticationTimeout")
        fake.BleakClient.pair = never
        original = fake.BleakClient.start_notify

        async def always_denied(self, uuid, cb):
            if self.address == phone:
                raise PermissionError("insufficient authentication")
            return await original(self, uuid, cb)
        fake.BleakClient.start_notify = always_denied
        with mock.patch.dict(sys.modules, {"bleak": fake}), \
                mock.patch.object(link, "PAIR_RETRY_WAITS", (0.02, 0.02)):
            result, notes = link.setup_iphone(scan_timeout=0.05,
                                              advanced_pairing=True)
        self.assertIsNone(result)
        self.assertTrue(any("not approved" in n for n in notes), notes)


class CeremonyPairingTests(unittest.TestCase):
    """3.4.2: 'Could not pair with device: FAILED', instant and
    promptless, was Windows aborting because pairing was requested with
    ConfirmOnly alone while the iPhone negotiates a confirm-code
    ceremony. The custom ceremony must request every kind an iPhone
    can use, accept in our handler, and surface the confirm code."""

    def _fake_winrt_enum_module(self):
        import enum

        class Kinds(enum.IntFlag):
            NONE = 0
            CONFIRM_ONLY = 1
            DISPLAY_PIN = 2
            PROVIDE_PIN = 4
            CONFIRM_PIN_MATCH = 8

        class Protection(enum.IntEnum):
            DEFAULT = 0
            ENCRYPTION = 2

        class Status(enum.IntEnum):
            PAIRED = 3
            ALREADY_PAIRED = 4
            FAILED = 19

        mod = types.ModuleType("winrt.windows.devices.enumeration")
        mod.DevicePairingKinds = Kinds
        mod.DevicePairingProtectionLevel = Protection
        mod.DevicePairingResultStatus = Status
        chain = {
            "winrt": types.ModuleType("winrt"),
            "winrt.windows": types.ModuleType("winrt.windows"),
            "winrt.windows.devices": types.ModuleType(
                "winrt.windows.devices"),
            "winrt.windows.devices.enumeration": mod,
        }
        return chain, Kinds, Status

    def test_all_iphone_ceremonies_requested_and_code_surfaced(self):
        import asyncio
        from unittest import mock
        from app.phone import link
        chain, Kinds, Status = self._fake_winrt_enum_module()
        recorded = {}

        class FakeCustom:
            def add_pairing_requested(self, handler):
                recorded["handler"] = handler
                return 42

            def remove_pairing_requested(self, token):
                recorded["removed"] = token

            async def pair_async(self, kinds, protection=None):
                recorded["kinds"] = kinds
                args = types.SimpleNamespace(pin="481 516")
                args.accepted = False
                args.accept = lambda: setattr(args, "accepted", True)
                recorded["handler"](None, args)
                recorded["args"] = args
                return types.SimpleNamespace(status=Status.PAIRED)

        class FakePairing:
            is_paired = False
            custom = FakeCustom()

        async def fake_lookup(_address):
            return FakePairing()

        said, notes = [], []
        with mock.patch.dict(sys.modules, chain), \
                mock.patch.object(link, "_windows_pairing", fake_lookup):
            ok = asyncio.run(link._pair_with_ceremonies(
                "AA:BB:CC:DD:EE:FF", said.append, notes))
        self.assertTrue(ok, notes)
        self.assertTrue(recorded["kinds"] & Kinds.CONFIRM_ONLY)
        self.assertTrue(recorded["kinds"] & Kinds.CONFIRM_PIN_MATCH,
                        "the iPhone's confirm-code ceremony must be "
                        "an accepted kind, or Windows aborts promptless")
        self.assertTrue(recorded["kinds"] & Kinds.DISPLAY_PIN)
        self.assertTrue(recorded["args"].accepted)
        self.assertTrue(any("481 516" in s for s in said),
                        "the confirm code must be shown to the user")
        self.assertEqual(42, recorded["removed"])
        self.assertTrue(any("status" in n for n in notes))

    def test_failed_ceremony_reports_and_returns_false(self):
        import asyncio
        from unittest import mock
        from app.phone import link
        chain, _Kinds, Status = self._fake_winrt_enum_module()

        class FakeCustom:
            def add_pairing_requested(self, handler):
                return 1

            def remove_pairing_requested(self, token):
                return None

            async def pair_async(self, kinds, protection=None):
                return types.SimpleNamespace(status=Status.FAILED)

        class FakePairing:
            is_paired = False
            custom = FakeCustom()

        async def fake_lookup(_address):
            return FakePairing()

        notes = []
        with mock.patch.dict(sys.modules, chain), \
                mock.patch.object(link, "_windows_pairing", fake_lookup):
            ok = asyncio.run(link._pair_with_ceremonies(
                "AA:BB:CC:DD:EE:FF", lambda _t: None, notes))
        self.assertFalse(ok)
        self.assertTrue(any("status" in n for n in notes))

    def test_winrt_dialect_fallbacks_are_wired(self):
        src = (ROOT / "app" / "phone" / "link.py").read_text(
            encoding="utf-8")
        for needed in ("_find_all_flex", "CONFIRM_PIN_MATCH",
                       "_pair_with_ceremonies(", "class PairingNeeded",
                       "find_all_async_aqs_filter"):
            self.assertIn(needed, src)


class ProbeEscalationTests(unittest.TestCase):
    """3.5.2: after a service-check failure, the device search must
    skip the stored-address and paired shortcuts (they reconnect to
    the same suspect record forever) and go straight to proving live
    Apple broadcasts. Also pins the 4-tuple paired-row unpack the
    worker silently broke in 3.3.2."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication
        cls.qt_app = QCoreApplication.instance() or QCoreApplication([])

    def test_needs_probe_skips_the_poisoned_fast_paths(self):
        import asyncio
        from unittest import mock
        from app import config
        from app.phone import link
        from app.phone.link import PhoneLinkWorker
        settings = config.Settings(phone_ble_address="41:1E:16:DD:D3:7A",
                                   phone_ble_name="iPhone")
        worker = PhoneLinkWorker(settings)
        worker._needs_probe = True
        by_address_calls = []

        class FakeScanner:
            @staticmethod
            async def find_device_by_address(addr, timeout=None):
                by_address_calls.append(addr)
                return types.SimpleNamespace(address=addr, name=None)

            @staticmethod
            async def discover(timeout=None, return_adv=False):
                adv = types.SimpleNamespace(
                    rssi=-50, manufacturer_data={0x004C: b"\x10"})
                dev = types.SimpleNamespace(
                    address="CC:11:22:33:44:55", name=None)
                return {dev.address: (dev, adv)}

        fake = types.ModuleType("bleak")
        fake.BleakScanner = FakeScanner

        async def fake_probe(device):
            return True
        learned = []
        worker.learned.connect(lambda n, a: learned.append(a))

        async def scenario():
            worker._loop = asyncio.get_running_loop()
            device = await worker._find_device(
                "41:1E:16:DD:D3:7A", "iPhone")
            return device

        with mock.patch.dict(sys.modules, {"bleak": fake}), \
                mock.patch.object(link, "probe_ancs", fake_probe):
            device = asyncio.run(scenario())
        self.assertEqual("CC:11:22:33:44:55", device.address)
        self.assertEqual([], by_address_calls,
                         "the poisoned stored address must not be "
                         "retried while proof is pending")
        self.assertEqual(["CC:11:22:33:44:55"], learned)
        self.assertFalse(worker._needs_probe,
                         "a successful proof clears the escalation")

    def test_worker_paired_rows_unpack_matches_the_row_shape(self):
        """The worker unpacked 3-tuples after rows became 4-tuples,
        so paired resolution silently threw for two releases. Pin the
        shapes to each other."""
        src = (ROOT / "app" / "phone" / "link.py").read_text(
            encoding="utf-8")
        self.assertIn("for row_name, row_addr, _src, _rssi in "
                      "await _paired_rows()", src)
        self.assertNotIn("for row_name, row_addr, _src in", src)


class RespawnTests(unittest.TestCase):
    """3.5.1: a Save-triggered re-aim stopped the worker, the old
    thread was mid-scan and slow to exit, and the scheduled restart
    gave up ('restart skipped this time'), leaving the link dead until
    the next app start. A delayed stop now hands the restart to the
    exiting thread itself."""

    class _SlowThread:
        def __init__(self, alive=True):
            self._alive = alive
            self.joined = False

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            self.joined = True

    def _worker(self):
        from app import config
        from app.phone.link import PhoneLinkWorker
        return PhoneLinkWorker(config.Settings())

    def test_slow_stop_hands_the_restart_to_the_exiting_thread(self):
        worker = self._worker()
        spawns = []
        worker._spawn = lambda: spawns.append(True)
        worker._thread = self._SlowThread(alive=True)
        worker._stop.set()             # a stop is in progress
        worker.start()
        self.assertEqual([], spawns, "cannot spawn while the old "
                         "thread lives")
        self.assertTrue(worker._restart_pending)
        # The old thread finally exits and honors the pending restart.
        worker._maybe_respawn()
        self.assertEqual([True], spawns)

    def test_stop_cancels_a_pending_restart(self):
        worker = self._worker()
        spawns = []
        worker._spawn = lambda: spawns.append(True)
        worker._restart_pending = True
        worker._want = True
        worker.stop()
        worker._maybe_respawn()
        self.assertEqual([], spawns, "an explicit stop wins")

    def test_respawn_is_wired_into_the_thread_exit_path(self):
        src = (ROOT / "app" / "phone" / "link.py").read_text(
            encoding="utf-8")
        self.assertIn("restart itself the moment it exits", src)
        run_body = src[src.index("    def _run(self)"):
                       src.index("    async def _main")]
        self.assertIn("_maybe_respawn", run_body)


class BootClockTests(unittest.TestCase):
    """3.3.1: time.monotonic() counts from machine boot, so a rate
    limiter seeded with 0.0 silently swallows every transition in the
    first ten minutes after a reboot (surfaced as a once-per-boot
    harness flake). The never-recorded state must be an explicit None."""

    class _Stub:
        def __init__(self):
            self.recorded = []
            self._feed_link_kind = "ok"
            self._feed_link_down_ms = None
            self._feed_link_loss_recorded = False
            self._feed_phone_state = "idle"
            self._feed_phone_down_ms = None
            self._feed_phone_loss_recorded = False
            self.activity = types.SimpleNamespace(
                record=lambda *a, **k: None)

        def _feed_record(self, kind, *a, **k):
            self.recorded.append(kind)

    def _patched_clock(self, seconds):
        from unittest import mock
        fake = types.SimpleNamespace(monotonic=lambda: seconds,
                                     time=__import__("time").time)
        return mock.patch("app.ui.main_window.time", fake)

    def test_link_transitions_record_even_minutes_after_boot(self):
        from app.ui.main_window import MainWindow
        stub = self._Stub()
        with self._patched_clock(42.0):        # fresh boot
            MainWindow._record_link_transition(stub, "fail", "gone")
            MainWindow._record_link_transition(stub, "ok", "back")
        self.assertEqual(["link-down", "link-up"], stub.recorded)

    def test_phone_transitions_record_even_minutes_after_boot(self):
        from app.ui.main_window import MainWindow
        stub = self._Stub()
        with self._patched_clock(42.0):
            MainWindow._on_phone_status(stub, "down", "not reachable")
        self.assertEqual(["phone-down"], stub.recorded)

    def test_flapping_link_records_one_symmetric_pair(self):
        from app.ui.main_window import MainWindow
        stub = self._Stub()
        with self._patched_clock(1000.0):
            for _ in range(5):
                MainWindow._record_link_transition(stub, "fail", "x")
                MainWindow._record_link_transition(stub, "ok", "x")
        self.assertEqual(["link-down", "link-up"], stub.recorded,
                         "flapping must not fill the bell from either "
                         "side within the rate window")

    def test_flapping_phone_settles_after_one_recovery(self):
        from app.ui.main_window import MainWindow
        stub = self._Stub()
        with self._patched_clock(1000.0):
            for _ in range(5):
                MainWindow._on_phone_status(stub, "up", "connected")
                MainWindow._on_phone_status(stub, "down", "gone")
        self.assertEqual(["phone-up", "phone-down", "phone-up"],
                         stub.recorded,
                         "first connect, one loss, one recovery; then "
                         "range-edge flapping stays quiet")


class LinkTestButtonTests(unittest.TestCase):
    """3.3.3: the Test link button answers usefully in EVERY state, and
    a long outage refreshes the bell instead of going silent forever."""

    class _LinkStub:
        def __init__(self, running=True, connected=True):
            self._r, self._c = running, connected
            self.kicked = False
            self.requested = False

        def running(self):
            return self._r

        def is_connected(self):
            return self._c

        def kick(self):
            self.kicked = True

        def request_link_test(self):
            self.requested = True

    def _window_stub(self, enabled=True, address="AA:AA:AA:AA:AA:AA",
                     running=True, connected=True):
        from app import config
        stub = types.SimpleNamespace()
        stub.settings = config.Settings(
            phone_link_enabled=enabled, phone_ble_address=address)
        stub.phone = self._LinkStub(running, connected)
        stub.feedback = []
        stub._phone_test_feedback = (
            lambda ok, text: stub.feedback.append((ok, text)))
        stub.activity = types.SimpleNamespace(record=lambda *a: None)
        stub._apply_phone_link_settings = lambda: None
        return stub

    def test_not_set_up_is_said_plainly(self):
        from app.ui.main_window import MainWindow
        stub = self._window_stub(enabled=False)
        MainWindow.test_phone_link(stub)
        self.assertIn("not set up", stub.feedback[0][1])
        self.assertFalse(stub.feedback[0][0])

    def test_disconnected_kicks_an_immediate_retry(self):
        from app.ui.main_window import MainWindow
        stub = self._window_stub(connected=False)
        MainWindow.test_phone_link(stub)
        self.assertTrue(stub.phone.kicked)
        self.assertIn("reconnecting immediately",
                      stub.feedback[0][1].lower())

    def test_connected_runs_the_real_round_trip(self):
        from app.ui.main_window import MainWindow
        stub = self._window_stub()
        MainWindow.test_phone_link(stub)
        self.assertTrue(stub.phone.requested)
        self.assertEqual([], stub.feedback,
                         "the answer arrives from the worker signal")

    def test_long_outage_refreshes_the_bell_half_hourly(self):
        from unittest import mock
        from app.ui.main_window import MainWindow
        stub = BootClockTests._Stub()
        clock = {"now": 1000.0}
        fake = types.SimpleNamespace(monotonic=lambda: clock["now"],
                                     time=__import__("time").time)
        with mock.patch("app.ui.main_window.time", fake):
            MainWindow._on_phone_status(stub, "down", "gone")
            clock["now"] = 2000.0      # 17 min: still quiet
            MainWindow._on_phone_status(stub, "down", "still gone")
            clock["now"] = 3000.0      # 33 min: quiet reminder
            MainWindow._on_phone_status(stub, "down", "still gone")
        self.assertEqual(["phone-down", "phone-down"], stub.recorded)

    def test_button_and_status_are_wired(self):
        dialog = (ROOT / "app" / "ui" / "settings_dialog.py").read_text(
            encoding="utf-8")
        for needed in ("Test link", "on_phone_test", "phone_status"):
            self.assertIn(needed, dialog)
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        for needed in ("def test_phone_link", "phone_link_status_text",
                       "_on_phone_test_result", "iPhone still unreachable",
                       "on_phone_test=self.test_phone_link"):
            self.assertIn(needed, main)
        link = (ROOT / "app" / "phone" / "link.py").read_text(
            encoding="utf-8")
        for needed in ("def is_connected", "def kick",
                       "def request_link_test", "_run_link_test",
                       "com.apple.Preferences",
                       "_consecutive_failures == 2"):
            self.assertIn(needed, link)


class PhoneWiringTests(unittest.TestCase):
    """The safety properties of the radio feature, pinned statically."""

    def test_bluetooth_imports_are_lazy_everywhere(self):
        for path in (ROOT / "app").rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith(("import bleak", "from bleak",
                                    "import winrt", "from winrt")):
                    self.fail(f"top-level Bluetooth import in {path}")
        link = (ROOT / "app" / "phone" / "link.py").read_text(
            encoding="utf-8")
        self.assertIn("import bleak", link)   # used, but only inside defs

    def test_agent_is_completely_untouched_by_the_phone_feature(self):
        for path in (ROOT / "app" / "agent").rglob("*.py"):
            src = path.read_text(encoding="utf-8")
            self.assertNotIn("phone", src.lower().replace(
                "iphone", "").replace("phone number", ""),
                f"{path} must not know the phone link exists")

    def test_disabled_by_default_and_saved_from_settings(self):
        from app.config import Settings
        s = Settings()
        self.assertFalse(s.phone_link_enabled)
        self.assertEqual("", s.phone_ble_address)
        dialog = (ROOT / "app" / "ui" / "settings_dialog.py").read_text(
            encoding="utf-8")
        for needed in ("phone_link_enabled", "phone_ble_address",
                       "phone_ignore_apps", "Choose iPhone"):
            self.assertIn(needed, dialog)

    def test_window_routes_phone_alerts_through_the_guarded_pipeline(self):
        import ast
        main_path = ROOT / "app" / "ui" / "main_window.py"
        source = main_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        body = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and \
                    node.name == "_on_phone_notification":
                body = ast.get_source_segment(source, node) or ""
        self.assertTrue(body, "_on_phone_notification must exist")
        self.assertIn("_feed_record", body)
        self.assertIn("_present_notification", body)
        self.assertIn("_signal_notification", body)
        self.assertIn("popups_enabled", body)
        self.assertIn("except Exception", body)
        self.assertIn("JRL_SMOKE", source)
        self.assertIn("phone-up", source)
        self.assertIn("phone-down", source)

    def test_worker_reconnects_with_capped_backoff_and_reports(self):
        link = (ROOT / "app" / "phone" / "link.py").read_text(
            encoding="utf-8")
        self.assertIn("min(60", link)
        self.assertIn("disconnected_callback", link)
        self.assertIn("pair", link)
        self.assertIn("MAX_PENDING", link)
        self.assertNotIn("setParent", link)
        self.assertNotIn("QWidget", link)     # worker owns no UI

    def test_requirements_carry_bleak_for_windows_only(self):
        requirements = (ROOT / "requirements.txt").read_text(
            encoding="utf-8")
        self.assertIn("bleak", requirements)
        line = next(l for l in requirements.splitlines() if "bleak" in l)
        self.assertIn("sys_platform", line)

    def test_bell_panel_knows_the_phone_kinds(self):
        panel = (ROOT / "app" / "ui" / "alert_center.py").read_text(
            encoding="utf-8")
        self.assertIn('"phone"', panel)
        self.assertIn('"phone-up"', panel)
        self.assertIn('"phone-down"', panel)


class PairedListAuthorityTests(unittest.TestCase):
    """3.5.3 (field report #13): 'its says my phone is not paired in
    Link but it is.' _windows_pairing(fresh RPA) is BLIND behind a
    rotating anonymous address and answered 'not paired' while the bond
    sat in Windows under the real identity. The paired LIST is now the
    authority; failures sideline the actual connected address and the
    worker rotates between paired iPhone entries; the wizard records
    refusal verdicts and keeps trying instead of ending on the first."""

    A = "AA:AA:AA:AA:AA:AA"
    B = "BB:BB:BB:BB:BB:BB"

    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication
        cls.qt_app = QCoreApplication.instance() or QCoreApplication([])

    def _rows(self, *entries):
        async def fake_paired_rows():
            return [(name, addr, "paired", None) for name, addr in entries]
        return fake_paired_rows

    def test_looks_like_iphone_matcher(self):
        from app.phone.link import _looks_like_iphone
        for yes in ("iPhone", "Jonathan's iPhone", "iPhone 15 Pro",
                    "iPhone (verified)"):
            self.assertTrue(_looks_like_iphone(yes), yes)
        for no in ("Jonathan's A14", "moto g54 5G", "Bose SoundLink",
                   "Apple device", "Apple device 2", "", None):
            self.assertFalse(_looks_like_iphone(no), no)

    def test_wanted_name_equality_ignores_the_fallback_labels(self):
        import asyncio
        from unittest import mock
        from app.phone import link
        rows = self._rows(("moto g54 5G", "11:11:11:11:11:11"))
        with mock.patch.object(link, "_paired_rows", rows):
            self.assertEqual(
                [], asyncio.run(link._paired_iphone_rows("your iPhone")))
            self.assertEqual(
                [], asyncio.run(link._paired_iphone_rows("Apple device 2")))
            chosen = asyncio.run(link._paired_iphone_rows("moto g54 5G"))
        self.assertEqual([("moto g54 5G", "11:11:11:11:11:11")], chosen,
                         "a name the user explicitly chose still counts")

    def test_worker_never_says_not_paired_while_the_list_holds_an_iphone(
            self):
        """The pinned lie: subscribe refused, per-address record blind,
        paired list holds 'iPhone'. The verdict must be the permission
        coaching, never 'not paired with this PC'."""
        import asyncio
        from unittest import mock
        from app import config
        from app.phone import link
        from app.phone.link import PhoneLinkWorker
        settings = config.Settings(phone_ble_address=self.A,
                                   phone_ble_name="iPhone")
        worker = PhoneLinkWorker(settings)
        statuses = []
        worker.status.connect(lambda lvl, txt: statuses.append(txt))
        fake = FakeRadioSessionTests._fake_bleak(
            self, subscribe_denied=True)

        async def blind_lookup(_address):
            return None                      # the field lie, verbatim

        async def scenario():
            worker._loop = asyncio.get_running_loop()
            worker._stop.clear()
            task = asyncio.create_task(worker._main())
            await asyncio.sleep(1.2)
            worker._stop.set()
            await asyncio.wait_for(task, 8)

        with mock.patch.dict(sys.modules, {"bleak": fake[0]}), \
                mock.patch.object(link, "_windows_pairing", blind_lookup), \
                mock.patch.object(link, "_paired_rows",
                                  self._rows(("iPhone", self.A))):
            asyncio.run(scenario())
        self.assertFalse(any("not paired with this PC" in s
                             for s in statuses), statuses)
        self.assertTrue(any("Share System Notifications" in s
                            for s in statuses), statuses)
        self.assertIn(fake[1]["client"].address.upper(), worker._sidelined,
                      "the actual connected address is the suspect")

    def test_rotation_returns_the_other_paired_identity_directly(self):
        """After a failure the sibling bond is tried next, returned as
        a plain identity string: the by-address scan is never called."""
        import asyncio
        from unittest import mock
        from app import config
        from app.phone import link
        from app.phone.link import PhoneLinkWorker
        settings = config.Settings(phone_ble_address=self.A,
                                   phone_ble_name="iPhone")
        worker = PhoneLinkWorker(settings)
        worker._sidelined = {self.A}
        worker._last_suspect = self.A
        scans = {"by_address": 0, "discover": 0}

        class Scanner:
            @staticmethod
            async def find_device_by_address(addr, timeout=None):
                scans["by_address"] += 1
                return None

            @staticmethod
            async def discover(timeout=None, return_adv=False):
                scans["discover"] += 1
                return {} if return_adv else []

        fake = types.ModuleType("bleak")
        fake.BleakScanner = Scanner
        rows = self._rows(("Jonathan's iPhone", self.A), ("iPhone", self.B))
        with mock.patch.dict(sys.modules, {"bleak": fake}), \
                mock.patch.object(link, "_paired_rows", rows):
            device = asyncio.run(worker._find_device(self.A, "iPhone"))
        self.assertEqual(self.B, device)
        self.assertEqual(0, scans["by_address"],
                         "a bonded identity connects directly; the "
                         "by-address scan is a pure wait")
        self.assertEqual(0, scans["discover"])

    def test_exhausted_sideline_resets_and_alternates(self):
        """Both entries tried and failed: the set resets to the most
        recent suspect, so the NEXT pick is the other one again and
        retries alternate A, B, A, B."""
        import asyncio
        from unittest import mock
        from app import config
        from app.phone import link
        from app.phone.link import PhoneLinkWorker
        settings = config.Settings(phone_ble_address=self.A,
                                   phone_ble_name="iPhone")
        worker = PhoneLinkWorker(settings)
        worker._sidelined = {self.A, self.B}
        worker._last_suspect = self.B
        fake = types.ModuleType("bleak")
        fake.BleakScanner = types.SimpleNamespace()
        rows = self._rows(("iPhone", self.A), ("iPhone", self.B))
        with mock.patch.dict(sys.modules, {"bleak": fake}), \
                mock.patch.object(link, "_paired_rows", rows):
            device = asyncio.run(worker._find_device(self.A, "iPhone"))
        self.assertEqual(self.A, device)
        self.assertEqual({self.B}, worker._sidelined)

    def test_probe_skips_sidelined_addresses(self):
        import asyncio
        from unittest import mock
        from app import config
        from app.phone import link
        from app.phone.link import PhoneLinkWorker
        settings = config.Settings(phone_ble_address=self.A,
                                   phone_ble_name="iPhone")
        worker = PhoneLinkWorker(settings)
        worker._sidelined = {self.A}
        probed = []

        async def fake_probe(device, timeout=10.0):
            probed.append(device.address)
            return False

        candidates = [
            (types.SimpleNamespace(address=self.A, name=None), -40),
            (types.SimpleNamespace(address=self.B, name=None), -60)]
        with mock.patch.object(link, "probe_ancs", fake_probe):
            asyncio.run(worker._probe_candidates(candidates, "iPhone"))
        self.assertEqual([self.B], probed,
                         "a just-failed RPA must not be re-proven")

    def test_wizard_paired_list_evidence_gives_paired_pending(self):
        """Default mode, subscription refused, per-address record says
        nothing: with an iPhone entry in the paired list the verdict is
        paired_pending (a permission wait), never needs_phone_link and
        never the 'not paired' line."""
        from unittest import mock
        from app.phone import link
        wizard = ConnectWizardTests()
        fake, calls, _airpods, phone = wizard._fake_radio()

        async def start_notify(self, uuid, cb):
            if self.address == phone:
                raise PermissionError("insufficient authentication")
            calls["notifies"][str(uuid).lower()] = cb
        fake.BleakClient.start_notify = start_notify

        async def blind_lookup(_address):
            return None

        said = []
        rows = self._rows(("iPhone", "44:55:66:77:88:99"))
        with mock.patch.dict(sys.modules, {"bleak": fake}), \
                mock.patch.object(link, "PAIR_RETRY_WAITS", (0.02,)), \
                mock.patch.object(link, "_windows_pairing", blind_lookup), \
                mock.patch.object(link, "_paired_rows", rows):
            result, notes = link.setup_iphone(
                progress=said.append, scan_timeout=0.05)
        self.assertIsNotNone(result, notes)
        self.assertTrue(result.get("paired_pending"), notes)
        self.assertFalse(result.get("needs_phone_link"), notes)
        self.assertEqual([], calls["paired"])
        self.assertFalse(any("not paired with this PC" in s for s in said),
                         said)
        self.assertTrue(any("permission" in n for n in notes), notes)

    def test_wizard_second_entry_full_win_beats_first_refusal(self):
        """Two paired 'iPhone' bonds, only one authorized: the refusal
        on the first is recorded, the run continues, and the working
        second entry wins outright."""
        from unittest import mock
        from app.phone import link
        calls = {"paired": [], "notifies": {}}
        FIRST, SECOND = "CC:00:00:00:00:01", "CC:00:00:00:00:02"

        class FakeService:
            def __init__(self, uuid):
                self.uuid = uuid

        class FakeClient:
            def __init__(self, target, timeout=None, **_kw):
                self.address = getattr(target, "address", target)

            async def connect(self):
                return None

            async def disconnect(self):
                return None

            async def pair(self):
                calls["paired"].append(self.address)

            @property
            def services(self):
                return [FakeService(ancs.SERVICE_UUID)]

            async def start_notify(self, uuid, cb):
                if self.address == FIRST:
                    raise PermissionError("insufficient authorization")
                calls["notifies"][str(uuid).lower()] = cb

            async def write_gatt_char(self, uuid, payload, response=True):
                ds = calls["notifies"].get(ancs.DATA_SOURCE_UUID)
                payload = bytes(payload)
                if ds and payload[0] == ancs.CMD_GET_APP_ATTRIBUTES:
                    app_id = payload[1:payload.index(0, 1)].decode()
                    ds(None, bytes([1]) + app_id.encode() + b"\x00"
                       + tlv(ancs.APP_ATTR_DISPLAY_NAME, "Settings"))

        class FakeScanner:
            @staticmethod
            async def discover(timeout=None, return_adv=False):
                return {}

        fake = types.ModuleType("bleak")
        fake.BleakClient = FakeClient
        fake.BleakScanner = FakeScanner

        async def blind_lookup(_address):
            return None

        rows = self._rows(("iPhone", FIRST), ("Jonathan's iPhone", SECOND))

        async def rows_with_notes():
            return ([("iPhone", FIRST, "paired", None),
                     ("Jonathan's iPhone", SECOND, "paired", None)], [])

        with mock.patch.dict(sys.modules, {"bleak": fake}), \
                mock.patch.object(link, "PAIR_RETRY_WAITS", (0.02,)), \
                mock.patch.object(link, "_windows_pairing", blind_lookup), \
                mock.patch.object(link, "_paired_rows_with_notes",
                                  rows_with_notes), \
                mock.patch.object(link, "_paired_rows", rows):
            result, notes = link.setup_iphone(scan_timeout=0.05)
        self.assertIsNotNone(result, notes)
        self.assertEqual(SECOND, result["address"])
        self.assertFalse(result.get("paired_pending"), notes)
        self.assertFalse(result.get("needs_phone_link"), notes)
        self.assertIsNotNone(result.get("ms"), notes)
        self.assertEqual([], calls["paired"], "default mode never pairs")
        self.assertTrue(any("refused" in n for n in notes), notes)

    def test_paired_iphone_note_replaces_probably_not_your_iphone(self):
        """AncsMissing on an entry the paired list names 'iPhone' says
        the phone answered without the service this time; the 'probably
        not your iPhone' guess is reserved for unknown devices."""
        import asyncio
        from unittest import mock
        from app import config
        from app.phone import link
        from app.phone.link import PhoneLinkWorker
        settings = config.Settings(phone_ble_address=self.A,
                                   phone_ble_name="iPhone")
        worker = PhoneLinkWorker(settings)
        statuses = []
        worker.status.connect(lambda lvl, txt: statuses.append(txt))
        fake = FakeRadioSessionTests._fake_bleak(
            self, with_ancs=False, connected_address=self.A)

        async def scenario():
            worker._loop = asyncio.get_running_loop()
            worker._stop.clear()
            task = asyncio.create_task(worker._main())
            await asyncio.sleep(1.0)
            worker._stop.set()
            await asyncio.wait_for(task, 8)

        with mock.patch.dict(sys.modules, {"bleak": fake[0]}), \
                mock.patch.object(link, "_paired_rows",
                                  self._rows(("iPhone", self.A))):
            asyncio.run(scenario())
        self.assertTrue(any("without the notification service this time"
                            in s for s in statuses), statuses)
        self.assertFalse(any("probably not your iPhone" in s
                             for s in statuses), statuses)


if __name__ == "__main__":
    unittest.main()
