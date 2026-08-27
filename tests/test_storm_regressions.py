"""Regressions for the 3.1.0 window-storm freeze and the 3.1.1 redesigns.

The 3.1.0 field failure: turning help tips Off led to runaway window
creation and a frozen, visually corrupted app. The mechanisms that could
produce or amplify such a storm are now redesigned with provable bounds:

  1. The application-wide tooltip filter is inert: it examines exactly one
     event type and can only consume it. It cannot touch any window.
  2. The popup manager is the sole birthplace of alert cards and enforces
     a creation-rate circuit breaker plus a physical hard cap counted from
     the screen, not from bookkeeping.
  3. The agent upgrade handoff has a wall-clock and spawn budget, so a
     stuck handoff degrades to the calm reconnect cadence instead of
     spawning and reconnecting forever.

These tests hold every one of those properties in place, alongside the
requested UI changes (eye-off Hide control, animated connection badge).
"""
from __future__ import annotations

import ast
import enum
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

try:
    import platformdirs  # noqa: F401
except ModuleNotFoundError:
    platformdirs = types.ModuleType("platformdirs")
    platformdirs.user_data_dir = lambda *_args, **_kwargs: tempfile.gettempdir()
    sys.modules["platformdirs"] = platformdirs

ROOT = Path(__file__).parents[1]


