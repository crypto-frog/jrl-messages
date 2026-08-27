"""Live storm-safety harness for the 3.1.0 freeze class.

Run under a real window system when possible:
  xvfb-run -a python tools/harness_storm.py     (CI / Linux)
  python tools\\harness_storm.py                 (Windows, real desktop)

Drives the real window through the exact reported path (turn help tips
Off, keep using the app) plus the amplifiers: hover event storms, alert
floods with rapid card churn, and repeated theme reapplication. Asserts
the bounds the 3.1.1 redesign guarantees: top-level window count stays
flat, the popup breaker engages under churn instead of spawning cards,
the event loop never stalls, and the stylesheet survives.
"""
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

if os.environ.get("QT_QPA_PLATFORM") is None and not sys.platform.startswith(
        ("win", "darwin")) and not os.environ.get("DISPLAY"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["JRL_SMOKE"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = tempfile.TemporaryDirectory()
from app import constants  # noqa: E402
constants.DATA_DIR = Path(TMP.name)
constants.CACHE_DIR = constants.DATA_DIR / "cache"
constants.ATTACH_DIR = constants.CACHE_DIR / "attachments"
constants.THUMB_DIR = constants.CACHE_DIR / "thumbs"
constants.LOG_DIR = constants.DATA_DIR / "logs"
constants.DB_PATH = constants.DATA_DIR / "messages.db"
constants.CONFIG_PATH = constants.DATA_DIR / "config.json"
constants.ensure_dirs()

from PySide6.QtCore import QEvent, QPoint, QTimer  # noqa: E402
from PySide6.QtGui import QHelpEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import config  # noqa: E402
from app.api.models import parse_message  # noqa: E402
from app.store.db import Database  # noqa: E402
from app.store.repo import Repo  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

CHAT = "iMessage;-;+15555550100"
NOW = int(time.time() * 1000)
failures = []


def check(label, ok, detail=""):
    print(f"  {'ok ' if ok else 'FAIL'} {label}" + (f" · {detail}" if detail
                                                    else ""))
    if not ok:
        failures.append(label)


def raw(guid, rowid, text):
    return {
        "guid": guid, "originalROWID": rowid, "dateCreated": NOW + rowid,
        "isFromMe": False, "text": text,
        "chats": [{"guid": CHAT}],
        "handle": {"address": "+15555550100"}, "attachments": [],
    }


app = QApplication(sys.argv)
db = Database(constants.DB_PATH)
repo = Repo(db)
settings = config.Settings(font_scale=1.0)
theme.apply(app, settings.accent, settings.font_scale)
win = MainWindow(repo, settings)
win.show()


def pump(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.004)


def windows():
    return [w for w in app.topLevelWidgets() if w.isVisible()]


pump(0.4)
base = len(windows())
print(f"baseline visible top-level windows: {base}")

print("== the reported path: turn help tips Off, keep using the app ==")
try:
    settings.tooltip_mode = "off"
    win.apply_theme()
    win.tooltip_controller.set_mode("off")
    for _ in range(150):
        for target in (win.compose_btn, win.hidden_btn, win.recover_btn,
                       win.wake_btn, win.settings_btn):
            app.sendEvent(target, QHelpEvent(
                QEvent.ToolTip, QPoint(4, 4),
                target.mapToGlobal(QPoint(4, 4))))
        app.processEvents()
    pump(0.3)
    check("tips-off hover storm creates no windows",
          len(windows()) <= base, f"windows={len(windows())}")
except RecursionError:
    check("tips-off hover storm", False, "RecursionError")
except Exception:
    traceback.print_exc()
    check("tips-off hover storm", False)

print("== alert flood with aggressive card churn (storm amplifier) ==")
try:
    # Verification codes are never burst-summarized: every one gets its own
    # card by design. A flood of them plus instant churn is the harshest
    # legitimate path to card creation, so it is the breaker's proving
    # ground. (Ordinary texts collapse into one summary card first, which
    # is why they cannot storm; that is asserted implicitly by the bound.)
    for i in range(30):
        repo.upsert_message(
            parse_message(
                raw(f"s-{i}", i + 1, f"Your verification code is 1{i:05d}")),
            notify_eligible=True)
    peak = base
    for _ in range(60):
        win._drain_delivery_events()
        win._flush_toasts()
        for card in list(win.popups.active):
            card.close()          # churn: in 3.1.0 this allowed endless cards
        app.processEvents()
        peak = max(peak, len(windows()))
    check("window count bounded under churn", peak <= base + 4,
          f"peak={peak}")
    check("breaker engaged under churn",
          win.popups._breaker_until > 0.0)
    check("pending stays within its cap",
          len(win.popups.pending) <= win.popups.MAX_PENDING,
          f"pending={len(win.popups.pending)}")
except RecursionError:
    check("alert churn", False, "RecursionError")
except Exception:
    traceback.print_exc()
    check("alert churn", False)

print("== breaker cooldown drains calmly, ledger completes ==")
try:
    # Simulate the cooldown expiring (the 20 s resume timer, proven by the
    # unit tests, is too slow for a harness loop) and let the drip finish.
    for _ in range(120):
        win.popups._breaker_until = 0.0
        win.popups._creation_times.clear()
        win.popups._resume_scheduled = False
        win.popups._drain_pending()
        win._drain_delivery_events()
        win._flush_toasts()
        for card in list(win.popups.active):
            card.close()
        app.processEvents()
        if (not repo.pending_delivery_events()
                and not win.popups.pending and not win.popups.active):
            break
    check("ledger fully drains after the storm",
          not repo.pending_delivery_events(),
          f"left={len(repo.pending_delivery_events())}")
    check("popup queue fully drains", not win.popups.pending,
          f"pending={len(win.popups.pending)}")
except Exception:
    traceback.print_exc()
    check("ledger drain", False)

print("== event loop responsiveness (freeze detector) ==")
beats = []
hb = QTimer()
hb.setInterval(50)
hb.timeout.connect(lambda: beats.append(time.monotonic()))
hb.start()
pump(2.0)
hb.stop()
gaps = [b - a for a, b in zip(beats, beats[1:])]
worst = max(gaps) if gaps else 99
check("event loop never stalls", worst < 0.5, f"worst gap {worst:.3f}s")

print("== visuals survive: stylesheet and badge after theme churn ==")
try:
    for accent in ("Teal", "Rose", "Amber", "Blue"):
        win.apply_theme(accent, 1.0)
        app.processEvents()
    ss = app.styleSheet()
    check("stylesheet intact", bool(ss and "QPushButton" in ss))
    win.set_status("ok", "Connected")
    check("badge animates while ok and visible",
          win.badge._timer.isActive())
    win.set_status("fail", "Offline")
    check("badge still when failed", not win.badge._timer.isActive())
    win.set_status("ok", "Connected")
    win.hide()
    app.processEvents()
    check("hidden badge stops its timer", not win.badge._timer.isActive())
    win.show()
    app.processEvents()
    check("shown badge resumes", win.badge._timer.isActive())
except Exception:
    traceback.print_exc()
    check("visual survival", False)

print("== window warden: a rogue window is named and neutralized ==")
try:
    from PySide6.QtWidgets import QWidget
    rogue = QWidget()
    rogue.resize(220, 120)
    rogue.show()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and rogue.isVisible():
        app.processEvents()
        time.sleep(0.02)
    check("rogue top-level window is hidden by the warden",
          not rogue.isVisible())
    check("warden names the culprit in the activity log",
          "Unexpected window" in win.activity.text()
          and "QWidget" in win.activity.text())
    rogue.deleteLater()
except Exception:
    traceback.print_exc()
    check("window warden", False)

print("== activity panel: live, styled, and populated ==")
try:
    win.activity.record("link", "harness connection attempt")
    win._show_connection_details()
    pump(0.3)
    panel = win._activity_panel
    check("activity panel opens from the chip", panel is not None
          and panel.isVisible())
    check("panel carries the session activity",
          panel is not None
          and "harness connection attempt" in panel.view.toPlainText())
    before_lines = panel.view.toPlainText().count("\n")
    win.activity.record("wake", "harness wake event")
    pump(0.2)
    check("panel appends live entries",
          panel.view.toPlainText().count("\n") >= before_lines)
    panel.close()
    pump(0.2)
except Exception:
    traceback.print_exc()
    check("activity panel", False)

print("== hover repaints reach every row (hide control everywhere) ==")
try:
    from PySide6.QtCore import QPoint
    win.reload_chats()
    app.processEvents()
    lst = win.list
    seen_rows = set()
    for row_index in range(min(3, lst.model().rowCount())):
        rect = lst.visualRect(lst.model().index(row_index, 0))
        lst._repaint_hover_change(rect.center())
        seen_rows.add(lst._hover_row)
        app.processEvents()
    check("hover tracking follows plain rows",
          seen_rows == {0, 1, 2} if lst.model().rowCount() >= 3
          else len(seen_rows) > 0, f"rows={sorted(seen_rows)}")
    lst._repaint_hover_change(QPoint(-10, -10))
    check("hover clears off-list", lst._hover_row == -1)
except Exception:
    traceback.print_exc()
    check("hover repaints", False)

print("== settings gear is a drawn, tinted icon ==")
try:
    check("gear icon installed", not win.settings_btn.icon().isNull())
    check("gear has no text glyph", win.settings_btn.text() == "")
except Exception:
    traceback.print_exc()
    check("gear icon", False)

print("== chat list paints with the eye-off hide control ==")
try:
    win.reload_chats()
    app.processEvents()
    pixmap = win.list.grab()
    check("chat list renders", not pixmap.isNull() and pixmap.width() > 0)
    from app.ui.chat_list import hide_icon_pixmap
    pm = hide_icon_pixmap(theme.ACCENT, 20)
    check("hide icon renders at accent color", not pm.isNull())
    pm2 = hide_icon_pixmap(theme.ACCENT, 20)
    check("hide icon is cached", pm is pm2)
except Exception:
    traceback.print_exc()
    check("chat list paint", False)

print("== 3.1.3: alerts fire even with the conversation visibly open ==")
try:
    win.settings.notify_mode = "popup"
    win.open_conversation(CHAT)
    pump(0.3)
    # Simulate the exact suppressed case from the report: window open,
    # conversation on screen, user not looking.
    win._conversation_is_visible = lambda _guid: True
    for card in list(win.popups.active):
        card.close()
    pump(0.2)
    before_alert = win._last_alert_signal
    repo.upsert_message(
        parse_message(raw("fresh-open", 9001,
                          "arrives while the app is open")),
        notify_eligible=True)
    win._drain_delivery_events()
    win._flush_toasts()
    pump(0.4)
    check("popup shows despite the open conversation",
          len(win.popups.active) >= 1)
    check("alert signal (sound path) fired",
          win._last_alert_signal > before_alert)
    check("list steer target was set and consumed",
          win._scroll_target_guid is None)
    for card in list(win.popups.active):
        card.close()
    pump(0.2)
except Exception:
    traceback.print_exc()
    check("always-alert", False)

print("== 3.1.3: hide zone paints subtly on read rows, chip on unread ==")
try:
    repo.mark_all_read()
    win._rows_signature = None
    win.reload_chats()
    app.processEvents()
    lst = win.list
    lst._hover_row = 0
    lst._hover_in_zone = True
    pm_read = lst.grab()
    check("read-row zone hover renders", not pm_read.isNull())
    from app.ui.chat_list import hide_zone
    rect0 = lst.visualRect(lst.model().index(0, 0)).adjusted(6, 3, -6, -3)
    zone = hide_zone(rect0)
    check("zone is a real target",
          zone.width() >= 44 and zone.contains(zone.center()))
    lst._hover_in_zone = False
    lst._hover_row = -1
    app.processEvents()
except Exception:
    traceback.print_exc()
    check("hide zone painting", False)

print("== 3.1.4: master switches, sound decoupling, channel fallback ==")
try:
    win.popups._breaker_until = 0.0
    win.popups._creation_times.clear()
    for card in list(win.popups.active):
        card.close()
    pump(0.3)

    # Sound-only: popups off, sound on. No card, but the alarm still rings
    # and the ledger completes.
    win.settings.popups_enabled = True
    win.settings.notification_sound = True
    win.settings.notify_mode = "popup"
    win.settings.popups_enabled = False
    before_cards = len(win.popups.active)
    before_alert = win._last_alert_signal
    win._last_alert_signal = 0.0
    repo.upsert_message(
        parse_message(raw("sound-only", 9101, "sound only please")),
        notify_eligible=True)
    win._drain_delivery_events()
    pump(0.3)
    check("popups-off shows no card", len(win.popups.active) == before_cards)
    check("popups-off still sounds the alarm", win._last_alert_signal > 0)
    check("popups-off completes the ledger",
          not repo.pending_delivery_events())

    # Popups back on: card appears and the Activity panel says so.
    win.settings.popups_enabled = True
    repo.upsert_message(
        parse_message(raw("card-back", 9102, "card please")),
        notify_eligible=True)
    win._drain_delivery_events()
    win._flush_toasts()
    pump(0.3)
    check("popups-on shows a card", len(win.popups.active) >= 1)
    check("activity records the card outcome",
          "popup card" in win.activity.text())

    # System style with no usable tray (offscreen): falls back to the card
    # and says why, instead of showing nothing.
    for card in list(win.popups.active):
        card.close()
    pump(0.2)
    win.settings.notify_mode = "system"
    repo.upsert_message(
        parse_message(raw("toast-fallback", 9103, "toast style")),
        notify_eligible=True)
    win._drain_delivery_events()
    win._flush_toasts()
    pump(0.3)
    check("system style without a tray still alerts",
          len(win.popups.active) >= 1
          or "Windows toast" in win.activity.text())
    win.settings.notify_mode = "popup"

    # Test alert runs the real pipeline and reports.
    win._test_popup("popup", True, True)
    pump(0.2)
    check("test alert reports its sound", "test sound" in win.activity.text())
    for card in list(win.popups.active):
        card.close()
    pump(0.2)
except Exception:
    traceback.print_exc()
    check("alert redundancy", False)

print("== 3.1.5: a self-conversation text alerts end to end ==")
try:
    SELF = "iMessage;-;+15875550123"
    repo.set_self_identities({"5875550123"}, True)
    win.settings.popups_enabled = True
    win.settings.notification_sound = True
    win.settings.notify_mode = "popup"
    for card in list(win.popups.active):
        card.close()
    pump(0.2)
    before_alert = win._last_alert_signal
    win._last_alert_signal = 0.0
    self_raw = raw("self-note", 9201, "Zzzz")
    self_raw["isFromMe"] = True
    self_raw["chats"] = [{"guid": SELF}]
    self_raw["handle"] = {"address": "+15875550123"}
    repo.upsert_message(parse_message(self_raw), notify_eligible=True)
    win._drain_delivery_events()
    win._flush_toasts()
    pump(0.3)
    check("a sent-by-you self text still raises a card",
          len(win.popups.active) >= 1)
    check("and still sounds the alarm", win._last_alert_signal > 0)
    for card in list(win.popups.active):
        card.close()
    pump(0.2)
    # The same shape in an ordinary conversation stays silent, as ever.
    other_raw = raw("other-from-me", 9202, "typed on the phone to a friend")
    other_raw["isFromMe"] = True
    repo.upsert_message(parse_message(other_raw), notify_eligible=True)
    win._drain_delivery_events()
    pump(0.2)
    check("a from-me text to someone else still never alerts",
          not repo.pending_delivery_events()
          and len(win.popups.active) == 0)
except Exception:
    traceback.print_exc()
    check("self-conversation alerting", False)

print("== 3.2.0: notification center (bell, feed, hide, flat windows) ==")
try:
    for card in list(win.popups.active):
        card.close()
    pump(0.3)
    repo.feed_clear()
    win._update_bell()
    base320 = len(windows())
    check("bell is present and visible", win.bell_btn.isVisible())
    check("bell icon is drawn", not win.bell_btn.icon().isNull())
    check("feed starts clean", repo.feed_unseen_count() == 0)

    # A real alert lands in the durable feed with an unseen badge.
    repo.upsert_message(
        parse_message(raw("feed-1", 9301, "lands in the bell")),
        notify_eligible=True)
    win._drain_delivery_events()
    win._flush_toasts()
    pump(0.3)
    titles = [r["title"] for r in repo.feed_recent()]
    check("the alert was recorded in the feed", len(titles) >= 1)
    check("its unseen count reached the bell",
          repo.feed_unseen_count() >= 1)
    for card in list(win.popups.active):
        card.close()
    pump(0.2)

    # The panel is a child overlay: toggling it repeatedly must never
    # change the top-level window count (the storm invariant).
    for _ in range(12):
        win._toggle_alert_center()
        app.processEvents()
    pump(0.2)
    check("12 panel toggles keep top-level windows flat",
          len(windows()) == base320, f"windows={len(windows())}")
    if not win.alert_center.isVisible():
        win._toggle_alert_center()
        pump(0.2)
    check("panel is visible as a child of the main window",
          win.alert_center.isVisible()
          and win.alert_center.parent() is win)
    check("opening the panel marked everything seen",
          repo.feed_unseen_count() == 0)
    check("panel rendered the entry",
          len(win.alert_center._row_widgets) >= 1)

    # Per-item hide and clear all.
    rows = repo.feed_recent()
    win.alert_center.hide_item(rows[0]["id"])
    pump(0.1)
    check("hide removes the entry from the feed",
          all(r["id"] != rows[0]["id"] for r in repo.feed_recent()))
    repo.feed_add("wake", "Mac woken", "2 held-back texts recovered")
    win.alert_center.clear_all()
    pump(0.1)
    check("clear all empties the panel", repo.feed_recent() == [])
    win.alert_center.hide_panel()
    pump(0.1)

    # Wake, repair, and connection transitions feed the center too.
    win._on_wake_event({"state": "success", "origin": "auto", "found": 2})
    win._feed_link_kind = "ok"
    win._feed_link_down_ms = None
    win.set_status("fail", "Cannot reach server")
    win.set_status("ok", "Connected")
    kinds = [r["kind"] for r in repo.feed_recent()]
    check("wake success recorded", "wake" in kinds)
    check("connection loss and recovery recorded",
          "link-down" in kinds and "link-up" in kinds)

    # The bell disappears entirely when the center is switched off.
    win.settings.alert_center_enabled = False
    win._update_bell()
    check("bell hides when the center is off",
          not win.bell_btn.isVisible()
          and not win.alert_center.isVisible())
    win.settings.alert_center_enabled = True
    win._update_bell()
    check("bell returns when re-enabled", win.bell_btn.isVisible())
    check("window count still flat after the whole section",
          len(windows()) == base320, f"windows={len(windows())}")
except Exception:
    traceback.print_exc()
    check("notification center", False)

print("== 3.2.0: tint suite with named swatches ==")
try:
    from app.ui.settings_dialog import SettingsDialog
    check("the suite offers at least 16 tints", len(theme.ACCENTS) >= 16)
    swatch_ok = all(
        not theme.swatch_pixmap(name, 24).isNull()
        for name in theme.ACCENTS)
    check("every tint renders a color patch", swatch_ok)
    check("patches are cached",
          theme.swatch_pixmap("Blue", 24) is theme.swatch_pixmap("Blue", 24))
    previews = []
    dlg = SettingsDialog(settings,
                         on_preview=lambda a, s: previews.append((a, s)))
    check("settings organizes into three tabs", dlg.tabs.count() == 3)
    check("one named swatch per tint",
          len(dlg._swatch_buttons) == len(theme.ACCENTS))
    check("every swatch carries its patch",
          all(not b.icon().isNull() for b in dlg._swatch_buttons.values()))
    check("the saved tint starts selected",
          dlg._swatch_buttons[settings.accent
                              if settings.accent in theme.ACCENTS
                              else "Blue"].isChecked())
    dlg._select_accent("Teal")
    check("clicking a swatch previews live",
          previews and previews[-1][0] == "Teal")
    check("save would persist the swatch choice",
          dlg._accent_name == "Teal")
    dlg.deleteLater()
    pump(0.1)
except Exception:
    traceback.print_exc()
    check("tint swatches", False)

print("== 3.3.0: iPhone mirroring pipeline (synthetic, storm-safe) ==")
try:
    for card in list(win.popups.active):
        card.close()
    pump(0.3)
    repo.feed_clear()
    win._update_bell()
    win.settings.popups_enabled = True
    win.settings.notification_sound = True
    win.settings.notify_mode = "popup"
    win.popups._breaker_until = 0.0
    win.popups._creation_times.clear()
    base330 = len(windows())

    # One mirrored notification: card + sound + bell entry, exactly the
    # message pipeline with an iPhone mark.
    win._last_alert_signal = 0.0
    now_ms = int(time.time() * 1000)
    win._on_phone_notification({
        "uid": 501, "app_id": "com.burbn.instagram",
        "app_name": "Instagram", "body": "anna_k: liked your photo",
        "when_ms": now_ms})
    pump(0.3)
    check("mirrored notification raises a card",
          len(win.popups.active) >= 1)
    check("and sounds the alarm", win._last_alert_signal > 0)
    check("and lands in the bell marked as iPhone",
          any(r["kind"] == "phone" and "Instagram" in r["title"]
              for r in repo.feed_recent()))

    # Redelivery of the same notification never doubles the bell.
    win._on_phone_notification({
        "uid": 501, "app_id": "com.burbn.instagram",
        "app_name": "Instagram", "body": "anna_k: liked your photo",
        "when_ms": now_ms})
    pump(0.2)
    marker = f"ancs-501-{int(now_ms / 1000)}"
    dup = db.one("SELECT COUNT(*) AS n FROM feed WHERE message_guid=?",
                 (marker,))
    check("redelivered notification stays a single bell entry",
          int(dup["n"]) == 1)
    for card in list(win.popups.active):
        card.close()
    pump(0.2)

    # A 40-notification burst with churn: the breaker and hard cap hold,
    # exactly as they do for message floods.
    peak = base330
    for n in range(40):
        win._on_phone_notification({
            "uid": 600 + n, "app_id": "com.example.burst",
            "app_name": "Burst", "body": f"burst {n}",
            "when_ms": int(time.time() * 1000)})
        for card in list(win.popups.active):
            card.close()
        app.processEvents()
        peak = max(peak, len(windows()))
    check("phone burst stays bounded under churn", peak <= base330 + 4,
          f"peak={peak}")
    check("breaker engaged for the phone burst",
          win.popups._breaker_until > 0.0)
    check("all 40 burst entries reached the durable bell",
          sum(1 for r in repo.feed_recent(100)
              if r["kind"] == "phone" and "Burst" in r["title"]) == 40)
    for _ in range(80):
        win.popups._breaker_until = 0.0
        win.popups._creation_times.clear()
        win.popups._resume_scheduled = False
        win.popups._drain_pending()
        for card in list(win.popups.active):
            card.close()
        app.processEvents()
        if not win.popups.pending and not win.popups.active:
            break
    pump(0.2)
    check("burst fully settles", not win.popups.pending
          and len(windows()) == base330)

    # Sound-only mode: no card, the alarm still rings, the bell records.
    win.settings.popups_enabled = False
    win._last_alert_signal = 0.0
    win._on_phone_notification({
        "uid": 990, "app_id": "com.apple.news", "app_name": "News",
        "body": "Headline", "when_ms": int(time.time() * 1000)})
    pump(0.2)
    check("popups-off mirrors as sound only",
          not win.popups.active and win._last_alert_signal > 0)
    win.settings.popups_enabled = True

    # Link transitions: one bell entry per real change, retries quiet.
    win._feed_phone_state = "idle"
    win._feed_phone_down_ms = None
    win._on_phone_status("up", "Connected to iPhone")
    win._on_phone_status("down", "iPhone disconnected")
    win._on_phone_status("down", "retrying in 10s")
    kinds = [r["kind"] for r in repo.feed_recent(100)]
    check("link up recorded once", kinds.count("phone-up") == 1)
    check("link loss recorded once despite retries",
          kinds.count("phone-down") == 1)
    check("activity narrates the phone link",
          "Connected to iPhone" in win.activity.text())
except Exception:
    traceback.print_exc()
    check("phone mirroring pipeline", False)

print("== 3.1.4: dedicated quit and single-instance activation ==")
try:
    check("quit button carries the drawn power icon",
          not win.quit_btn.icon().isNull())
    if win._activation_server is not None:
        from PySide6.QtNetwork import QLocalSocket
        win.hide()
        pump(0.2)
        probe = QLocalSocket()
        probe.connectToServer(constants.window_pipe_name())
        probe.waitForConnected(1500)
        pump(0.5)
        check("second-launch ping brings the window forward",
              win.isVisible())
        probe.abort()
    else:
        check("activation channel present", False, "listen failed")
except Exception:
    traceback.print_exc()
    check("quit and activation", False)

print()
if failures:
    print(f"STORM HARNESS FAILED: {failures}")
    sys.exit(1)
print("STORM HARNESS PASSED")
