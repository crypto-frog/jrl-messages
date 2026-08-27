"""Background agent entry point.

Runs headless under pythonw from logon (see install.bat and
agent_supervisor.pyw) and keeps collecting, verifying, and storing
messages whether or not a window is open. One instance per user; a
second launch exits immediately with AGENT_EXIT_DUPLICATE so the
supervisor knows the service is already covered.

    python run_agent.py           run in the foreground (log to console too)
    python run_agent.py --stop    ask a running agent to exit, then return
"""
import logging
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLockFile, QTimer

from .. import config, constants
from ..logging_setup import setup_logging
from ..store.db import Database
from ..store.repo import Repo
from .core import AgentCore
from .server import AgentServer

log = logging.getLogger(__name__)


def _request_stop() -> int:
    """Ask a running agent to exit and wait until its endpoint is gone."""
    from PySide6.QtNetwork import QLocalSocket
    from . import serialize
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    sock = QLocalSocket()
    sock.connectToServer(constants.agent_pipe_name())
    if not sock.waitForConnected(1500):
        print("No agent is running.")
        return 0
    sock.write(serialize.encode({"cmd": "stop_agent"}))
    sock.waitForBytesWritten(1500)
    # Do not race an upgrade/new supervisor against the old QLockFile.  The
    # server disconnects only as the agent's shutdown path closes.
    if sock.state() == QLocalSocket.ConnectedState:
        sock.waitForDisconnected(35_000)
    if sock.state() == QLocalSocket.ConnectedState:
        print("Agent did not stop within the safe shutdown window.")
        del app
        return 1
    time.sleep(0.75)
    print("Agent stopped.")
    del app
    return 0


def _launch_installed_supervisor() -> None:
    """Best-effort launch used by install's version-verified handoff."""
    root = Path(__file__).resolve().parents[2]
    supervisor = root / "agent_supervisor.pyw"
    if not supervisor.exists():
        return
    flags = 0x08000000 if sys.platform.startswith("win") else 0
    kwargs = {"creationflags": flags} if flags else {"start_new_session": True}
    try:
        subprocess.Popen(
            [sys.executable, str(supervisor)], cwd=str(root),
            close_fds=True, **kwargs)
    except Exception as exc:
        print(f"Could not launch agent supervisor: {exc}")


def _wait_ready(timeout_s: float = 60.0) -> int:
    """Require this exact version, retiring old-folder agents as needed."""
    from PySide6.QtNetwork import QLocalSocket
    from . import serialize
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    next_launch = 0.0
    saw_mismatch = False
    while time.monotonic() < deadline:
        if time.monotonic() >= next_launch:
            _launch_installed_supervisor()
            next_launch = time.monotonic() + 5.0
        sock = QLocalSocket()
        sock.connectToServer(constants.agent_pipe_name())
        if sock.waitForConnected(1000):
            buf = bytearray()
            end = min(deadline, time.monotonic() + 2.0)
            while time.monotonic() < end:
                if sock.bytesAvailable() or sock.waitForReadyRead(500):
                    chunk = bytes(sock.readAll().data())
                    for payload in serialize.feed(buf, chunk):
                        if payload.get("event") == "hello":
                            got = payload.get("version") or "unknown"
                            if got == constants.VERSION:
                                sock.disconnectFromServer()
                                print(f"Agent {got} is ready.")
                                del app
                                return 0
                            saw_mismatch = True
                            print(f"Retiring agent version {got}; expected "
                                  f"{constants.VERSION}.")
                            sock.write(serialize.encode({"cmd": "stop_agent"}))
                            sock.waitForBytesWritten(1500)
                            if sock.state() == QLocalSocket.ConnectedState:
                                remaining_ms = max(
                                    1, min(35_000, int(
                                        (deadline - time.monotonic()) * 1000)))
                                sock.waitForDisconnected(remaining_ms)
                            next_launch = 0.0
                            break
                else:
                    break
        time.sleep(0.35)
    print("Agent did not become ready in time.")
    del app
    return 2 if saw_mismatch else 1


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    constants.ensure_dirs()
    if "--stop" in argv:
        return _request_stop()
    if "--wait-ready" in argv:
        return _wait_ready()

    setup_logging(filename="jrl-agent.log")
    app = QCoreApplication(argv)
    app.setApplicationName(f"{constants.APP_NAME} Agent")

    lock = QLockFile(str(constants.DATA_DIR / "agent.lock"))
    # This lock protects an indefinitely running service. Age alone must
    # never make a healthy agent "stale" and permit a second sender/sync core.
    # QLockFile can still identify a dead same-host PID after a real crash.
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        log.info("Another agent instance already holds the lock; exiting")
        return constants.AGENT_EXIT_DUPLICATE

    log.info("JRL Messages agent v%s starting", constants.VERSION)
    settings = config.load()
    db = Database(constants.DB_PATH)
    repo = Repo(db)

    core = AgentCore(repo, settings)
    try:
        server = AgentServer()
    except RuntimeError:
        # An endpoint that cannot be created almost always means another
        # healthy agent won a startup race after the lock's stale window.
        log.exception("Agent channel unavailable; exiting as duplicate")
        return constants.AGENT_EXIT_DUPLICATE

    core.broadcast.connect(server.broadcast)
    server.command.connect(
        lambda payload, sock: core.handle_command(
            payload, lambda answer: server.send(sock, answer)))
    server.client_connected.connect(
        lambda sock: server.send(sock, core.hello()))

    def shutdown():
        log.info("Agent shutting down")
        try:
            core.shutdown()
        finally:
            server.close()
    app.aboutToQuit.connect(shutdown)

    QTimer.singleShot(50, core.start_backend)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
