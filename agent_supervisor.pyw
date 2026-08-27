"""Keeps the background agent alive.

This tiny stdlib-only loop is what the Startup entry actually launches.
It starts run_agent.py, watches its durable heartbeat, and restarts it with
backoff after failure. A per-user supervisor lock keeps hourly failsafe
launches from accumulating. If another agent already owns the service, the
one supervisor stays resident and periodically probes for takeover.

Runs silently under pythonw; a short log is kept beside the app logs.
"""
import os
import getpass
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DUPLICATE_EXIT = 3
LOG_MAX = 512 * 1024
HEARTBEAT_STALE_S = 180
_MUTEX_HANDLE = None
_LOCK_FILE = None


def _data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        p = Path(base) / "jrl-messages" if base else Path.home() / \
            "AppData" / "Local" / "jrl-messages"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        p = (Path(base) if base else Path.home() / ".local" / "share") / \
            "jrl-messages"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _log_path() -> Path:
    p = _data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p / "agent-supervisor.log"


def _acquire_supervisor_lock() -> bool:
    """Exactly one resident supervisor per user, including hourly launches."""
    global _MUTEX_HANDLE, _LOCK_FILE
    if sys.platform.startswith("win"):
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        safe_user = "".join(
            ch if ch.isalnum() else "_" for ch in getpass.getuser())
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(
            None, False, f"Local\\JRLMessagesSupervisor_{safe_user}")
        if not handle:
            return False
        if ctypes.get_last_error() == 183:  # already exists
            kernel32.CloseHandle(handle)
            return False
        _MUTEX_HANDLE = handle
        return True
    try:
        import fcntl
        fh = (_data_dir() / "agent-supervisor.lock").open("a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FILE = fh
        return True
    except Exception:
        return False


def _heartbeat_age() -> float | None:
    db = _data_dir() / "messages.db"
    if not db.exists():
        return None
    try:
        with sqlite3.connect(
                f"file:{db.as_posix()}?mode=ro", uri=True,
                timeout=1.0) as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='agent_heartbeat_ms'") \
                .fetchone()
        if not row:
            return None
        return max(0.0, time.time() - int(row[0]) / 1000.0)
    except Exception:
        return None


def _log(text: str):
    try:
        path = _log_path()
        if path.exists() and path.stat().st_size > LOG_MAX:
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except Exception:
        pass


def _python() -> str:
    for name in ("pythonw.exe", "python.exe"):
        candidate = ROOT / ".venv" / "Scripts" / name
        if candidate.exists():
            return str(candidate)
    candidate = ROOT / ".venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def main() -> int:
    if not _acquire_supervisor_lock():
        return 0
    backoff = 2.0
    _log(f"supervisor started (pid {os.getpid()})")
    while True:
        started = time.monotonic()
        flags = 0
        if sys.platform.startswith("win"):
            flags = 0x08000000  # CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                [_python(), str(ROOT / "run_agent.py")],
                cwd=str(ROOT), creationflags=flags)
            while True:
                polled = proc.poll()
                if polled is not None:
                    rc = polled
                    break
                age = _heartbeat_age()
                if (age is not None and age > HEARTBEAT_STALE_S
                        and time.monotonic() - started > HEARTBEAT_STALE_S):
                    _log(f"agent heartbeat stale for {age:.0f}s; restarting")
                    proc.terminate()
                    try:
                        rc = proc.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        rc = proc.wait(timeout=5)
                    if rc == 0:
                        rc = -2  # stale termination is a recovery, not Stop
                    break
                # Keep clean Stop/upgrade handoffs tight so a replacement
                # supervisor never loses a mutex race to this exiting copy.
                time.sleep(0.5)
        except Exception as e:
            _log(f"could not start agent: {e!r}")
            rc = -1
        lived = time.monotonic() - started
        if rc == DUPLICATE_EXIT:
            # Stay resident and adopt the service if an orphaned/older agent
            # later exits. The single-instance mutex prevents hourly launches
            # from accumulating duplicate supervisors.
            _log("agent already running elsewhere; monitoring for takeover")
            time.sleep(15)
            continue
        if rc == 0:
            # A clean zero exit only happens when a stop was requested
            # (Stop-Agent.bat, Agent-Console.bat taking over, an upgrade).
            # Honor it; logon, the app window, or install.bat bring the
            # service back. Crashes exit nonzero and are restarted below.
            _log("agent stopped on request; supervisor exiting")
            return 0
        if lived >= 120:
            backoff = 2.0
        else:
            backoff = min(backoff * 1.7, 60.0)
        _log(f"agent exited rc={rc} after {lived:.0f}s; "
             f"restarting in {backoff:.0f}s")
        time.sleep(backoff)


if __name__ == "__main__":
    raise SystemExit(main())