def _function_source(path: Path, class_name: str, func_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (isinstance(item, ast.FunctionDef)
                        and item.name == func_name):
                    return ast.get_source_segment(source, item) or ""
    raise AssertionError(f"{class_name}.{func_name} not found in {path}")


class TooltipFilterInertnessTests(unittest.TestCase):
    """The global filter must be incapable of creating or hiding windows."""

    def setUp(self):
        self.path = ROOT / "app" / "ui" / "tooltips.py"
        self.filter_src = _function_source(
            self.path, "TooltipController", "eventFilter")

    def test_filter_examines_only_tooltip_events(self):
        self.assertIn("QEvent.Type.ToolTip", self.filter_src)
        for forbidden in ("Destroy", "Leave", "MouseButtonPress",
                          "MouseButtonRelease", "Paint", "Show", "Hide"):
            self.assertNotIn(forbidden, self.filter_src,
                             f"filter must not handle {forbidden} events")

    def test_filter_never_calls_into_tooltip_or_window_machinery(self):
        # QToolTip.hideText() from inside the application-global filter is
        # exactly the interference that produced the 3.1.0 window storm.
        for forbidden in ("hideText", "showText", "QToolTip", "close(",
                          "show(", "deleteLater", "setParent"):
            self.assertNotIn(forbidden, self.filter_src,
                             f"filter must never call {forbidden}")

    def test_single_remaining_qtooltip_call_is_outside_the_filter(self):
        source = self.path.read_text(encoding="utf-8")
        self.assertEqual(1, source.count("QToolTip.hideText()"))
        set_mode = _function_source(
            self.path, "TooltipController", "set_mode")
        self.assertIn("QToolTip.hideText()", set_mode)

    def test_off_mode_consumes_and_limited_mode_counts(self):
        from app.ui import tooltips as tooltips_module
        from app.ui.tooltips import TooltipController

        class Buttons:
            NoButton = 0

        class QtStub:
            MouseButton = Buttons

        class AppStub:
            mouse = Buttons.NoButton

            @classmethod
            def mouseButtons(cls):
                return cls.mouse

        class Event:
            def __init__(self, kind):
                self._kind = kind

            def type(self):
                return self._kind

        class Obj:
            def __init__(self, tip_id=""):
                self._tip_id = tip_id

            def property(self, _name):
                return self._tip_id

        original_app = tooltips_module.QApplication
        original_qt = tooltips_module.Qt
        tooltips_module.QApplication = AppStub
        tooltips_module.Qt = QtStub
        try:
            from PySide6.QtCore import QEvent
            settings = types.SimpleNamespace(
                tooltip_mode="limited", tooltip_seen={})
            controller = TooltipController.__new__(TooltipController)
            controller.settings = settings
            controller.mode = "limited"
            controller._session_seen = {}
            controller._save_timer = types.SimpleNamespace(
                start=lambda: None)
            tooltip_event = Event(QEvent.Type.ToolTip)
            other_event = Event(QEvent.Type.MouseButtonPress)
            stable = Obj("stable-control")

            # Non-tooltip events are never consumed, whatever the mode.
            controller.mode = "off"
            self.assertFalse(
                controller.eventFilter(stable, other_event))
            # Off consumes every tooltip event.
            self.assertTrue(controller.eventFilter(stable, tooltip_event))

            # Limited: two distinct hovers show, the third is consumed.
            controller.mode = "limited"
            self.assertFalse(controller.eventFilter(stable, tooltip_event))
            controller._session_seen.clear()   # end the hover window
            self.assertFalse(controller.eventFilter(stable, tooltip_event))
            controller._session_seen.clear()
            self.assertTrue(controller.eventFilter(stable, tooltip_event))
            self.assertEqual(2, settings.tooltip_seen["stable-control"])

            # A held mouse button suppresses tips without touching windows.
            controller.mode = "always"
            AppStub.mouse = 1
            self.assertTrue(controller.eventFilter(stable, tooltip_event))
            AppStub.mouse = Buttons.NoButton
            self.assertFalse(controller.eventFilter(stable, tooltip_event))
        finally:
            tooltips_module.QApplication = original_app
            tooltips_module.Qt = original_qt

    def test_repeated_events_during_one_hover_count_once(self):
        from app.ui import tooltips as tooltips_module
        from app.ui.tooltips import TooltipController
        from PySide6.QtCore import QEvent

        class AppStub:
            @staticmethod
            def mouseButtons():
                return 0

        class QtStub:
            class MouseButton:
                NoButton = 0

        class Event:
            @staticmethod
            def type():
                return QEvent.Type.ToolTip

        class Obj:
            @staticmethod
            def property(_name):
                return "same-control"

        original_app = tooltips_module.QApplication
        original_qt = tooltips_module.Qt
        tooltips_module.QApplication = AppStub
        tooltips_module.Qt = QtStub
        try:
            settings = types.SimpleNamespace(
                tooltip_mode="limited", tooltip_seen={})
            controller = TooltipController.__new__(TooltipController)
            controller.settings = settings
            controller.mode = "limited"
            controller._session_seen = {}
            controller._save_timer = types.SimpleNamespace(
                start=lambda: None)
            for _ in range(6):   # one resting hover streams many events
                self.assertFalse(controller.eventFilter(Obj, Event))
            self.assertEqual(1, settings.tooltip_seen["same-control"])
        finally:
            tooltips_module.QApplication = original_app
            tooltips_module.Qt = original_qt


class PopupBreakerTests(unittest.TestCase):
    """Card creation is rate-bounded and physically capped."""

    def _namespace(self, live_cards=0):
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

        class FakePopup:
            def __init__(self, _title="", _body="", _code=None,
                         _chat_guid="", event_key=""):
                self.event_key = event_key
                self.open_requested = Signal()
                self.dismissed = Signal()

            def show(self):
                pass

            def raise_(self):
                pass

            def deleteLater(self):
                pass

            def isVisible(self):
                return True

            def height(self):
                return 10

            def width(self):
                return 10

            def move(self, *_a):
                pass

        live = [FakePopup() for _ in range(live_cards)]

        class ApplicationStub:
            topLevelWidgets = staticmethod(lambda: list(live))

        scheduled = []

        class Timer:
            @staticmethod
            def singleShot(delay, callback):
                scheduled.append((delay, callback))

        class GuiApplication:
            screenAt = staticmethod(lambda _pos: None)
            primaryScreen = staticmethod(lambda: None)

        class Cursor:
            pos = staticmethod(lambda: None)

        namespace = {
            "Enum": enum.Enum,
            "NotificationPopup": FakePopup,
            "QTimer": Timer,
            "QGuiApplication": GuiApplication,
            "QCursor": Cursor,
            "QApplication": ApplicationStub,
            "log": logging.getLogger("storm-regression"),
            "Signal": Signal,
            "time": time,
        }
        module = ast.fix_missing_locations(
            ast.Module(body=selected, type_ignores=[]))
        exec(compile(module, "notify-breaker-subset", "exec"), namespace)
        namespace["scheduled"] = scheduled
        namespace["FakePopup"] = FakePopup
        return namespace

    def test_breaker_stops_creation_bursts_and_schedules_resume(self):
        ns = self._namespace()
        manager = ns["PopupManager"](lambda _guid: None)
        created_limit = manager.BREAKER_MAX_CREATIONS
        results = []
        for index in range(created_limit + 6):
            results.append(manager.show(
                f"t{index}", "b", None, "chat", event_key=f"k{index}"))
            # Simulate the churn of a storm: every card vanishes at once,
            # which in 3.1.0 allowed endless further creations.
            for popup in list(manager.active):
                manager._gone(popup)
        shown = [r for r in results if r is ns["PresentationResult"].SHOWN]
        self.assertLessEqual(len(shown), created_limit)
        self.assertGreater(len(manager.pending), 0)
        self.assertTrue(manager._breaker_until > 0)
        self.assertTrue(ns["scheduled"], "breaker must schedule a resume")

    def test_breaker_releases_after_cooldown(self):
        ns = self._namespace()
        manager = ns["PopupManager"](lambda _guid: None)
        manager._creation_times = [time.monotonic()] * (
            manager.BREAKER_MAX_CREATIONS)
        self.assertIs(ns["PresentationResult"].QUEUED,
                      manager.show("t", "b", None, "chat", event_key="k1"))
        # After the window has passed, the same request flows again.
        manager._breaker_until = 0.0
        manager._creation_times = [
            time.monotonic() - manager.BREAKER_WINDOW_S - 1.0
        ] * manager.BREAKER_MAX_CREATIONS
        manager._drain_pending()
        self.assertEqual([], manager.pending)
        self.assertEqual(1, len(manager.active))

    def test_hard_cap_counts_the_screen_not_the_books(self):
        # Even with empty internal accounting, eight live card windows on
        # screen mean no further card may be created.
        ns = self._namespace(live_cards=8)
        manager = ns["PopupManager"](lambda _guid: None)
        self.assertEqual([], manager.active)   # books say nothing is shown
        result = manager.show("t", "b", None, "chat", event_key="cap")
        self.assertIs(ns["PresentationResult"].UNAVAILABLE, result)

    def test_queue_remains_bounded_under_flood(self):
        ns = self._namespace(live_cards=8)
        manager = ns["PopupManager"](lambda _guid: None)
        manager.active = [ns["FakePopup"]() for _ in range(manager.MAX)]
        results = [manager.show("t", "b", None, "chat", event_key=f"q{i}")
                   for i in range(manager.MAX_PENDING + 50)]
        self.assertLessEqual(len(manager.pending), manager.MAX_PENDING)
        self.assertIn(ns["PresentationResult"].UNAVAILABLE, results)


class UpgradeHandoffBoundsTests(unittest.TestCase):
    def test_upgrade_loops_check_their_budget(self):
        source = (ROOT / "app" / "ui" / "agent_link.py").read_text(
            encoding="utf-8")
        self.assertIn("_upgrade_overall_deadline", source)
        self.assertIn("_upgrade_forced_spawns < 8", source)
        for looper in ("_poll_upgrade_stop", "_launch_upgrade_agent",
                       "_retry_upgrade_launch"):
            body = _function_source(
                ROOT / "app" / "ui" / "agent_link.py", "AgentLink", looper)
            self.assertIn("_upgrade_expired()", body,
                          f"{looper} must consult the upgrade budget")

    def test_expiry_returns_link_to_calm_cadence(self):
        body = _function_source(
            ROOT / "app" / "ui" / "agent_link.py", "AgentLink",
            "_upgrade_expired")
        self.assertIn("self._upgrade_active = False", body)
        self.assertIn("install.bat", body)


class RequestedUiChangesTests(unittest.TestCase):
    def test_chat_list_hide_control_is_an_accent_eye_off(self):
        source = (ROOT / "app" / "ui" / "chat_list.py").read_text(
            encoding="utf-8")
        self.assertIn("from .icons import eye_off", source)
        self.assertIn("hide_icon_pixmap(theme.ACCENT", source)
        # The old ✕ strokes are gone entirely.
        self.assertNotIn("drawLine", source)
        # Scale-responsive hit target and icon.
        self.assertIn("theme.dim(32)", source)
        self.assertIn("cr.width() * 0.62", source)
        # The icon cache prevents per-mouse-move icon rebuilding.
        self.assertIn("_HIDE_ICON_CACHE", source)

    def test_connection_badge_replaces_the_static_dot(self):
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        self.assertNotIn("self.dot", main)
        self.assertIn("ConnectionBadge", main)
        self.assertIn("connChip", main)
        badge = (ROOT / "app" / "ui" / "connection_badge.py").read_text(
            encoding="utf-8")
        # Animated only while visible; hidden badge stops its timer.
        hide_event = _function_source(
            ROOT / "app" / "ui" / "connection_badge.py", "ConnectionBadge",
            "hideEvent")
        self.assertIn("self._timer.stop()", hide_event)
        for state in ('"ok"', '"warn"', '"fail"'):
            self.assertIn(state, badge)
        self.assertIn("theme.dim(24)", badge)      # larger than the old dot
        self.assertIn("_ORBIT_DEG_PER_S", badge)   # it actually moves

    def test_badge_states_follow_status_kinds(self):
        set_status = _function_source(
            ROOT / "app" / "ui" / "main_window.py", "MainWindow",
            "set_status")
        self.assertIn("self.badge.set_state", set_status)
        flash = _function_source(
            ROOT / "app" / "ui" / "main_window.py", "MainWindow",
            "_flash_status")
        self.assertIn("prev_state = self.badge.state", flash)


class GhostWindowEliminationTests(unittest.TestCase):
    """3.1.2: the Windows ghost-window storms (bare white frames flashing
    during settings save, hide, and unhide) are attacked at every layer:
    no visible widget is ever orphaned into a top-level window object,
    rebuilds are batched, needless churn is deduplicated, and a runtime
    warden names and hides anything unexpected that still appears."""

    def test_no_teardown_path_orphans_a_visible_widget(self):
        for path, cls, func in (
                ("app/ui/thread_view.py", "ThreadView", "_clear_layout"),
                ("app/ui/thread_view.py", "ThreadView", "refresh_outbox"),
                ("app/ui/composer.py", "Composer", "_remove"),
                ("app/ui/compose_dialog.py", "ComposeDialog",
                 "_remove_pick")):
            body = _function_source(ROOT / path, cls, func)
            self.assertNotIn("setParent(None)",
                             body.replace("# setParent(None)", ""),
                             f"{cls}.{func} must not orphan widgets")
            self.assertIn("deleteLater", body)
            self.assertIn("hide()", body)

    def test_thread_rebuild_is_batched_with_updates_paused(self):
        body = _function_source(
            ROOT / "app" / "ui" / "thread_view.py", "ThreadView", "_render")
        self.assertIn("setUpdatesEnabled(False)", body)
        self.assertIn("finally", body)
        self.assertIn("setUpdatesEnabled(True)", body)

    def test_chat_list_generates_hover_repaints_for_every_row(self):
        source = (ROOT / "app" / "ui" / "chat_list.py").read_text(
            encoding="utf-8")
        self.assertIn("WA_Hover", source)
        self.assertIn("viewport().setMouseTracking(True)", source)
        self.assertIn("_repaint_hover_change", source)
        for func in ("mouseMoveEvent", "leaveEvent"):
            self.assertIn(f"def {func}", source)

    def test_reload_skips_identical_rows_and_theme_skips_no_op_saves(self):
        reload_body = _function_source(
            ROOT / "app" / "ui" / "main_window.py", "MainWindow",
            "reload_chats")
        self.assertIn("_rows_signature", reload_body)
        settings_body = _function_source(
            ROOT / "app" / "ui" / "main_window.py", "MainWindow",
            "open_settings")
        self.assertIn("appearance_before", settings_body)

    def test_tray_menu_is_owned_and_parented(self):
        source = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        self.assertIn("self._tray_menu = QMenu(self)", source)

    def test_window_warden_names_and_neutralizes_with_a_rate_cap(self):
        warden_src = (ROOT / "app" / "ui" / "activity_log.py").read_text(
            encoding="utf-8")
        self.assertIn("class WindowWarden", warden_src)
        self.assertIn("MAX_HIDES_PER_MINUTE", warden_src)
        self.assertIn("Unexpected window detected and hidden", warden_src)
        expected = _function_source(
            ROOT / "app" / "ui" / "activity_log.py", "WindowWarden",
            "_expected")
        for cover in ("QMainWindow", "QDialog", "Popup", "ToolTip"):
            self.assertIn(cover, expected)
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        self.assertIn("WindowWarden(", main)
        self.assertIn("self.warden.start()", main)
        self.assertIn("self.warden.stop()", main)

    def test_badge_motion_is_clamped_against_event_loop_saturation(self):
        badge = (ROOT / "app" / "ui" / "connection_badge.py").read_text(
            encoding="utf-8")
        self.assertIn("_last_paint", badge)
        self.assertIn("dt = min(", badge)
        # The angle must come from the clamped step, not raw wall time.
        paint = _function_source(
            ROOT / "app" / "ui" / "connection_badge.py", "ConnectionBadge",
            "paintEvent")
        self.assertNotIn("time.monotonic() - self._t0", paint)


class ActivityLogTests(unittest.TestCase):
    def test_recorder_is_bounded_and_collapses_repeats(self):
        from app.ui.activity_log import RING_SIZE, ActivityRecorder
        recorder = ActivityRecorder()
        heard = []
        recorder.entry_added.connect(heard.append)
        for _ in range(30):
            recorder.record("status", "Connected")
        self.assertEqual(
            1 + 2, len(heard),
            "identical lines must collapse (first + the ×5 and ×25 marks)")
        recorder.record("error", "boom")
        self.assertIn("ERROR", recorder.text())
        for index in range(RING_SIZE + 50):
            recorder.record("status", f"line {index}")
        self.assertLessEqual(len(recorder.entries), RING_SIZE)

    def test_logging_bridge_mirrors_warnings_from_any_module(self):
        from app.ui.activity_log import ActivityLogHandler, ActivityRecorder
        recorder = ActivityRecorder()
        handler = ActivityLogHandler(recorder)
        probe = logging.getLogger("storm.probe")
        probe.addHandler(handler)
        try:
            probe.warning("socket connect failed: refused")
            probe.error("wake refused: send active")
            probe.info("routine detail")   # below the bridge's level
        finally:
            probe.removeHandler(handler)
        text = recorder.text()
        self.assertIn("socket connect failed", text)
        self.assertIn("wake refused", text)
        self.assertNotIn("routine detail", text)

    def test_window_wires_the_activity_surface(self):
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        self.assertIn("ActivityRecorder", main)
        self.assertIn("ActivityLogHandler", main)
        self.assertIn("ActivityPanel", main)
        # Connection attempts, refusals, resets, wakes, recoveries all land
        # in the recorder.
        self.assertGreaterEqual(main.count("self.activity.record"), 6)
        for kind in ('"link"', '"push"', '"wake"', '"repair"',
                     '"settings"', '"agent"'):
            self.assertIn(kind, main)
        # The chip opens the live panel, not a modal box.
        details = _function_source(
            ROOT / "app" / "ui" / "main_window.py", "MainWindow",
            "_show_connection_details")
        self.assertIn("ActivityPanel", details)
        self.assertNotIn("QMessageBox", details)


class ModernGearIconTests(unittest.TestCase):
    def test_gear_is_drawn_scaled_and_accent_tinted(self):
        icons = (ROOT / "app" / "ui" / "icons.py").read_text(encoding="utf-8")
        self.assertIn("def gear(", icons)
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        self.assertIn("gear(theme.ACCENT)", main)
        self.assertIn("self.settings_btn.setIconSize", main)
        self.assertNotIn("⚙", main)


class AlwaysAlertTests(unittest.TestCase):
    """3.1.3: an open window is not an attentive reader. Sound and popup
    fire for every fresh incoming text regardless of window or
    conversation visibility; only notifications Off or stale age stays
    quiet. Unread badges still respect what is visibly on screen."""

    def test_alerts_no_longer_gate_on_conversation_visibility(self):
        drain = _function_source(
            ROOT / "app" / "ui" / "main_window.py", "MainWindow",
            "_drain_delivery_events_inner")
        self.assertIn("age > constants.NOTIFY_MAX_AGE_MS", drain)
        self.assertNotIn("chat_open or mode", drain)
        # Unread state still uses visibility, exactly as before.
        self.assertIn("chat_is_open=chat_open", drain)
        # The master switches are the only quiet paths, and the sweep body
        # is exception-guarded so alerts can never die silently.
        self.assertIn("popups_on", drain)
        outer = _function_source(
            ROOT / "app" / "ui" / "main_window.py", "MainWindow",
            "_drain_delivery_events")
        self.assertIn("except Exception", outer)
        self.assertIn("log.exception", outer)

    def test_sound_plays_regardless_of_focus(self):
        signal = _function_source(
            ROOT / "app" / "ui" / "main_window.py", "MainWindow",
            "_signal_notification")
        self.assertNotIn("_conversation_is_visible", signal)
        self.assertIn("play_notification_sound()", signal)
        self.assertIn("isActiveWindow", signal)   # taskbar flash only

    def test_new_arrival_steers_the_conversation_list(self):
        main_path = ROOT / "app" / "ui" / "main_window.py"
        drain = _function_source(
            main_path, "MainWindow", "_drain_delivery_events_inner")
        self.assertIn("self._scroll_target_guid = chat_guid", drain)
        reload_body = _function_source(
            main_path, "MainWindow", "reload_chats")
        self.assertEqual(2, reload_body.count("_scroll_list_to_target"),
                         "both the rebuild and the deduped-skip path must "
                         "steer the list")
        scroll = _function_source(
            main_path, "MainWindow", "_scroll_list_to_target")
        self.assertIn("scrollTo", scroll)
        # Scroll only: reading must never be hijacked.
        self.assertNotIn("setCurrentIndex", scroll)
        self.assertNotIn("open_conversation", scroll)


class HideZoneTests(unittest.TestCase):
    """3.1.3: every row is two actions. Left opens, the right zone hides.
    Unread rows advertise the zone with the full eye-off chip; read rows
    keep only a subtle hover shade, by design."""

    def test_zone_geometry_scales_and_contains_the_chip(self):
        from PySide6.QtCore import QRect
        from app.ui.chat_list import close_rect, hide_zone
        row = QRect(0, 0, 340, 60)
        zone = hide_zone(row)
        chip = close_rect(row)
        self.assertEqual(row.right(), zone.right())
        self.assertGreaterEqual(zone.width(), 44)
        self.assertEqual(row.height(), zone.height())
        self.assertTrue(zone.contains(chip),
                        "the chip must sit inside the click zone so both "
                        "visuals share one hit area")

    def test_click_routing_uses_the_zone_not_the_chip(self):
        source = (ROOT / "app" / "ui" / "chat_list.py").read_text(
            encoding="utf-8")
        press = _function_source(
            ROOT / "app" / "ui" / "chat_list.py", "ChatListView",
            "mousePressEvent")
        self.assertIn("hide_zone(r).contains", press)
        self.assertNotIn("close_rect(r).contains", press)
        # Zone hover state is tracked and repainted.
        self.assertIn("_hover_in_zone", source)

    def test_unread_rows_get_the_chip_and_read_rows_get_the_shade(self):
        paint = _function_source(
            ROOT / "app" / "ui" / "chat_list.py", "RowDelegate", "paint")
        hover_block = paint[paint.index("if hovered and row.focus_guid"):]
        self.assertIn("if row.unread:", hover_block)
        self.assertIn("hide_icon_pixmap", hover_block)
        self.assertIn("QLinearGradient", hover_block)
        # The subtle path is clipped to the rounded row and uses a low-alpha
        # accent tint, not a second chip.
        self.assertIn("setClipPath", hover_block)
        self.assertIn("setAlpha", hover_block)


class AlertRedundancyTests(unittest.TestCase):
    """3.1.4: after alerts went silent in the field with no visible cause,
    the alert path is redundant and self-reporting. Sound is decoupled
    from card success, popup and Windows toast fall back to each other,
    two unmissable master switches control everything, and every attempt
    writes its outcome to the Activity panel."""

    def test_sound_is_decoupled_from_presentation_success(self):
        flush = _function_source(
            ROOT / "app" / "ui" / "main_window.py", "MainWindow",
            "_flush_toasts")
        # The burst signal fires before any card machinery runs.
        self.assertLess(flush.index("_signal_notification"),
                        flush.index("code_items"))

    def test_popup_and_toast_fall_back_to_each_other(self):
        present = _function_source(
            ROOT / "app" / "ui" / "main_window.py", "MainWindow",
            "_present_notification")
        self.assertIn("Windows toast unavailable; using the popup card",
                      present)
        self.assertIn("popup refused; Windows toast used", present)
        self.assertIn("alert could not be shown, retrying", present)
        # Every outcome is recorded.
        self.assertGreaterEqual(present.count("self.activity.record"), 5)

    def test_master_switches_exist_and_are_saved(self):
        dialog = (ROOT / "app" / "ui" / "settings_dialog.py").read_text(
            encoding="utf-8")
        self.assertIn("Show a popup for every new message", dialog)
        self.assertIn("Play a sound for every new message", dialog)
        self.assertIn("self.settings.popups_enabled", dialog)
        self.assertIn("self.settings.notification_sound", dialog)
        # Off is no longer a style; the switch owns it.
        self.assertNotIn('"Off": "off"', dialog)

    def test_legacy_off_mode_migrates_to_the_popup_switch(self):
        import json
        from app import config, constants as consts
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "config.json"
            fake.write_text(json.dumps({"notify_mode": "off"}),
                            encoding="utf-8")
            original = consts.CONFIG_PATH
            consts.CONFIG_PATH = fake
            try:
                settings = config.load()
            finally:
                consts.CONFIG_PATH = original
        self.assertFalse(settings.popups_enabled)
        self.assertEqual("popup", settings.notify_mode)

    def test_sound_only_mode_still_announces_and_completes(self):
        drain = _function_source(
            ROOT / "app" / "ui" / "main_window.py", "MainWindow",
            "_drain_delivery_events_inner")
        block = drain[drain.index("if not popups_on:"):]
        self.assertIn("self._signal_notification()", block)
        self.assertIn("finish_notification_event", block)

    def test_test_alert_reports_through_the_real_pipeline(self):
        test_popup = _function_source(
            ROOT / "app" / "ui" / "main_window.py", "MainWindow",
            "_test_popup")
        self.assertIn("_present_notification", test_popup)
        self.assertIn("play_notification_sound()", test_popup)
        self.assertIn("self.activity.record", test_popup)


class DedicatedQuitTests(unittest.TestCase):
    """3.1.4: closing the app must never require Task Manager."""

    def test_quit_button_exists_with_drawn_scaled_icon(self):
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        self.assertIn("self.quit_btn", main)
        self.assertIn("power(theme.ACCENT)", main)
        self.assertIn("quit_completely", main)
        icons = (ROOT / "app" / "ui" / "icons.py").read_text(encoding="utf-8")
        self.assertIn("def power(", icons)

    def test_shutdown_carries_a_process_kill_guarantee(self):
        shutdown = _function_source(
            ROOT / "app" / "ui" / "main_window.py", "MainWindow",
            "_shutdown")
        self.assertIn("os._exit(0)", shutdown)

    def test_second_launch_activates_instead_of_erroring(self):
        entry = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("window_pipe_name", entry)
        self.assertIn("waitForConnected", entry)
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        self.assertIn("_on_activation_request", main)
        self.assertIn("_show_window()", main)


class SelfConversationAlertTests(unittest.TestCase):
    """3.1.5: the field-silence mystery, solved by a screenshot. The user
    tests alerts by texting himself; Apple marks those texts sent-by-you
    on every device, and the ledger excluded all sent-by-you rows, so his
    self-texts could never alert while everything else worked. Texts in a
    conversation with one of your own addresses are now incoming for the
    alert ledger; texts sent from this app never re-alert."""

    def setUp(self):
        from app.store.db import Database
        from app.store.repo import Repo
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "messages.db")
        self.repo = Repo(self.db)

    def tearDown(self):
        connection = getattr(self.db._local, "c", None)
        if connection is not None:
            connection.close()
            self.db._local.c = None
        self.tmp.cleanup()

    @staticmethod
    def _message(guid, rowid, chat, from_me):
        from app.api.models import parse_message
        parsed = parse_message({
            "guid": guid, "originalROWID": rowid,
            "dateCreated": 1_800_000_000_000 + rowid,
            "isFromMe": from_me, "text": f"m {guid}",
            "chats": [{"guid": chat}],
            "handle": {"address": chat.split(";")[-1]},
            "attachments": [],
        })
        assert parsed is not None
        return parsed

    SELF = "iMessage;-;+15875550123"
    OTHER = "iMessage;-;+15555550100"

    def test_self_chat_from_me_text_creates_an_alert_event(self):
        self.repo.set_self_identities({"5875550123"}, True)
        self.repo.upsert_message(
            self._message("self-1", 1, self.SELF, True),
            notify_eligible=True)
        events = self.repo.pending_delivery_events()
        self.assertEqual(["self-1"], [e["message_guid"] for e in events])

    def test_other_chats_and_groups_keep_the_from_me_exclusion(self):
        self.repo.set_self_identities({"5875550123"}, True)
        self.repo.upsert_message(
            self._message("out-1", 1, self.OTHER, True),
            notify_eligible=True)
        group = "iMessage;+;chat123"
        self.repo.upsert_message(
            self._message("out-2", 2, group, True), notify_eligible=True)
        self.assertEqual([], self.repo.pending_delivery_events())

    def test_switch_off_restores_the_old_behavior(self):
        self.repo.set_self_identities({"5875550123"}, False)
        self.repo.upsert_message(
            self._message("self-2", 1, self.SELF, True),
            notify_eligible=True)
        self.assertEqual([], self.repo.pending_delivery_events())

    def test_texts_sent_from_this_app_never_alert(self):
        self.repo.set_self_identities({"5875550123"}, True)
        oid = self.repo.enqueue(self.SELF, "note to self", None)
        self.repo.outbox_set(oid, "sending")
        # The send completion stamps the recorded marker...
        self.repo.upsert_message(
            self._message("app-sent", 5, self.SELF, True),
            complete_outbox_id=oid)
        # ...so the reconciler's later authoritative re-read cannot turn
        # our own outgoing note into an alert.
        self.repo.upsert_message(
            self._message("app-sent", 5, self.SELF, True),
            notify_eligible=True, allow_existing_event=True)
        self.assertEqual([], self.repo.pending_delivery_events())

    def test_rescans_create_exactly_one_event(self):
        self.repo.set_self_identities({"5875550123"}, True)
        for _ in range(3):
            self.repo.upsert_message(
                self._message("self-3", 7, self.SELF, True),
                notify_eligible=True, allow_existing_event=True)
        self.assertEqual(1, len(self.repo.pending_delivery_events()))

    def test_identities_persist_for_the_next_agent_start(self):
        from app.store.repo import Repo
        self.repo.set_self_identities({"5875550123", "me@example.com"}, True)
        reborn = Repo(self.db)
        self.assertTrue(reborn.is_self_chat(self.SELF))
        self.assertTrue(reborn.is_self_chat("iMessage;-;me@example.com"))
        self.assertFalse(reborn.is_self_chat(self.OTHER))

    def test_agent_and_settings_wire_the_identities(self):
        core = (ROOT / "app" / "agent" / "core.py").read_text(
            encoding="utf-8")
        self.assertIn("_apply_self_identities", core)
        for caller in ("def _on_server_info", "def reload_settings"):
            body = core[core.index(caller):]
            end = body.find("\n    def ", 10)
            if end > 0:          # a last method has no successor to slice at
                body = body[:end]
            self.assertIn("_apply_self_identities", body,
                          f"{caller} must refresh self identities")
        dialog = (ROOT / "app" / "ui" / "settings_dialog.py").read_text(
            encoding="utf-8")
        self.assertIn("Alert for texts you send to yourself", dialog)
        self.assertIn("self.settings.self_chat_alerts", dialog)
        self.assertIn("self.settings.self_addresses", dialog)


