"""All timestamps are normalized to unix milliseconds at the parse boundary.
The BlueBubbles server returns unix ms; raw Apple Core Data nanoseconds
(epoch 2001-01-01) are detected and converted defensively."""
from datetime import datetime, timedelta
from typing import Optional

APPLE_EPOCH_OFFSET_MS = 978_307_200_000  # 2001-01-01 in unix ms


def normalize_ts(v) -> Optional[int]:
    if v in (None, 0, "", "0"):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n > 5_000_000_000_000_000:      # Apple nanoseconds since 2001
        return n // 1_000_000 + APPLE_EPOCH_OFFSET_MS
    if n < 100_000_000_000:            # unix seconds
        return n * 1000
    return n                            # unix ms


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000)


def fmt_clock(ms: int) -> str:
    d = _dt(ms)
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def fmt_day(ms: int) -> str:
    d = _dt(ms).date()
    today = datetime.now().date()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    if d.year == today.year:
        return _dt(ms).strftime("%a, %b ") + str(d.day)
    return _dt(ms).strftime("%b ") + f"{d.day}, {d.year}"


def fmt_list_time(ms: Optional[int]) -> str:
    if not ms:
        return ""
    d = _dt(ms)
    today = datetime.now().date()
    if d.date() == today:
        return fmt_clock(ms)
    if d.date() == today - timedelta(days=1):
        return "Yesterday"
    if (today - d.date()).days < 7:
        return d.strftime("%a")
    if d.year == today.year:
        return d.strftime("%b ") + str(d.day)
    return f"{d.month}/{d.day}/{d.year % 100:02d}"


def fmt_ago(ms: int, now_ms: Optional[int] = None) -> str:
    """Compact relative stamp for the notification center: 'now', minutes
    and hours for today, then calendar forms. Pure given now_ms."""
    import time as _time
    now = int(now_ms if now_ms is not None else _time.time() * 1000)
    delta_s = max(0, (now - int(ms)) // 1000)
    if delta_s < 60:
        return "now"
    if delta_s < 3600:
        return f"{delta_s // 60}m ago"
    then, today = _dt(ms).date(), _dt(now).date()
    if then == today:
        return f"{delta_s // 3600}h ago"
    if then == today - timedelta(days=1):
        return "Yesterday " + fmt_clock(ms)
    return fmt_list_time(ms)


def same_day(a: int, b: int) -> bool:
    return _dt(a).date() == _dt(b).date()


def fmt_receipt(ms: int) -> str:
    """Read-receipt style stamp: clock for today, day plus clock otherwise."""
    d = _dt(ms).date()
    today = datetime.now().date()
    if d == today:
        return fmt_clock(ms)
    if d == today - timedelta(days=1):
        return "Yesterday " + fmt_clock(ms)
    return f"{fmt_day(ms)} {fmt_clock(ms)}"
