"""Automatic Wake Mac policy.

Messages on the Mac goes quiet after long idle and Apple then holds
incoming texts back entirely (see SETUP.md, "Wake Mac"). The manual button
cures it; this policy runs the same cure on a schedule so held-back texts
arrive without anyone pressing anything.

The decision is a pure function so every gate is unit-testable. All times
are monotonic seconds from the caller.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class AutoWakeInputs:
    now: float                    # monotonic seconds
    interval_minutes: int         # 0 disables the policy
    connected: bool               # a backend exists (URL and password set)
    poll_healthy: bool            # the reconciler succeeded recently
    outbox_active: int            # queued or on-the-wire sends, any chat
    busy: bool                    # a wake or recovery is already in flight
    last_incoming_ts: float       # newest incoming message seen (monotonic)
    last_wake_ts: float           # last wake of any origin (monotonic)


def should_auto_wake(i: AutoWakeInputs) -> bool:
    """True when it is both useful and safe to restart Messages on the Mac.

    Useful: nothing has arrived for the configured quiet interval, and no
    wake happened within that same interval (so silence yields at most one
    restart per interval, and an active conversation never triggers one).

    Safe: never while one of the user's own messages is queued or on the
    wire, because quitting Messages mid-send could interrupt a delivery to
    a client; never while another wake or a recovery is already running;
    and never when the Mac is unreachable, where the call would only fail.
    """
    if i.interval_minutes <= 0:
        return False
    if not i.connected or not i.poll_healthy:
        return False
    if i.busy or i.outbox_active > 0:
        return False
    quiet_s = i.interval_minutes * 60
    if i.now - i.last_incoming_ts < quiet_s:
        return False
    if i.now - i.last_wake_ts < quiet_s:
        return False
    return True
