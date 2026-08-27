"""Live pipeline harness: the real agent core against a mock BlueBubbles.

Run:  QT_QPA_PLATFORM=offscreen python tools/harness_pipeline.py

A real HTTP server plays the Mac. The real AgentCore boots against it
with its real workers (sync, reconcile, send, download, socket) and this
harness verifies, end to end and headless:

  1. Boot sync connects, freezes the notification floor, and imports the
     pre-existing history without creating stale notification events.
  2. A message that appears on the "Mac" afterwards is discovered by the
     ROWID reconciler, stored, broadcast as a batch event, and creates a
     durable delivery event (the popup/unread ledger) with no window.
  3. A send enqueued in the outbox goes out through the real SendThread,
     hits the documented route, and the outbox row completes to 'sent'.
  4. The Wake Mac state machine calls the documented restart route and
     reaches its watching phase.
  5. The live-push socket being unavailable (this mock has none) leaves
     the system fully functional on the 3-second checks, as designed.
  6. Self-conversation texts (v3.1.5): the Mac's account is learned from
     server/info, a text typed on the phone to yourself (isFromMe on
     every Apple device) creates a delivery event, and a self-text sent
     from THIS app never does, even after later reconciler rescans.
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

from app import config  # noqa: E402
from app.agent.core import AgentCore  # noqa: E402
from app.store.db import Database  # noqa: E402
from app.store.repo import Repo  # noqa: E402

CHAT = "iMessage;-;+15555550100"
SELF_CHAT = "iMessage;-;user@icloud.test"
NOW = int(time.time() * 1000)
failures = []


def check(label, condition):
    print(f"  {'ok ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


class MacState:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = [self.make(1, "history: already on the Mac")]
        self.sent = []
        self.restarts = 0

    @staticmethod
    def make(rowid, text, from_me=False, chat=CHAT,
             address="+15555550100"):
        return {
            "guid": f"mac-{rowid}",
            "originalROWID": rowid,
            "dateCreated": NOW + rowid,
            "isFromMe": from_me,
            "text": text,
            "chats": [{"guid": chat}],
            "handle": {"address": address},
            "attachments": [],
        }

    def add(self, text, from_me=False, chat=CHAT,
            address="+15555550100"):
        with self.lock:
            rowid = max(r["originalROWID"] for r in self.rows) + 1
            self.rows.append(
                self.make(rowid, text, from_me, chat, address))
            return rowid


MAC = MacState()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, data, status=200):
        body = json.dumps({"status": status, "data": data}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/v1/ping":
            self._reply("pong")
        elif path == "/api/v1/server/info":
            self._reply({"private_api": False,
                         "detected_icloud": "user@icloud.test"})
        elif path == "/api/v1/contact":
            self._reply([])
        else:
            self._reply({"message": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        if path == "/api/v1/chat/query":
            self._reply([{"guid": CHAT,
                          "participants": [{"address": "+15555550100"}],
                          "lastMessage": {"dateCreated": NOW}}])
        elif path == "/api/v1/message/query":
            self._reply(self.query_messages(body))
        elif path == "/api/v1/message/text":
            with MAC.lock:
                MAC.sent.append(body)
                target = body.get("chatGuid") or CHAT
                rowid = max(r["originalROWID"] for r in MAC.rows) + 1
                echo = MacState.make(rowid, body.get("message") or "",
                                     from_me=True, chat=target,
                                     address=target.split(";")[-1])
                echo["guid"] = f"sent-{rowid}"
                MAC.rows.append(echo)
            self._reply(dict(echo, chatGuid=target))
        elif path == "/api/v1/mac/imessage/restart":
            with MAC.lock:
                MAC.restarts += 1
            self._reply({"success": True})
        else:
            self._reply({"message": "not found"}, 404)

    @staticmethod
    def query_messages(body):
        with MAC.lock:
            rows = list(MAC.rows)
        where = body.get("where") or []
        statement = (where[0].get("statement") if where else "") or ""
        args = (where[0].get("args") if where else {}) or {}
        if "MAX(ROWID)" in statement:
            newest = max(rows, key=lambda r: r["originalROWID"], default=None)
            return [newest] if newest else []
        if "message.ROWID >" in statement:
            low, high = int(args.get("low", 0)), int(args.get("high", 0))
            return [r for r in rows if low < r["originalROWID"] <= high]
        match = re.search(r"message.guid = :guid", statement)
        if match:
            return [r for r in rows if r["guid"] == args.get("guid")]
        if body.get("after") is not None:
            rows = [r for r in rows if r["dateCreated"] >= body["after"]]
        if body.get("chatGuid"):
            rows = [r for r in rows
                    if any(c["guid"] == body["chatGuid"]
                           for c in r["chats"])]
        rows.sort(key=lambda r: r["dateCreated"],
                  reverse=(body.get("sort", "DESC").upper() == "DESC"))
        return rows[:int(body.get("limit") or 100)]


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

app = QCoreApplication(sys.argv)
db = Database(constants.DB_PATH)
repo = Repo(db)
settings = config.Settings(server_url=f"http://127.0.0.1:{port}",
                           self_addresses="+1 (587) 555-0123")
settings._password_fallback = "pw"
events = []
core = AgentCore(repo, settings)
core.broadcast.connect(events.append)


def pump_until(predicate, seconds, label):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            check(label, True)
            return True
        time.sleep(0.02)
    check(label, False)
    return False


print("== live pipeline against a mock BlueBubbles ==")
core.start_backend()

# 1. Boot: history imported, no stale notification events.
pump_until(lambda: db.one(
    "SELECT 1 FROM messages WHERE guid='mac-1'") is not None, 20,
    "boot sync imports pre-existing history")
pump_until(lambda: repo.meta_int("rowid_sync_supported", 0) == 1, 20,
    "ROWID reconciliation negotiates as supported")
check("history created no notification events",
      repo.pending_delivery_events() == [])
pump_until(lambda: repo.meta_int("notification_baseline_complete", 0) == 1,
           20, "notification baseline arms after first complete pass")

# 2. A new message appears on the Mac afterwards.
MAC.add("new text arriving while no window is open")
pump_until(lambda: db.one(
    "SELECT 1 FROM messages WHERE guid='mac-2'") is not None, 20,
    "reconciler discovers the new Mac row within its cycles")
pump_until(lambda: any(
    e.get("event") == "batch"
    and any(i.get("guid") == "mac-2" for i in e.get("items") or [])
    for e in events), 5,
    "the new message is broadcast as a batch event")
pump_until(lambda: any(
    ev["message_guid"] == "mac-2"
    for ev in repo.pending_delivery_events()), 5,
    "a durable delivery event awaits the next window (popup/unread ledger)")

# 3. A send through the durable outbox and the real send worker.
oid = repo.enqueue(CHAT, "sent from the harness", None)
core.handle_command({"cmd": "submit_outbox", "id": oid}, lambda _:.0)
pump_until(lambda: (repo.outbox_row(oid) or {"state": ""})["state"] == "sent",
           20, "outbox row completes to 'sent' through the real worker")
check("the documented text route was hit",
      any(b.get("message") == "sent from the harness" for b in MAC.sent))
pump_until(lambda: db.one(
    "SELECT 1 FROM messages WHERE text='sent from the harness'") is not None,
    5, "the server echo of the send is stored")

# 4. Wake Mac against the documented restart route.
check("wake accepted", core.wake_mac(origin="manual"))
pump_until(lambda: core._wake_state == "watching", 20,
           "wake reaches its watching phase")
check("the restart route was called exactly once", MAC.restarts == 1)
core._cancel_wake()

# 5. Push has never connected against this mock; polls carry everything.
check("socket remained down throughout (poll path proven)",
      core._socket_up is False)

# 6. Self-conversation texts. Apple marks a text to your own number or
#    email as sent by you on every device; here it is an arrival.
check("the Mac's reported account became a self identity",
      repo.is_self_chat(SELF_CHAT))
check("the extra address from Settings became a self identity",
      repo.is_self_chat("iMessage;-;+15875550123"))
check("an ordinary contact's chat is never treated as self",
      not repo.is_self_chat(CHAT))
MAC.add("note to self typed on the phone", from_me=True,
        chat=SELF_CHAT, address="user@icloud.test")
pump_until(lambda: any(
    ev["chat_guid"] == SELF_CHAT
    for ev in repo.pending_delivery_events()), 20,
    "a phone-typed self text creates a delivery event despite isFromMe")
sid = repo.enqueue(SELF_CHAT, "note to self sent from this app", None)
core.handle_command({"cmd": "submit_outbox", "id": sid}, lambda _: 0)
pump_until(lambda: (repo.outbox_row(sid) or {"state": ""})["state"] == "sent",
           20, "a self-chat send completes through the real worker")
_row = repo.outbox_row(sid)
app_guid = _row["server_guid"] if _row else None
# Let the reconciler rescan the echoed row at least once (3 s cadence)
# before asserting silence; the recorded marker must hold under re-reads.
deadline = time.monotonic() + 7
while time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.02)
check("the app-sent self text never created a delivery event",
      app_guid is not None and not any(
          ev["message_guid"] == app_guid
          for ev in repo.pending_delivery_events()))
marker = db.one("SELECT delivery_event_recorded AS r FROM messages "
                "WHERE guid=?", (app_guid,)) if app_guid else None
check("the app-sent row carries the recorded marker against rescans",
      bool(marker and marker["r"]))

core.shutdown()
app.processEvents()
server.shutdown()

print()
if failures:
    print(f"HARNESS FAILED: {len(failures)} check(s)")
    raise SystemExit(1)
print("PIPELINE HARNESS PASSED")
