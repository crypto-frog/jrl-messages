"""Apple Notification Center Service: the pure protocol layer.

Everything here is bytes-in, values-out with no I/O, so the whole
protocol is unit-testable in any container. The radio worker in
``link.py`` feeds raw GATT notifications into these parsers.

Reference: Apple's public ANCS specification. The iPhone (Notification
Provider) hosts the service; this app (Notification Consumer) subscribes
to the Notification Source for 8-byte events, then asks the Control
Point for human-readable attributes, which arrive on the Data Source,
possibly fragmented across several GATT notifications.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# 128-bit UUIDs, as published by Apple.
SERVICE_UUID = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
NOTIFICATION_SOURCE_UUID = "9fbf120d-6301-42d9-8c58-25e699a21dbd"
CONTROL_POINT_UUID = "69d1d8f3-45e1-49a8-9821-9bbdfdaad9d9"
DATA_SOURCE_UUID = "22eac6e9-24d6-4bb5-be44-b36ace7c7bfb"

# EventID
EVENT_ADDED = 0
EVENT_MODIFIED = 1
EVENT_REMOVED = 2

# EventFlags
FLAG_SILENT = 1 << 0
FLAG_IMPORTANT = 1 << 1
FLAG_PRE_EXISTING = 1 << 2
FLAG_POSITIVE_ACTION = 1 << 3
FLAG_NEGATIVE_ACTION = 1 << 4

CATEGORIES = {
    0: "Other", 1: "Incoming call", 2: "Missed call", 3: "Voicemail",
    4: "Social", 5: "Schedule", 6: "Email", 7: "News",
    8: "Health & Fitness", 9: "Business & Finance", 10: "Location",
    11: "Entertainment",
}

# CommandID
CMD_GET_NOTIFICATION_ATTRIBUTES = 0
CMD_GET_APP_ATTRIBUTES = 1

# Notification attribute IDs
ATTR_APP_IDENTIFIER = 0
ATTR_TITLE = 1          # needs a 2-byte max length parameter
ATTR_SUBTITLE = 2       # needs a 2-byte max length parameter
ATTR_MESSAGE = 3        # needs a 2-byte max length parameter
ATTR_MESSAGE_SIZE = 4
ATTR_DATE = 5

# App attribute IDs
APP_ATTR_DISPLAY_NAME = 0

TITLE_MAX = 96
SUBTITLE_MAX = 96
MESSAGE_MAX = 320

# A Data Source response can never legitimately grow past this; a
# runaway buffer is dropped rather than eating memory forever.
MAX_RESPONSE_BYTES = 8192

# Texts already arrive through the Mac relay with full history, richer
# popups, and reply actions; mirroring them again over Bluetooth would
# double-alert every message. Same for FaceTime, which the relay's wake
# machinery treats separately.
DEFAULT_IGNORED_APP_IDS = ("com.apple.MobileSMS",)


@dataclass(frozen=True)
class SourceEvent:
    """One 8-byte Notification Source event."""

    event_id: int
    flags: int
    category: int
    category_count: int
    uid: int

    @property
    def silent(self) -> bool:
        return bool(self.flags & FLAG_SILENT)

    @property
    def pre_existing(self) -> bool:
        return bool(self.flags & FLAG_PRE_EXISTING)

    @property
    def category_name(self) -> str:
        return CATEGORIES.get(self.category, "Other")


def parse_source_event(data: bytes) -> Optional[SourceEvent]:
    """Parse one Notification Source packet; None if malformed."""
    if len(data) != 8:
        return None
    event_id, flags, category, count = data[0], data[1], data[2], data[3]
    if event_id not in (EVENT_ADDED, EVENT_MODIFIED, EVENT_REMOVED):
        return None
    (uid,) = struct.unpack_from("<I", data, 4)
    return SourceEvent(event_id, flags, category, count, uid)


def should_alert(event: SourceEvent, ignored_app_check=None) -> bool:
    """The single arrival gate: only fresh, audible, newly added
    notifications alert. Modified events are progress updates, silent
    ones asked not to be heard, and pre-existing ones are the backlog
    the iPhone replays on every connect (alerting those would storm the
    screen each reconnection)."""
    if event.event_id != EVENT_ADDED:
        return False
    if event.silent or event.pre_existing:
        return False
    return True


def app_id_ignored(app_id: str, extra_csv: str = "") -> bool:
    """True when this app's notifications should be dropped, matching
    case-insensitively on substrings of the bundle identifier."""
    hay = (app_id or "").lower()
    for needle in DEFAULT_IGNORED_APP_IDS:
        if needle.lower() in hay:
            return True
    for raw in (extra_csv or "").split(","):
        needle = raw.strip().lower()
        if needle and needle in hay:
            return True
    return False


def build_get_notification_attributes(uid: int) -> bytes:
    """Control Point command asking for everything a popup needs."""
    return (
        struct.pack("<BI", CMD_GET_NOTIFICATION_ATTRIBUTES, uid)
        + bytes([ATTR_APP_IDENTIFIER])
        + struct.pack("<BH", ATTR_TITLE, TITLE_MAX)
        + struct.pack("<BH", ATTR_SUBTITLE, SUBTITLE_MAX)
        + struct.pack("<BH", ATTR_MESSAGE, MESSAGE_MAX)
        + bytes([ATTR_DATE])
    )


def build_get_app_attributes(app_id: str) -> bytes:
    """Control Point command asking for an app's display name."""
    return (bytes([CMD_GET_APP_ATTRIBUTES])
            + app_id.encode("utf-8") + b"\x00"
            + bytes([APP_ATTR_DISPLAY_NAME]))


