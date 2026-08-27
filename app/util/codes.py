"""Verification-code detection. Conservative on purpose: a number only
counts as a code when the message talks like a code delivery or the
sender is a short code, so prices, addresses, and file numbers in real
conversations never trigger the buttons."""
import re
import unicodedata
from typing import Optional

_KEYWORDS = re.compile(
    r"\b(code|verification|verify|passcode|pin|otp|2fa|mfa|"
    r"two[- ]?factor|one[- ]?time|login|log[- ]?in|sign[- ]?in|"
    r"security|authenticate|authentication|authorization|confirmation)\b",
    re.IGNORECASE)

_NEGATIVE_CONTEXT = re.compile(
    r"\b(order|invoice|case|file|claim|tracking|reference|confirmation)\s*"
    r"(?:number|no\.?|#)?\s*(?:is|:|=|-)?\s*$", re.IGNORECASE)

_EXPLICIT = [
    re.compile(
        r"\b(?:code|passcode|pin|otp)\s*(?:is|:|=|-)?\s*"
        r"(\d{3})[- ](\d{3})\b", re.IGNORECASE),
    re.compile(
        r"\b(?:code|passcode|pin|otp)\s*(?:is|:|=|-)?\s*"
        r"(?:G-)?(\d{4,8})\b", re.IGNORECASE),
    re.compile(
        r"\b(?:G-)?(\d{4,8})\s+(?:is\s+)?(?:your\s+)?"
        r"(?:verification\s+)?(?:code|passcode|pin|otp)\b", re.IGNORECASE),
]

_PATTERNS = [
    re.compile(r"\bG-(\d{6})\b"),
    re.compile(r"\b(\d{3})[- ](\d{3})\b"),
    re.compile(r"\b(\d{6})\b"),
    re.compile(r"\b(\d{5})\b"),
    re.compile(r"\b(\d{7,8})\b"),
    re.compile(r"\b(\d{4})\b"),
]


def _is_shortcode(sender: Optional[str]) -> bool:
    if not sender or "@" in sender:
        return False
    digits = re.sub(r"\D", "", sender)
    return 3 <= len(digits) <= 6


def extract_code(text: Optional[str], sender: Optional[str] = None
                 ) -> Optional[str]:
    if not text:
        return None
    text = unicodedata.normalize("NFKC", str(text))
    text = re.sub(r"[\u00a0\u2007\u202f]", " ", text)
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    bare = text.strip()
    m = re.fullmatch(r"(?:G-)?(\d{3})[- ]?(\d{3})", bare)
    if m:
        return "".join(m.groups())
    if re.fullmatch(r"\d{4,8}", bare):
        return bare
    # Prefer a number grammatically attached to the word "code". This avoids
    # picking an expiry time, phone number, or amount that appears earlier.
    for pat in _EXPLICIT:
        m = pat.search(text)
        if m:
            return "".join(m.groups())
    if not (_KEYWORDS.search(text) or _is_shortcode(sender)):
        return None
    for pat in _PATTERNS:
        m = pat.search(text)
        if m:
            prefix = text[max(0, m.start() - 32):m.start()]
            if _NEGATIVE_CONTEXT.search(prefix):
                continue
            return "".join(m.groups())
    return None
