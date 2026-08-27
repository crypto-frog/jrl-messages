"""Domain-level reads and writes over the local cache.
Everything the UI or sync engine needs goes through here."""
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from .db import Database
from ..util.textutil import normalize_address

log = logging.getLogger(__name__)

# Tapback rows (adds 2000-2005, removals 3000-3005, stickers around 1000)
# are stored but never rendered as bubbles.
_NOT_BUBBLE = "(associated_type IS NULL OR associated_type < 1000 OR associated_type > 3999)"


@dataclass(frozen=True)
class UpsertResult:
    """Outcome of an idempotent server-message merge."""

    is_new: bool
    changed: bool
    event_pending: bool = False

    def __bool__(self):
        # Preserve the intuitive/legacy truth value while callers migrate to
        # the explicit fields.
        return self.is_new


class Repo:
    def __init__(self, db: Database):
        self.db = db
        # Self-conversation alerting. Apple marks a text you send yourself
        # as sent by you on every device, so it looks outgoing here even
        # though it arrived. When enabled, messages in a 1:1 conversation
        # with one of the user's own addresses are treated as incoming for
        # the alert ledger. Identities live in settings plus the account
        # the Mac reports; the learned set is persisted in meta so alerts
        # work from the first moment after a restart.
        self._self_alerts_enabled = True
        self._self_norms: set[str] = set()
        stored = self.meta("self_address_norms") or ""
        self._self_norms = {n for n in stored.split(",") if n}

    def set_self_identities(self, norms, enabled: bool) -> None:
        self._self_alerts_enabled = bool(enabled)
        clean = {n for n in (norms or set()) if n}
        if clean != self._self_norms:
            self._self_norms = clean
            self.set_meta("self_address_norms", ",".join(sorted(clean)))

    def add_self_identity(self, norm: str) -> None:
        if norm and norm not in self._self_norms:
            self.set_self_identities(
                self._self_norms | {norm}, self._self_alerts_enabled)

    def is_self_chat(self, chat_guid: str) -> bool:
        """A 1:1 conversation with one of the user's own addresses."""
        if not (self._self_alerts_enabled and self._self_norms
                and chat_guid):
            return False
        if ";+;" in chat_guid:
            return False
        tail = chat_guid.split(";")[-1]
        return normalize_address(tail) in self._self_norms

    # ---------- small durable key/value state ----------

    def meta(self, key: str, default=None):
        row = self.db.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row is not None else default

    def meta_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.meta(key, default))
        except (TypeError, ValueError):
            return default

    def set_meta(self, key: str, value) -> None:
        self.db.write(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))

    def set_meta_min(self, key: str, value: int) -> int:
        """Atomically retain the smallest integer ever proposed for key."""
        def txn(c):
            row = c.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            try:
                current = int(row["value"]) if row is not None else None
            except (TypeError, ValueError):
                current = None
            chosen = int(value) if current is None else min(current, int(value))
            c.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(chosen)))
            return chosen
        return self.db.txn(txn)

    # ---------- contacts / handles ----------

    def upsert_contacts(self, mapping: dict):
        def txn(c):
            for addr, name in mapping.items():
                c.execute(
                    "INSERT INTO handles(address, norm, display_name) VALUES(?,?,?) "
                    "ON CONFLICT(address) DO UPDATE SET norm=excluded.norm, "
                    "display_name=excluded.display_name",
                    (addr, normalize_address(addr), name))
        self.db.txn(txn)

    def handles_map(self) -> dict:
        out = {}
        for r in self.db.query("SELECT norm, display_name FROM handles"):
            if r["norm"] and r["display_name"]:
                out[r["norm"]] = r["display_name"]
        return out

    def contacts_all(self) -> list:
        """Every (name, address) pair from the synced address book."""
        return [(r["display_name"], r["address"]) for r in self.db.query(
            "SELECT display_name, address FROM handles "
            "WHERE display_name IS NOT NULL ORDER BY display_name, address")]

    def chat_for_address(self, norm: str) -> Optional[str]:
        """Existing 1:1 chat guid for a normalized address, preferring
        iMessage over SMS, then most recent activity."""
        best = None
        for r in self.db.query(
                "SELECT guid, participants, last_activity FROM chats "
                "WHERE is_group=0"):
            try:
                parts = json.loads(r["participants"] or "[]")
            except Exception:
                continue
            if len(parts) != 1 or normalize_address(parts[0]) != norm:
                continue
            score = ((1 if r["guid"].startswith("iMessage") else 0),
                     r["last_activity"] or 0)
            if best is None or score > best[0]:
                best = (score, r["guid"])
        return best[1] if best else None

    def participants_of(self, chat_guid: str) -> list:
        r = self.db.one("SELECT participants FROM chats WHERE guid=?",
                        (chat_guid,))
        if not r:
            return []
        try:
            return json.loads(r["participants"] or "[]")
        except Exception:
            return []

    def set_participants(self, chat_guid: str, addresses: list):
        self.db.write("UPDATE chats SET participants=? WHERE guid=?",
                      (json.dumps(addresses), chat_guid))

    def group_for_addresses(self, norms: set) -> Optional[str]:
        """Existing group whose membership matches exactly, latest first."""
        best = None
        for r in self.db.query(
                "SELECT guid, participants, last_activity FROM chats "
                "WHERE is_group=1"):
            try:
                parts = json.loads(r["participants"] or "[]")
            except Exception:
                continue
            if {normalize_address(p) for p in parts} != norms:
                continue
            score = r["last_activity"] or 0
            if best is None or score > best[0]:
                best = (score, r["guid"])
        return best[1] if best else None

    def unread_total(self) -> int:
        r = self.db.one("SELECT COALESCE(SUM(unread),0) AS n FROM chats "
                        "WHERE archived=0 AND COALESCE(hidden,0)=0")
        return int(r["n"]) if r else 0

    def mark_all_read(self):
        def txn(c):
            c.execute("UPDATE chats SET unread=0 WHERE archived=0")
            c.execute(
                "UPDATE delivery_events SET unread_done=1 "
                "WHERE unread_done=0 AND chat_guid IN "
                "(SELECT guid FROM chats WHERE archived=0)")
        self.db.txn(txn)

    def first_unread_chat(self, exclude_guid=None):
        rows = self.db.query(
            "SELECT guid FROM chats WHERE archived=0 AND "
            "COALESCE(hidden,0)=0 AND unread>0 "
            "ORDER BY COALESCE(last_activity,0) DESC")
        guids = [r["guid"] for r in rows]
        for g in guids:
            if g != exclude_guid:
                return g
        return guids[0] if guids else None

    def name_for(self, address: Optional[str], handles: dict) -> str:
        if not address:
            return "Unknown"
        return handles.get(normalize_address(address), address)

    # ---------- chats ----------

    def upsert_chat(self, ch: dict):
        def txn(c):
            c.execute(
                "INSERT INTO chats(guid, display_name, is_group, participants, "
                "last_activity, archived) VALUES(:guid,:display_name,:is_group,"
                ":participants,:last_activity,:archived) "
                "ON CONFLICT(guid) DO UPDATE SET display_name=excluded.display_name, "
                "is_group=excluded.is_group, participants=excluded.participants, "
                "archived=excluded.archived, "
                "last_activity=MAX(COALESCE(chats.last_activity,0), "
                "COALESCE(excluded.last_activity,0))",
                ch)
        self.db.txn(txn)

    def ensure_chat(self, guid: str):
        self.db.write(
            "INSERT OR IGNORE INTO chats(guid, is_group, participants) VALUES(?,?,?)",
            (guid, 1 if ";+;" in guid else 0, "[]"))

    def chats(self) -> list:
        return self.db.query(
            "SELECT c.*, "
            " (SELECT m.text FROM messages m WHERE m.chat_guid=c.guid AND m.item_type=0 "
            f"  AND {_NOT_BUBBLE} ORDER BY m.date_created DESC LIMIT 1) AS last_text, "
            " (SELECT m.is_from_me FROM messages m WHERE m.chat_guid=c.guid AND m.item_type=0 "
            f"  AND {_NOT_BUBBLE} ORDER BY m.date_created DESC LIMIT 1) AS last_from_me, "
            " (SELECT a.file_name FROM attachments a JOIN messages m2 ON a.message_guid=m2.guid "
            "  WHERE m2.chat_guid=c.guid ORDER BY m2.date_created DESC LIMIT 1) AS last_attach "
            "FROM chats c WHERE c.archived=0 AND COALESCE(c.hidden,0)=0 "
            "ORDER BY COALESCE(c.last_activity,0) DESC")

    def chat_title(self, row, handles: dict) -> str:
        if row["display_name"]:
            return row["display_name"]
        try:
            parts = json.loads(row["participants"] or "[]")
        except Exception:
            parts = []
        names = [self.name_for(p, handles) for p in parts]
        if not names:
            tail = (row["guid"].split(";")[-1]) if row["guid"] else "Conversation"
            return tail or "Conversation"
        if row["is_group"]:
            short = [n.split(" ")[0] for n in names]
            return ", ".join(short[:4]) + ("\u2026" if len(short) > 4 else "")
        return names[0]

    def hide_chat(self, chat_guid: str):
        """Hiding acknowledges: the conversation leaves the list and its
        unread leaves the counter together."""
        def txn(c):
            c.execute(
                "UPDATE chats SET hidden=1, unread=0 WHERE guid=?",
                (chat_guid,))
            # A notification drain already queued on the GUI thread must not
            # resurrect unread after the user intentionally hides the chat.
            c.execute(
                "UPDATE delivery_events SET unread_done=1 "
                "WHERE chat_guid=? AND unread_done=0", (chat_guid,))
        self.db.txn(txn)

    def unhide_chat(self, chat_guid: str):
        self.db.write("UPDATE chats SET hidden=0 WHERE guid=?", (chat_guid,))

    def is_hidden(self, chat_guid: str) -> bool:
        r = self.db.one("SELECT hidden FROM chats WHERE guid=?", (chat_guid,))
        return bool(r and r["hidden"])

    def hidden_chats(self) -> list:
        return self.db.query(
            "SELECT * FROM chats WHERE COALESCE(hidden,0)=1 AND archived=0 "
            "ORDER BY COALESCE(last_activity,0) DESC")

    def mark_read(self, chat_guid: str):
        def txn(c):
            c.execute("UPDATE chats SET unread=0 WHERE guid=?", (chat_guid,))
            # A delayed GUI drain must not resurrect unread after the user has
            # explicitly acknowledged this conversation.
            c.execute(
                "UPDATE delivery_events SET unread_done=1 "
                "WHERE chat_guid=? AND unread_done=0", (chat_guid,))
        self.db.txn(txn)

    def bump_unread(self, chat_guid: str):
        self.db.write("UPDATE chats SET unread=unread+1 WHERE guid=?", (chat_guid,))

    # ---------- messages ----------

    def existing_message_guids(self, guids: list[str]) -> set[str]:
        """Return known GUIDs for a bounded reconciliation page."""
        clean = list(dict.fromkeys(g for g in guids if g))
        if not clean:
            return set()
        placeholders = ",".join("?" for _ in clean)
        return {
            row["guid"] for row in self.db.query(
                f"SELECT guid FROM messages WHERE guid IN ({placeholders})",
                clean)
        }

    def upsert_message(self, m: dict, *, notify_eligible: bool = False,
                       allow_existing_event: bool = False,
                       complete_outbox_id: Optional[int] = None) -> UpsertResult:
        """Atomically merge one authoritative message and its attachments.

        ``notify_eligible`` creates a durable incoming-delivery event in the
        same transaction.  ``allow_existing_event`` is used for a real socket
        event: if startup catch-up won the GUID race first, the live event can
        still create the one missing notification record.
        """
        atts = m.get("attachments") or []
        now_ms = int(time.time() * 1000)
        present = m.get("_present_fields") or frozenset()
        values = {
            k: v for k, v in m.items()
            if k != "attachments" and not k.startswith("_")
        }
        values["first_seen_ms"] = now_ms
        values["has_is_from_me"] = int("isFromMe" in present)
        values["has_item_type"] = int("itemType" in present)
        values["has_error"] = int("error" in present)
        compare_keys = (
            "source_rowid", "chat_guid", "sender_address", "is_from_me",
            "text", "subject", "service", "date_created", "date_delivered",
            "date_read", "is_edited", "is_retracted",
            "thread_originator_guid", "associated_guid", "associated_type",
            "item_type", "error",
        )

        def txn(c):
            existing = c.execute(
                "SELECT * FROM messages WHERE guid=?", (m["guid"],)).fetchone()
            is_new = existing is None
            if existing is None:
                changed = True
            else:
                changed = any(
                    values.get(k) is not None
                    and not (
                        (k == "is_from_me" and "isFromMe" not in present)
                        or (k == "item_type" and "itemType" not in present)
                        or (k == "error" and "error" not in present)
                        or (k == "is_edited" and "dateEdited" not in present)
                        or (k == "is_retracted"
                            and "dateRetracted" not in present)
                    )
                    and existing[k] != values.get(k)
                    for k in compare_keys)
                current_atts = {
                    r["guid"]: r for r in c.execute(
                        "SELECT * FROM attachments WHERE message_guid=?",
                        (m["guid"],)).fetchall()
                }
                for attachment in atts:
                    old = current_atts.get(attachment["guid"])
                    if old is None or any(
                            attachment.get(k) is not None
                            and old[k] != attachment.get(k)
                            for k in ("mime_type", "file_name", "total_bytes",
                                      "width", "height")):
                        changed = True
                        break

            c.execute(
                "INSERT INTO messages(guid, source_rowid, chat_guid, sender_address, "
                "is_from_me, text, subject, service, date_created, date_delivered, "
                "date_read, is_edited, is_retracted, thread_originator_guid, "
                "associated_guid, associated_type, item_type, error, raw, first_seen_ms) "
                "VALUES(:guid,:source_rowid,:chat_guid,:sender_address,:is_from_me,"
                ":text,:subject,:service,:date_created,:date_delivered,:date_read,"
                ":is_edited,:is_retracted,:thread_originator_guid,:associated_guid,"
                ":associated_type,:item_type,:error,:raw,:first_seen_ms) "
                "ON CONFLICT(guid) DO UPDATE SET "
                "source_rowid=COALESCE(excluded.source_rowid,messages.source_rowid), "
                "chat_guid=excluded.chat_guid, "
                "sender_address=COALESCE(excluded.sender_address,messages.sender_address), "
                "is_from_me=CASE WHEN :has_is_from_me=1 "
                "THEN excluded.is_from_me ELSE messages.is_from_me END, "
                "text=COALESCE(excluded.text,messages.text), "
                "subject=COALESCE(excluded.subject,messages.subject), "
                "service=COALESCE(excluded.service,messages.service), "
                "date_created=excluded.date_created, "
                "date_delivered=COALESCE(excluded.date_delivered,messages.date_delivered), "
                "date_read=COALESCE(excluded.date_read,messages.date_read), "
                "is_edited=MAX(messages.is_edited,excluded.is_edited), "
                "is_retracted=MAX(messages.is_retracted,excluded.is_retracted), "
                "thread_originator_guid=COALESCE(excluded.thread_originator_guid,"
                "messages.thread_originator_guid), "
                "associated_guid=COALESCE(excluded.associated_guid,messages.associated_guid), "
                "associated_type=COALESCE(excluded.associated_type,messages.associated_type), "
                "item_type=CASE WHEN :has_item_type=1 THEN excluded.item_type "
                "ELSE messages.item_type END, "
                "error=CASE WHEN :has_error=1 THEN excluded.error "
                "ELSE messages.error END, raw=excluded.raw",
                values)
            for a in atts:
                c.execute(
                    "INSERT INTO attachments(guid, message_guid, mime_type, file_name, "
                    "total_bytes, width, height) VALUES(:guid,:message_guid,:mime_type,"
                    ":file_name,:total_bytes,:width,:height) "
                    "ON CONFLICT(guid) DO UPDATE SET mime_type=excluded.mime_type, "
                    "file_name=excluded.file_name, total_bytes=excluded.total_bytes, "
                    "width=COALESCE(excluded.width,attachments.width), "
                    "height=COALESCE(excluded.height,attachments.height)", a)
            c.execute("INSERT OR IGNORE INTO chats(guid, is_group, participants) "
                      "VALUES(?,?, '[]')",
                      (m["chat_guid"], 1 if ";+;" in m["chat_guid"] else 0))
            c.execute("UPDATE chats SET last_activity="
                      "MAX(COALESCE(last_activity,0), ?) WHERE guid=?",
                      (m["date_created"], m["chat_guid"]))

            stored = c.execute(
                "SELECT is_from_me,item_type,associated_guid,"
                "delivery_event_recorded FROM messages "
                "WHERE guid=?", (m["guid"],)).fetchone()
            # A text the user sends to their own number or email is marked
            # by Apple as sent-by-you on every device, yet here it is an
            # arrival and must alert like one. Self-conversation rows are
            # therefore incoming for the ledger; texts sent from THIS app
            # never are, because their outbox completion below stamps the
            # recorded marker first.
            from_me_ok = (not bool(stored["is_from_me"])
                          if stored is not None else False) or \
                self.is_self_chat(m["chat_guid"])
            real_incoming = (
                notify_eligible and stored is not None
                and from_me_ok
                and not bool(stored["item_type"])
                and not bool(stored["associated_guid"]))
            if (real_incoming and not bool(stored["delivery_event_recorded"])
                    and (is_new or allow_existing_event)):
                c.execute(
                    "INSERT OR IGNORE INTO delivery_events("
                    "message_guid,chat_guid,first_seen_ms) VALUES(?,?,?)",
                    (m["guid"], m["chat_guid"], now_ms))
                c.execute(
                    "UPDATE messages SET delivery_event_recorded=1 "
                    "WHERE guid=?", (m["guid"],))
            pending = c.execute(
                "SELECT 1 FROM delivery_events WHERE message_guid=? "
                "AND notification_done=0", (m["guid"],)).fetchone() is not None
            if complete_outbox_id is not None:
                c.execute(
                    "UPDATE outbox SET state='sent',server_guid=?,"
                    "last_error=NULL WHERE id=?",
                    (m["guid"], complete_outbox_id))
                # This row left through our own composer; it must never
                # come back as an alert when a later rescan re-reads it.
                c.execute(
                    "UPDATE messages SET delivery_event_recorded=1 "
                    "WHERE guid=?", (m["guid"],))
            return UpsertResult(is_new=is_new, changed=changed,
                                event_pending=pending)

        return self.db.txn(txn)

    def messages_window(self, chat_guid: str, before_key, limit: int) -> list:
        """A stable local page ordered by the composite (date, GUID) key.

        Older builds used only ``date_created < boundary`` and permanently
        hid messages sharing the page-boundary millisecond.
        """
        before_ts = before_guid = None
        if isinstance(before_key, tuple):
            before_ts, before_guid = before_key
        elif before_key is not None:  # compatibility with an old caller
            before_ts, before_guid = int(before_key), ""
        rows = self.db.query(
            "SELECT m.* FROM messages m WHERE m.chat_guid=? AND m.item_type=0 "
            f"AND {_NOT_BUBBLE} AND (? IS NULL OR m.date_created < ? "
            "OR (m.date_created=? AND m.guid < ?)) "
            "ORDER BY m.date_created DESC, m.guid DESC LIMIT ?",
            (chat_guid, before_ts, before_ts, before_ts, before_guid, limit))
        return list(reversed(rows))

    def messages_around(self, chat_guid: str, center_ts: int, radius: int) -> list:
        older = self.db.query(
            "SELECT m.* FROM messages m WHERE m.chat_guid=? AND m.item_type=0 "
            f"AND {_NOT_BUBBLE} AND m.date_created <= ? "
            "ORDER BY m.date_created DESC, m.guid DESC LIMIT ?",
            (chat_guid, center_ts, radius))
        newer = self.db.query(
            "SELECT m.* FROM messages m WHERE m.chat_guid=? AND m.item_type=0 "
            f"AND {_NOT_BUBBLE} AND m.date_created > ? "
            "ORDER BY m.date_created ASC, m.guid ASC LIMIT ?",
            (chat_guid, center_ts, radius))
        return list(reversed(older)) + list(newer)

    def read_watermark(self, chat_guid: str):
        """(read_ts, anchor_created) of the newest from-me message Apple
        stamped as read; everything sent before anchor_created was
        necessarily read by read_ts."""
        r = self.db.one(
            "SELECT date_read, date_created FROM messages "
            "WHERE chat_guid=? AND is_from_me=1 AND date_read IS NOT NULL "
            "ORDER BY date_created DESC LIMIT 1", (chat_guid,))
        return (r["date_read"], r["date_created"]) if r else None

    def message_ts(self, guid: str) -> Optional[int]:
        r = self.db.one("SELECT date_created FROM messages WHERE guid=?", (guid,))
        return r["date_created"] if r else None

    def message_text(self, guid: str) -> Optional[str]:
        r = self.db.one("SELECT text FROM messages WHERE guid=?", (guid,))
        return r["text"] if r else None

    def attachments_for(self, guids: list) -> dict:
        if not guids:
            return {}
        ph = ",".join("?" for _ in guids)
        out = {}
        for r in self.db.query(
                f"SELECT * FROM attachments WHERE message_guid IN ({ph})", guids):
            out.setdefault(r["message_guid"], []).append(r)
        return out

    def tapbacks_for(self, guids: list) -> dict:
        """{target_guid: [(emoji_index, count), ...]} with removals applied."""
        if not guids:
            return {}
        ph = ",".join("?" for _ in guids)
        counts: dict = {}
        for r in self.db.query(
                "SELECT associated_guid AS g, associated_type AS t FROM messages "
                f"WHERE associated_guid IN ({ph}) "
                "AND associated_type BETWEEN 2000 AND 3999", guids):
            t = r["t"]
            if 2000 <= t <= 2005:
                key = (r["g"], t - 2000)
                counts[key] = counts.get(key, 0) + 1
            elif 3000 <= t <= 3005:
                key = (r["g"], t - 3000)
                counts[key] = counts.get(key, 0) - 1
        out: dict = {}
        for (g, idx), n in counts.items():
            if n > 0:
                out.setdefault(g, []).append((idx, n))
        return out

    def max_ts(self) -> Optional[int]:
        r = self.db.one("SELECT MAX(date_created) AS m FROM messages")
        return r["m"] if r and r["m"] else None

    def message_count(self, chat_guid: Optional[str] = None) -> int:
        if chat_guid:
            r = self.db.one(
                "SELECT COUNT(*) AS n FROM messages WHERE chat_guid=?",
                (chat_guid,))
        else:
            r = self.db.one("SELECT COUNT(*) AS n FROM messages")
        return int(r["n"]) if r else 0

    # ---------- durable incoming delivery ----------

    def pending_delivery_events(self, limit: int = 200) -> list:
        return self.db.query(
            "SELECT e.*, m.sender_address, m.is_from_me, m.text, "
            "m.date_created, m.associated_guid, m.item_type "
            "FROM delivery_events e JOIN messages m ON m.guid=e.message_guid "
            "WHERE e.notification_done=0 OR e.unread_done=0 "
            "ORDER BY e.first_seen_ms, e.message_guid LIMIT ?", (limit,))

    def apply_unread_event(self, message_guid: str, *, chat_is_open: bool) -> bool:
        """Apply an unread increment at most once.  Returns whether it bumped."""
        def txn(c):
            row = c.execute(
                "SELECT chat_guid,unread_done FROM delivery_events "
                "WHERE message_guid=?", (message_guid,)).fetchone()
            if row is None or row["unread_done"]:
                return False
            if not chat_is_open:
                c.execute("UPDATE chats SET unread=unread+1 WHERE guid=?",
                          (row["chat_guid"],))
            c.execute("UPDATE delivery_events SET unread_done=1 "
                      "WHERE message_guid=?", (message_guid,))
            return not chat_is_open
        return self.db.txn(txn)

    def finish_notification_event(self, message_guid: str) -> None:
        self.db.write(
            "UPDATE delivery_events SET notification_done=1 WHERE message_guid=?",
            (message_guid,))

    def prune_delivery_events(self, before_ms: int) -> int:
        """Delete only old ledger rows whose side effects both completed."""
        cur = self.db.write(
            "DELETE FROM delivery_events WHERE unread_done=1 "
            "AND notification_done=1 AND first_seen_ms<?", (int(before_ms),))
        return max(0, int(cur.rowcount or 0))

    # ---------- in-app notification center feed ----------

    def feed_add(self, kind: str, title: str, body: str = "",
                 chat_guid: Optional[str] = None,
                 message_guid: Optional[str] = None,
                 created_ms: Optional[int] = None) -> bool:
        """Append one row to the in-app notification center.

        Message rows carry their GUID and deduplicate durably, so ledger
        sweeps and popup retries can call this blindly. Returns whether a
        new row was actually created."""
        now = int(time.time() * 1000)
        cur = self.db.write(
            "INSERT OR IGNORE INTO feed(kind,title,body,chat_guid,"
            "message_guid,created_ms) VALUES(?,?,?,?,?,?)",
            (kind, (title or "Notification")[:200], (body or "")[:400],
             chat_guid, message_guid, int(created_ms or now)))
        return int(cur.rowcount or 0) == 1

    def feed_recent(self, limit: int = 60) -> list:
        return self.db.query(
            "SELECT * FROM feed WHERE hidden=0 "
            "ORDER BY created_ms DESC, id DESC LIMIT ?", (int(limit),))

    def feed_unseen_count(self) -> int:
        r = self.db.one(
            "SELECT COUNT(*) AS n FROM feed WHERE hidden=0 AND seen=0")
        return int(r["n"]) if r else 0

    def feed_mark_all_seen(self) -> None:
        self.db.write("UPDATE feed SET seen=1 WHERE seen=0 AND hidden=0")

    def feed_hide(self, feed_id: int) -> None:
        """Soft-hide one entry: gone from the panel, kept for dedupe."""
        self.db.write(
            "UPDATE feed SET hidden=1, seen=1 WHERE id=?", (int(feed_id),))

    def feed_clear(self) -> None:
        self.db.write("UPDATE feed SET hidden=1, seen=1 WHERE hidden=0")

    def feed_prune(self, keep: int = 500, max_age_days: int = 45) -> int:
        """Bound the feed table: keep the newest ``keep`` rows and drop
        anything older than the age cap. Dedupe only needs rows younger
        than the 30-minute alert window, so pruning is always safe."""
        cutoff = int(time.time() * 1000) - max_age_days * 86_400_000

        def txn(c):
            cur = c.execute(
                "DELETE FROM feed WHERE created_ms < ? OR id NOT IN ("
                "SELECT id FROM feed ORDER BY created_ms DESC, id DESC "
                "LIMIT ?)", (cutoff, int(keep)))
            return max(0, int(cur.rowcount or 0))
        return self.db.txn(txn)

    # ---------- quarantined source rows ----------

    def record_sync_failure(self, source_rowid: int, guid: Optional[str],
                            raw: dict, error: str) -> None:
        now = int(time.time() * 1000)
        self.db.write(
            "INSERT INTO sync_failures(source_rowid,guid,raw,error,last_attempt_ms) "
            "VALUES(?,?,?,?,?) ON CONFLICT(source_rowid) DO UPDATE SET "
            "guid=excluded.guid,raw=excluded.raw,error=excluded.error,"
            "attempts=sync_failures.attempts+1,last_attempt_ms=excluded.last_attempt_ms",
            (source_rowid, guid, json.dumps(raw, default=str), error, now))

    def sync_failures(self, limit: int = 20) -> list:
        return self.db.query(
            "SELECT * FROM sync_failures ORDER BY last_attempt_ms LIMIT ?", (limit,))

    def clear_sync_failure(self, source_rowid: int) -> None:
        self.db.write("DELETE FROM sync_failures WHERE source_rowid=?",
                      (source_rowid,))

    # ---------- attachments ----------

    def set_attachment_local(self, guid: str, path: Optional[str], state: str):
        self.db.write("UPDATE attachments SET local_path=?, state=? WHERE guid=?",
                      (path, state, guid))

    def attachment(self, guid: str):
        return self.db.one("SELECT * FROM attachments WHERE guid=?", (guid,))

    # ---------- search ----------

    def search(self, match_expr: str, limit: int = 120) -> list:
        try:
            return self.db.query(
                "SELECT m.guid, m.chat_guid, m.date_created, m.is_from_me, "
                "snippet(messages_fts, 0, '', '', ' \u2026 ', 14) AS snip, "
                "c.display_name, c.participants, c.is_group "
                "FROM messages_fts f JOIN messages m ON m.rowid=f.rowid "
                "JOIN chats c ON c.guid=m.chat_guid "
                "WHERE messages_fts MATCH ? ORDER BY m.date_created DESC LIMIT ?",
                (match_expr, limit))
        except Exception:
            log.exception("Search failed")
            return []

    # ---------- sync state ----------

    def sync_row(self, chat_guid: str):
        return self.db.one("SELECT * FROM sync_state WHERE chat_guid=?", (chat_guid,))

    def set_sync(self, chat_guid: str, oldest: Optional[int], done: bool):
        self.db.write(
            "INSERT INTO sync_state(chat_guid, oldest_synced, backfill_done) "
            "VALUES(?,?,?) ON CONFLICT(chat_guid) DO UPDATE SET "
            "oldest_synced=excluded.oldest_synced, backfill_done=excluded.backfill_done",
            (chat_guid, oldest, 1 if done else 0))

    def chats_needing_backfill(self) -> list:
        return self.db.query(
            "SELECT c.guid FROM chats c LEFT JOIN sync_state s ON s.chat_guid=c.guid "
            "WHERE COALESCE(s.backfill_done,0)=0 AND c.archived=0 "
            "ORDER BY COALESCE(c.last_activity,0) DESC")

    # ---------- outbox ----------

    def enqueue(self, chat_guid: str, text: Optional[str],
                attach_path: Optional[str]) -> int:
        temp = f"temp-{uuid.uuid4()}"
        cur = self.db.write(
            "INSERT INTO outbox(temp_guid, chat_guid, text, attach_path, created_ts) "
            "VALUES(?,?,?,?,?)",
            (temp, chat_guid, text, attach_path, int(time.time() * 1000)))
        return cur.lastrowid

    def outbox_row(self, oid: int):
        return self.db.one("SELECT * FROM outbox WHERE id=?", (oid,))

    def claim_outbox(self, oid: int):
        """Atomically move one eligible row to sending and return it.

        The conditional UPDATE is the single-claimant boundary. Even if two
        agent processes or duplicate in-memory submissions race, only one can
        change queued/failed -> sending and reach the remote send call. A
        crash after the remote service accepts a send remains inherently
        ambiguous, so recovery deliberately requires a human retry.
        """
        def txn(c):
            cur = c.execute(
                "UPDATE outbox SET state='sending',last_error=NULL,"
                "attempts=attempts+1 WHERE id=? "
                "AND state IN ('queued','failed') AND NOT EXISTS ("
                "SELECT 1 FROM meta WHERE key='mac_maintenance_until_ms' "
                "AND CAST(value AS INTEGER)>?)",
                (int(oid), int(time.time() * 1000)))
            if cur.rowcount != 1:
                return None
            return c.execute(
                "SELECT * FROM outbox WHERE id=?", (int(oid),)).fetchone()
        return self.db.txn(txn)

    def try_begin_mac_maintenance(self, ttl_ms: int = 150_000) -> bool:
        """Atomically fence new send claims before Messages is restarted.

        The lease write obtains SQLite's writer reservation before checking
        the outbox, closing the gap where a send could start immediately after
        Wake Mac's old count-only guard. New compositions may still queue and
        are released when maintenance ends. The expiry makes a crashed agent
        self-healing.
        """
        now_ms = int(time.time() * 1000)
        until_ms = now_ms + max(30_000, int(ttl_ms))

        def txn(c):
            cur = c.execute(
                "INSERT INTO meta(key,value) VALUES("
                "'mac_maintenance_until_ms',?) ON CONFLICT(key) DO UPDATE "
                "SET value=excluded.value WHERE CAST(meta.value AS INTEGER)<=?",
                (str(until_ms), now_ms))
            if cur.rowcount != 1:
                return False
            active = c.execute(
                "SELECT COUNT(*) AS n FROM outbox "
                "WHERE state IN ('queued','sending')").fetchone()["n"]
            if int(active):
                c.execute(
                    "DELETE FROM meta WHERE key='mac_maintenance_until_ms'")
                return False
            return True
        return bool(self.db.txn(txn))

    def end_mac_maintenance(self) -> None:
        self.db.write(
            "DELETE FROM meta WHERE key='mac_maintenance_until_ms'")

    def mac_maintenance_active(self) -> bool:
        return self.meta_int("mac_maintenance_until_ms", 0) > int(
            time.time() * 1000)

    def outbox_pending(self, chat_guid: str) -> list:
        return self.db.query(
            "SELECT * FROM outbox WHERE chat_guid=? AND state IN "
            "('queued','sending','failed') ORDER BY id", (chat_guid,))

    def outbox_active_count(self) -> int:
        """Sends that are queued or on the wire right now, in any chat.
        Failed rows wait for a human and do not block Mac-side actions."""
        row = self.db.one(
            "SELECT COUNT(*) AS n FROM outbox "
            "WHERE state IN ('queued','sending')")
        return int(row["n"]) if row else 0

    def recover_outbox(self, *, mark_sending_uncertain: bool = True) -> list:
        """Recover durable queue state after a crash.

        ``queued`` is safe to submit because no send attempt began.  A row
        left at ``sending`` has an ambiguous remote outcome and is never
        retried automatically; doing so could double-text a client.
        """
        uncertain = (
            "Delivery uncertain after the app stopped. Check Messages on the "
            "Mac or iPhone before retrying.")

        def txn(c):
            if mark_sending_uncertain:
                c.execute(
                    "UPDATE outbox SET state='failed',last_error=? "
                    "WHERE state='sending'", (uncertain,))
            return [r["id"] for r in c.execute(
                "SELECT id FROM outbox WHERE state='queued' ORDER BY id").fetchall()]
        return self.db.txn(txn)

    def outbox_set(self, oid: int, state: str, server_guid=None, error=None):
        self.db.write(
            "UPDATE outbox SET state=?, server_guid=COALESCE(?, server_guid), "
            "last_error=? "
            "WHERE id=?",
            (state, server_guid, error, oid))
