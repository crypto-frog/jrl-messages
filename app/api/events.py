"""Live event stream from the BlueBubbles server over Socket.IO.
Runs on its own QThread; every server event crosses into the UI thread
only through Qt signals. Reconnection is handled with backoff."""
import json
import logging
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import socketio
from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)


class SocketThread(QThread):
    connected = Signal()
    disconnected = Signal(str)
    new_message = Signal(object)
    updated_message = Signal(object)
    send_error = Signal(object)
    chats_dirty = Signal()
    push_broken = Signal(str)
    read_status = Signal(object)

    def __init__(self, base_url: str, password: str, parent=None):
        super().__init__(parent)
        self.base = base_url.rstrip("/")
        self.password = password
        self._stop = False
        self._sio = None
        self._wake = threading.Event()
        self.last_activity_ts = time.monotonic()

    def stop(self):
        self._stop = True
        self._wake.set()
        try:
            if self._sio is not None:
                self._sio.disconnect()
        except Exception:
            pass

    @staticmethod
    def _payload(data):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                return data
        if isinstance(data, dict) and "guid" not in data and isinstance(data.get("data"), dict):
            return data["data"]
        return data

    def run(self):
        backoff = 1.0
        fails = 0
        attempt = 0
        while not self._stop:
            sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
            self._sio = sio

            @sio.event
            def connect():
                log.info("Socket connected")
                self.last_activity_ts = time.monotonic()
                self.connected.emit()

            @sio.event
            def disconnect():
                log.info("Socket disconnected")
                self.last_activity_ts = time.monotonic()
                self.disconnected.emit("Connection lost")

            @sio.event
            def connect_error(data):
                log.warning("Socket connect_error: %s", str(data)[:160])

            @sio.on("new-message")
            def _nm(data):
                self.last_activity_ts = time.monotonic()
                d = self._payload(data)
                if isinstance(d, dict):
                    self.new_message.emit(d)

            @sio.on("updated-message")
            def _um(data):
                self.last_activity_ts = time.monotonic()
                d = self._payload(data)
                if isinstance(d, dict):
                    self.updated_message.emit(d)

            @sio.on("message-send-error")
            def _se(data):
                self.send_error.emit(self._payload(data))

            @sio.on("chat-read-status-changed")
            def _rs(data):
                self.read_status.emit(self._payload(data))

            for ev in ("group-name-change", "participant-added",
                       "participant-removed", "participant-left"):
                sio.on(ev, lambda data, _ev=ev: self.chats_dirty.emit())

            try:
                parts = urlsplit(self.base)
                query = list(parse_qsl(parts.query, keep_blank_values=True))
                query.extend((("guid", self.password),
                              ("password", self.password)))
                url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                  urlencode(query), parts.fragment))
                attempt += 1
                transports = (["websocket"] if attempt % 2 == 0
                              else ["polling", "websocket"])
                log.info("Socket attempt %d via %s", attempt, transports)
                connected_at = time.monotonic()
                sio.connect(url, transports=transports, wait_timeout=10)
                # bounded wait: a stop request or a dead link always exits
                while not self._stop and sio.connected:
                    sio.sleep(1)
                lived = time.monotonic() - connected_at
                if lived >= 20:
                    backoff = 1.0
                    fails = 0
                elif not self._stop:
                    fails += 1
            except Exception as e:
                reason = f"{e.__class__.__name__}: {str(e)[:120]}" if str(e) \
                    else e.__class__.__name__
                log.warning("Socket connect failed: %s", reason)
                fails += 1
                if fails == 3:
                    self.push_broken.emit(reason)
                self.disconnected.emit(reason)
            finally:
                try:
                    sio.disconnect()
                except Exception:
                    pass
                self._sio = None

            if self._stop:
                break
            # Unlike time.sleep, stop() can interrupt a minute-long backoff.
            self._wake.wait(backoff)
            self._wake.clear()
            backoff = min(backoff * 1.8, 60.0)
