"""Focus-safe Win32 Unicode input used by verification-code popups.

The native structures live outside the Qt UI so their exact ABI layout and
event sequence can be unit-tested on any development machine.
"""
import ctypes
import logging
import os
import sys
from enum import Enum

log = logging.getLogger(__name__)


class FillResult(Enum):
    SUCCESS = "success"
    BLOCKED = "blocked"
    PARTIAL = "partial"


def foreground_window():
    if not sys.platform.startswith("win"):
        return None
    try:
        return int(ctypes.windll.user32.GetForegroundWindow()) or None
    except Exception:
        log.exception("Could not read the foreground window")
        return None


def _modifiers_down() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        # Shift, Ctrl, Alt, left Windows, right Windows.
        return any(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000
                   for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C))
    except Exception:
        log.exception("Could not read modifier state")
        return True


def _own_process_window(hwnd) -> bool:
    if not sys.platform.startswith("win") or not hwnd:
        return False
    try:
        pid = ctypes.c_uint32()
        ctypes.windll.user32.GetWindowThreadProcessId(
            ctypes.c_void_p(hwnd), ctypes.byref(pid))
        return int(pid.value) == os.getpid()
    except Exception:
        log.exception("Could not identify the foreground window")
        return True


def input_types(pointer_type=None):
    """Return native INPUT and KEYBDINPUT types for the requested ABI."""
    ulong_ptr = pointer_type or ctypes.c_size_t

    class MouseInput(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32),
                    ("mouseData", ctypes.c_uint32),
                    ("dwFlags", ctypes.c_uint32),
                    ("time", ctypes.c_uint32),
                    ("dwExtraInfo", ulong_ptr)]

    class KeyboardInput(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_uint16),
                    ("wScan", ctypes.c_uint16),
                    ("dwFlags", ctypes.c_uint32),
                    ("time", ctypes.c_uint32),
                    ("dwExtraInfo", ulong_ptr)]

    class HardwareInput(ctypes.Structure):
        _fields_ = [("uMsg", ctypes.c_uint32),
                    ("wParamL", ctypes.c_uint16),
                    ("wParamH", ctypes.c_uint16)]

    class InputUnion(ctypes.Union):
        _fields_ = [("mi", MouseInput), ("ki", KeyboardInput),
                    ("hi", HardwareInput)]

    class Input(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("u", InputUnion)]

    return Input, KeyboardInput


def keyboard_inputs(text: str, pointer_type=None):
    """Build exactly one Unicode key-down/up pair per UTF-16 code unit."""
    keyeventf_unicode = 0x0004
    keyeventf_keyup = 0x0002
    input_keyboard = 1
    Input, KeyboardInput = input_types(pointer_type)
    values = []
    for char in text:
        units = char.encode("utf-16-le")
        for offset in range(0, len(units), 2):
            scan = int.from_bytes(units[offset:offset + 2], "little")
            for flags in (keyeventf_unicode,
                          keyeventf_unicode | keyeventf_keyup):
                item = Input()
                item.type = input_keyboard
                item.u.ki = KeyboardInput(0, scan, flags, 0, 0)
                values.append(item)
    return Input, (Input * len(values))(*values)


def type_text(text: str, target_hwnd=None) -> FillResult:
    """Type Unicode into the foreground target without changing focus."""
    if not sys.platform.startswith("win") or not text:
        return FillResult.BLOCKED
    try:
        user32 = ctypes.windll.user32
        current = int(user32.GetForegroundWindow()) or None
        if target_hwnd is None or current != int(target_hwnd):
            log.warning("Fill refused because the foreground window changed")
            return FillResult.BLOCKED
        if _own_process_window(target_hwnd):
            log.warning("Fill refused because JRL Messages held focus")
            return FillResult.BLOCKED
        if _modifiers_down():
            log.warning("Fill refused while a keyboard modifier was held")
            return FillResult.BLOCKED

        Input, inputs = keyboard_inputs(text)
        native_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        if ctypes.sizeof(Input) != native_size:
            log.error("Unsafe Win32 INPUT layout: %d rather than %d",
                      ctypes.sizeof(Input), native_size)
            return FillResult.BLOCKED

        user32.SendInput.argtypes = (
            ctypes.c_uint32, ctypes.POINTER(Input), ctypes.c_int)
        user32.SendInput.restype = ctypes.c_uint32
        sent = user32.SendInput(
            len(inputs), inputs, ctypes.sizeof(Input))
        if sent == len(inputs):
            return FillResult.SUCCESS
        if sent:
            log.error("Fill was partial: %d of %d input events", sent,
                      len(inputs))
            return FillResult.PARTIAL
        return FillResult.BLOCKED
    except Exception:
        log.exception("Fill failed")
        return FillResult.BLOCKED
