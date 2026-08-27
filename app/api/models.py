"""Parsers from BlueBubbles server JSON to local DB records.
Every timestamp is normalized to unix ms here and nowhere else.
Unknown fields are preserved in the raw column for forward compatibility."""
import json
import re
from typing import Optional

from ..util.timefmt import normalize_ts

_ASSOC_PREFIX = re.compile(r"^(?:p:\d+/|bp:)")


def _positive_int(value) -> Optional[int]:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def parse_message(d: dict) -> Optional[dict]:
    guid = d.get("guid")
    if not guid:
        return None
    chats = d.get("chats") or []
    chat_guid = (chats[0].get("guid") if chats and isinstance(chats[0], dict)
                 else d.get("chatGuid"))
    if not chat_guid:
        return None

    # A missing creation date is characteristic of a sparse update payload,
    # not a complete message.  Inventing "now" used to poison the global
    # timestamp cursor and could move an old message to the head of a thread.
    # The caller will ask the REST reconciliation path for the full record.
    date_created = normalize_ts(d.get("dateCreated"))
    if date_created is None:
        return None

    handle = d.get("handle") or {}
    assoc = d.get("associatedMessageGuid") or None
    if assoc:
        assoc = _ASSOC_PREFIX.sub("", assoc)

    service = None
    if ";" in chat_guid:
        service = chat_guid.split(";", 1)[0]

    text = (d.get("text") or "").replace("\ufffc", "").strip() or None

    atts = []
    for a in d.get("attachments") or []:
        if not isinstance(a, dict) or not a.get("guid"):
            continue
        atts.append({
            "guid": a["guid"],
            "message_guid": guid,
            "mime_type": a.get("mimeType"),
            "file_name": a.get("transferName") or "attachment",
            "total_bytes": a.get("totalBytes"),
            "width": a.get("width"),
            "height": a.get("height"),
        })

    raw = {k: v for k, v in d.items() if k not in ("attachments", "chats")}
    return {
        # Merge logic uses this to avoid treating an omitted boolean/numeric
        # field in a sparse update event as an authoritative zero.
        "_present_fields": frozenset(d.keys()),
        "guid": guid,
        "source_rowid": _positive_int(d.get("originalROWID") or d.get("ROWID")),
        "chat_guid": chat_guid,
        "sender_address": handle.get("address"),
        "is_from_me": 1 if d.get("isFromMe") else 0,
        "text": text,
        "subject": d.get("subject") or None,
        "service": service,
        "date_created": date_created,
        "date_delivered": normalize_ts(d.get("dateDelivered")),
        "date_read": normalize_ts(d.get("dateRead")),
        "is_edited": 1 if d.get("dateEdited") else 0,
        "is_retracted": 1 if d.get("dateRetracted") else 0,
        "thread_originator_guid": d.get("threadOriginatorGuid") or None,
        "associated_guid": assoc,
        "associated_type": d.get("associatedMessageType") or None,
        "item_type": d.get("itemType") or 0,
        "error": d.get("error") or 0,
        "raw": json.dumps(raw, default=str),
        "attachments": atts,
    }


def parse_chat(d: dict) -> Optional[dict]:
    guid = d.get("guid")
    if not guid:
        return None
    parts = [p.get("address") for p in d.get("participants") or []
             if isinstance(p, dict) and p.get("address")]
    is_group = 1 if (";+;" in guid or len(parts) > 1) else 0
    last = d.get("lastMessage") or {}
    return {
        "guid": guid,
        "display_name": (d.get("displayName") or "").strip() or None,
        "is_group": is_group,
        "participants": json.dumps(parts),
        "last_activity": normalize_ts(last.get("dateCreated")),
        "archived": 1 if d.get("isArchived") else 0,
    }


def parse_contacts(items: list) -> dict:
    """Return {address: display name} across phones and emails."""
    out = {}
    for c in items or []:
        if not isinstance(c, dict):
            continue
        name = (c.get("displayName")
                or " ".join(x for x in (c.get("firstName"), c.get("lastName")) if x)
                or c.get("nickname") or "").strip()
        if not name:
            continue
        for key in ("phoneNumbers", "emails"):
            for entry in c.get(key) or []:
                addr = entry.get("address") if isinstance(entry, dict) else entry
                if addr:
                    out[str(addr)] = name
    return out
