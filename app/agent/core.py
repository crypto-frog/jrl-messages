"""The always-on sync core.

This is the exact backend that used to live inside the main window: the
same socket listener, 3-second ROWID reconciler, bounded head scans,
24-hour audits, send/download workers, 15-second watchdog, generation-
tagged teardown, Recover, and Wake Mac. Moving it here changes where it
runs, not how: messages are collected, verified, and stored from logon to
shutdown whether or not a window is open.

New in this process: the automatic Wake Mac policy (policy.py). When
nothing has arrived for the configured quiet interval and no send is in
flight, the agent restarts Messages on the Mac itself, so texts Apple was
holding back arrive without anyone pressing the button.

Every user-visible outcome is broadcast over the local channel as small
JSON events; the window renders them and reads the shared database for
everything heavy.
"""
import logging
import os
import time
import uuid

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from .. import config, constants
from ..api.events import SocketThread
from ..api.models import parse_message
from ..api.rest import ApiError, BBClient
from ..store.attach_cache import DownloadThread
from ..store.outbox import SendThread
from ..store.repo import Repo
from ..store.sync import ReconcileThread, SyncThread
from ..util.textutil import normalize_address
from . import serialize
from .policy import AutoWakeInputs, should_auto_wake

log = logging.getLogger(__name__)

_LAST_INCOMING_META = "last_incoming_observed_ms"
_LAST_WAKE_META = "last_mac_wake_ms"


class _WakeWorker(QThread):
    """One bounded REST call on its own thread: ask BlueBubbles to quit
    and reopen Messages on the Mac. The agent loop never blocks on the
    Mac, and the call carries its own extended timeout."""
    done = Signal(bool, str, int)   # ok, human-readable error, status code

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client = client

    def stop(self):
        # Teardown symmetry with the other workers. The REST call is
        # already time-bounded, so there is nothing to interrupt.
        pass

    def run(self):
        try:
            self.client.restart_messages_app()
            self.done.emit(True, "", 0)
        except ApiError as e:
            self.done.emit(False, str(e), int(e.status_code or 0))
        except Exception:
            log.exception("Wake Mac call failed unexpectedly")
            self.done.emit(False, "Unexpected error; see log", 0)


