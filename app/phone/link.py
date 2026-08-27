"""The Bluetooth radio worker for iPhone notification mirroring.

Runs on its own daemon thread with its own asyncio loop, entirely apart
from Qt's event loop and the agent process. Everything Bluetooth is
imported lazily inside functions, so machines without a radio, without
the bleak package, or on another OS lose exactly one optional feature
and nothing else. Every state change is reported through the ``status``
signal so the Activity panel and the bell always know what the link is
doing; silence is never mysterious.

The worker is deliberately UI-free: it owns no widgets and emits plain
dictionaries. The main window decides how a notification looks and
which master switches apply.
"""
from __future__ import annotations

import logging
import platform
import threading
import time

from PySide6.QtCore import QObject, Signal

from . import ancs

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 15
SCAN_TIMEOUT_S = 8
COMMAND_TIMEOUT_S = 10
MAX_PENDING = 10          # attribute fetches queued during a burst
BACKOFF_START_S = 5
# Waits between subscribe retries while the user approves the pairing
# prompt on the phone (about 40 seconds of patience in total).
PAIR_RETRY_WAITS = (4, 6, 8, 10, 12)

# Bluetooth SIG company identifier carried in Apple advertisements.
# iPhones deliberately broadcast with NO name and a rotating anonymous
# address (so strangers cannot track them), which is why a naive scan
# shows every speaker and headset but never the phone. Manufacturer
# data still says "Apple", and the Windows paired list still knows the
# phone by its real name, so discovery leans on those two facts.
APPLE_COMPANY_ID = 0x004C


def format_ble_address(value: int) -> str:
    """48-bit integer Bluetooth address to canonical AA:BB:CC:DD:EE:FF."""
    raw = f"{int(value) & 0xFFFFFFFFFFFF:012X}"
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2))


def address_from_aep_id(aep_id: str) -> str:
    """The remote MAC inside a Windows association-endpoint id, e.g.
    'BluetoothLE#BluetoothLE48:51:c5:aa:bb:cc-04:52:c7:bb:79:0e'."""
    tail = (aep_id or "").rsplit("-", 1)[-1].strip().upper()
    parts = tail.split(":")
    if len(parts) == 6 and all(len(p) == 2 for p in parts):
        return tail
    return ""


def closeness(rssi) -> str:
    """A human word for signal strength, for telling several anonymous
    Apple devices apart: the phone in your hand reads very close."""
    if rssi is None:
        return ""
    if rssi >= -55:
        return "very close"
    if rssi >= -70:
        return "nearby"
    return "in range"


# Discovery rows are (name, address, source, rssi). Sources:
#   verified      proven to serve Apple's notification service (ANCS)
#   paired        Windows Bluetooth LE paired list (real name, stable)
#   paired-voice  Windows classic paired list (real name; voice/audio
#                 transport, so its address may not accept LE directly)
#   scan          named live advertisement
#   apple         anonymous live advertisement with Apple manufacturer data
_SOURCE_RANK = {"verified": 0, "paired": 1, "paired-voice": 2,
                "scan": 3, "apple": 4}


def merge_device_rows(rows: list) -> list:
    """Order and dedupe discovery rows.

    Proof beats pairing beats names beats anonymity: verified iPhones
    first, then the paired lists, then named devices with iPhones on
    top, then anonymous Apple broadcasters strongest-signal first. The
    first occurrence of an address wins, so a phone found several ways
    shows once, under its strongest identity."""
    def rank(row):
        name, _address, source, rssi = row
        lowered = (name or "").lower()
        iphone_rank = 0 if "iphone" in lowered else 1
        signal_rank = -(rssi if rssi is not None else -999)
        return (_SOURCE_RANK.get(source, 5), iphone_rank,
                signal_rank, lowered)

    seen: set = set()
    merged = []
    for row in sorted(rows, key=rank):
        key = (row[1] or "").lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(row)
    return merged


def _looks_like_iphone(name: str) -> bool:
    """True when a device row's name plainly names an iPhone.

    Android phones ("Jonathan's A14", "moto g54 5G"), speakers, and
    Windows' generic labels do not match; a real Windows paired-list
    entry for an iPhone always carries the word iPhone in its name."""
    return "iphone" in (name or "").strip().lower()


# Display fallbacks this app itself invents. They must never count as a
# user-chosen name when matching paired rows by equality: "your iPhone"
# is what the worker says when no name is stored, and "Apple device N"
# is a scan label for an anonymous broadcaster.
_FALLBACK_WANTED = ("your iphone", "apple device")


async def _paired_iphone_rows(wanted_name: str = "") -> list:
    """(name, ADDRESS) for every Windows paired entry that is an iPhone.

    THE PAIRED LIST IS THE AUTHORITY on the question "is an iPhone
    paired with this PC". The per-address record (_windows_pairing) is
    BLIND behind a rotating anonymous address: it answers "not paired"
    for a fresh RPA while the bond sits in Windows under the phone's
    real identity, which produced the false "Your iPhone is not paired
    with this PC yet" verdict in the field (report #13). The per-address
    record is therefore only ever trusted when it says YES.

    A row counts as an iPhone when its name says so, or when it equals
    the user's chosen name; the equality path ignores this app's own
    display fallbacks so "your iPhone" never manufactures a match."""
    wanted = (wanted_name or "").strip().lower()
    if wanted in _FALLBACK_WANTED or wanted.startswith("apple device"):
        wanted = ""
    rows = []
    seen: set = set()
    for row_name, row_addr, _src, _rssi in await _paired_rows():
        label = (row_name or "").strip()
        address = (row_addr or "").upper()
        if not address or address in seen:
            continue
        if _looks_like_iphone(label) or (wanted
                                         and label.lower() == wanted):
            seen.add(address)
            rows.append((label, address))
    return rows


# Windows association-endpoint protocol ids (documented by Microsoft).
_AQS_LE_PAIRED = (
    'System.Devices.Aep.ProtocolId:='
    '"{bb7bb05e-5972-42b5-94fc-76eaa7084d49}" AND '
    'System.Devices.Aep.IsPaired:=System.StructuredQueryType.Boolean#True')
_AQS_CLASSIC_PAIRED = (
    'System.Devices.Aep.ProtocolId:='
    '"{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}" AND '
    'System.Devices.Aep.IsPaired:=System.StructuredQueryType.Boolean#True')


