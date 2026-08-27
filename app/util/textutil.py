"""Small text helpers shared across the app."""
import html
import re
from typing import Optional

_URL_RE = re.compile(r"(https?://[^\s<>\"]+)", re.IGNORECASE)
_WORD_RE = re.compile(r"\w+", re.UNICODE)

TAPBACK_EMOJI = {0: "\u2764\ufe0f", 1: "\U0001F44D", 2: "\U0001F44E",
                 3: "\U0001F602", 4: "\u203c\ufe0f", 5: "\u2753"}


def normalize_address(addr: str) -> str:
    """Key used to match a message handle to a contact entry."""
    if not addr:
        return ""
    a = addr.strip().lower()
    if "@" in a:
        return a
    digits = re.sub(r"\D", "", a)
    return digits[-10:] if len(digits) >= 10 else digits


def initials(name: str) -> str:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:1].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def human_size(n: Optional[int]) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n / 1.0:.1f} {unit}"
        n /= 1024
    return ""


def snippet(text: Optional[str], attach_name: Optional[str], from_me: bool) -> str:
    prefix = "You: " if from_me else ""
    if text:
        one = " ".join(text.split())
        return prefix + (one[:90] + ("\u2026" if len(one) > 90 else ""))
    if attach_name:
        return prefix + "\U0001F4CE " + attach_name
    return prefix + "Attachment" if attach_name is not None else ""


def fts_escape(q: str) -> str:
    toks = _WORD_RE.findall(q or "")
    return " ".join(f'"{t}"' for t in toks)


def linkify(text: str) -> str:
    """Escape HTML then wrap URLs in anchors for a rich-text QLabel."""
    esc = html.escape(text)
    esc = _URL_RE.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', esc)
    return esc.replace("\n", "<br>")


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name or "attachment").strip() or "attachment"


def to_imessage_address(raw: str) -> str:
    """Format user input for sending: emails pass through, North American
    ten-digit numbers gain +1, full international numbers keep their +."""
    a = (raw or "").strip()
    if "@" in a:
        return a.lower()
    digits = re.sub(r"\D", "", a)
    if a.startswith("+") and len(digits) >= 8:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return a


def looks_like_address(raw: str) -> bool:
    a = (raw or "").strip()
    if "@" in a and "." in a.split("@")[-1]:
        return True
    return len(re.sub(r"\D", "", a)) >= 7


_EMOJI_CORE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF"
    "\u2190-\u21FF\u2B50\u2764]")
_EMOJI_EXTRA = re.compile("[\uFE0F\u200D\U0001F3FB-\U0001F3FF \t\n]")


def is_emoji_only(text, max_clusters: int = 3) -> bool:
    """True when a message is nothing but one to a few emoji."""
    if not text:
        return False
    stripped = _EMOJI_EXTRA.sub("", text)
    if not stripped:
        return False
    core = _EMOJI_CORE.findall(stripped)
    return len(core) == len(stripped) and 1 <= len(core) <= max_clusters
