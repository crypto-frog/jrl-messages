# Changelog

Condensed history. The full, candid engineering story of each release is in the corresponding `RELEASE-NOTES-<version>.txt` file at the repository root; those are preserved verbatim and are worth reading.

## 3.6.0 (current)

- Per-conversation drafts: unsent text and staged files are kept per conversation, restored on return, cleared on send. This also fixes a defect where a half-typed message followed the user into the next conversation opened.
- The jump pill counts new arrivals ("2 new messages") while the user is scrolled up reading history, instead of yanking the view or staying silent.
- Whole-conversation plain-text transcript export: a header button and Ctrl+E write every message as dated lines with Me/contact labels, edit and retraction markers, and attachment names.
- A keyboard shortcuts reference card on Ctrl+/ or F1.
- Twelve new tests pin these behaviors; the agent process is untouched.

## 3.5.3

- The paired-list authority fix (field report #13, "its says my phone is not paired in Link but it is"). The Windows paired-device list is now the sole authority on whether an iPhone is paired; the per-address record, blind behind a rotating anonymous address, is only trusted when it answers yes. The false "not paired with this PC" verdict is pinned out by test whenever the list holds an iPhone entry.
- Failures sideline the actual connected address and the worker rotates between paired iPhone entries (two bonds can coexist and only one carries the phone's notification permission), alternating with fast retries while an untried sibling remains.
- The setup wizard records refusal verdicts and keeps trying remaining candidates: a working second entry beats an earlier refusal, and the paired list gets the last word before any "needs Phone Link" verdict.
- Nine new tests (PairedListAuthorityTests) pin the doctrine.

## 3.5.2

- Bluetooth: the Windows GATT cache is bypassed on every service inspection (`use_cached_services=False` everywhere), curing verdicts poisoned by stale cached data.
- When identity proof is pending, stored and paired shortcuts are skipped instead of trusted.
- Paired-row plumbing unified on a four-tuple shape; unpack fixed and test-pinned.

## 3.5.1

- Worker lifecycle: a delayed stop now hands the respawn to the exiting thread, so a restart is never dropped and the link cannot silently die after a settings change.

## 3.5.0

- The pairing pivot: the default iPhone setup became **attach-only**. The app never initiates Bluetooth pairing in its default flow; it finds the phone by proof, connects, and subscribes over the bond that Microsoft Phone Link created. Phone Link's QR flow is acknowledged as the only reliably correct iOS pairing UI, and the wizard routes users there.
- The advanced coached pairing ceremony from 3.4.x remains available behind an explicit option.

## 3.4.0 through 3.4.4

- The pairing campaign: coached pairing ceremonies, prompt diagnosis, ghost-entry recovery recipes, and finally the NO-UNPAIR doctrine: the app never unpairs automatically and never pairs in the background. Three distinct field-reported bugs fixed from a single log in 3.4.2.

## 3.3.0 through 3.3.3

- iPhone notification mirroring over Bluetooth LE (Apple's ANCS), as an experimental feature: pure-protocol ANCS parser, device discovery, find-by-proof identification, learned-address persistence, a Test link diagnostic, and escalating probes when the phone withholds the notification service.

## 3.2.0

- The in-app notification center (the bell), theme accent tints, and tabbed Settings.

## 3.1.0 through 3.1.5

- The reliability series: ghost-window storms run to ground and pinned; alerts made independent of window focus; the silent-alert failure answered structurally (sound decoupled and first); self-conversation texts recognized and alerting correctly; Mac installer hotfix.

## 3.0.0

- The architecture split. Everything network-facing moved into a background **agent** process that starts at logon and runs regardless of the window: sync, watchdog, outbox, Wake Mac, notifications, and all database writes. The window became a pure viewer and composer over a local pipe with a versioned handshake. Kept alive by a supervisor, a Startup entry, and an hourly failsafe.

## 2.3.0

- The missing-texts diagnosis: proof that Apple's Messages.app can hold messages upstream when idle, and the first automated Wake Mac repair (restarting the Messages connection on the Mac through BlueBubbles).

## 2.2.0

- Recover: a non-destructive global resync button.

## 2.1.0

- Verification-code popups with Copy and Fill.