async def _paired_rows_with_notes() -> tuple:
    """The Windows paired lists, three ways, every failure surfaced.

    The device-interface lookup misses phones whose LE side has not
    materialized an interface yet (exactly what happened in the field:
    an iPhone paired through Phone Link showed nowhere), so the
    association-endpoint queries ask Windows for the paired list the
    way the Settings page itself does, for LE and for classic. Returns
    (rows, notes): notes are shown to the user, never buried in a
    debug log."""
    import sys
    notes = []
    if not sys.platform.startswith("win"):
        return [], ["paired list: only available on Windows"]
    try:
        from winrt.windows.devices.bluetooth import BluetoothLEDevice
        from winrt.windows.devices.enumeration import (
            DeviceInformation, DeviceInformationKind)
    except Exception as e:
        return [], [f"paired list: WinRT unavailable ({e.__class__.__name__};"
                    " run install.bat once)"]
    rows = []

    async def aep_query(aqs, source, label):
        found = 0
        try:
            infos = await _find_all_flex(
                DeviceInformation, aqs, [],
                DeviceInformationKind.ASSOCIATION_ENDPOINT)
            for info in infos:
                address = address_from_aep_id(info.id)
                if not address:
                    continue
                name = (info.name or "").strip() or "Paired device"
                rows.append((name, address, source, None))
                found += 1
            notes.append(f"{label}: {found}")
        except Exception as e:
            notes.append(f"{label} failed: {str(e)[:80] or type(e).__name__}")

    await aep_query(_AQS_LE_PAIRED, "paired", "paired (LE)")
    await aep_query(_AQS_CLASSIC_PAIRED, "paired-voice", "paired (classic)")

    try:
        selector = BluetoothLEDevice.get_device_selector_from_pairing_state(
            True)
        infos = await _find_all_flex(DeviceInformation, selector)
        found = 0
        for info in infos:
            try:
                device = await BluetoothLEDevice.from_id_async(info.id)
                if device is None:
                    continue
                name = ((info.name or "").strip()
                        or (device.name or "").strip() or "Paired device")
                rows.append((name,
                             format_ble_address(device.bluetooth_address),
                             "paired", None))
                found += 1
            except Exception:
                continue
        notes.append(f"paired (interfaces): {found}")
    except Exception as e:
        notes.append("paired (interfaces) failed: "
                     f"{str(e)[:80] or type(e).__name__}")
    return rows, notes


async def _paired_rows() -> list:
    rows, _notes = await _paired_rows_with_notes()
    return rows


async def _scan_rows(timeout: float) -> list:
    """A live advertisement scan. Anonymous Apple broadcasters are kept
    and carry their signal strength so several can be told apart."""
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    rows = []
    for device, adv in found.values():
        name = ((device.name or "").strip()
                or (getattr(adv, "local_name", "") or "").strip())
        is_apple = APPLE_COMPANY_ID in (
            getattr(adv, "manufacturer_data", None) or {})
        rssi = getattr(adv, "rssi", None)
        if name:
            rows.append((name, device.address,
                         "apple" if is_apple else "scan", rssi))
        elif is_apple:
            rows.append(("Apple device", device.address, "apple", rssi))
    return rows


async def _find_all_flex(DeviceInformation, aqs, props=None, kind=None):
    """Call DeviceInformation.FindAllAsync across pywinrt dialects.

    The field produced 'Invalid parameter count' from every shape one
    projection accepted, so every known overload spelling is tried and
    the first that binds wins. Signature errors fall through; a real
    Windows error propagates."""
    if kind is not None:
        attempts = (
            lambda: DeviceInformation.find_all_async(aqs, props or [],
                                                     kind),
            lambda: getattr(
                DeviceInformation,
                "find_all_async_aqs_filter_and_additional_properties"
                "_and_kind")(aqs, props or [], kind),
            lambda: DeviceInformation.find_all_async(
                aqs, list(props or []), kind),
            lambda: DeviceInformation.find_all_async(aqs, None, kind),
            lambda: DeviceInformation.find_all_async(aqs, props or []),
        )
    else:
        attempts = (
            lambda: DeviceInformation.find_all_async(aqs),
            lambda: getattr(DeviceInformation,
                            "find_all_async_aqs_filter")(aqs),
            lambda: DeviceInformation.find_all_async(aqs, []),
        )
    last = None
    for attempt in attempts:
        try:
            return await attempt()
        except Exception as e:
            message = str(e).lower()
            signature_issue = (
                isinstance(e, (TypeError, AttributeError))
                or "parameter" in message or "argument" in message
                or "overload" in message)
            if not signature_issue:
                raise
            last = e
    raise last


async def _pair_with_ceremonies(address: str, say, notes: list) -> bool:
    """Pair through Windows' custom-pairing API with EVERY ceremony an
    iPhone can negotiate. The field failure 'Could not pair with
    device: FAILED', instant and promptless, happens when pairing is
    requested with ConfirmOnly alone while the phone negotiates a
    confirm-code ceremony; Windows then aborts before anything crosses
    the air. Requesting ConfirmOnly + ConfirmPinMatch + DisplayPin and
    accepting in our own handler lets the phone finally show its
    prompt. Returns True when Windows reports paired."""
    pairing = await _windows_pairing(address)
    if pairing is None:
        return False
    try:
        from winrt.windows.devices.enumeration import (
            DevicePairingKinds, DevicePairingProtectionLevel,
            DevicePairingResultStatus)
    except Exception:
        return False
    try:
        custom = pairing.custom
    except Exception:
        return False

    def on_requested(_sender, args):
        try:
            pin = ""
            try:
                pin = str(getattr(args, "pin", "") or "")
            except Exception:
                pin = ""
            if pin:
                say("ACTION NEEDED · Code "
                    f"{pin} — confirm it on the iPhone (tap Pair) and "
                    "anywhere Windows shows it. Accepting on this side "
                    "for you now.")
            else:
                say("ACTION NEEDED · Tap Pair / Allow on your iPhone "
                    "RIGHT NOW.")
            args.accept()
        except Exception:
            try:
                args.accept()
            except Exception:
                log.debug("Pairing accept failed", exc_info=True)

    token = custom.add_pairing_requested(on_requested)
    try:
        kinds = (DevicePairingKinds.CONFIRM_ONLY
                 | DevicePairingKinds.CONFIRM_PIN_MATCH
                 | DevicePairingKinds.DISPLAY_PIN)
        try:
            result = await custom.pair_async(
                kinds, DevicePairingProtectionLevel.ENCRYPTION)
        except Exception as e:
            message = str(e).lower()
            if not (isinstance(e, (TypeError, AttributeError))
                    or "parameter" in message or "argument" in message):
                raise
            result = await custom.pair_async(kinds)
        status = getattr(result, "status", None)
        notes.append(f"{address}: pairing ceremony finished with "
                     f"status {status}")
        return status in (DevicePairingResultStatus.PAIRED,
                          DevicePairingResultStatus.ALREADY_PAIRED)
    except Exception as e:
        notes.append(f"{address}: pairing ceremony error "
                     f"({str(e)[:60] or type(e).__name__})")
        return False
    finally:
        try:
            custom.remove_pairing_requested(token)
        except Exception:
            pass


async def _windows_pairing(address: str):
    """Windows' pairing record for this device, or None. This is how a
    STALE one-sided bond is detected: Windows says is_paired while the
    phone no longer honors the key, so pair() short-circuits with
    'already paired', nothing crosses the air, and the phone never
    shows a prompt. Kept as a module function so tests can script it."""
    import sys
    if not sys.platform.startswith("win"):
        return None
    try:
        from winrt.windows.devices.bluetooth import BluetoothLEDevice
        device = await BluetoothLEDevice.from_bluetooth_address_async(
            int(address.replace(":", ""), 16))
        if device is None:
            return None
        info = device.device_information
        return info.pairing if info is not None else None
    except Exception:
        log.debug("Pairing-record lookup failed", exc_info=True)
        return None


