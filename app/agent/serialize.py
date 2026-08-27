"""Wire helpers for the local agent channel.

Newline-delimited JSON. Events stay small on purpose: the window rebuilds
anything heavy (threads, attachments, chat rows) from the shared database,
which the workers always commit before an event is emitted. A batch event
therefore only carries what the window needs to react: which chats moved,
which messages are new, and enough text for the verify-line echo.
"""
import json
import logging

log = logging.getLogger(__name__)

MAX_LINE = 2_000_000  # defensive bound; events are normally a few KB


def encode(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), default=str).encode(
        "utf-8") + b"\n"


def feed(buffer: bytearray, chunk: bytes):
    """Append a chunk, yield every complete JSON line as a dict."""
    buffer.extend(chunk)
    out = []
    while True:
        idx = buffer.find(b"\n")
        if idx < 0:
            if len(buffer) > MAX_LINE:
                log.warning("Oversized agent-channel line dropped")
                buffer.clear()
            return out
        line = bytes(buffer[:idx])
        del buffer[:idx + 1]
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except Exception:
            log.warning("Undecodable agent-channel line dropped")
            continue
        if isinstance(value, dict):
            out.append(value)


def slim_message(m: dict, is_new, changed) -> dict:
    """Just the fields the window reacts to; never raw payloads."""
    return {
        "guid": m.get("guid"),
        "chat_guid": m.get("chat_guid"),
        "date_created": m.get("date_created") or 0,
        "is_from_me": 1 if m.get("is_from_me") else 0,
        "text": m.get("text"),
        "is_new": bool(is_new),
        "changed": bool(changed),
    }


def slim_batch(items) -> list:
    out = []
    for item in items or []:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        m = item[0]
        if not isinstance(m, dict):
            continue
        changed = bool(item[2]) if len(item) > 2 else True
        out.append(slim_message(m, item[1], changed))
    return out
