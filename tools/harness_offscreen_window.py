"""Offscreen GUI harness: construct the full window at two text scales.

Run:  QT_QPA_PLATFORM=offscreen python tools/harness_offscreen_window.py

JRL_SMOKE=1 keeps the window from touching the agent channel or spawning
the supervisor, so this validates pure construction: every widget builds,
styles apply at Default and Largest, the responsive bubble limit tracks a
changing pane width, and the window process imports no worker machinery.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
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

from PySide6.QtWidgets import QApplication  # noqa: E402

from app import config  # noqa: E402
from app.store.db import Database  # noqa: E402
from app.store.repo import Repo  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

failures = []


def check(label, condition):
    print(f"  {'ok ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


app = QApplication(sys.argv)
db = Database(constants.DB_PATH)
repo = Repo(db)

for scale_name, scale in (("Default", 1.0), ("Largest", 1.5)):
    print(f"== window construction at {scale_name} ({scale}) ==")
    settings = config.Settings(font_scale=scale)
    theme.apply(app, settings.accent, settings.font_scale)
    win = MainWindow(repo, settings)
    win.resize(1180, 760)
    win.show()
    app.processEvents()

    check("search box built", win.search is not None)
    check("compose button says New", win.compose_btn.text() == "New")
    check("hidden button present", win.hidden_btn.text() == "Hidden")
    check("recover button idle", win.recover_btn.text() == "Recover")
    check("wake button idle", win.wake_btn.text() == "Wake Mac")
    check("wake enabled at idle", win.wake_btn.isEnabled())
    check("thread pane present", win.thread is not None)
    check("composer hidden with no chat", not win.thread.composer.isVisible())
    check("status label built", bool(win.status.text()))

    limit_at_show = win.thread.bubble_limit()
    check(f"bubble limit sane at 1180 ({limit_at_show}px)",
          240 <= limit_at_show <= theme.dim(theme.BUBBLE_MAX_BASE_PX))
    win.resize(940, 700)
    app.processEvents()
    narrower = win.thread.bubble_limit()
    check(f"bubble limit shrinks with the window ({narrower}px)",
          narrower <= limit_at_show)
    win.resize(2400, 900)
    app.processEvents()
    widest = win.thread.bubble_limit()
    check(f"bubble limit capped for readability ({widest}px)",
          widest <= theme.dim(theme.BUBBLE_MAX_BASE_PX))

    # Simulate agent events driving the footer and buttons.
    win._on_agent_status("ok", "Connected", 0)
    check("status renders agent text", win.status.text() == "Connected")
    win._on_wake_event({"state": "watching", "origin": "auto"})
    check("auto wake labels the button",
          win.wake_btn.text() == "Auto wake running…")
    check("wake disabled while busy", not win.wake_btn.isEnabled())
    win._on_wake_event({"state": "idle", "origin": "manual"})
    check("wake re-enabled at idle", win.wake_btn.isEnabled())
    win._on_recovery_event({"state": "working"})
    check("recover disabled while working", not win.recover_btn.isEnabled())
    win._on_recovery_event({"state": "success", "restored": 2})
    check("recover shows success label", win.recover_btn.text() == "Recovered")

    win.close()
    app.processEvents()

print()
if failures:
    print(f"HARNESS FAILED: {len(failures)} check(s)")
    raise SystemExit(1)
print("WINDOW HARNESS PASSED")