def forget_coaching(address: str = "") -> str:
    """The clean-slate recipe, spelled out. Used whenever pairing never
    prompts or the phone shows a ghost entry with no (i): repair by
    hand with both sides visible, pairing the official Windows way
    first, because that flow makes the phone list the PC as a real
    computer with a working detail page."""
    pc = platform.node() or "this PC"
    return (f"Clean both sides once, then pair the official way. On "
            "Windows: Settings → Bluetooth & devices → Devices → "
            "remove any iPhone or unknown entries. On the iPhone: tap "
            f"Forget This Device on any '{pc}' entry that offers an "
            "(i); if it shows no (i), toggle Bluetooth off and on, and "
            "restart the phone if the ghost entry lingers. Then pair "
            "in WINDOWS SETTINGS (Add device → Bluetooth → your "
            f"iPhone, confirm the code on both), so the phone lists "
            f"'{pc}' as a computer with a working (i) page, and run "
            "Connect my iPhone again.")


def open_phone_link() -> str:
    """Launch Microsoft Phone Link's pairing surface (the QR flow).

    Its QR code starts Microsoft's own Link to Windows app on the
    iPhone, which performs the phone-side pairing from inside iOS —
    something no Windows program can do. Bluetooth bonds belong to
    Windows, not to the app that made them, so once that flow has
    paired the phone, THIS app attaches to the same bond and never
    needs to pair at all. Returns a short note about what launched."""
    import os
    import subprocess
    import sys
    if not sys.platform.startswith("win"):
        return "Phone Link exists only on Windows"
    attempts = (
        ("Phone Link", lambda: os.startfile("ms-phone:")),
        ("Phone Link (app folder)", lambda: subprocess.Popen(
            ["explorer.exe",
             r"shell:AppsFolder\Microsoft.YourPhone_8wekyb3d8bbwe!App"])),
        ("Windows mobile-devices settings",
         lambda: os.startfile("ms-settings:mobile-devices")),
    )
    for label, launch in attempts:
        try:
            launch()
            return f"opened {label}"
        except Exception:
            continue
    return ("could not open Phone Link automatically; open it from the "
            "Start menu and choose iPhone")


