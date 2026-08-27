"""Standalone iPhone-link diagnostic. Run this ON THE WINDOWS PC:

    python tools\\phone_link_probe.py

It walks the exact steps the in-app link performs, printing everything:
scan, connect, pair if needed, subscribe to ANCS, then live-print every
notification event and its fetched attributes until Ctrl+C. If the
in-app link ever misbehaves, run this and send the output; it shows
precisely which step the adapter, driver, or iPhone objected to.

Before running: pair the iPhone with Windows once (Windows Settings >
Bluetooth & devices > Add device), keep Bluetooth on on both, and keep
the phone unlocked nearby for the first pairing prompt.
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.phone import ancs  # noqa: E402


def say(msg):
    print(f"  {msg}", flush=True)


async def main():
    try:
        from bleak import BleakClient, BleakScanner
    except Exception as e:
        say(f"FAIL bleak is not installed: {e}")
        say("     run: pip install bleak")
        return 1

    from app.phone.link import (_paired_rows_with_notes, _scan_rows,
                                closeness, merge_device_rows, probe_ancs)
    say("reading the Windows paired lists and scanning 10 s…")
    rows, notes = [], []
    try:
        rows, notes = await _paired_rows_with_notes()
    except Exception as e:
        say(f"paired lists unavailable: {e}")
    for note in notes:
        say(f"note: {note}")
    try:
        rows.extend(await _scan_rows(10))
    except Exception as e:
        say(f"scan failed: {e}")
    rows = merge_device_rows(rows)
    if not rows:
        say("FAIL nothing found. Is Bluetooth on? Is the iPhone paired "
            "with Windows (Settings > Bluetooth & devices > Add device)?")
        return 1
    for index, (name, address, source, rssi) in enumerate(rows):
        where = closeness(rssi)
        say(f"[{index}] {name}  ({address})  [{source}"
            + (f", {where}]" if where else "]"))
    choice = input("pick a number, or f to FIND the iPhone by proof: "
                   ).strip().lower()
    if choice == "f":
        say("asking each Apple/paired device whether it serves iPhone "
            "notifications to this PC…")
        hit = None
        candidates = [r for r in rows if r[2] in ("paired", "apple")]
        for name, address, source, rssi in candidates[:8]:
            say(f"probing {address} [{source}]…")
            seen = await probe_ancs(address)
            say(f"  -> {'ANCS VISIBLE (this is the iPhone)' if seen else 'no ANCS' if seen is False else 'no connection'}")
            if seen and hit is None:
                hit = (name, address)
        if hit is None:
            say("no device proved to be the iPhone; re-pair with the "
                "phone unlocked and check Share System Notifications")
            return 1
        name, address = hit
        say(f"using {name} ({address})")
    else:
        name, address, _source, _rssi = rows[int(choice or 0)]
    say(f"resolving {name}…")
    device = await BleakScanner.find_device_by_address(address, timeout=10)
    if device is None:
        say("no live advertisement for that address; trying it directly "
            "(works for paired phones)")
        device = address

    say(f"connecting to {name}…")
    events: asyncio.Queue = asyncio.Queue()
    pending = {}

    def on_ns(_c, data):
        event = ancs.parse_source_event(bytes(data))
        say(f"event: {event}")
        if event is not None and event.event_id == ancs.EVENT_ADDED:
            events.put_nowait(event)

    def on_ds(_c, data):
        r = pending.get("r")
        if r is not None:
            r.feed(bytes(data))

    async with BleakClient(device, timeout=20) as client:
        say("connected. subscribing to ANCS…")
        try:
            await client.start_notify(ancs.DATA_SOURCE_UUID, on_ds)
            await client.start_notify(ancs.NOTIFICATION_SOURCE_UUID, on_ns)
        except Exception as e:
            say(f"subscribe refused ({e}); pairing (watch both screens)…")
            try:
                await client.pair()
            except Exception as e2:
                say(f"pair() said: {e2} (often still fine)")
            await asyncio.sleep(1)
            await client.start_notify(ancs.DATA_SOURCE_UUID, on_ds)
            await client.start_notify(ancs.NOTIFICATION_SOURCE_UUID, on_ns)
        say("SUBSCRIBED. Send yourself a notification (or wait for one).")
        say("If nothing arrives: iPhone Settings > Bluetooth > tap this "
            "PC's (i) > turn on Share System Notifications.")
        while True:
            try:
                event = await asyncio.wait_for(events.get(), timeout=300)
            except asyncio.TimeoutError:
                say("(still listening…)")
                continue
            response = ancs.NotificationAttributesResponse(event.uid)
            pending["r"] = response
            await client.write_gatt_char(
                ancs.CONTROL_POINT_UUID,
                ancs.build_get_notification_attributes(event.uid),
                response=True)
            for _ in range(200):
                if response.done:
                    break
                await asyncio.sleep(0.05)
            pending["r"] = None
            if response.done and not response.overflowed:
                say(f"attributes: {response.result()}")
            else:
                say("attribute fetch timed out or was malformed")


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("bye")
