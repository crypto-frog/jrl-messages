"""Application constants and filesystem locations."""
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "JRL Messages"
APP_ID = "jrl-messages"
VERSION = "3.6.0"

DATA_DIR = Path(user_data_dir(APP_ID, appauthor=False))
CACHE_DIR = DATA_DIR / "cache"
ATTACH_DIR = CACHE_DIR / "attachments"
THUMB_DIR = CACHE_DIR / "thumbs"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "messages.db"
CONFIG_PATH = DATA_DIR / "config.json"

# Rendering
THUMB_MAX = 380          # px, longest side of inline image previews
PAGE_SIZE = 60           # messages loaded per window in the thread view
BACKFILL_PAGE = 100      # messages per request during history sync
INITIAL_RECENT_MESSAGES = 250  # make a fresh install useful immediately
GAP_OVERLAP_MS = 10 * 60 * 1000  # reconnect gap-fill overlap window
POLL_INTERVAL_S = 3              # reconciliation poll cadence, always
FAST_POLL_S = 3                  # poll cadence while live push is offline
DEEP_OVERLAP_MS = 24 * 60 * 60 * 1000  # wall-clock repair window
NOTIFY_MAX_AGE_MS = 30 * 60 * 1000     # never toast for messages older than this
EMPTY_BASELINE_STABLE_MS = 5000  # confirm a truly empty Mac before arming

# Reliability sync.  BlueBubbles exposes the Mac chat.db ROWID as
# ``originalROWID``.  Walking fixed numeric windows gives a stable, durable
# cursor even when iCloud inserts an old-dated message days later.
# Keep each fixed numeric interval within the server's conservative page
# size. Because ROWID is integral, (cursor, cursor + 100] contains at most
# 100 rows and therefore never needs mutable OFFSET pagination.
ROWID_WINDOW = 100
UPDATE_AUDIT_EVERY = 10          # audit recent edits/receipts every ~30 seconds
CHAT_REFRESH_EVERY = 20          # refresh names/participants every ~60 seconds
GLOBAL_HEAD_LIMIT = 250          # cursor-independent newest-message safety net
HEAD_AUDIT_INTERVAL_S = 60       # bounded safety net; forced checks bypass this
DEEP_AUDIT_INTERVAL_S = 5 * 60   # 24-hour wall-clock audit cadence
POLL_FAILURE_RECOVERY = 3        # rebuild network workers after this many failures
POLL_SUCCESS_STALE_S = 150       # allow a slow bounded multi-request cycle
# Recheck short ROWID responses once before committing a numeric window.  A
# transiently incomplete BlueBubbles response must not become a permanent
# cursor hole merely because the last row happened to be present.
ROWID_RECHECK_PASSES = 2
# Independent tail verification catches a newly inserted, old-dated iCloud
# row even if a prior ROWID response was transiently incomplete.  The newest
# 200 database rows are small enough to re-read continuously over Tailscale.
ROWID_TAIL_SPAN = 200
ROWID_TAIL_AUDIT_INTERVAL_S = 30
ROWID_ARCHIVE_AUDIT_INTERVAL_S = 60  # rolling permanent-hole repair
# A server rejected for ROWID support is periodically re-probed.  A temporary
# incomplete response during a Messages restart must not disable the strongest
# completeness path until the entire agent is restarted.
ROWID_REPROBE_EVERY = 20

# Background agent. The sync engine runs in its own always-on process; the
# window is a viewer over the shared database plus a local event channel.
AGENT_EXIT_DUPLICATE = 3         # another agent already holds the lock
AUTO_WAKE_CHOICES = (0, 10, 15, 30, 60, 120)   # minutes; 0 = off
AUTO_WAKE_DEFAULT_MIN = 30
BATCH_EVENT_CHUNK = 250          # message summaries per IPC event
NOTIFICATION_SWEEP_MS = 2500     # durable alert ledger, independent of IPC
REFRESH_WAKE_COOLDOWN_S = 180    # repeated F5 presses cannot thrash Messages


def window_pipe_name() -> str:
    """Single-instance activation channel for the window process: a second
    launch asks the first window to come forward instead of erroring."""
    import getpass
    try:
        user = getpass.getuser()
    except Exception:
        user = "default"
    return f"jrl-messages-window-{user}"


def agent_pipe_name() -> str:
    """Per-user local channel name. Windows additionally ACLs the named
    pipe to the current user via QLocalServer's UserAccessOption; the
    suffix keeps POSIX development machines collision-free too."""
    import getpass
    try:
        user = getpass.getuser()
    except Exception:
        user = "default"
    return f"jrl-messages-agent-{user}"


def ensure_dirs() -> None:
    for p in (DATA_DIR, CACHE_DIR, ATTACH_DIR, THUMB_DIR, LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)