def setup_iphone(progress=None, scan_timeout: float = SCAN_TIMEOUT_S,
                 max_pair_attempts: int = 3,
                 advanced_pairing: bool = False) -> tuple:
    """The guided connect flow, blocking, for the picker's wizard.

    DEFAULT (attach-only): find the candidates by proof, connect,
    subscribe over the bond Windows already holds (created by Phone
    Link's QR flow or Windows Settings), and prove the link with a
    live round trip. It NEVER pairs. A refusal on an unpaired phone
    returns {"needs_phone_link": True, ...} so the wizard can send the
    user through Phone Link's pairing, which runs Microsoft's own app
    on the phone and produces the correct iOS UI every time.

    ADVANCED (`advanced_pairing=True`): additionally runs this app's
    own coached pairing ceremony, kept for machines without Phone
    Link. Returns (result, notes); result carries "address", "ms",
    and possibly "paired_pending" or "needs_phone_link"."""
    import asyncio

    def say(text):
        if progress is not None:
            try:
                progress(text)
            except Exception:
                pass

    async def _try_candidate(label, address, rssi, notes, allow_pairing,
                             source="apple"):
        from bleak import BleakClient
        where = closeness(rssi)
        say(f"Connecting to {label}" + (f" ({where})" if where else "")
            + "…")
        slot = {"client": None}

        async def fresh_connect():
            old = slot["client"]
            if old is not None:
                try:
                    await old.disconnect()
                except Exception:
                    pass
            # Short timeout: anonymous identities churn fast, and a
            # dead one must not stall the ceremony for long. Fresh
            # service discovery: never trust the Windows GATT cache.
            new = BleakClient(address, timeout=8,
                              winrt={"use_cached_services": False})
            await new.connect()
            slot["client"] = new
            return new

        try:
            client = await fresh_connect()
        except Exception as e:
            if source == "paired" and "not found" in str(e).lower():
                # A bonded identity never broadcasts, so a scan-based
                # connect cannot see it. Expected, not a failure: the
                # same phone is reached through its anonymous
                # broadcast addresses instead.
                notes.append(f"{address}: bonded identity (reached via "
                             "its anonymous broadcast instead)")
            else:
                notes.append(f"{address}: no connection "
                             f"({str(e)[:60] or type(e).__name__})")
            return None
        try:
            try:
                uuids = {str(getattr(s, "uuid", "")).lower()
                         for s in client.services}
            except Exception:
                uuids = set()
            if ancs.SERVICE_UUID not in uuids:
                if source == "paired" and _looks_like_iphone(label):
                    # Radio truth: iPhones publish ANCS intermittently
                    # and only toward connections they trust. A paired
                    # entry NAMED iPhone that answers without the
                    # service is withholding, not a wrong device.
                    notes.append(
                        f"{address}: answered without the notification "
                        "service this time (iPhones publish it "
                        "intermittently, toward connections they "
                        "trust); it stays your iPhone")
                else:
                    notes.append(f"{address}: not an iPhone (no "
                                 "notification service)")
                return None
            say("This one serves iPhone notifications. Linking…")

            state = {"pending": None}

            def on_data(_c, data):
                pending = state["pending"]
                if pending is not None:
                    pending.feed(bytes(data))

            def on_event(_c, _data):
                return None

            async def subscribe():
                await slot["client"].start_notify(
                    ancs.DATA_SOURCE_UUID, on_data)
                await slot["client"].start_notify(
                    ancs.NOTIFICATION_SOURCE_UUID, on_event)

            try:
                await subscribe()
            except Exception:
                async def is_paired_now():
                    try:
                        record = await _windows_pairing(address)
                        return bool(record is not None and getattr(
                            record, "is_paired", False))
                    except Exception:
                        return False

                # NEVER UNPAIRS, and by default NEVER PAIRS either.
                # Pairing an iPhone correctly is Phone Link's home
                # turf (its QR starts Microsoft's own app on the
                # phone); this app attaches to the bond Windows keeps.
                just_paired = False
                if await is_paired_now():
                    notes.append(
                        f"{address}: already paired at the Windows "
                        "level; not re-pairing (waiting on the "
                        "phone's permission switch)")
                    say("Already paired. One switch left, on the "
                        "phone itself.")
                    return {"address": address.upper(), "ms": None,
                            "paired_pending": True}
                # PAIRED LIST IS THE AUTHORITY (3.5.3, field report
                # #13): the per-address record above is blind behind a
                # rotating anonymous address. If the Windows paired
                # list holds ANY iPhone entry, this PC is paired; the
                # remaining gate is the phone's permission switch, and
                # the "not paired" verdict would be a lie.
                paired_hint = []
                try:
                    paired_hint = await _paired_iphone_rows()
                except Exception:
                    paired_hint = []
                if paired_hint:
                    listed = ", ".join(n for n, _a in paired_hint[:2])
                    notes.append(
                        f"{address}: the phone refused the "
                        "subscription, but the Windows paired list "
                        f"already holds an iPhone entry ({listed}); "
                        "waiting on the phone's permission switch")
                    say("Paired at the Windows level. One switch "
                        "left, on the phone itself.")
                    return {"address": address.upper(), "ms": None,
                            "paired_pending": True}
                if not advanced_pairing:
                    notes.append(
                        f"{address}: this is an iPhone, but the PC "
                        "holds no pairing for it yet; pair through "
                        "Phone Link first")
                    say("Found your iPhone, but it is not paired with "
                        "this PC yet.")
                    return {"address": address.upper(), "ms": None,
                            "needs_phone_link": True}
                if not allow_pairing:
                    notes.append(f"{address}: needs pairing; skipped "
                                 "(pairing attempts used up)")
                    return None
                say("ACTION NEEDED · Unlock your iPhone and look at it "
                    "RIGHT NOW: tap Pair / Allow on the prompt. If "
                    "Windows shows an 'Add a device' message, allow "
                    "that too.")
                # The full ceremony set first: an iPhone negotiates a
                # confirm-code ceremony, and requesting ConfirmOnly
                # alone makes Windows abort instantly and promptlessly
                # ('Could not pair with device: FAILED' in the field).
                ceremony_ok = False
                try:
                    ceremony_ok = await _pair_with_ceremonies(
                        address, say, notes)
                except Exception as e:
                    notes.append(f"{address}: ceremony error "
                                 f"({str(e)[:60] or type(e).__name__})")
                if not ceremony_ok:
                    try:
                        await slot["client"].pair()
                        notes.append(f"{address}: Windows reports "
                                     "pairing completed")
                        ceremony_ok = True
                    except Exception as e:
                        notes.append(f"{address}: pair() said "
                                     f"{str(e)[:60] or type(e).__name__}")
                just_paired = ceremony_ok
                subscribed = False
                for attempt, wait in enumerate(PAIR_RETRY_WAITS, 1):
                    await asyncio.sleep(wait)
                    try:
                        await subscribe()
                        subscribed = True
                        break
                    except Exception:
                        say("Still waiting… unlock the phone if it is "
                            "locked; the prompt hides behind the lock "
                            f"screen. (attempt {attempt} of "
                            f"{len(PAIR_RETRY_WAITS)})")
                if not subscribed:
                    if just_paired or await is_paired_now():
                        # PAIRED, yet the phone declines the
                        # subscription: the one remaining gate is the
                        # phone's own sharing switch. This is a WIN to
                        # keep, never a bond to destroy: save the
                        # phone, coach the switch, and let the
                        # background link finish by itself the moment
                        # it is flipped.
                        notes.append(
                            f"{address}: paired, waiting only for the "
                            "phone's permission switch")
                        say("Paired! One switch left, on the phone "
                            "itself.")
                        return {"address": address.upper(), "ms": None,
                                "paired_pending": True}
                    notes.append(f"{address}: pairing was not approved "
                                 f"in time. {forget_coaching(address)}")
                    return None
                client = slot["client"]
            say("Linked. Asking the phone a test question…")
            response = ancs.AppAttributesResponse("com.apple.Preferences")
            state["pending"] = response
            started = time.monotonic()
            answered = ""
            try:
                await client.write_gatt_char(
                    ancs.CONTROL_POINT_UUID,
                    ancs.build_get_app_attributes("com.apple.Preferences"),
                    response=True)
                deadline = time.monotonic() + COMMAND_TIMEOUT_S
                while not response.done and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                answered = response.display_name if response.done else ""
            except Exception as e:
                notes.append(f"{address}: test question failed "
                             f"({str(e)[:60] or type(e).__name__})")
            finally:
                state["pending"] = None
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if answered:
                say(f"Proven: your iPhone answered in {elapsed_ms} ms.")
                return {"address": (client.address or address).upper(),
                        "ms": elapsed_ms}
            # Subscribed successfully: the bond and both channels work,
            # which is the hard part; note the quiet test and accept.
            notes.append(f"{address}: linked and subscribed; the test "
                         "question went unanswered (harmless quirk)")
            say("Linked and subscribed.")
            return {"address": (client.address or address).upper(),
                    "ms": None}
        finally:
            current = slot["client"]
            if current is not None:
                try:
                    await current.disconnect()
                except Exception:
                    pass

    async def _run():
        notes = []
        say("Reading the Windows paired lists…")
        paired, paired_notes = await _paired_rows_with_notes()
        notes.extend(paired_notes)
        say("Scanning for Apple devices nearby (hold the phone next "
            "to the PC)…")
        try:
            scanned = await _scan_rows(scan_timeout)
        except Exception as e:
            scanned = []
            notes.append(f"scan failed: {str(e)[:80] or type(e).__name__}")
        # Anonymous broadcasts first: they are the CONNECTABLE form of
        # the phone. Bonded identities never broadcast, so they go
        # last, kept mostly for the notes they produce.
        candidates = sorted(
            (r for r in scanned if r[2] == "apple"),
            key=lambda r: -(r[3] if r[3] is not None else -999))
        candidates += [r for r in merge_device_rows(paired)
                       if r[2] == "paired"]
        seen: set = set()
        ordered = []
        for name, address, source, rssi in candidates:
            if address.lower() not in seen:
                seen.add(address.lower())
                ordered.append((name, address, source, rssi))
        if not ordered:
            notes.append("nothing to try: no paired LE entries and no "
                         "Apple devices broadcasting in range")
            return None, notes
        pair_attempts = 0
        # 3.5.3: a refusal verdict (paired_pending / needs_phone_link)
        # no longer ends the run. It is RECORDED and the remaining
        # candidates still get their turn: with two paired "iPhone"
        # entries (an old ceremony bond plus Phone Link's bond) only
        # one carries the phone's notification permission, and a
        # working second entry beats an earlier refusal outright.
        pending_verdict = None

        def remember(result):
            nonlocal pending_verdict
            if pending_verdict is None:
                pending_verdict = result
            elif (result.get("paired_pending")
                    and pending_verdict.get("needs_phone_link")):
                # paired_pending outranks needs_phone_link: evidence
                # of a bond beats the guess that none exists.
                pending_verdict = result

        for index, (name, address, source, rssi) in enumerate(
                ordered[:8]):
            label = (name if source == "paired"
                     else f"Apple device {index + 1}")
            allow_pairing = pair_attempts < max_pair_attempts
            result = await _try_candidate(
                label, address, rssi, notes, allow_pairing, source)
            if result is not None:
                if (result.get("paired_pending")
                        or result.get("needs_phone_link")):
                    remember(result)
                    continue
                return result, notes
            if "pairing was not approved" in (notes[-1] if notes else ""):
                pair_attempts += 1
        if pending_verdict is not None:
            if (pending_verdict.get("needs_phone_link")
                    and not advanced_pairing):
                # Last authority check before the harshest verdict:
                # an iPhone entry anywhere in the Windows paired list
                # means this is a permission wait, not a missing
                # pairing (the per-address record is blind behind a
                # rotating anonymous address).
                paired_hint = []
                try:
                    paired_hint = await _paired_iphone_rows()
                except Exception:
                    paired_hint = []
                if paired_hint:
                    notes.append(
                        "the Windows paired list holds an iPhone "
                        "entry, so this is a permission wait, not a "
                        "missing pairing")
                    pending_verdict = {
                        "address": pending_verdict.get("address"),
                        "ms": None, "paired_pending": True}
            return pending_verdict, notes
        return None, notes

    return asyncio.run(_run())


