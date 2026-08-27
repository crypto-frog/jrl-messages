---
layout: default
title: Known issues
---

# Known issues and open problems

[Home](index.html) | [Architecture](architecture.html) | [Installation](installation.html)

## The headline issue: iPhone notification mirroring over Bluetooth is not dependable

**Status: experimental, off by default, sometimes works, usually does not.** This is the project's primary open problem and its standing invitation to contributors.

### What the feature is

iMessage itself flows over the Mac relay and is rock solid; that is the core product. Separately, since v3.3.0 the app can try to mirror the iPhone's **own notification banners** (from any app on the phone: banking, calendars, other messengers) to the PC, so the phone can stay in a drawer. iOS exposes exactly one sanctioned channel for this: the Apple Notification Center Service (ANCS), a Bluetooth LE GATT service the phone offers to paired devices it trusts.

### Why it is hard, in plain terms

Five independent difficulties stack on top of each other:

1. **The phone hides its identity.** An iPhone advertises over Bluetooth LE using rotating anonymous addresses (resolvable private addresses). The "same" phone is a different address every time you look, and only devices holding the pairing bond can resolve the rotation. Any code that stores "the phone's address" is building on sand.
2. **Windows answers identity questions wrongly.** Asking Windows "is this address paired?" about a fresh rotated address returns no, even while the bond sits in Windows under the phone's real identity. The 3.5.x series established the doctrine that the paired-device **list** is the only authority, and the per-address answer is only trusted when it says yes.
3. **ANCS appears and disappears.** The phone publishes the notification service intermittently and only toward connections it currently trusts. Seeing no ANCS on a known-paired iPhone usually means the phone is withholding it at that moment, not that you found the wrong device.
4. **Windows caches stale service data.** The GATT cache can report yesterday's service list for today's connection. Every service inspection in this codebase bypasses the cache, and that rule is test-pinned.
5. **Pairing UI on iOS is hostile to third parties.** The only flow that reliably produces a correct, fully-authorized bond is Microsoft Phone Link's own QR pairing. After a long campaign (the 3.4.x notes tell the story), the project adopted the attach-only doctrine: the default setup never initiates pairing; it rides Phone Link's bond and coaches the user to Phone Link when no bond exists. The app also never unpairs anything, ever.

On top of all five, duplicate bonds happen in the wild (an old pairing attempt plus Phone Link's bond, both named "iPhone"), and only one of them carries the phone's "Share System Notifications" permission. Current code sidelines a failing entry and rotates to the other, alternating with backoff.

### The current open question

Field evidence suggests one more layer: the phone may scope ANCS authorization **per connection**, serving notifications to Phone Link's live link while refusing a second subscriber on the same bond. Establishing whether that is true (and if so, whether a subscriber can present itself in a way the phone accepts) is genuinely new territory. The decisive experiment is simple to state: with pairing healthy and Phone Link's own window demonstrably showing a test notification, does this app's subscribe still get refused on every paired entry? Field reports containing that comparison, plus the wizard notes and Test link output, are the most valuable data the project can receive. There is a dedicated issue template for exactly this.

### How this limits functionality, and how it does not

- **Not limited:** everything about messaging. Sending, receiving, history, group chats, attachments, and notifications **for your messages** travel over the Mac relay (Tailscale + BlueBubbles) and involve no Bluetooth whatsoever. With the Bluetooth feature off (the default), JRL Messages is a complete and dependable iMessage client. The stack is also architecturally quarantined: the messaging pipeline never imports it, and the app runs fine on PCs with no Bluetooth hardware.
- **Limited:** mirroring of the phone's other notifications to the PC. Until this problem is solved, do not rely on it; keep Microsoft Phone Link as the fallback for those banners (and if both apps announce the same event, turn off notifications in one of them).

### If you want to work on it

Start with `app/phone/link.py` and `app/phone/ancs.py`, the `tests/test_phone_link.py` suite (72 tests encoding everything learned so far), and release notes 3.3.0 through 3.5.3 in order; they are a complete field diary. The doctrine section of [CONTRIBUTING.md](https://github.com/OWNER/jrl-messages/blob/main/CONTRIBUTING.md) lists the radio invariants that must survive any redesign. Useful background: Apple's ANCS specification, Bluetooth LE privacy (RPA resolution), and the Windows.Devices.Bluetooth pairing and GATT session APIs.

## Other known limits

- **SMS requires the iPhone to be on.** Green-bubble traffic physically routes through the phone, so it needs power and service somewhere in the world. iMessage does not care where the phone is.
- **Some group-management actions** (renaming, adding participants) depend on BlueBubbles' Private API mode being enabled on the Mac.
- **The Mac must stay healthy.** Sleep, App Nap, and Messages.app idling are the enemies; the `mac/` keepalive scripts and the automatic Wake Mac repair exist for exactly this. A still, broken connection ring in the app means the chain is down at the Mac or Tailscale layer.
- **Two machines, by design.** The project deliberately does not pursue Apple-protocol reverse-engineering, for durability and legal reasons. See the positioning note at the end of the [architecture](architecture.html) page.
