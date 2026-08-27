"""Window-side client for the background agent.

Owns one QLocalSocket to the agent's per-user channel, turns its JSON
events into Qt signals for the main window, and keeps the service
healthy: if the channel is down it reconnects on a timer and, at most
once every 30 seconds, launches the supervisor so a stopped agent comes
back without the user doing anything. Commands sent while offline are
not lost where durability matters: sends live in the outbox table and
are kicked on reconnect; small idempotent commands are buffered.
"""
import logging
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalSocket

from .. import constants
from ..agent import serialize

log = logging.getLogger(__name__)

_BUFFERABLE = {"settings_changed", "poke", "recover", "wake"}


class AgentLink(QObject):
    hello = Signal(dict)
    status = Signal(str, str, object)      # kind, text, newest_ts
    batch = Signal(list, str)              # slim items, source
    chats_refreshed = Signal()
    chat_refreshed = Signal(str, object, bool)  # guid, newest, wake_watching
    backfill_page = Signal(str)
    backfill_done = Signal()
    poll_ok = Signal()
    poll_failed = Signal(str)
    socket_state = Signal(bool, str)
    caps_changed = Signal(dict)
    wake_event = Signal(dict)
    recovery_event = Signal(dict)
    outbox_changed = Signal(str)
    message_sent = Signal(object)
    attachment_ready = Signal(str, str)
    attachment_failed = Signal(str, str)
    push_broken = Signal(str)
    settings_applied = Signal(dict)
    link_state = Signal(str)               # connecting | connected | offline

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sock = None
        self._buffer = bytearray()
        self._pending: list = []
        self._state = "offline"
        self._hello_validated = False
        self._last_spawn = 0.0
        self._connect_paused_until = 0.0
        self._upgrade_active = False
        self._upgrade_deadline = 0.0
        # The whole upgrade dance is bounded: a wall-clock deadline and a
        # forced-spawn budget. When either runs out the link falls back to
        # the calm 30-second cadence instead of spawning and reconnecting
        # forever, and tells the user what to run.
        self._upgrade_overall_deadline = 0.0
        self._upgrade_forced_spawns = 0
        self._retry = QTimer(self)
        self._retry.setInterval(3000)
        self._retry.timeout.connect(self._ensure)
        self._hello_watchdog = QTimer(self)
        self._hello_watchdog.setSingleShot(True)
        self._hello_watchdog.setInterval(5000)
        self._hello_watchdog.timeout.connect(self._on_hello_timeout)

    # ------------------------------------------------ lifecycle

    def start(self):
        self._retry.start()
        self._ensure()

    def stop(self):
        self._retry.stop()
        self._hello_watchdog.stop()
        if self._sock is not None:
            try:
                self._sock.disconnectFromServer()
            except Exception:
                pass

    def is_connected(self) -> bool:
        return (self._sock is not None
                and self._sock.state() == QLocalSocket.ConnectedState)

    def _set_state(self, state: str):
        if state != self._state:
            self._state = state
            self.link_state.emit(state)

    def _ensure(self):
        if time.monotonic() < self._connect_paused_until:
            return
        if self.is_connected():
            return
        if (self._sock is not None
                and self._sock.state() == QLocalSocket.ConnectingState):
            return
        self._connect_now()

    def restart_for_upgrade(self):
        """Retire an older agent, waiting for its endpoint before relaunch.

        A fixed delay is unsafe because a clean agent shutdown can spend up
        to 25 seconds joining bounded REST workers. We wait for the actual
        disconnect, give the old supervisor time to release its mutex, then
        retry the new supervisor until a matching-version hello arrives.
        """
        if self._upgrade_active:
            return
        self._upgrade_active = True
        self._upgrade_deadline = time.monotonic() + 40.0
        self._upgrade_overall_deadline = time.monotonic() + 180.0
        self._upgrade_forced_spawns = 0
        self._connect_paused_until = self._upgrade_deadline + 2.0
        if self.is_connected():
            self.send({"cmd": "stop_agent"})
            try:
                self._sock.flush()
            except Exception:
                pass
        QTimer.singleShot(250, self._poll_upgrade_stop)

    def _upgrade_expired(self) -> bool:
        """End a version handoff that is not converging. The service layers
        (Startup entry, hourly failsafe, the normal 30-second spawn
        throttle) keep working; only the aggressive forced loop stops."""
        if not self._upgrade_active:
            return False
        if (time.monotonic() < self._upgrade_overall_deadline
                and self._upgrade_forced_spawns < 8):
            return False
        log.error(
            "Agent version handoff did not converge within its budget; "
            "falling back to normal reconnection. Run install.bat to "
            "complete the upgrade.")
        self._upgrade_active = False
        self._connect_paused_until = 0.0
        self._set_state("offline")
        return True

    def _poll_upgrade_stop(self):
        if not self._upgrade_active or self._upgrade_expired():
            return
        if not self.is_connected():
            # Endpoint closure precedes process exit by a very small amount;
            # let the old supervisor observe rc=0 and release its mutex.
            QTimer.singleShot(1000, self._launch_upgrade_agent)
            return
        if time.monotonic() >= self._upgrade_deadline:
            log.error("Old agent did not disconnect during version upgrade")
            self._drop_socket()
            QTimer.singleShot(1000, self._launch_upgrade_agent)
            return
        QTimer.singleShot(500, self._poll_upgrade_stop)

    def _launch_upgrade_agent(self):
        if not self._upgrade_active or self._upgrade_expired():
            return
        self._connect_paused_until = 0.0
        self._upgrade_forced_spawns += 1
        self._maybe_spawn_agent(force=True)
        QTimer.singleShot(750, self._ensure)
        QTimer.singleShot(5000, self._retry_upgrade_launch)

    def _retry_upgrade_launch(self):
        if not self._upgrade_active or self._upgrade_expired():
            return
        if not self.is_connected():
            # Covers the narrow case where the first new supervisor launched
            # just before the old supervisor released its per-user mutex.
            self._upgrade_forced_spawns += 1
            self._maybe_spawn_agent(force=True)
            self._ensure()
        QTimer.singleShot(5000, self._retry_upgrade_launch)

    def _connect_now(self):
        self._drop_socket()
        self._set_state("connecting")
        sock = QLocalSocket(self)
        self._sock = sock
        self._buffer = bytearray()
        sock.connected.connect(self._on_connected)
        sock.readyRead.connect(self._on_ready)
        sock.errorOccurred.connect(self._on_error)
        sock.disconnected.connect(self._on_disconnected)
        sock.connectToServer(constants.agent_pipe_name())

    def _drop_socket(self):
        self._hello_watchdog.stop()
        self._hello_validated = False
        if self._sock is not None:
            try:
                self._sock.blockSignals(True)
                self._sock.abort()
                self._sock.deleteLater()
            except Exception:
                pass
            self._sock = None

    def _on_connected(self):
        log.info("Agent channel connected; waiting for version handshake")
        self._set_state("connecting")
        self._hello_validated = False
        self._hello_watchdog.start()

    def _on_hello_timeout(self):
        if self.is_connected() and not self._hello_validated:
            log.error("Agent channel did not provide a version hello")
            self._drop_socket()
            self._set_state("offline")
            self._maybe_spawn_agent(force=self._upgrade_active)

    def _on_error(self, _err):
        self._maybe_spawn_agent()
        self._set_state("offline")
        self._drop_socket()

    def _on_disconnected(self):
        log.warning("Agent channel disconnected")
        self._set_state("offline")
        self._drop_socket()

    def _maybe_spawn_agent(self, *, force: bool = False):
        """Launch the supervisor, normally at most once per 30 seconds.

        The per-user supervisor and agent locks make extra launch attempts
        harmless. Version upgrades use ``force`` while waiting for an older
        folder's supervisor mutex to be released.
        """
        now = time.monotonic()
        if not force and now - self._last_spawn < 30.0:
            return
        self._last_spawn = now
        root = Path(__file__).resolve().parents[2]
        supervisor = root / "agent_supervisor.pyw"
        if not supervisor.exists():
            log.error("agent_supervisor.pyw missing beside the app")
            return
        try:
            flags = 0
            kwargs = {}
            if sys.platform.startswith("win"):
                # Detached and windowless: closing this window later must
                # never take the agent down with it.
                flags = 0x08000000 | 0x00000008 | 0x00000200
                kwargs["creationflags"] = flags
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([self._python(root), str(supervisor)],
                             cwd=str(root), close_fds=True, **kwargs)
            log.info("Launched the agent supervisor")
        except Exception:
            log.exception("Could not launch the agent supervisor")

    @staticmethod
    def _python(root: Path) -> str:
        for name in ("pythonw.exe", "python.exe"):
            candidate = root / ".venv" / "Scripts" / name
            if candidate.exists():
                return str(candidate)
        candidate = root / ".venv" / "bin" / "python"
        if candidate.exists():
            return str(candidate)
        return sys.executable

    # ------------------------------------------------ sending

    def send(self, payload: dict) -> bool:
        command = payload.get("cmd")
        if (self.is_connected()
                and (self._hello_validated or command == "stop_agent")):
            try:
                self._sock.write(serialize.encode(payload))
                return True
            except Exception:
                log.exception("Agent command failed to send")
                return False
        if command in _BUFFERABLE:
            self._pending = [
                p for p in self._pending if p.get("cmd") != command]
            self._pending.append(payload)
        return False

    # ------------------------------------------------ receiving

    def _on_ready(self):
        if self._sock is None:
            return
        chunk = bytes(self._sock.readAll().data())
        for payload in serialize.feed(self._buffer, chunk):
            try:
                self._dispatch(payload)
            except Exception:
                log.exception("Agent event dispatch failed")

    def _dispatch(self, p: dict):
        event = p.get("event")
        if event == "hello":
            got = p.get("version") or ""
            if got != constants.VERSION:
                # Do not deliver queued commands to an old folder's agent.
                # Only the explicit stop command is allowed pre-handshake.
                self._hello_watchdog.stop()
                if self._upgrade_active and self._upgrade_expired():
                    self.hello.emit(p)
                    return
                if not self._upgrade_active:
                    self.restart_for_upgrade()
                else:
                    # A previous supervisor can win one final launch during
                    # handoff. Retire that mismatched copy and keep waiting.
                    self._upgrade_deadline = time.monotonic() + 40.0
                    self._connect_paused_until = self._upgrade_deadline + 2.0
                    self.send({"cmd": "stop_agent"})
                    try:
                        self._sock.flush()
                    except Exception:
                        pass
                    QTimer.singleShot(250, self._poll_upgrade_stop)
                self.hello.emit(p)
                return
            self._hello_watchdog.stop()
            self._hello_validated = True
            self._set_state("connected")
            if self._upgrade_active:
                log.info(
                    "Matching agent version %s is ready", constants.VERSION)
                self._upgrade_active = False
                self._connect_paused_until = 0.0
                self._upgrade_forced_spawns = 0
            # Durable sends first: anything enqueued while the channel was
            # down. No command crosses the pipe before version validation.
            self.send({"cmd": "kick_outbox"})
            pending, self._pending = self._pending, []
            for payload in pending:
                self.send(payload)
            self.hello.emit(p)
        elif not self._hello_validated:
            log.warning("Ignoring agent event before version handshake: %s",
                        event)
        elif event == "status":
            self.status.emit(p.get("kind") or "warn", p.get("text") or "",
                             p.get("newest_ts"))
        elif event == "batch":
            self.batch.emit(p.get("items") or [], p.get("source") or "")
        elif event == "chats_refreshed":
            self.chats_refreshed.emit()
        elif event == "chat_refreshed":
            self.chat_refreshed.emit(p.get("chat_guid") or "",
                                     p.get("newest"),
                                     bool(p.get("wake_watching")))
        elif event == "backfill_page":
            self.backfill_page.emit(p.get("chat_guid") or "")
        elif event == "backfill_done":
            self.backfill_done.emit()
        elif event == "poll_ok":
            self.poll_ok.emit()
        elif event == "poll_failed":
            self.poll_failed.emit(p.get("error") or "")
        elif event == "socket_state":
            self.socket_state.emit(bool(p.get("up")), p.get("reason") or "")
        elif event == "caps":
            self.caps_changed.emit(p.get("caps") or {})
        elif event == "wake":
            self.wake_event.emit(p)
        elif event == "recovery":
            self.recovery_event.emit(p)
        elif event == "outbox_changed":
            self.outbox_changed.emit(p.get("chat_guid") or "")
        elif event == "message_sent":
            self.message_sent.emit(p.get("message"))
        elif event == "attachment_ready":
            self.attachment_ready.emit(p.get("guid") or "", p.get("path") or "")
        elif event == "attachment_failed":
            self.attachment_failed.emit(p.get("guid") or "",
                                        p.get("error") or "")
        elif event == "push_broken":
            self.push_broken.emit(p.get("reason") or "")
        elif event == "settings_applied":
            self.settings_applied.emit(p)