class AncsMissing(Exception):
    """Connected fine, but the device offers no notification service:
    almost certainly not the user's iPhone (or its trust was revoked)."""


class PairingNeeded(Exception):
    """The phone refused the subscription for lack of a bond. Only the
    guided setup may pair (a background pairing attempt pops prompts on
    the phone at random moments where they expire unseen; that loop was
    the hardest field bug in this feature's history). The worker just
    says so and waits."""


class PhoneLinkWorker(QObject):
    """Owns the ANCS consumer connection to the user's iPhone."""

    # level: "up" | "down" | "info" | "error"; text is user-readable.
    status = Signal(str, str)
    # A fully assembled, presentation-ready notification dictionary:
    # {uid, app_id, app_name, title, subtitle, message, when_ms, category}
    notification = Signal(dict)
    # The phone was re-found at a different address (rotation or a
    # re-pairing); the window persists it so next start connects fast.
    learned = Signal(str, str)      # (name, address)
    # Answer to request_link_test(): a live ANCS round trip succeeded
    # (with timing) or failed (with the reason).
    test_result = Signal(bool, str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._thread = None
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._test_flag = threading.Event()
        self._loop = None
        self._app_names: dict[str, str] = {}
        self._needs_probe = False
        self._connected = False
        self._consecutive_failures = 0
        # 3.5.3 rotation state. Two paired "iPhone" entries can coexist
        # (an old ceremony bond plus Phone Link's bond) and only one
        # carries the phone's notification permission. A failure
        # sidelines the ACTUAL connected address; _find_device then
        # rotates to the other un-sidelined paired iPhone entry, and
        # exhaustion resets the set to the most recent failure so
        # retries alternate A, B, A, B instead of hammering one bond.
        self._sidelined: set = set()
        self._last_suspect = ""
        self._want = False            # the desired end state: running
        self._restart_pending = False  # a delayed stop owes us a spawn

    # ------------------------------------------------------------ control

    def start(self) -> None:
        self._want = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            if not self._stop.is_set():
                return               # already running normally
            # A stop was requested moments ago (settings save, setup
            # wizard); wait briefly for the old thread so this start
            # is not silently swallowed.
            thread.join(timeout=4.0)
            if thread.is_alive():
                # A long radio wait has not noticed the stop yet. Do
                # NOT drop the restart (that left the link dead until
                # the next app start in the field): hand it to the
                # exiting thread, which respawns the moment it ends.
                self._restart_pending = True
                log.warning("Phone link thread slow to stop; it will "
                            "restart itself the moment it exits")
                if not thread.is_alive():
                    # It exited in the instant between the check and
                    # the flag; nobody else will respawn, so do it.
                    self._maybe_respawn()
                return
        self._spawn()

    def _spawn(self) -> None:
        self._stop.clear()
        self._kick.clear()
        self._restart_pending = False
        self._thread = threading.Thread(
            target=self._run, name="jrl-phone-link", daemon=True)
        self._thread.start()

    def _maybe_respawn(self) -> None:
        """Called as a stopping thread exits: honor a restart that
        arrived while it was still winding down."""
        if self._restart_pending and self._want:
            log.info("Phone link restarting after its delayed stop")
            self._spawn()

    def stop(self) -> None:
        self._want = False
        self._restart_pending = False
        self._stop.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(lambda: None)  # wake the loop
            except RuntimeError:
                pass

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_connected(self) -> bool:
        """Live and subscribed right now, not merely configured."""
        return self._connected

    def kick(self) -> None:
        """Skip whatever backoff wait is in progress and retry now."""
        self._kick.set()

    def request_link_test(self) -> None:
        """Ask the live session to run one real ANCS round trip and
        answer on the test_result signal. Only meaningful while
        connected; the window checks is_connected() first."""
        self._test_flag.set()

    # ------------------------------------------------------------- thread

    def _run(self) -> None:
        import asyncio
        try:
            asyncio.run(self._main())
        except Exception:
            log.exception("Phone link thread ended unexpectedly")
            self.status.emit("error", "Phone link stopped unexpectedly; "
                             "switch it off and on to retry")
        finally:
            self._loop = None
            try:
                self._maybe_respawn()
            except Exception:
                log.exception("Phone link respawn failed")

    async def _main(self) -> None:
        import asyncio
        self._loop = asyncio.get_running_loop()
        try:
            import bleak  # noqa: F401
        except Exception as e:
            self.status.emit(
                "error",
                f"Bluetooth support is not installed ({e.__class__.__name__});"
                " run install.bat once to add it")
            return
        backoff = BACKOFF_START_S
        while not self._stop.is_set():
            address = (getattr(self._settings, "phone_ble_address", "")
                       or "").strip()
            name = (getattr(self._settings, "phone_ble_name", "")
                    or "").strip()
            if not name or name.lower().startswith("apple device"):
                name = "your iPhone"
            if not address:
                self.status.emit(
                    "info", "No iPhone chosen yet. Settings → Alerts → "
                    "Choose iPhone")
                await self._sleep(15)
                continue
            try:
                await self._session(address, name)
                backoff = BACKOFF_START_S     # a real session ran
                self._consecutive_failures = 0
            except PairingNeeded:
                # The phone refused the subscription. Never pair from
                # here: background pairing prompts appear at random
                # moments and expire unseen. Distinguish the two
                # causes so the user fixes the right thing; the
                # paired-but-blocked case then completes BY ITSELF on
                # a later retry, the moment the switch is flipped.
                suspect = (self._last_suspect or address or "").upper()
                if suspect:
                    self._sidelined.add(suspect)
                rows = await self._paired_iphones()
                paired_now = False
                try:
                    record = await _windows_pairing(address)
                    paired_now = bool(record is not None and getattr(
                        record, "is_paired", False))
                except Exception:
                    paired_now = False
                # PAIRED LIST IS THE AUTHORITY (3.5.3, field report
                # #13): _windows_pairing is blind behind a rotating
                # anonymous address and answers "not paired" while the
                # bond sits in Windows under the real identity. Any
                # iPhone entry in the paired list means paired; the
                # per-address record is only trusted when it says YES.
                if paired_now or rows:
                    pc = platform.node() or "this PC"
                    self.status.emit(
                        "down",
                        f"Paired with {name}, but the phone is not "
                        "sharing notifications yet. On the iPhone: "
                        f"Settings → Bluetooth → '{pc}' → (i) → turn "
                        "ON Share System Notifications (pick Other if "
                        "asked what kind of device; if the switch is "
                        "missing, wait a minute while this app retries "
                        "and reopen the (i)). The link finishes by "
                        f"itself once it is on. No (i) at all? "
                        f"{forget_coaching()}")
                else:
                    self.status.emit(
                        "down",
                        "Your iPhone is not paired with this PC yet. "
                        "Pair it through Phone Link's QR flow (the "
                        "wizard's 'Phone Link pairing…' button opens "
                        "it), check a notification shows in Phone "
                        "Link once, then run Connect my iPhone in "
                        "Settings → Alerts. This app attaches to that "
                        "pairing; it never creates its own.")
                # Pace by evidence: while an un-tried paired sibling
                # remains, come back fast and try it; only slow down
                # once every entry has had its turn.
                await self._sleep(8 if self._untried(rows) else 60)
                backoff = 60
            except AncsMissing:
                # Reachable, but not serving iPhone notifications. If
                # the paired list names this very entry an iPhone, the
                # phone is WITHHOLDING the service (iPhones publish
                # ANCS intermittently and only toward connections they
                # trust), not answering from the wrong device. Either
                # way, sideline it and keep probing.
                suspect = (self._last_suspect or address or "").upper()
                if suspect:
                    self._sidelined.add(suspect)
                self._needs_probe = True
                rows = await self._paired_iphones()
                known = {addr for _n, addr in rows}
                if suspect and suspect in known:
                    self.status.emit(
                        "down",
                        f"{name} answered without the notification "
                        "service this time (iPhones publish it "
                        "intermittently, toward connections they "
                        "trust). Retrying, and trying the other "
                        "paired entry if there is one.")
                else:
                    self.status.emit(
                        "down",
                        "The chosen device does not serve iPhone "
                        "notifications, so it is probably not your "
                        "iPhone. The app is now checking nearby Apple "
                        "devices for the right one; you can also use "
                        "Connect my iPhone in Settings → Alerts.")
                await self._sleep(8 if self._untried(rows) else 30)
                backoff = 30
            except Exception as e:
                reason = str(e) or e.__class__.__name__
                self._consecutive_failures += 1
                if self._consecutive_failures == 2:
                    # The remembered address may be a rotated ghost that
                    # still advertises but never answers. Stop hammering
                    # it: from here the phone is re-identified by proof
                    # (the notification-service probe) instead.
                    self._needs_probe = True
                    reason += "; will re-identify the phone by proof"
                self.status.emit(
                    "down", f"{name} not reachable ({reason[:150]}); "
                    f"retrying in {backoff}s")
                await self._sleep(backoff)
                backoff = min(60, backoff * 2)

    async def _sleep(self, seconds: float) -> None:
        import asyncio
        step_end = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < step_end:
            if self._kick.is_set():
                self._kick.clear()
                return          # the user asked for an immediate retry
            await asyncio.sleep(0.25)

    # ------------------------------------------------------------ session

    async def _paired_iphones(self) -> list:
        """The Windows paired list's iPhone entries, never raising."""
        try:
            return await _paired_iphone_rows(
                getattr(self._settings, "phone_ble_name", "") or "")
        except Exception:
            return []

    def _untried(self, rows: list) -> bool:
        """True while a paired iPhone entry has not been sidelined yet:
        the cue to retry fast (8s) instead of the long backoff."""
        return any(addr not in self._sidelined for _n, addr in rows)

    async def _rotation_choice(self):
        """After a failure, the other un-sidelined paired iPhone entry.

        Returned as a plain identity STRING for a direct connect: the
        entry came from the paired list, so a by-address scan adds
        nothing but a wait. When every entry has been tried, the set
        resets to the most recent suspect so retries alternate A, B,
        A, B between two bonds instead of settling on either. None
        while nothing has failed, or nothing paired remains."""
        if not self._sidelined:
            return None
        rows = await self._paired_iphones()
        addresses = [addr for _n, addr in rows]
        if not addresses:
            return None
        fresh = [a for a in addresses if a not in self._sidelined]
        if not fresh:
            recent = (self._last_suspect or "").upper()
            self._sidelined = {recent} if recent else set()
            fresh = [a for a in addresses if a not in self._sidelined]
        if not fresh:
            return None
        return fresh[0]

    async def _find_device(self, address: str, name: str):
        """Resolve the phone with the strongest identity available.

        The paired list is consulted first: a bonded iPhone is reachable
        by its stable identity address even while its advertisements are
        anonymous, and a re-pairing that changed the address is healed
        here by matching the remembered name. Then the address lookup,
        then a scan that also recognizes anonymous Apple broadcasters."""
        from bleak import BleakScanner
        self.status.emit("info", f"Looking for {name}…")
        # 3.5.3 rotation first: a failure sidelined the last connected
        # address, and the paired list may hold a sibling bond that
        # actually carries the phone's notification permission. It is
        # a bonded identity, so connect to it directly; a by-address
        # scan cannot see a bonded identity anyway.
        rotated = await self._rotation_choice()
        if rotated is not None:
            self.status.emit(
                "info", f"Trying the next paired entry for {name}…")
            return rotated
        if self._needs_probe:
            # The stored entry just failed the service check, so the
            # stored-address and paired shortcuts are poisoned: they
            # reconnect to the same suspect record forever. Go straight
            # to proof over the live anonymous broadcasts.
            found = await BleakScanner.discover(
                timeout=SCAN_TIMEOUT_S, return_adv=True)
            candidates = [
                (device, getattr(adv, "rssi", None))
                for device, adv in found.values()
                if APPLE_COMPANY_ID in (
                    getattr(adv, "manufacturer_data", None) or {})]
            if candidates:
                device = await self._probe_candidates(candidates, name)
                if device is not None:
                    return device
            # Nothing proved itself; fall through to the normal paths.
        target = address
        try:
            wanted_name = (getattr(self._settings, "phone_ble_name", "")
                           or "").strip().lower()
            for row_name, row_addr, _src, _rssi in await _paired_rows():
                if (row_addr.lower() == address.lower()
                        or (wanted_name
                            and row_name.strip().lower() == wanted_name)):
                    target = row_addr
                    break
        except Exception:
            log.debug("Paired resolution failed", exc_info=True)
        try:
            device = await BleakScanner.find_device_by_address(
                target, timeout=SCAN_TIMEOUT_S)
            if device is not None:
                return device
        except Exception:
            log.debug("Address lookup failed; scanning", exc_info=True)
        wanted = (getattr(self._settings, "phone_ble_name", "")
                  or "").strip().lower()
        found = await BleakScanner.discover(
            timeout=SCAN_TIMEOUT_S, return_adv=True)
        apple_only = []
        for device, adv in found.values():
            device_addr = (device.address or "").lower()
            if device_addr in (address.lower(), target.lower()):
                return device
            if wanted and (device.name or "").strip().lower() == wanted:
                return device
            if APPLE_COMPANY_ID in (
                    getattr(adv, "manufacturer_data", None) or {}):
                apple_only.append((device, getattr(adv, "rssi", None)))
        if len(apple_only) == 1 and not self._needs_probe:
            # Exactly one anonymous Apple broadcaster in range: that is
            # the phone. With several (AirPods, an iPad), guessing could
            # latch onto the wrong thing, so those are probed instead.
            return apple_only[0][0]
        if apple_only:
            device = await self._probe_candidates(apple_only, name)
            if device is not None:
                return device
        return target        # let bleak try the identity address anyway

    async def _probe_candidates(self, apple_only, name):
        """The stored address failed, so identify the phone by proof:
        briefly ask the strongest anonymous Apple devices whether they
        serve the notification service to this PC. This is how the
        link survives address rotation without ever knowing a name."""
        apple_only.sort(key=lambda pair: -(pair[1] if pair[1] is not None
                                           else -999))
        for device, _rssi in apple_only[:3]:
            if self._stop.is_set():
                return None
            if ((getattr(device, "address", "") or "").upper()
                    in self._sidelined):
                # This very address just failed (refused or serviceless);
                # proving it again would only re-enter the same dead end.
                continue
            self.status.emit(
                "info", f"Checking a nearby Apple device for {name}…")
            if await probe_ancs(device):
                self._needs_probe = False
                self.learned.emit(
                    getattr(self._settings, "phone_ble_name", "")
                    or "iPhone (verified)", device.address)
                return device
        return None

    async def _session(self, address: str, name: str) -> None:
        """One connected session: subscribe, serve, until disconnect."""
        import asyncio
        from bleak import BleakClient
        device = await self._find_device(address, name)
        disconnected = asyncio.Event()

        def on_disconnect(_client) -> None:
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(disconnected.set)

        client = BleakClient(device, timeout=CONNECT_TIMEOUT_S,
                             disconnected_callback=on_disconnect,
                             winrt={"use_cached_services": False})
        await client.connect()
        # Whatever fails from here on, THIS is the address that failed:
        # sidelining the stored address instead let a rotated ghost keep
        # its clean record while the real culprit was retried forever.
        self._last_suspect = (
            (getattr(client, "address", "") or "")
            or (device if isinstance(device, str) else "")
            or address or "").upper()
        advertiser = None
        try:
            # Proof before subscription: a device without the service
            # gets a precise diagnosis instead of a generic failure.
            try:
                uuids = {str(getattr(s, "uuid", "")).lower()
                         for s in client.services}
            except Exception:
                uuids = set()
            if uuids and ancs.SERVICE_UUID not in uuids:
                raise AncsMissing(name)
            state = await self._subscribe(client, name)
            # A live subscription settles every open question about
            # which bond is the right one: the sideline set restarts
            # empty so the next hiccup is judged on fresh evidence.
            self._sidelined.clear()
            connected_addr = (getattr(client, "address", "") or "").upper()
            if connected_addr and connected_addr != address.upper():
                self.learned.emit(
                    getattr(self._settings, "phone_ble_name", "")
                    or "iPhone", connected_addr)
            advertiser = await self._try_solicitation_advert()
            self._connected = True
            self.status.emit(
                "up", f"Connected to {name}. New iPhone notifications "
                "will appear here while it is in Bluetooth range.")
            worker = asyncio.create_task(self._serve(client, state))
            stopper = asyncio.create_task(self._watch_stop())
            done, pending = await asyncio.wait(
                {worker, stopper, asyncio.create_task(disconnected.wait())},
                return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if self._stop.is_set():
                self.status.emit("info", "Phone link switched off")
                return
            self.status.emit(
                "down", f"{name} disconnected (out of range or Bluetooth "
                "off); reconnecting…")
        finally:
            self._connected = False
            if advertiser is not None:
                try:
                    advertiser.stop()
                except Exception:
                    pass
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _watch_stop(self) -> None:
        import asyncio
        while not self._stop.is_set():
            await asyncio.sleep(0.25)

    # -------------------------------------------------------- subscribing

    async def _subscribe(self, client, name: str) -> dict:
        """Subscribe Data Source then Notification Source. The worker
        NEVER pairs: a refused subscription means the bond is missing
        or revoked, and only the guided setup may create it, where the
        user is watching the phone for the prompt."""
        import asyncio
        state = {
            "queue": asyncio.Queue(),
            "pending": None,          # the response object being fed
            "dropped": 0,
        }

        def on_data_source(_char, data: bytearray) -> None:
            pending = state["pending"]
            if pending is not None:
                pending.feed(bytes(data))

        def on_notification_source(_char, data: bytearray) -> None:
            event = ancs.parse_source_event(bytes(data))
            if event is None or not ancs.should_alert(event):
                return
            if state["queue"].qsize() >= MAX_PENDING:
                state["dropped"] += 1
                return
            state["queue"].put_nowait(event)

        async def start_notifies() -> None:
            await client.start_notify(
                ancs.DATA_SOURCE_UUID, on_data_source)
            await client.start_notify(
                ancs.NOTIFICATION_SOURCE_UUID, on_notification_source)

        try:
            await start_notifies()
        except Exception as e:
            raise PairingNeeded(name) from e
        return state

    # ------------------------------------------------------------ serving

    async def _serve(self, client, state: dict) -> None:
        """Fetch attributes for queued events, one command in flight."""
        import asyncio
        while not self._stop.is_set():
            if self._test_flag.is_set():
                self._test_flag.clear()
                await self._run_link_test(client, state)
            try:
                # A short tick so an on-demand link test is picked up
                # promptly; notifications themselves arrive by callback
                # and never wait on this.
                event = await asyncio.wait_for(
                    state["queue"].get(), timeout=0.25)
            except asyncio.TimeoutError:
                if state["dropped"]:
                    n, state["dropped"] = state["dropped"], 0
                    self.status.emit(
                        "info", f"{n} more iPhone notifications arrived "
                        "in a burst and were kept quiet")
                continue
            try:
                assembled = await self._fetch_attributes(
                    client, state, event)
            except Exception as e:
                log.debug("Attribute fetch failed for %s: %s",
                          event.uid, e)
                continue
            if assembled is None:
                continue
            app_id = assembled.get("app_id", "")
            if ancs.app_id_ignored(
                    app_id,
                    getattr(self._settings, "phone_ignore_apps", "")):
                continue
            app_name = await self._app_display_name(client, state, app_id)
            shaped = ancs.presentation_of(assembled, app_name)
            payload = dict(assembled)
            payload["app_name"] = shaped["app_name"]
            payload["body"] = shaped["body"]
            payload["category"] = event.category_name
            self.notification.emit(payload)

    async def _run_link_test(self, client, state: dict) -> None:
        """One real question over the live link, on demand: ask the
        phone for the display name of its own Settings app through the
        Control Point and Data Source. An answer proves the connection,
        the encryption, and both ANCS channels end to end; only the
        phone-side 'Share System Notifications' switch is beyond what
        this PC can measure, and the result text says so."""
        probe_app = "com.apple.Preferences"
        started = time.monotonic()
        try:
            self._app_names.pop(probe_app, None)   # force a real fetch
            answer = await self._app_display_name(client, state, probe_app)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if answer:
                self.test_result.emit(
                    True,
                    f"Your iPhone answered in {elapsed_ms} ms (its "
                    f"'{answer}' app). The link is fully alive; if real "
                    "notifications still stay quiet, turn on Share "
                    "System Notifications on the phone: Settings → "
                    "Bluetooth → (i) next to this PC.")
            else:
                self.test_result.emit(
                    False,
                    "Connected, but the phone did not answer the test "
                    "question within 10 seconds. Lock and unlock the "
                    "phone and try once more.")
        except Exception as e:
            self.test_result.emit(
                False, f"The test could not run: "
                f"{str(e)[:120] or e.__class__.__name__}")

    async def _fetch_attributes(self, client, state: dict, event):
        import asyncio
        response = ancs.NotificationAttributesResponse(event.uid)
        state["pending"] = response
        try:
            await client.write_gatt_char(
                ancs.CONTROL_POINT_UUID,
                ancs.build_get_notification_attributes(event.uid),
                response=True)
            deadline = time.monotonic() + COMMAND_TIMEOUT_S
            while not response.done and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        finally:
            state["pending"] = None
        if not response.done or response.overflowed:
            return None
        return response.result()

    async def _app_display_name(self, client, state: dict,
                                app_id: str) -> str:
        """Cached per app; a failed lookup falls back to the bundle id."""
        import asyncio
        if not app_id:
            return ""
        cached = self._app_names.get(app_id)
        if cached is not None:
            return cached
        response = ancs.AppAttributesResponse(app_id)
        state["pending"] = response
        try:
            await client.write_gatt_char(
                ancs.CONTROL_POINT_UUID,
                ancs.build_get_app_attributes(app_id), response=True)
            deadline = time.monotonic() + COMMAND_TIMEOUT_S
            while not response.done and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        except Exception:
            log.debug("App name lookup failed for %s", app_id,
                      exc_info=True)
        finally:
            state["pending"] = None
        name = response.display_name if response.done else ""
        self._app_names[app_id] = name
        return name

    # ------------------------------------------- solicitation (optional)

    async def _try_solicitation_advert(self):
        """Best effort: advertise a service solicitation for ANCS so the
        iPhone itself reconnects to us when it comes back in range, the
        way it does for a watch. Many Windows adapters refuse custom
        advertisement sections; that is fine, the app reconnects on its
        own either way, so any failure here only logs."""
        import sys
        import uuid as uuid_mod
        if not sys.platform.startswith("win"):
            return None
        try:
            from winrt.windows.devices.bluetooth.advertisement import (
                BluetoothLEAdvertisementDataSection,
                BluetoothLEAdvertisementPublisher)
            from winrt.windows.storage.streams import DataWriter
            writer = DataWriter()
            writer.write_bytes(list(reversed(
                uuid_mod.UUID(ancs.SERVICE_UUID).bytes)))
            section = BluetoothLEAdvertisementDataSection()
            section.data_type = 0x15   # 128-bit Service Solicitation
            section.data = writer.detach_buffer()
            publisher = BluetoothLEAdvertisementPublisher()
            publisher.advertisement.data_sections.append(section)
            publisher.start()
            log.info("ANCS solicitation advertising started")
            return publisher
        except Exception as e:
            log.info("Solicitation advertising unavailable (%s); the app "
                     "will reconnect by itself instead", e)
            return None


def discover_phones(timeout: float = SCAN_TIMEOUT_S) -> tuple:
    """Blocking helper for the Settings picker. Returns (rows, notes):
    the merged paired-plus-scan rows, and human-readable notes about
    what each lookup found or why it failed, because a silently empty
    paired list already cost a field debugging round."""
    import asyncio

    async def _both():
        rows, notes = await _paired_rows_with_notes()
        try:
            scanned = await _scan_rows(timeout)
            rows.extend(scanned)
            notes.append(f"scan: {len(scanned)}")
        except Exception as e:
            if not rows:
                raise
            notes.append(f"scan failed: {str(e)[:80] or type(e).__name__}")
        return rows, notes

    rows, notes = asyncio.run(_both())
    if not any(r[2] in ("paired", "paired-voice") for r in rows):
        log.warning("Windows paired list came back empty: %s",
                    " · ".join(notes))
    return merge_device_rows(rows), notes


async def probe_ancs(target, timeout: float = 10.0):
    """Connect briefly and look for Apple's notification service.

    This is the decisive identification: among anonymous Apple
    broadcasters, the device that shows ANCS to this PC is an iPhone
    (or iPad) that trusts this PC. It never pairs, never subscribes,
    and disconnects immediately, so probing a stranger's device is a
    harmless glance at its public service list. Returns True (seen),
    False (connected, not seen), or None (could not connect)."""
    from bleak import BleakClient
    # Fresh discovery always: Windows caches a paired device's service
    # list, and one stale snapshot makes a genuine iPhone look like it
    # has no notification service on every later connect (field report
    # #12: a paired entry named 'iPhone' failing this check forever).
    client = BleakClient(target, timeout=timeout,
                         winrt={"use_cached_services": False})
    try:
        await client.connect()
    except Exception:
        return None
    try:
        uuids = {str(getattr(s, "uuid", "")).lower()
                 for s in client.services}
        return ancs.SERVICE_UUID in uuids
    except Exception:
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def find_iphones(progress=None, scan_timeout: float = SCAN_TIMEOUT_S,
                 max_probes: int = 8) -> tuple:
    """Blocking identification pass for the picker's Find button.

    Probes the paired LE entries and then the anonymous Apple
    broadcasters, strongest signal first, and returns
    (verified_rows, notes). A verified row is proof, not a guess."""
    import asyncio

    def say(text):
        if progress is not None:
            try:
                progress(text)
            except Exception:
                pass

    async def _find():
        verified = []
        paired, notes = await _paired_rows_with_notes()
        candidates = [r for r in merge_device_rows(paired)
                      if r[2] == "paired"]
        say("Checking the Windows paired list…")
        try:
            scanned = await _scan_rows(scan_timeout)
        except Exception as e:
            scanned = []
            notes.append(f"scan failed: {str(e)[:80] or type(e).__name__}")
        anonymous = sorted(
            (r for r in scanned if r[2] == "apple"),
            key=lambda r: -(r[3] if r[3] is not None else -999))
        candidates.extend(anonymous)
        candidates = candidates[:max_probes]
        if not candidates:
            notes.append("nothing to probe: no paired LE entries and no "
                         "Apple broadcasters in range")
        for index, (name, address, source, rssi) in enumerate(candidates):
            where = closeness(rssi)
            say(f"Asking device {index + 1} of {len(candidates)} whether "
                f"it serves iPhone notifications"
                + (f" ({where})" if where else "") + "…")
            seen = await probe_ancs(address)
            if seen:
                label = (name if source == "paired"
                         else "Your iPhone (verified: serves iPhone "
                         "notifications to this PC)")
                verified.append((label, address, "verified", rssi))
            elif seen is False:
                notes.append(f"{address}: connected, no notification "
                             "service visible")
            else:
                notes.append(f"{address}: did not accept a connection")
        return verified, notes

    return asyncio.run(_find())
