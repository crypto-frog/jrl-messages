"""Local channel server inside the agent process.

QLocalServer is a named pipe on Windows, restricted to the current user
via UserAccessOption; on POSIX development machines it is a socket file.
Protocol: newline-delimited JSON both ways (see serialize.py). Every
connected window receives every broadcast; commands arrive as dicts and
are dispatched by the core.
"""
import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .. import constants
from . import serialize

log = logging.getLogger(__name__)


class AgentServer(QObject):
    command = Signal(dict, object)   # payload, client socket
    client_connected = Signal(object)

    def __init__(self, name: str = None, parent=None):
        super().__init__(parent)
        self.name = name or constants.agent_pipe_name()
        self._clients: list[QLocalSocket] = []
        self._buffers: dict[int, bytearray] = {}
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.UserAccessOption)
        # A crashed previous agent can leave a stale endpoint behind.
        QLocalServer.removeServer(self.name)
        if not self._server.listen(self.name):
            raise RuntimeError(
                f"Agent channel could not listen: {self._server.errorString()}")
        self._server.newConnection.connect(self._on_new_connection)
        log.info("Agent channel listening at %s", self.name)

    def close(self):
        for c in list(self._clients):
            try:
                c.disconnectFromServer()
            except Exception:
                pass
        try:
            self._server.close()
            QLocalServer.removeServer(self.name)
        except Exception:
            pass

    # ------------------------------------------------ connections

    def _on_new_connection(self):
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            if sock is None:
                return
            self._clients.append(sock)
            self._buffers[id(sock)] = bytearray()
            sock.readyRead.connect(lambda s=sock: self._on_ready(s))
            sock.disconnected.connect(lambda s=sock: self._drop(s))
            log.info("Window connected (%d client(s))", len(self._clients))
            self.client_connected.emit(sock)

    def _drop(self, sock):
        self._buffers.pop(id(sock), None)
        self._clients = [c for c in self._clients if c is not sock]
        try:
            sock.deleteLater()
        except Exception:
            pass
        log.info("Window disconnected (%d client(s))", len(self._clients))

    def _on_ready(self, sock):
        buf = self._buffers.get(id(sock))
        if buf is None:
            return
        chunk = bytes(sock.readAll().data())
        for payload in serialize.feed(buf, chunk):
            try:
                self.command.emit(payload, sock)
            except Exception:
                log.exception("Command dispatch failed")

    # ------------------------------------------------ sending

    def send(self, sock, payload: dict):
        try:
            if sock is not None and sock.state() == QLocalSocket.ConnectedState:
                sock.write(serialize.encode(payload))
        except Exception:
            log.exception("Send to window failed")

    def broadcast(self, payload: dict):
        data = serialize.encode(payload)
        for c in list(self._clients):
            try:
                if c.state() == QLocalSocket.ConnectedState:
                    c.write(data)
            except Exception:
                log.exception("Broadcast to a window failed")

    def client_count(self) -> int:
        return len(self._clients)
