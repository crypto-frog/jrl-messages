---
layout: default
title: Architecture
---

# Architecture

[Home](index.html) | [Installation](installation.html) | [Known issues](known-issues.html)

This page explains how JRL Messages is built and why. The design goal is stated once and governs everything: **a message must never be silently missed.** Convenience, elegance, and even simplicity were traded away where they conflicted with that.

## The chain

```
iPhone
  |  (Apple's own sync)
Apple's cloud
  |  (Apple's own sync)
Mac: Messages.app  <-- chat.db -->  BlueBubbles server (REST + Socket.IO)
  |
Tailscale (WireGuard mesh; the Mac is reachable by a stable private address)
  |
Windows PC
  +-- Agent process (run_agent.py): sync, storage, notifications, repair
  +-- Window process (run.py): viewer and composer over a local pipe
```

Two principles picked this chain:

1. **Write custom code where the need is unusual; use maintained open source where the ground shifts underneath.** Apple changes macOS constantly, so talking to Messages.app is delegated to BlueBubbles, a maintained project whose whole job is tracking those changes. The unusual part, an aggressively complete Windows client, is custom.
2. **Own every link.** No Firebase, no Cloudflare tunnel, no third-party relay. BlueBubbles runs in plain LAN URL mode; Tailscale makes the LAN address privately reachable from anywhere.

## The two-process split (v3.0.0)

The single most important structural decision. The Windows side is two processes with distinct failure domains:

**The agent** (`app/agent/`) is a headless `QCoreApplication` started at logon. It owns:

- the Socket.IO connection and all HTTP polling against BlueBubbles
- the reconciliation engine and every SQLite write
- the notification pipeline (popups, sounds, the bell feed)
- the outbox (sends survive restarts and offline periods)
- the watchdog, the Wake Mac / Recover repair logic, and a maintenance lease
- a local pipe server for windows to attach to, with a versioned handshake

**The window** (`app/ui/`) is a pure viewer and composer. It renders from the shared database and the agent's event channel, and submits sends to the agent. It can be closed, killed, or never opened; collection continues.

The agent is kept alive by three overlapping mechanisms: a supervisor script (`agent_supervisor.pyw`), a Startup entry created by the installer, and an hourly scheduled-task failsafe. Duplicate launches are harmless by design; a second copy detects the first and exits.

Why it matters: before the split, a UI crash or an accidental window close stopped message collection. After it, the pipeline that receives messages contains no UI code at all. It also gives debugging clean lines: "the agent recorded it but the window did not show it" and "the agent never saw it" are different investigations.

## Module map

```
app/
  constants.py     Version, paths, and every tuning number, each with a
                   comment explaining the field lesson behind it
  config.py        Settings persistence; secrets go to the OS keyring
  agent/           The background process: core.py (workers, watchdog,
                   repair), server.py (pipe), policy.py, serialize.py
  api/             BlueBubbles REST and Socket.IO clients, typed models
  store/           SQLite schema, repository, the reconciliation engine
                   (sync.py, reconcile_core.py), outbox, attachment cache
  ui/              The window: main_window, thread_view, chat_list,
                   composer, settings, notification center, popups,
                   emoji picker, image viewer, theme
  phone/           The experimental Bluetooth stack: ancs.py (pure
                   protocol) and link.py (discovery, sessions, worker)
  util/            Text, time, verification-code extraction, Windows glue
mac/               Keepalive install scripts for the relay Mac
tools/             End-to-end harnesses and the startup-launcher builder
tests/             The regression battery (234 tests)
```

## The reconciliation engine

Push (Socket.IO) is treated as a latency optimization, never as the source of truth. Truth is established by reconciliation against the Mac's message database, keyed on `chat.db` ROWIDs, which BlueBubbles exposes as `originalROWID`.