class NotificationFeedTests(unittest.TestCase):
    """3.2.0: the in-app notification center's durable feed. Message rows
    dedupe on GUID so sweeps and retries can record blindly; hiding is
    soft; pruning is bounded; everything survives a restart."""

    def setUp(self):
        from app.store.db import Database
        from app.store.repo import Repo
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "messages.db")
        self.repo = Repo(self.db)

    def tearDown(self):
        connection = getattr(self.db._local, "c", None)
        if connection is not None:
            connection.close()
            self.db._local.c = None
        self.tmp.cleanup()

    def test_add_and_recent_order_newest_first(self):
        self.assertTrue(self.repo.feed_add(
            "message", "Anna", "hi", "chat-a", "g-1", created_ms=1000))
        self.assertTrue(self.repo.feed_add(
            "wake", "Mac woken", "2 recovered", created_ms=2000))
        rows = self.repo.feed_recent()
        self.assertEqual(["Mac woken", "Anna"], [r["title"] for r in rows])
        self.assertEqual("message", rows[1]["kind"])

    def test_message_rows_dedupe_durably_on_guid(self):
        self.assertTrue(self.repo.feed_add(
            "message", "Anna", "hi", "chat-a", "g-1"))
        self.assertFalse(self.repo.feed_add(
            "message", "Anna", "hi again", "chat-a", "g-1"))
        self.assertEqual(1, len(self.repo.feed_recent()))
        # Rows without a GUID (wake, repair, link) never dedupe each other.
        self.assertTrue(self.repo.feed_add("wake", "Mac woken"))
        self.assertTrue(self.repo.feed_add("wake", "Mac woken"))
        self.assertEqual(3, len(self.repo.feed_recent()))

    def test_unseen_count_and_mark_all_seen(self):
        self.repo.feed_add("message", "Anna", "hi", "c", "g-1")
        self.repo.feed_add("message", "Ben", "yo", "c2", "g-2")
        self.assertEqual(2, self.repo.feed_unseen_count())
        self.repo.feed_mark_all_seen()
        self.assertEqual(0, self.repo.feed_unseen_count())

    def test_hide_is_soft_and_keeps_dedupe(self):
        self.repo.feed_add("message", "Anna", "hi", "c", "g-1")
        row = self.repo.feed_recent()[0]
        self.repo.feed_hide(row["id"])
        self.assertEqual([], self.repo.feed_recent())
        self.assertEqual(0, self.repo.feed_unseen_count())
        # The hidden row still blocks a duplicate alert entry.
        self.assertFalse(self.repo.feed_add(
            "message", "Anna", "hi", "c", "g-1"))
        self.assertEqual([], self.repo.feed_recent())

    def test_clear_hides_everything_at_once(self):
        for n in range(4):
            self.repo.feed_add("message", f"T{n}", "b", "c", f"g-{n}")
        self.repo.feed_clear()
        self.assertEqual([], self.repo.feed_recent())
        self.assertEqual(0, self.repo.feed_unseen_count())

    def test_prune_keeps_only_the_newest(self):
        now = int(time.time() * 1000)
        for n in range(30):
            self.repo.feed_add("message", f"T{n}", "b", "c", f"g-{n}",
                               created_ms=now + n)
        removed = self.repo.feed_prune(keep=10)
        self.assertEqual(20, removed)
        rows = self.repo.feed_recent(100)
        self.assertEqual(10, len(rows))
        self.assertEqual("T29", rows[0]["title"])

    def test_feed_survives_a_restart(self):
        from app.store.repo import Repo
        self.repo.feed_add("message", "Anna", "hi", "c", "g-1")
        reborn = Repo(self.db)
        self.assertEqual(1, len(reborn.feed_recent()))
        self.assertEqual(1, reborn.feed_unseen_count())


