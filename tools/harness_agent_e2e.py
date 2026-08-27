"""End-to-end agent-channel harness: a real AgentCore and AgentServer in
one process, exercised by a real QLocalSocket client.

Run:  QT_QPA_PLATFORM=offscreen python tools/harness_agent_e2e.py

Covers the live path unit tests cannot: hello on connect, command
dispatch into the core, event fan-out back to the client, the wake
command refusing cleanly with no configured backend, durable outbox
enqueue plus kick_outbox handling, and stop_agent ending the loop.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
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

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtNetwork import QLocalSocket  # noqa: E402

from app import config  # noqa: E402
from app.agent import serialize  # noqa: E402
from app.agent.core import AgentCore  # noqa: E402
from app.agent.server import AgentServer  # noqa: E402
from app.store.db import Database  # noqa: E402
from app.store.repo import Repo  # noqa: E402

PIPE = f"jrl-harness-{os.getpid()}"
failures = []


def check(label, condition):
    print(f"  {'ok ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


app = QCoreApplication(sys.argv)
db = Database(constants.DB_PATH)
repo = Repo(db)
settings = config.Settings()          # no server configured on purpose
core = AgentCore(repo, settings)
server = AgentServer(name=PIPE)
core.broadcast.connect(server.broadcast)
server.command.connect(
    lambda payload, sock: core.handle_command(
        payload, lambda answer: server.send(sock, answer)))
server.client_connected.connect(
    lambda sock: server.send(sock, core.hello()))

received = []
buffer = bytearray()
client = QLocalSocket()
client.readyRead.connect(
    lambda: received.extend(
        serialize.feed(buffer, bytes(client.readAll().data()))))
client.connectToServer(PIPE)


def pump(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def wait_for(predicate, seconds, label):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            check(label, True)
            return True
        time.sleep(0.01)
    check(label, False)
    return False


print("== agent channel end to end ==")
wait_for(lambda: client.state() == QLocalSocket.ConnectedState, 3,
         "client connects to the agent channel")
wait_for(lambda: any(p.get("event") == "hello" for p in received), 3,
         "hello arrives on connect")
hello = next(p for p in received if p.get("event") == "hello")
check("hello reports version", hello.get("version") == constants.VERSION)
check("hello reports unconfigured backend", hello.get("connected") is False)
check("hello reports auto-wake default",
      hello.get("auto_wake_minutes") == constants.AUTO_WAKE_DEFAULT_MIN)

# The backend cannot start with no server settings; the status broadcast
# must say so rather than crash.
core.start_backend()
wait_for(lambda: any(p.get("event") == "status"
                     and "Settings" in (p.get("text") or "")
                     for p in received), 3,
         "unconfigured start broadcasts a settings status")

# A wake with no backend must refuse with a clean status, never a crash.
received.clear()
client.write(serialize.encode({"cmd": "wake"}))
wait_for(lambda: any(p.get("event") == "status"
                     and "Not connected" in (p.get("text") or "")
                     for p in received), 3,
         "wake without a backend refuses cleanly")

# Durable sending: enqueue in the shared database, then kick_outbox. With
# no sender worker the row must simply stay queued; nothing is lost.
oid = repo.enqueue("iMessage;-;+15555550100", "queued while offline", None)
client.write(serialize.encode({"cmd": "kick_outbox"}))
pump(0.3)
row = repo.outbox_row(oid)
check("queued send survives kick with no backend",
      row is not None and row["state"] == "queued")

# Command fan-out: a status request answers only this client.
received.clear()
client.write(serialize.encode({"cmd": "status"}))
wait_for(lambda: any(p.get("event") == "hello" for p in received), 3,
         "status command answers with hello")

# Broadcast fan-out from the core reaches the client.
received.clear()
core.set_status("ok", "harness broadcast line")
wait_for(lambda: any(p.get("text") == "harness broadcast line"
                     for p in received), 3,
         "core broadcasts reach the client")

# Unknown commands are ignored without killing the channel.
client.write(serialize.encode({"cmd": "definitely-not-a-command"}))
client.write(serialize.encode({"cmd": "status"}))
received.clear()
wait_for(lambda: any(p.get("event") == "hello" for p in received), 3,
         "channel survives an unknown command")

# stop_agent must end the real event loop, exactly as in production where
# main() sits in app.exec(). A failsafe timer distinguishes success from
# a hang: if it fires first, stop_agent did not quit the loop.
from PySide6.QtCore import QTimer  # noqa: E402
marker = {"timeout": False}
QTimer.singleShot(3000, lambda: (marker.update(timeout=True), app.quit()))
client.write(serialize.encode({"cmd": "stop_agent"}))
app.exec()
check("stop_agent quits the agent loop", not marker["timeout"])

core.shutdown()
server.close()
client.abort()

print()
if failures:
    print(f"HARNESS FAILED: {len(failures)} check(s)")
    raise SystemExit(1)
print("AGENT E2E HARNESS PASSED")