- **Fixed numeric windows.** The cursor advances through intervals like `(cursor, cursor + 100]`. Because ROWID is integral, a window holds at most 100 rows and never needs mutable OFFSET pagination, which breaks when rows are inserted mid-walk.
- **Short-read protection.** A transiently incomplete server response is re-checked before a window is committed, so a hiccup cannot become a permanent hole.
- **Tail audit.** The newest 200 rows are re-read continuously; an old-dated message inserted late by iCloud still surfaces.
- **Deep audits.** A 24-hour wall-clock repair window and a rolling archive audit catch anything that slipped past everything else.
- **Gap-fill on reconnect** with a deliberate overlap window, plus a global newest-message safety net that works even if cursors are wrong.

Every constant in this engine lives in `app/constants.py` with a comment explaining the incident that set its value.

## Wake Mac: repairing Apple's idle holding

The strangest field bug in the project's history: texts existed on the iPhone but never reached the app, and then all appeared the moment a message was sent. Diagnosis (v2.3.0): the delay was upstream of Windows entirely. When Messages.app on the Mac goes idle, Apple can hold fresh messages back from the local database until the app is poked. No amount of Windows-side rescanning can fix that, because the rows do not exist on the Mac yet.

The cure is to force Messages to reconnect, via BlueBubbles' `POST /api/v1/mac/imessage/restart`. The agent detects the signature of the held state and performs this repair automatically, then verifies that the repair produced rows. A manual Wake Mac button exists too. The `mac/` keepalive scripts reduce how often the state arises by fighting sleep and App Nap.

## Notifications

Alerting was debugged in the field across the entire 3.1.x series until it earned trust, and the resulting rules are pinned:

- Sound is decoupled from the popup and fires first; a popup failure can never silence the sound.
- Popups fall back to Windows toasts and vice versa.
- Storm protection: a reconnect that back-fills history must not fire a burst of stale alerts. Nothing older than the freshness window ever alerts.
- A delivery ledger prevents duplicate alerts across agent restarts; the Test alert deliberately bypasses it.
- Texts you send to yourself (a natural way to test the line) alert like any other arrival, while anything sent from this app's own composer never alerts anywhere.
- Verification codes are detected, shown prominently, and offered with Copy and Fill buttons. Code-bearing messages also get inline Copy chips in the thread.
- The bell: an in-app notification center recording alerts, link events, and repairs, with half-hourly refresh of outage notices.

## The experimental Bluetooth stack

Fully described on the [known issues](known-issues.html) page. In one paragraph: `app/phone/ancs.py` is a pure-protocol implementation of Apple's Notification Center Service, and `app/phone/link.py` handles the genuinely hard part, which is finding and staying attached to a phone that rotates anonymous Bluetooth addresses and only serves ANCS to connections it trusts. The default flow never pairs; it rides the bond Microsoft Phone Link creates. This feature is architecturally quarantined: the agent's messaging pipeline never imports it, Bluetooth modules load lazily, and the whole stack is exercised in tests through fakes, so the core client builds and runs on machines with no Bluetooth at all.

## Testing philosophy

The battery is the project's institutional memory. The pattern, repeated for three major versions: a field report arrives (often a screenshot and an Activity log), the failure is reproduced in a test, the fix lands, and the test stays forever with a name that tells the story. Suites run headless (`QT_QPA_PLATFORM=offscreen`, `JRL_SMOKE=1`) on any OS. Four harnesses in `tools/` go further, running the pipeline, storm scenarios, an offscreen window, and a live agent process against a mock BlueBubbles server.

The consequence for contributors: the tests are not decoration. If your change breaks one, the presumption is that your change re-introduced a bug someone already lived through.

## Positioning: why "bring your own Mac"

A structural note for anyone eyeing a fork or a product. A mass-market Windows iMessage client has a two-machine floor (Apple's terms and technical reality both require a real Mac), legal risk on the reverse-engineering path, and poor economics on the cloud-Mac path. The viable niche is exactly what this project is: a premium, reliability-obsessed client for people who own a Mac and need their messages on a PC, with every link under their own control.