class AlertCenterSafetyTests(unittest.TestCase):
    """3.2.0: the notification center must be structurally incapable of
    reviving the 3.1.x ghost-window storm, and the window must feed it
    from every alert branch."""

    def test_panel_is_a_child_overlay_never_a_top_level(self):
        src = (ROOT / "app" / "ui" / "alert_center.py").read_text(
            encoding="utf-8")
        self.assertIn("class AlertCenterPanel(QFrame)", src)
        self.assertIn("super().__init__(window)", src,
                      "the panel must be parented to the main window")
        self.assertNotIn("setParent(None)", src)
        self.assertNotIn("Qt.Popup", src)
        self.assertNotIn("Qt.Tool", src)
        self.assertNotIn("WindowStaysOnTop", src)

    def test_row_teardown_never_orphans_widgets(self):
        refresh = _function_source(
            ROOT / "app" / "ui" / "alert_center.py", "AlertCenterPanel",
            "refresh")
        self.assertIn("removeWidget", refresh)
        self.assertIn("deleteLater", refresh)
        self.assertIn("setUpdatesEnabled(False)", refresh)
        self.assertIn("finally", refresh)

    def test_outside_click_filter_is_inert(self):
        src_path = ROOT / "app" / "ui" / "alert_center.py"
        filter_src = _function_source(
            src_path, "_OutsideClickFilter", "eventFilter")
        self.assertIn("MouseButtonPress", filter_src)
        self.assertIn("return False", filter_src)
        # Exactly one event type is examined and nothing is ever consumed.
        self.assertEqual(1, filter_src.count("event.type()"))
        self.assertNotIn("return True", filter_src)
        # Installed only while the panel is open, removed on hide.
        show = _function_source(src_path, "AlertCenterPanel", "show_panel")
        hide = _function_source(src_path, "AlertCenterPanel", "hide_panel")
        self.assertIn("installEventFilter", show)
        self.assertIn("removeEventFilter", hide)

    def test_window_feeds_the_center_from_every_alert_branch(self):
        main_path = ROOT / "app" / "ui" / "main_window.py"
        drain = _function_source(
            main_path, "MainWindow", "_drain_delivery_events_inner")
        self.assertIn("_feed_record", drain)
        self.assertIn('event["first_seen_ms"] or now_ms', drain)
        for method in ("_on_wake_event", "_on_recovery_event",
                       "_record_link_transition", "_test_popup"):
            body = _function_source(main_path, "MainWindow", method)
            self.assertIn("_feed_record", body,
                          f"{method} must record to the center")
        record = _function_source(main_path, "MainWindow", "_feed_record")
        self.assertIn("except Exception", record,
                      "feed bookkeeping must never break the alert path")
        main = main_path.read_text(encoding="utf-8")
        self.assertIn("AlertCenterPanel(", main)
        self.assertIn("feed_prune", main)
        self.assertIn("_toggle_alert_center", main)
        resize = _function_source(main_path, "MainWindow", "resizeEvent")
        self.assertIn("reposition", resize)

    def test_bell_and_settings_wiring(self):
        main = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        self.assertIn("self.bell_btn", main)
        self.assertIn("Ctrl+B", main)
        icons_src = (ROOT / "app" / "ui" / "icons.py").read_text(
            encoding="utf-8")
        self.assertIn("def bell(", icons_src)
        dialog = (ROOT / "app" / "ui" / "settings_dialog.py").read_text(
            encoding="utf-8")
        self.assertIn("alert_center_enabled", dialog)
        self.assertIn("self.settings.alert_center_enabled", dialog)
        from app.config import Settings
        self.assertTrue(Settings().alert_center_enabled)


