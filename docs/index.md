---
layout: default
title: JRL Messages
---

# JRL Messages

**A reliability-first iMessage client for Windows. Bring your own Mac.**

JRL Messages puts full iMessage on a Windows PC: blue bubbles, group chats, attachments, verification-code popups, and desktop notifications. It relays through a Mac you own, running the open-source BlueBubbles server, reached privately over Tailscale. No cloud middleman ever sees a message.

It was built by a practicing lawyer with one requirement: **never miss a message.** The whole design follows from that.

- [Repository and source code](https://github.com/OWNER/jrl-messages)
- [Installation guide](installation.html)
- [Architecture](architecture.html)
- [Known issues and open problems](known-issues.html)
- [Report a bug or contribute](https://github.com/OWNER/jrl-messages/issues)

## The idea in one diagram

```
iPhone <--> Apple's cloud <--> Mac (Messages.app + BlueBubbles)
                                    |
                               Tailscale (private mesh VPN)
                                    |
                          Windows PC running JRL Messages
                            agent process: always-on sync
                            window process: viewer/composer
```

Your Mac stays home, signed into your Apple ID, acting as a relay you control end to end. Your PC (a laptop works from anywhere in the world) runs two cooperating processes: a background **agent** that collects, records, and announces messages from the moment you log on, and a **window** that is purely a viewer and composer. Close the window, or crash it outright, and messages keep arriving.

## Why not the alternatives

- Cloud "Mac rental" iMessage services hold your Apple ID and every message on hardware you cannot inspect.
- Protocol reverse-engineering approaches get shut down by Apple.
- Microsoft Phone Link's iMessage bridge is shallow: no history, weak group support, silent gaps.

The two-machine design is the honest architecture. It costs a spare Mac; it buys you a private, inspectable, repairable message pipeline.

## What makes it dependable

- A reconciliation engine that walks the Mac's message database in fixed ROWID windows, with tail audits, deep wall-clock audits, and rolling archive repair, so late-inserted iCloud messages and transient short reads cannot create silent gaps.
- **Wake Mac**: automatic, verified repair of Apple's known idle-holding behavior, where Messages.app quietly delays handing over new texts until poked.
- A watchdog, a supervisor, a Startup entry, and an hourly failsafe that keep the agent alive without you thinking about it.
- Notification storm protection, verification-code Copy and Fill, an in-app notification center, and alerting that was debugged in the field until it earned trust.
- 234 unit tests and four end-to-end harnesses, nearly all of them pinning specific bugs that once happened on a real machine.

## Honest limits

One feature is explicitly experimental and unreliable today: mirroring the iPhone's own notifications (from other apps) to the PC over Bluetooth. It sometimes works and usually does not, for deep reasons explained on the [known issues](known-issues.html) page. It is off by default and completely separate from messaging: **iMessage itself does not touch Bluetooth and works regardless.** This is the project's main open problem and the most valuable place to contribute.

## Get involved

The project welcomes field reports, code, and Mac-side hardening. Start with the [repository README](https://github.com/OWNER/jrl-messages#readme), then [CONTRIBUTING](https://github.com/OWNER/jrl-messages/blob/main/CONTRIBUTING.md). Bug reports with a version number, an Activity log, and a screenshot have driven every fix in this project's history; the issue templates show you exactly what to include.