class AgentCore(QObject):
    broadcast = Signal(dict)

    def __init__(self, repo: Repo, settings, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.settings = settings
        self._instance_id = uuid.uuid4().hex
        self.repo.set_meta("active_agent_instance_id", self._instance_id)
        self.client = None
        self.socket = self.sync = self.sender_t = self.downloader = None
        self.reconcile = None
        self._socket_up = False
        self._socket_reason = ""
        # Historical rows are quiet until the first complete ROWID baseline
        # finishes. Once armed, later transports create durable delivery
        # events for newly discovered incoming messages.
        self._notify_new = bool(
            self.repo.meta_int("notification_baseline_complete", 0))
        self._backend_generation = 0
        self._retired_backends: list = []
        self._closing = False
        self._restarting = False
        self._manual_recovery = False
        self._manual_recovery_generation = None
        self._manual_recovered_count = 0
        self._manual_poll_ok = False
        self._manual_sync_audit_done = False
        self._manual_recovery_token = 0
        self._recovery_state = "idle"
        # Wake Mac: idle | working (REST call out) | watching (Messages is
        # relaunching; count what floods in) | success (brief label).
        self._wake_state = "idle"
        self._wake_origin = "manual"
        self._wake_token = 0
        self._wake_found = 0
        self._wake_worker = None
        self._wake_lease_active = False
        self._wake_lease_release_worker = None
        self._wake_poll_verified = False
        self._wake_verify_earliest = 0.0
        self._wake_finish_extended = False
        self._refresh_probe_token = 0
        self._refresh_probes: dict[str, tuple[int, int]] = {}
        self.caps = {"private_api": False}
        self._server_newest_ts = 0
        self._last_status = ("warn", "Starting…")
        self._sync_running = False
        self._last_poll_ok_wall = 0.0
        # Auto wake bookkeeping (monotonic). Seeding "last incoming" at boot
        # means the first automatic wake happens only after a full quiet
        # interval of the agent actually running, never as a boot storm.
        self._last_incoming_wall = self._restore_monotonic_anchor(
            _LAST_INCOMING_META, default_now=True)
        self._last_wake_wall = self._restore_monotonic_anchor(
            _LAST_WAKE_META, default_now=False)
        self._agent_started_ms = int(time.time() * 1000)

        self._recovery_timeout = QTimer(self)
        self._recovery_timeout.setSingleShot(True)
        # A healthy Tailscale path can legitimately need several bounded
        # 20-second REST reads; do not declare failure while it is progressing.
        self._recovery_timeout.setInterval(180000)
        self._recovery_timeout.timeout.connect(self._manual_recovery_timed_out)

        self._wd_wall = time.monotonic()
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(15000)
        self._watchdog.timeout.connect(self._watchdog_tick)
        self._watchdog.start()

        self._retired_cleanup = QTimer(self)
        self._retired_cleanup.setInterval(1000)
        self._retired_cleanup.timeout.connect(self._clean_retired_backends)
        self._retired_cleanup.start()

        self._auto_wake_timer = QTimer(self)
        self._auto_wake_timer.setInterval(60000)
        self._auto_wake_timer.timeout.connect(self._auto_wake_tick)
        self._auto_wake_timer.start()

        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(15000)
        self._heartbeat.timeout.connect(self._heartbeat_tick)
        self._heartbeat.start()
        self._heartbeat_tick()

        self._apply_self_identities()
        self.repo.set_meta("agent_started_ms", self._agent_started_ms)
        try:
            pruned = self.repo.prune_delivery_events(
                int(time.time() * 1000) - 14 * 24 * 60 * 60 * 1000)
            if pruned:
                log.info("Pruned %d completed notification ledger row(s)",
                         pruned)
        except Exception:
            log.exception("Could not prune completed notification ledger")
        log.info("JRL agent v%s core ready", constants.VERSION)

    def _restore_monotonic_anchor(self, key: str, *,
                                  default_now: bool) -> float:
        """Translate persisted wall time into this process's monotonic time."""
        now_wall_ms = int(time.time() * 1000)
        now_mono = time.monotonic()
        stored = self.repo.meta_int(key, 0)
        if stored <= 0:
            return now_mono if default_now else 0.0
        age_s = max(0.0, (now_wall_ms - stored) / 1000.0)
        return now_mono - age_s

    def _apply_self_identities(self):
        """Teach the store which 1:1 conversations are the user's own.

        A text sent to your own number or email is marked sent-by-you by
        Apple everywhere, yet it arrives here and must alert. Identities:
        the account the Mac reports, plus any addresses the user listed in
        Settings; previously learned ones persist in meta via the repo."""
        try:
            norms = set(self.repo._self_norms)
            account = (self.caps.get("account") or "").strip()
            if account:
                norms.add(normalize_address(account))
            extra = getattr(self.settings, "self_addresses", "") or ""
            for piece in extra.replace(";", ",").split(","):
                piece = piece.strip()
                if piece:
                    norms.add(normalize_address(piece))
            norms.discard("")
            enabled = bool(getattr(self.settings, "self_chat_alerts", True))
            self.repo.set_self_identities(norms, enabled)
        except Exception:
            log.exception("Could not apply self-conversation identities")

    def _heartbeat_tick(self):
        try:
            owner = self.repo.meta("active_agent_instance_id")
            if owner and owner != self._instance_id:
                log.critical(
                    "A newer agent instance owns the database; retiring "
                    "this process")
                from PySide6.QtCore import QCoreApplication
                QTimer.singleShot(0, QCoreApplication.quit)
                return
            self.repo.set_meta(
                "agent_heartbeat_ms", int(time.time() * 1000))
        except Exception:
            log.exception("Agent heartbeat write failed")

    # ------------------------------------------------ status plumbing

    def set_status(self, kind: str, text: str):
        self._last_status = (kind, text)
        self.broadcast.emit({
            "event": "status", "kind": kind, "text": text,
            "newest_ts": self._server_newest_ts,
        })

    def hello(self) -> dict:
        kind, text = self._last_status
        return {
            "event": "hello",
            "version": constants.VERSION,
            "pid": os.getpid(),
            "instance_id": self._instance_id,
            "connected": self.client is not None,
            "socket_up": self._socket_up,
            "socket_reason": self._socket_reason,
            "caps": dict(self.caps),
            "wake_state": self._wake_state,
            "wake_origin": self._wake_origin,
            "recovery_state": self._recovery_state,
            "status_kind": kind,
            "status_text": text,
            "newest_ts": self._server_newest_ts,
            "auto_wake_minutes": self._auto_wake_minutes(),
        }

    def _auto_wake_minutes(self) -> int:
        try:
            return max(0, int(getattr(self.settings, "auto_wake_minutes",
                                      constants.AUTO_WAKE_DEFAULT_MIN)))
        except (TypeError, ValueError):
            return constants.AUTO_WAKE_DEFAULT_MIN

    # ------------------------------------------------ backend lifecycle

    def _accept_backend_signal(self) -> bool:
        """Reject a queued callback emitted by a retired worker generation."""
        source = self.sender()
        generation = getattr(source, "_jrl_generation", None)
        return generation is None or generation == self._backend_generation

    def start_backend(self):
        if self._closing or self.client is not None:
            return
        pw = config.get_password(self.settings)
        base = self.settings.base_url()
        if not base or not pw:
            self.set_status(
                "warn",
                "Offline · set the server address and password in "
                "Settings")
            return
        self._backend_generation += 1
        generation = self._backend_generation
        queued = Qt.ConnectionType.QueuedConnection
        self.client = BBClient(base, pw)

        retired_send_active = any(
            isinstance(t, SendThread) and t.isRunning()
            for workers, _client, _when in self._retired_backends
            for t in workers)
        self.sender_t = SendThread(
            self.client, self.repo,
            recover_sending=not retired_send_active)
        self.sender_t._jrl_generation = generation
        self.sender_t.outbox_changed.connect(
            self._on_outbox_changed, type=queued)
        self.sender_t.message_sent.connect(
            self._on_message_sent, type=queued)
        self.sender_t.start()

        self.downloader = DownloadThread(self.client, self.repo)
        self.downloader._jrl_generation = generation
        self.downloader.ready.connect(
            self._on_attachment_ready, type=queued)
        self.downloader.failed.connect(
            self._on_download_failed, type=queued)
        self.downloader.start()

        self.reconcile = ReconcileThread(
            self.client, self.repo, notify_new=self._notify_new)
        self.reconcile._jrl_generation = generation
        self.reconcile.batch_upserted.connect(
            self._on_reconcile_batch, type=queued)
        self.reconcile.chats_refreshed.connect(
            self._on_chats_refreshed, type=queued)
        self.reconcile.rescued.connect(self._on_rescued, type=queued)
        self.reconcile.chat_refreshed.connect(
            self._on_chat_refreshed, type=queued)
        self.reconcile.poll_ok.connect(self._on_poll_ok, type=queued)
        self.reconcile.poll_failed.connect(
            self._on_poll_failed, type=queued)
        self.reconcile.caught_up.connect(
            self._on_caught_up, type=queued)
        self.reconcile.start()

        self.socket = SocketThread(base, pw)
        self.socket._jrl_generation = generation
        self.socket.connected.connect(
            self._on_socket_connected, type=queued)
        self.socket.disconnected.connect(
            self._on_socket_down, type=queued)
        self._on_socket_down("connecting")
        self.socket.new_message.connect(
            self._handle_incoming, type=queued)
        self.socket.updated_message.connect(
            self._handle_update, type=queued)
        self.socket.chats_dirty.connect(
            self._on_chats_dirty, type=queued)
        self.socket.push_broken.connect(
            self._on_push_broken, type=queued)
        self.socket.read_status.connect(
            self._on_read_status, type=queued)
        self.socket.start()

        self._wd_wall = time.monotonic()
        self._start_full_sync()

    def stop_backend(self):
        # Invalidate queued callbacks before detaching anything. A callback
        # already in the queue from the retired generation must not land
        # during the short teardown/restart gap.
        self._backend_generation += 1
        workers = [t for t in (
            self.socket, self.sync, self.reconcile,
            self.sender_t, self.downloader, self._wake_worker)
            if t is not None]
        old_client = self.client
        # Detach the active generation first. A replacement can no longer
        # submit work to, or inspect, a retired worker.
        self.socket = self.sync = self.reconcile = None
        self.sender_t = self.downloader = self.client = None
        self._wake_worker = None
        self._sync_running = False
        self._refresh_probes.clear()
        for t in workers:
            try:
                t.blockSignals(True)
                t.disconnect()
            except Exception:
                pass
            try:
                t.stop()
            except Exception:
                log.exception("Worker stop failed")
        for t in workers:
            try:
                t.wait(250)
            except Exception:
                log.exception("Worker join failed")
        # In-flight REST calls have bounded timeouts. Keep their QThreads and
        # HTTP client alive until they actually finish; never destroy or close
        # resources out from under a running request.
        if any(t.isRunning() for t in workers):
            self._retired_backends.append(
                (workers, old_client, time.monotonic()))
        elif old_client is not None:
            old_client.close()

    @Slot()
    def _clean_retired_backends(self, final: bool = False):
        keep = []
        for workers, client, retired_at in self._retired_backends:
            if final:
                # At process exit there will be no replacement generation.
                # Closing the pool cancels an in-flight request so the worker
                # can observe failure and leave its durable outbox state safe.
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        log.exception("HTTP client close during exit failed")
                deadline = time.monotonic() + 25.0
                for t in workers:
                    remaining_ms = max(
                        0, int((deadline - time.monotonic()) * 1000))
                    if remaining_ms and t.isRunning():
                        t.wait(remaining_ms)
            if any(t.isRunning() for t in workers):
                keep.append((workers, client, retired_at))
                continue
            if client is not None and not final:
                try:
                    client.close()
                except Exception:
                    log.exception("Retired HTTP client close failed")
        self._retired_backends = keep
        # A backend rebuild cannot cancel the already-issued Mac restart
        # request. Keep its durable send fence until that exact worker has
        # returned, even though its old-generation signals were disconnected.
        pending_wake = self._wake_lease_release_worker
        if pending_wake is not None and not pending_wake.isRunning():
            self._wake_lease_release_worker = None
            self._release_wake_lease(kick=not self._closing)

    def shutdown(self):
        self._closing = True
        self._watchdog.stop()
        self._retired_cleanup.stop()
        self._auto_wake_timer.stop()
        self._heartbeat.stop()
        self._cancel_wake(kick=False)
        self.stop_backend()
        self._clean_retired_backends(final=True)

    # ------------------------------------------------ full sync

    def _start_full_sync(self):
        if self.client is None or self._closing:
            return
        queued = Qt.ConnectionType.QueuedConnection
        s = SyncThread(self.client, self.repo, mode="full",
                       horizon_days=self.settings.backfill_horizon_days,
                       notify_new=self._notify_new)
        s._jrl_generation = self._backend_generation
        s.status.connect(self._on_sync_status, type=queued)
        s.connected_ok.connect(self._on_server_info, type=queued)
        s.failed.connect(self._on_sync_failed, type=queued)
        s.contacts_ready.connect(self._on_contacts_ready, type=queued)
        s.chats_ready.connect(self._on_chats_ready, type=queued)
        s.batch_upserted.connect(self._on_sync_batch, type=queued)
        s.backfill_page.connect(self._on_backfill_page, type=queued)
        s.backfill_done.connect(self._on_backfill_done, type=queued)
        s.recovery_audit_done.connect(
            self._on_recovery_audit_done, type=queued)
        self.sync = s
        self._sync_running = True
        s.start()

    # ------------------------------------------------ watchdog

    def _watchdog_tick(self):
        now = time.monotonic()
        gap = now - self._wd_wall
        self._wd_wall = now
        if self.client is None or self._restarting:
            return
        if gap > 60:
            log.warning("Resume detected (watchdog gap %.0fs)", gap)
            self._recover("system resume")
            return
        r = self.reconcile
        if r is None or not r.isRunning():
            log.error("Reconcile thread not running")
            self._recover("worker dead")
            return
        # The reconciler is not the only worker whose silent death would
        # strand the user; a dead sender leaves texts queued forever.
        for label, worker in (("send", self.sender_t),
                              ("download", self.downloader),
                              ("push", self.socket)):
            if worker is None or not worker.isRunning():
                log.error("%s worker not running", label)
                self._recover(f"{label} worker dead")
                return
        stale = now - getattr(r, "last_success_ts", now)
        limit = max(float(constants.POLL_SUCCESS_STALE_S),
                    float(getattr(r, "interval", 30)) * 4 + 15)
        if stale > limit:
            log.error("Reconcile stalled for %.0fs (limit %.0fs)",
                      stale, limit)
            self._recover("polling stalled")
            return
        if (getattr(r, "consecutive_failures", 0)
                >= constants.POLL_FAILURE_RECOVERY):
            log.error("Reconcile failed %d consecutive times",
                      r.consecutive_failures)
            self._recover("repeated polling failures")

    # ------------------------------------------------ recovery

    def recover_messages(self):
        """Non-destructively rebuild every transport and rescan recent mail."""
        if self._closing or self._restarting or self._manual_recovery:
            return
        if not self.settings.base_url() or not config.get_password(self.settings):
            self.set_status("fail", "Set the server address and password first")
            return
        self._manual_recovery = True
        self._manual_recovery_generation = None
        self._manual_recovered_count = 0
        self._manual_poll_ok = False
        self._manual_sync_audit_done = False
        self._manual_recovery_token += 1
        recovery_token = self._manual_recovery_token
        self._recovery_state = "working"
        self._broadcast_recovery()
        self._recovery_timeout.start()
        QTimer.singleShot(
            45000,
            lambda token=recovery_token:
            self._manual_recovery_slow_notice(token))
        self._recover("manual reconnect and rescan", manual=True)

    def _recover(self, why: str, manual: bool = False):
        """The automated version of closing and reopening the app."""
        manual = manual or self._manual_recovery
        if self._closing or self._restarting:
            return
        self._restarting = True
        self._cancel_wake()
        self._socket_up = False
        self.set_status("warn", "Recovering messages…")
        log.warning("Backend recovery started: %s", why)
        try:
            self.stop_backend()
        except Exception:
            log.exception("Recovery teardown error (continuing)")

        def go():
            try:
                self.start_backend()
                if self.client is None:
                    raise RuntimeError("Backend could not be started")
                if manual:
                    self._manual_poll_ok = False
                    self._manual_sync_audit_done = False
                    self._manual_recovery_generation = (
                        self._backend_generation)
                    self._kick_recovery_checks()
                log.warning("Backend recovered after: %s", why)
            except Exception:
                log.exception("Recovery restart failed")
                if manual:
                    self._finish_manual_recovery(False)
            finally:
                self._restarting = False
        QTimer.singleShot(250, go)

    def _kick_recovery_checks(self):
        if self.reconcile is None:
            return
        self.reconcile.poke(chats=True, head=True)

    def _maybe_finish_manual_recovery(self):
        if (self._manual_recovery and self._manual_poll_ok
                and self._manual_sync_audit_done):
            self._finish_manual_recovery(True)

    def _finish_manual_recovery(self, success: bool):
        if not self._manual_recovery:
            return
        self._recovery_timeout.stop()
        restored = self._manual_recovered_count
        self._manual_recovery = False
        self._manual_recovery_generation = None
        self._manual_poll_ok = False
        self._manual_sync_audit_done = False
        self._recovery_state = "success" if success else "idle"
        self._broadcast_recovery(restored=restored)
        if success:
            detail = (f"{restored} message{'s' if restored != 1 else ''} "
                      "restored" if restored
                      else "no Windows-side gaps found")
            self.set_status(
                "ok", f"Windows checks complete · {detail} · now "
                "refreshing Messages on the Mac…")
            # A Windows rebuild cannot retrieve a text that has not reached
            # the Mac database.  Manual Recover is therefore an end-to-end,
            # staged repair and safely escalates to the same Mac restart that
            # the user's Wake Mac observation has shown to work.
            QTimer.singleShot(150, self._start_recovery_wake)
            QTimer.singleShot(3000, self._clear_recovery_success)
        else:
            self.set_status(
                "fail", "Recovery could not reconnect. Check Settings.")

    def _start_recovery_wake(self):
        if self._closing:
            return
        if not self.wake_mac(origin="recovery"):
            self.set_status(
                "warn", "Windows checks completed, but the Mac refresh "
                "was postponed because a message is sending or the Mac is "
                "not reachable.")

    def _clear_recovery_success(self):
        if not self._manual_recovery and self._recovery_state == "success":
            self._recovery_state = "idle"
            self._broadcast_recovery()

    def _manual_recovery_timed_out(self):
        if not self._manual_recovery:
            return
        self._manual_recovery = False
        self._manual_recovery_generation = None
        self._manual_poll_ok = False
        self._manual_sync_audit_done = False
        self._recovery_state = "idle"
        self._broadcast_recovery()
        self.set_status(
            "warn", "Recovery is still waiting for the Mac. You can try again.")

    def _manual_recovery_slow_notice(self, token: int):
        if (self._manual_recovery
                and token == self._manual_recovery_token):
            self.set_status(
                "warn", "Recovery is still working over the slow connection…")

    def _broadcast_recovery(self, restored: int = 0):
        self.broadcast.emit({
            "event": "recovery", "state": self._recovery_state,
            "restored": restored,
        })

    # ------------------------------------------------ wake the Mac

    def wake_mac(self, origin: str = "manual") -> bool:
        """Ask BlueBubbles to quit and reopen Messages on the Mac.

        Relaunching forces Messages to reconnect to Apple, and Apple then
        hands over any texts it was still holding back. This is the same
        wake-up that sending a message causes, with nothing sent to anyone.
        The Windows cache, settings, and history are untouched; whatever
        floods in is ingested by the normal reconcilers."""
        if self._closing or self._restarting or self._manual_recovery:
            return False
        if self._wake_state in ("working", "watching"):
            return False
        if self._wake_lease_active:
            # A canceled old-generation REST call may still be quitting or
            # reopening Messages. Never overlap it or replace its send fence.
            if origin != "auto":
                self.set_status(
                    "warn", "A previous Mac refresh is still finishing. "
                    "Try Wake Mac again in a moment.")
            return False
        if self.client is None:
            if origin != "auto":
                self.set_status("fail", "Not connected. Check settings.")
            return False
        try:
            send_safe = self.repo.try_begin_mac_maintenance()
        except Exception:
            log.exception("Could not acquire the Mac maintenance lease")
            send_safe = False
        if not send_safe:
            # Quitting Messages mid-send could interrupt an AppleScript
            # delivery. A held-back text can wait ten more seconds; a
            # doubled or lost outgoing message to a client cannot.
            if origin != "auto":
                if self.repo.mac_maintenance_active():
                    self.set_status(
                        "warn", "A previous Mac refresh is still protected. "
                        "Try Wake Mac again in a moment.")
                else:
                    self.set_status(
                        "warn", "A message is queued or sending. Let it "
                        "finish, then press Wake Mac again.")
            else:
                log.info("Auto wake skipped: a send is active")
            return False
        self._wake_lease_active = True
        self._wake_token += 1
        self._wake_found = 0
        self._wake_poll_verified = False
        self._wake_verify_earliest = 0.0
        self._wake_finish_extended = False
        self._wake_origin = origin
        self._wake_state = "working"
        self._last_wake_wall = time.monotonic()
        self.repo.set_meta(_LAST_WAKE_META, int(time.time() * 1000))
        self._broadcast_wake()
        if origin != "auto":
            self.set_status("ok", "Asking the Mac to restart Messages…")
        else:
            log.info("Auto wake: asking the Mac to restart Messages")
        worker = _WakeWorker(self.client)
        worker._jrl_generation = self._backend_generation
        worker.done.connect(
            self._on_wake_done, type=Qt.ConnectionType.QueuedConnection)
        self._wake_worker = worker
        worker.start()
        return True

    @Slot(bool, str, int)
    def _on_wake_done(self, ok: bool, message: str, status: int):
        if not self._accept_backend_signal():
            return
        # The worker reference stays in place until stop_backend retires it
        # or the next wake replaces it; dropping the last reference to a
        # QThread that is still finishing would crash Qt.
        if self._wake_state != "working":
            return
        if not ok:
            self._release_wake_lease(kick=True)
            self._wake_state = "idle"
            self._broadcast_wake()
            if status == 404:
                text = ("This BlueBubbles server is too old for the Messages "
                        "restart command. Update BlueBubbles on the Mac, or "
                        "send any text from here instead.")
            else:
                text = (f"Wake Mac failed: {message}" if message
                        else "Wake Mac failed.")
            if self._wake_origin != "auto":
                self.set_status("fail", text)
            log.warning("Wake Mac (%s) failed (status %s): %s",
                        self._wake_origin, status, message)
            return
        log.info("Messages restart accepted by the Mac; watching for "
                 "held-back texts (%s)", self._wake_origin)
        self._wake_state = "watching"
        self._wake_verify_earliest = time.monotonic() + 4.0
        self._broadcast_wake()
        if self._wake_origin != "auto":
            self.set_status("ok", self._wake_watch_line())
        token = self._wake_token
        # Messages needs a few seconds to relaunch and talk to Apple, and a
        # slow link can add more. Forced re-checks ride on top of the normal
        # 3-second poll so the recovered texts appear as soon as they land.
        for delay in (4000, 9000, 16000, 30000, 50000):
            QTimer.singleShot(
                delay, lambda t=token: self._wake_poke(t))
        QTimer.singleShot(60000, lambda t=token: self._finish_wake(t))

    def _wake_watch_line(self) -> str:
        n = self._wake_found
        if n:
            return (f"Messages restarted on the Mac · {n} held-back "
                    f"message{'s' if n != 1 else ''} recovered so far…")
        return ("Messages is restarting on the Mac · watching for "
                "held-back texts…")

    def _wake_poke(self, token: int):
        if token != self._wake_token or self._wake_state != "watching":
            return
        if self.reconcile:
            self.reconcile.poke(chats=True, head=True)

    def _finish_wake(self, token: int):
        if token != self._wake_token or self._wake_state != "watching":
            return
        if not self._wake_poll_verified and not self._wake_finish_extended:
            # Do not claim that the Mac held nothing merely because a timer
            # expired. Give the authoritative post-restart scan another 30 s.
            self._wake_finish_extended = True
            self._wake_poke(token)
            if self._wake_origin != "auto":
                self.set_status(
                    "warn", "Messages restarted · waiting for a verified "
                    "post-restart check…")
            QTimer.singleShot(30000, lambda t=token: self._finish_wake(t))
            return
        if not self._wake_poll_verified:
            self._release_wake_lease(kick=True)
            self._wake_state = "idle"
            self._broadcast_wake()
            if self._wake_origin != "auto":
                self.set_status(
                    "warn", "Messages restarted, but the Mac recheck has not "
                    "completed. Background checks are continuing.")
            log.warning(
                "Wake Mac (%s) ended without a verified post-restart scan",
                self._wake_origin)
            return
        n = self._wake_found
        self._release_wake_lease(kick=True)
        self._wake_state = "success"
        self._broadcast_wake(found=n)
        if self._wake_origin != "auto":
            if n:
                self.set_status(
                    "ok",
                    f"Wake complete · {n} held-back "
                    f"message{'s' if n != 1 else ''} arrived")
            else:
                self.set_status(
                    "ok",
                    "Wake complete · the Mac was holding nothing back")
        elif n:
            # An automatic wake stays quiet unless it actually recovered
            # something worth telling the user about.
            self.set_status(
                "ok",
                f"Auto Wake Mac · {n} held-back "
                f"message{'s' if n != 1 else ''} arrived")
        log.info("Wake Mac (%s) window closed with %d recovered message(s)",
                 self._wake_origin, n)
        QTimer.singleShot(4000, lambda t=token: self._clear_wake_success(t))

    def _clear_wake_success(self, token: int):
        if token == self._wake_token and self._wake_state == "success":
            self._wake_state = "idle"
            self._broadcast_wake()

    def _release_wake_lease(self, *, kick: bool):
        if not self._wake_lease_active:
            return
        try:
            self.repo.end_mac_maintenance()
        except Exception:
            # The lease has a bounded expiry, so even a database failure here
            # cannot strand outgoing messages indefinitely.
            log.exception("Could not release the Mac maintenance lease")
        self._wake_lease_active = False
        if kick and not self._closing:
            self._cmd_kick_outbox()

    def _cancel_wake(self, *, kick: bool = False):
        """Backend rebuilds invalidate any wake in flight; bump the token so
        a stale timer or worker callback cannot land later. The worker
        reference itself is retired by stop_backend, never dropped here.
        Crucially, an issued restart REST call cannot be cancelled: its send
        fence remains durable until that worker actually returns."""
        self._wake_token += 1
        worker = self._wake_worker
        pending_worker = self._wake_lease_release_worker
        if (self._wake_lease_active and pending_worker is not None
                and pending_worker.isRunning()):
            pass
        elif (self._wake_lease_active and worker is not None
                and worker.isRunning()):
            self._wake_lease_release_worker = worker
        else:
            self._wake_lease_release_worker = None
            self._release_wake_lease(kick=kick)
        self._wake_poll_verified = False
        self._wake_verify_earliest = 0.0
        self._wake_finish_extended = False
        if self._wake_state != "idle":
            self._wake_state = "idle"
            self._broadcast_wake()

    def _broadcast_wake(self, found: int = None):
        self.broadcast.emit({
            "event": "wake", "state": self._wake_state,
            "origin": self._wake_origin,
            "found": self._wake_found if found is None else found,
        })

    # ------------------------------------------------ automatic wake

    def _auto_wake_tick(self):
        try:
            inputs = AutoWakeInputs(
                now=time.monotonic(),
                interval_minutes=self._auto_wake_minutes(),
                connected=self.client is not None,
                poll_healthy=(time.monotonic() - self._last_poll_ok_wall
                              <= 90.0),
                outbox_active=(self.repo.outbox_active_count()
                               if self.client is not None else 1),
                busy=(self._wake_state in ("working", "watching")
                      or self._manual_recovery or self._restarting),
                last_incoming_ts=self._last_incoming_wall,
                last_wake_ts=self._last_wake_wall,
            )
        except Exception:
            log.exception("Auto wake evaluation failed")
            return
        if should_auto_wake(inputs):
            log.info("Auto wake: %d minute(s) without incoming messages",
                     inputs.interval_minutes)
            self.wake_mac(origin="auto")

    # ------------------------------------------------ worker slots

    @Slot(dict)
    def _on_server_info(self, info):
        if not self._accept_backend_signal():
            return
        if isinstance(info, dict):
            self.caps["private_api"] = bool(info.get("private_api"))
            acct = (info.get("detected_icloud")
                    or info.get("detected_imessage") or "")
            if isinstance(acct, str) and acct:
                self.caps["account"] = acct
                self._apply_self_identities()
        self.broadcast.emit({"event": "caps", "caps": dict(self.caps)})
        if self._recovery_state != "success":
            self.set_status("ok", "Connected · 3-second checks")

    @Slot(str)
    def _on_sync_status(self, text: str):
        if (self._accept_backend_signal()
                and self._recovery_state != "success"):
            self.set_status("ok", text)

    @Slot(str)
    def _on_sync_failed(self, error: str):
        if not self._accept_backend_signal():
            return
        self._sync_running = False
        if (self._manual_recovery
                and self._manual_recovery_generation
                == self._backend_generation):
            self._finish_manual_recovery(False)
            return
        self.set_status("fail", error)

    @Slot()
    def _on_contacts_ready(self):
        if self._accept_backend_signal():
            self.broadcast.emit({"event": "chats_refreshed"})

    @Slot()
    def _on_chats_ready(self):
        if self._accept_backend_signal():
            self.broadcast.emit({"event": "chats_refreshed"})

    @Slot()
    def _on_backfill_done(self):
        if not self._accept_backend_signal():
            return
        self._sync_running = False
        self.broadcast.emit({"event": "backfill_done"})
        if self._recovery_state != "success":
            self.set_status("ok", "Connected")

    @Slot()
    def _on_recovery_audit_done(self):
        if not self._accept_backend_signal():
            return
        if (self._manual_recovery
                and self._manual_recovery_generation
                == self._backend_generation):
            self._manual_sync_audit_done = True
            self._maybe_finish_manual_recovery()

    @Slot(str)
    def _on_backfill_page(self, chat_guid: str):
        if self._accept_backend_signal():
            self.broadcast.emit(
                {"event": "backfill_page", "chat_guid": chat_guid})

    @Slot(object)
    def _on_sync_batch(self, items):
        if self._accept_backend_signal():
            self._apply_committed_batch(items, source="boot")

    @Slot(object)
    def _on_reconcile_batch(self, items):
        if self._accept_backend_signal():
            self._apply_committed_batch(items, source="poll")

    @Slot(int)
    def _on_rescued(self, n: int):
        if not self._accept_backend_signal():
            return
        log.info("Poll recovered %d message(s)", n)

    @Slot()
    def _on_poll_ok(self):
        if not self._accept_backend_signal():
            return
        self._last_poll_ok_wall = time.monotonic()
        if (self._wake_state == "watching"
                and time.monotonic() >= self._wake_verify_earliest):
            self._wake_poll_verified = True
        self.broadcast.emit({"event": "poll_ok"})
        if (self._manual_recovery
                and self._manual_recovery_generation
                == self._backend_generation):
            self._manual_poll_ok = True
            self._maybe_finish_manual_recovery()
            return
        if self._wake_state == "watching" and self._wake_origin != "auto":
            # The wake window owns the footer so the user can watch the
            # recovered count grow instead of a generic Connected line.
            self.set_status("ok", self._wake_watch_line())
            return
        if self.sync is not None and self.sync.isRunning():
            return  # backfill owns the status line while it works
        if self.repo.meta_int("rowid_sync_supported", 1) == 0:
            self.set_status(
                "warn",
                "Compatibility checks only · update BlueBubbles")
            return
        if self._socket_up:
            self.set_status("ok", "Connected")
        else:
            why = f" ({self._socket_reason})" if self._socket_reason else ""
            self.set_status(
                "warn",
                f"Live push offline, checking every "
                f"{constants.FAST_POLL_S} s{why}")

    @Slot(str)
    def _on_poll_failed(self, error: str):
        if self._accept_backend_signal():
            self.broadcast.emit({"event": "poll_failed", "error": error})
            self.set_status("warn", f"Checking… ({error})")

    @Slot()
    def _on_caught_up(self):
        if not self._accept_backend_signal():
            return
        if not self._notify_new:
            self.repo.set_meta("notification_baseline_complete", 1)
            self._notify_new = True
            log.info("Initial message baseline complete; notifications armed")
        if self.reconcile is not None:
            self.reconcile.arm_notifications()

    @Slot()
    def _on_chats_refreshed(self):
        if self._accept_backend_signal():
            self.broadcast.emit({"event": "chats_refreshed"})

    @Slot(str, object)
    def _on_chat_refreshed(self, chat_guid: str, newest_ts=None):
        if not self._accept_backend_signal():
            return
        self.broadcast.emit({
            "event": "chat_refreshed", "chat_guid": chat_guid,
            "newest": newest_ts,
            "wake_watching": self._wake_state == "watching",
        })
        self._finish_refresh_probe(chat_guid)

    @Slot()
    def _on_chats_dirty(self):
        if (self._accept_backend_signal()
                and self.reconcile is not None):
            self.reconcile.poke(chats=True)

    @Slot(object)
    def _on_read_status(self, data):
        if not self._accept_backend_signal():
            return
        guid = ""
        if isinstance(data, dict):
            guid = data.get("chatGuid") or ""
            if not guid:
                chats = data.get("chats") or []
                if chats and isinstance(chats[0], dict):
                    guid = chats[0].get("guid") or ""
        if guid and self.reconcile:
            self.reconcile.poke_chat(guid)

    @Slot(str)
    def _on_push_broken(self, reason: str):
        if not self._accept_backend_signal():
            return
        self.broadcast.emit({"event": "push_broken", "reason": reason})
        if self.reconcile:
            self.reconcile.poke(head=True)

    @Slot()
    def _on_socket_connected(self):
        if not self._accept_backend_signal():
            return
        self._socket_up = True
        self._socket_reason = ""
        self.broadcast.emit({"event": "socket_state", "up": True, "reason": ""})
        self.set_status("ok", "Connected · 3-second checks")
        if self.reconcile:
            self.reconcile.poke(head=True)

    @Slot(str)
    def _on_socket_down(self, why: str):
        if not self._accept_backend_signal():
            return
        self._socket_up = False
        self._socket_reason = why
        self.broadcast.emit(
            {"event": "socket_state", "up": False, "reason": why})
        if self.reconcile:
            self.reconcile.poke(head=True)
        self.set_status(
            "warn",
            f"Live push offline, 3-second checks ({why})")

    @Slot(str, str)
    def _on_attachment_ready(self, guid: str, path: str):
        if self._accept_backend_signal():
            self.broadcast.emit(
                {"event": "attachment_ready", "guid": guid, "path": path})

    @Slot(str, str)
    def _on_download_failed(self, guid: str, error: str):
        if self._accept_backend_signal():
            self.broadcast.emit(
                {"event": "attachment_failed", "guid": guid, "error": error})
            self.set_status("warn", error)

    @Slot(str)
    def _on_outbox_changed(self, chat_guid: str):
        if self._accept_backend_signal():
            self.broadcast.emit(
                {"event": "outbox_changed", "chat_guid": chat_guid})

    @Slot(object)
    def _on_message_sent(self, m):
        if not self._accept_backend_signal():
            return
        payload = None
        if isinstance(m, dict):
            payload = serialize.slim_message(m, True, True)
        self.broadcast.emit({"event": "message_sent", "message": payload})
        if self.reconcile:
            self.reconcile.poke()
            if not self._socket_up:
                # Apple's echo of our own send arrives by poll when the live
                # push channel is down; check soon rather than eventually.
                QTimer.singleShot(4000, self._poke_reconcile)
                QTimer.singleShot(12000, self._poke_reconcile)

    def _poke_reconcile(self):
        if self.reconcile:
            self.reconcile.poke()

    # ------------------------------------------------ push events

    @Slot(object)
    def _handle_incoming(self, data: dict):
        if not self._accept_backend_signal():
            return
        try:
            m = parse_message(data)
            if not m:
                log.warning("Push event missing chat context; verifying by poll")
                if self.reconcile:
                    self.reconcile.poke(chats=True)
                return
            result = self.repo.upsert_message(
                m, notify_eligible=True, allow_existing_event=True)
            self._apply_committed_batch(
                [(m, result.is_new, result.changed)], source="push")
        except Exception:
            log.exception("Failed to handle incoming event")
            if self.reconcile:
                self.reconcile.poke()

    @Slot(object)
    def _handle_update(self, data: dict):
        if not self._accept_backend_signal():
            return
        try:
            m = parse_message(data)
            if not m:
                if self.reconcile:
                    self.reconcile.poke()
                return
            result = self.repo.upsert_message(m, notify_eligible=True)
            self._apply_committed_batch(
                [(m, result.is_new, result.changed)], source="update")
        except Exception:
            log.exception("Failed to handle update event")
            if self.reconcile:
                self.reconcile.poke()

    # ------------------------------------------------ committed batches

    def _apply_committed_batch(self, items, source: str):
        """Account for a committed worker batch and publish it."""
        wake_before = self._wake_found
        observed_incoming = False
        slim = []
        for item in items or []:
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                continue
            m = item[0]
            is_new = bool(item[1])
            changed = bool(item[2]) if len(item) > 2 else True
            if not isinstance(m, dict):
                continue
            ts = m.get("date_created") or 0
            self._server_newest_ts = max(self._server_newest_ts, ts)
            if is_new:
                if self._manual_recovery:
                    self._manual_recovered_count += 1
                if self._wake_state == "watching":
                    self._wake_found += 1
                if (not m.get("is_from_me")
                        and not m.get("item_type")
                        and not m.get("associated_guid")):
                    self._last_incoming_wall = time.monotonic()
                    observed_incoming = True
                # Any sign of life restores a hidden conversation, exactly
                # as it did when this logic lived in the window. It must
                # happen here now, so a message that arrives while no
                # window is open still restores the conversation durably.
                self._resurrect_if_hidden(m.get("chat_guid"), "new activity")
            slim.append(serialize.slim_message(m, is_new, changed))
        if observed_incoming:
            self.repo.set_meta(
                _LAST_INCOMING_META, int(time.time() * 1000))
        if (self._wake_state == "watching"
                and self._wake_found != wake_before):
            self._broadcast_wake()
            if self._wake_origin != "auto":
                self.set_status("ok", self._wake_watch_line())
        for start in range(0, len(slim), constants.BATCH_EVENT_CHUNK):
            self.broadcast.emit({
                "event": "batch", "source": source,
                "items": slim[start:start + constants.BATCH_EVENT_CHUNK],
            })

    def _resurrect_if_hidden(self, chat_guid: str, reason: str):
        try:
            if chat_guid and self.repo.is_hidden(chat_guid):
                self.repo.unhide_chat(chat_guid)
                log.info("Hidden conversation restored (%s)", reason)
        except Exception:
            log.exception("Hidden restore failed")

    # ------------------------------------------------ commands

    def handle_command(self, payload: dict, reply):
        cmd = payload.get("cmd")
        if cmd in ("hello", "status"):
            reply(self.hello())
        elif cmd == "submit_outbox":
            self._cmd_submit_outbox(payload)
        elif cmd == "kick_outbox":
            self._cmd_kick_outbox()
        elif cmd == "download":
            if self.downloader:
                self.downloader.request(
                    payload.get("guid") or "",
                    payload.get("file_name") or "attachment")
            else:
                self.set_status("fail", "Not connected. Check settings.")
        elif cmd == "poke":
            if self.reconcile:
                self.reconcile.poke(
                    chats=bool(payload.get("chats")),
                    head=bool(payload.get("head")))
        elif cmd == "poke_chat":
            if self.reconcile and payload.get("chat_guid"):
                self.reconcile.poke_chat(payload["chat_guid"])
        elif cmd == "refresh_chat":
            self._cmd_refresh_chat(payload)
        elif cmd == "group_changed":
            if self.reconcile:
                self.reconcile.poke(chats=True)
                if payload.get("chat_guid"):
                    self.reconcile.poke_chat(payload["chat_guid"])
        elif cmd == "recover":
            self.recover_messages()
        elif cmd == "wake":
            self.wake_mac(origin="manual")
        elif cmd == "settings_changed":
            self.reload_settings()
        elif cmd == "stop_agent":
            log.warning("Stop requested over the agent channel")
            from PySide6.QtCore import QCoreApplication
            reply({"event": "stopping", "version": constants.VERSION})
            QTimer.singleShot(50, QCoreApplication.quit)
        else:
            log.warning("Unknown agent command: %r", cmd)

    def _cmd_submit_outbox(self, payload):
        try:
            oid = int(payload.get("id"))
        except (TypeError, ValueError):
            return
        if self.sender_t:
            self.sender_t.submit(oid)
        else:
            self.set_status("fail", "Not connected. Check settings.")

    def _cmd_kick_outbox(self):
        """Submit every safely queued row. Used when a window reconnects
        after enqueueing while the channel was down. Rows stuck at
        'sending' are never touched here; that ambiguity stays human."""
        if not self.sender_t:
            return
        for oid in self.repo.recover_outbox(mark_sending_uncertain=False):
            self.sender_t.submit(oid)

    def _cmd_refresh_chat(self, payload):
        chat_guid = payload.get("chat_guid") or ""
        if not self.reconcile:
            self.set_status("fail", "Not connected. Check settings.")
            return
        self.set_status("ok", "Checking conversation and recent messages…")
        if chat_guid:
            self._refresh_probe_token += 1
            self._refresh_probes[chat_guid] = (
                self._refresh_probe_token,
                self.repo.message_count(chat_guid))
            self.reconcile.poke_chat(chat_guid)
        # The contact may have switched between an SMS and iMessage sibling
        # GUID. Also audit the global head and current chat list so Refresh is
        # useful even when the selected GUID is no longer where Apple wrote.
        self.reconcile.poke(chats=True, head=True)

    def _finish_refresh_probe(self, chat_guid: str):
        probe = self._refresh_probes.pop(chat_guid, None)
        if probe is None or self._closing:
            return
        _token, before_count = probe
        if self.repo.message_count(chat_guid) > before_count:
            return
        if (time.monotonic() - self._last_wake_wall
                < constants.REFRESH_WAKE_COOLDOWN_S):
            return
        # F5 used to stop at the BlueBubbles database.  If the selected-chat
        # and global scans found no new row, escalate once to the guarded Mac
        # restart that can release a text Apple has not delivered there yet.
        self.wake_mac(origin="refresh")

    def reload_settings(self):
        """Re-read config.json (the window just saved it). Restart the
        backend only when the connection itself changed."""
        old_base = self.settings.base_url()
        old_pw = config.get_password(self.settings)
        self.settings = config.load()
        new_base = self.settings.base_url()
        new_pw = config.get_password(self.settings)
        self._apply_self_identities()
        log.info("Settings reloaded (auto wake: %d min)",
                 self._auto_wake_minutes())
        self.broadcast.emit({
            "event": "settings_applied",
            "auto_wake_minutes": self._auto_wake_minutes(),
        })
        if (new_base, new_pw) != (old_base, old_pw) or self.client is None:
            self._cancel_wake()
            self.stop_backend()
            self.start_backend()