class TintSuiteTests(unittest.TestCase):
    """3.2.0: the expanded tint suite with named swatches. Old names keep
    their exact values so an existing config lands on the same look."""

    ORIGINAL = {
        "Blue": ("#4f8cff", "#3a6fd6", "#2f5fd0"),
        "Teal": ("#35c2b0", "#2a9c8d", "#1f8175"),
        "Green": ("#4cc077", "#3a9d5f", "#2e8150"),
        "Violet": ("#8b7cf6", "#6f61d8", "#5b4fc7"),
        "Rose": ("#ec6a9c", "#c9527f", "#ad3f69"),
        "Amber": ("#e3a53c", "#bd8630", "#9c6f28"),
        "Graphite": ("#93a1b8", "#75839a", "#49536a"),
    }

    @staticmethod
    def _luminance(color: str) -> float:
        r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def test_suite_is_expanded_and_backward_compatible(self):
        from app.ui import theme
        self.assertGreaterEqual(len(theme.ACCENTS), 16)
        for name, triple in self.ORIGINAL.items():
            self.assertEqual(triple, theme.ACCENTS[name],
                             f"{name} must keep its exact pre-3.2 values")

    def test_every_tint_is_well_formed_and_legible(self):
        import re
        from app.ui import theme
        hex_form = re.compile(r"^#[0-9a-f]{6}$")
        for name, (accent, pressed, bubble) in theme.ACCENTS.items():
            for value in (accent, pressed, bubble):
                self.assertRegex(value.lower(), hex_form, f"{name}: {value}")
            self.assertGreaterEqual(
                self._luminance(accent), 0.16,
                f"{name} accent must stay visible on the dark theme")
            self.assertLess(
                self._luminance(bubble), self._luminance(accent),
                f"{name} bubble must be darker than its accent so white "
                "bubble text stays readable")
            # The envelope of the shipped design: no bubble may be lighter
            # than the long-standing Amber bubble carrying white text.
            self.assertLessEqual(
                self._luminance(bubble), self._luminance("#9c6f28") + 0.001,
                f"{name} bubble carries white text")

    def test_swatch_picker_replaces_the_bare_name_combo(self):
        dialog = (ROOT / "app" / "ui" / "settings_dialog.py").read_text(
            encoding="utf-8")
        self.assertIn("swatch_pixmap", dialog)
        self.assertIn("Tint color", dialog)
        self.assertIn("QTabWidget", dialog)
        self.assertIn("self._accent_name", dialog)
        self.assertNotIn("accent_combo", dialog)
        theme_src = (ROOT / "app" / "ui" / "theme.py").read_text(
            encoding="utf-8")
        self.assertIn("def swatch_pixmap(", theme_src)
        self.assertIn("_SWATCH_CACHE", theme_src)

    def test_unknown_accent_still_falls_back_to_blue(self):
        from app.ui import theme
        self.assertEqual(theme.ACCENTS["Blue"],
                         theme.ACCENTS.get("NotAColor", theme.ACCENTS["Blue"]))