def parse_ancs_date(value: str) -> Optional[int]:
    """ANCS date string (yyyyMMdd'T'HHmmSS) to unix milliseconds."""
    try:
        return int(datetime.strptime(value.strip(), "%Y%m%dT%H%M%S")
                   .timestamp() * 1000)
    except (ValueError, OSError, OverflowError):
        return None


def prettify_app_id(app_id: str) -> str:
    """A readable fallback while (or if) the display name is unknown:
    'com.burbn.instagram' -> 'Instagram'."""
    tail = (app_id or "").rsplit(".", 1)[-1]
    return tail[:1].upper() + tail[1:] if tail else "iPhone app"


class NotificationAttributesResponse:
    """Reassembles one GetNotificationAttributes response that may span
    several Data Source notifications. Feed every chunk; ``done`` flips
    when the byte stream is a complete, self-consistent response."""

    def __init__(self, uid: int):
        self.uid = uid
        self._buf = bytearray()
        self.attributes: dict[int, str] = {}
        self.done = False
        self.overflowed = False

    def feed(self, chunk: bytes) -> bool:
        """Add one Data Source packet. Returns ``done``."""
        if self.done:
            return True
        self._buf.extend(chunk)
        if len(self._buf) > MAX_RESPONSE_BYTES:
            self.overflowed = True
            self.done = True
            return True
        self._try_parse()
        return self.done

    def _try_parse(self) -> None:
        buf = self._buf
        if len(buf) < 5:
            return
        if buf[0] != CMD_GET_NOTIFICATION_ATTRIBUTES:
            # Not our response; a stray packet must not wedge the queue.
            self.overflowed = True
            self.done = True
            return
        (uid,) = struct.unpack_from("<I", buf, 1)
        if uid != self.uid:
            self.overflowed = True
            self.done = True
            return
        offset = 5
        parsed: dict[int, str] = {}
        while offset < len(buf):
            if offset + 3 > len(buf):
                return                      # header split across chunks
            attr_id = buf[offset]
            (length,) = struct.unpack_from("<H", buf, offset + 1)
            if offset + 3 + length > len(buf):
                return                      # value split across chunks
            raw = bytes(buf[offset + 3:offset + 3 + length])
            parsed[attr_id] = raw.decode("utf-8", errors="replace")
            offset += 3 + length
        # The buffer parses cleanly end to end; the command requested a
        # fixed attribute set and iOS answers each requested attribute
        # (empty ones with zero length), so a fully consumed buffer with
        # the date attribute present is a complete response.
        self.attributes = parsed
        if ATTR_DATE in parsed:
            self.done = True

    def result(self) -> dict:
        """The assembled notification, ready for presentation."""
        app_id = self.attributes.get(ATTR_APP_IDENTIFIER, "")
        return {
            "uid": self.uid,
            "app_id": app_id,
            "title": self.attributes.get(ATTR_TITLE, "").strip(),
            "subtitle": self.attributes.get(ATTR_SUBTITLE, "").strip(),
            "message": self.attributes.get(ATTR_MESSAGE, "").strip(),
            "when_ms": parse_ancs_date(
                self.attributes.get(ATTR_DATE, "")),
        }


class AppAttributesResponse:
    """Reassembles one GetAppAttributes (display name) response."""

    def __init__(self, app_id: str):
        self.app_id = app_id
        self._buf = bytearray()
        self.display_name = ""
        self.done = False
        self.overflowed = False

    def feed(self, chunk: bytes) -> bool:
        if self.done:
            return True
        self._buf.extend(chunk)
        if len(self._buf) > MAX_RESPONSE_BYTES:
            self.overflowed = True
            self.done = True
            return True
        self._try_parse()
        return self.done

    def _try_parse(self) -> None:
        buf = self._buf
        expected = (bytes([CMD_GET_APP_ATTRIBUTES])
                    + self.app_id.encode("utf-8") + b"\x00")
        if len(buf) < len(expected):
            return
        if not bytes(buf[:len(expected)]) == expected:
            self.overflowed = True
            self.done = True
            return
        offset = len(expected)
        while offset < len(buf):
            if offset + 3 > len(buf):
                return
            attr_id = buf[offset]
            (length,) = struct.unpack_from("<H", buf, offset + 1)
            if offset + 3 + length > len(buf):
                return
            raw = bytes(buf[offset + 3:offset + 3 + length])
            if attr_id == APP_ATTR_DISPLAY_NAME:
                self.display_name = raw.decode(
                    "utf-8", errors="replace").strip()
                self.done = True
                return
            offset += 3 + length


def presentation_of(assembled: dict, display_name: str = "") -> dict:
    """Popup-ready title and body from an assembled notification."""
    app_name = (display_name or prettify_app_id(assembled.get("app_id", ""))
                or "iPhone")
    parts = [p for p in (assembled.get("title", ""),
                         assembled.get("subtitle", ""),
                         assembled.get("message", "")) if p]
    body = " · ".join(parts[:1]) if len(parts) == 1 else ""
    if len(parts) >= 2:
        body = f"{parts[0]}: " + " · ".join(parts[1:])
    return {"app_name": app_name, "body": body or "New notification"}
