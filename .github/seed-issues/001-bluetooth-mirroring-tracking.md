Title: Tracking: make iPhone notification mirroring over Bluetooth dependable

Labels: bluetooth, help wanted, tracking

---

This is the project's primary open problem. Full background: [docs/known-issues.md](../blob/main/docs/known-issues.md), release notes 3.3.0 through 3.5.3, and the radio invariants in [CONTRIBUTING.md](../blob/main/CONTRIBUTING.md).

**Scope.** This issue tracks the experimental feature that mirrors the iPhone's own notification banners to the PC via Apple's ANCS over Bluetooth LE. It is entirely separate from messaging: iMessage sync travels over the Mac relay and is unaffected.

**Where things stand (v3.6.0).**

- The default flow is attach-only: the app never pairs, it rides the bond created by Microsoft Phone Link, and it never unpairs anything.
- Rotating anonymous addresses are handled by proof-based discovery, learned-address persistence, and cache-bypassing probes (`use_cached_services=False` everywhere).
- The Windows paired-device list is treated as the sole authority on "is an iPhone paired"; per-address queries are only trusted when they answer yes.
- Duplicate "iPhone" bonds are handled by sidelining the failing entry and rotating to the other, with alternating retries.
- 72 unit tests pin everything learned so far (`tests/test_phone_link.py`).

**The open question.** Field evidence suggests the phone may authorize ANCS per connection, serving Phone Link's live link while refusing a second subscriber on the same bond. The decisive experiment: with Phone Link's own window demonstrably showing a test notification, does this app's subscribe still get refused on every paired entry?

**How to contribute.**

1. Field data: file a "Bluetooth pairing report" issue (there is a dedicated template) including the Phone Link comparison above, wizard notes, and Test link output.
2. Code: candidates include deeper Windows.Devices.Bluetooth session control, GATT session maintenance strategy, and any documented path by which a second subscriber can present acceptably to iOS. All radio invariants in CONTRIBUTING.md must survive any approach: never auto-unpair, never background-pair, never trust the GATT cache.

Sub-issues welcome; keep this one as the index.