class RelativeTimeTests(unittest.TestCase):
    """fmt_ago drives the notification center's timestamps."""

    def test_relative_forms(self):
        from datetime import datetime, timedelta
        from app.util.timefmt import fmt_ago
        now_dt = datetime(2026, 8, 19, 15, 0, 0)
        now = int(now_dt.timestamp() * 1000)
        self.assertEqual("now", fmt_ago(now - 20_000, now))
        self.assertEqual("5m ago", fmt_ago(now - 5 * 60_000, now))
        self.assertEqual("3h ago", fmt_ago(now - 3 * 3_600_000, now))
        yesterday = int((now_dt - timedelta(hours=16)).timestamp() * 1000)
        self.assertTrue(fmt_ago(yesterday, now).startswith("Yesterday"))
        last_month = int((now_dt - timedelta(days=40)).timestamp() * 1000)
        self.assertNotIn("ago", fmt_ago(last_month, now))
        # The clock never runs backwards even if timestamps skew.
        self.assertEqual("now", fmt_ago(now + 90_000, now))


class DependencyHygieneTests(unittest.TestCase):
    def test_requirements_are_exactly_the_imports_plus_transports(self):
        requirements = (ROOT / "requirements.txt").read_text(
            encoding="utf-8").lower()
        # requests is real: python-socketio's long-polling fallback uses it,
        # and the socket layer alternates polling and websocket transports.
        for needed in ("pyside6", "httpx", "python-socketio", "requests",
                       "websocket-client", "keyring", "platformdirs",
                       "pillow", "pillow-heif"):
            self.assertIn(needed, requirements)
        lines = [line.strip() for line in requirements.splitlines()
                 if line.strip()]
        self.assertEqual(len(lines), len(set(lines)), "duplicate entries")


if __name__ == "__main__":
    unittest.main()
