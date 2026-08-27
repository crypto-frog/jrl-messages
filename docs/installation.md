---
layout: default
title: Installation
---

# Installation

[Home](index.html) | [Architecture](architecture.html) | [Known issues](known-issues.html)

The authoritative, step-by-step guides ship inside the repository and are written for careful non-programmers: [SETUP.md](https://github.com/crypto-frog/jrl-messages/blob/main/SETUP.md) for the whole chain and [MAC-SETUP.md](https://github.com/crypto-frog/jrl-messages/blob/main/MAC-SETUP.md) for hardening the Mac. This page is the orientation layer: what you need, what gets installed, and in what order.

## What you need

| Piece | Details |
|---|---|
| An iPhone | Your normal phone, anywhere in the world. Nothing is installed on it |
| A Mac | Stays at home, plugged in, signed into your Apple ID, Messages working. Clamshell mode (lid closed) is fine. This is the relay |
| A Windows PC | Windows 10 or 11. This is where you read and write messages. A laptop works from anywhere |
| Python 3.12 | On the PC, from python.org. The installer script expects the `py -3.12` launcher |
| BlueBubbles server | Free, open source, installed on the Mac. Run it in plain LAN URL mode: no Firebase, no Cloudflare proxy. Set a strong server password |
| Tailscale | Free tier, installed on both the Mac and the PC, signed into the same account. It gives the Mac a stable private address reachable from anywhere |

Optional, only for the experimental iPhone notification mirroring feature: a Bluetooth LE adapter on the PC and Microsoft Phone Link paired to the iPhone. Read [known issues](known-issues.html) before spending time on this.

## Order of operations

**1. iPhone (5 minutes).** Confirm iMessage is signed in and syncing with the Mac (Settings, Messages, Text Message Forwarding for SMS if you want green bubbles too).

**2. Mac (30 to 45 minutes, once).**

- Sign Messages into your Apple ID and confirm conversations appear.
- Install the BlueBubbles server, set the password, note the port (default 1234).
- Install Tailscale and sign in. Note the Mac's tailnet name (like `my-mac.tail1234.ts.net`) or its 100.x.x.x address.
- Recommended: enable Screen Sharing so you can reach the Mac's screen over Tailscale from anywhere for repairs.
- Run the keepalive installer from the repository's `mac/` folder. It configures the Mac against sleep and App Nap, the main causes of the "held messages" condition.

**3. Windows PC (15 minutes).**

- Install Python 3.12 and Tailscale (same Tailscale account as the Mac).
- Download the latest release from the [releases page](https://github.com/crypto-frog/jrl-messages/releases) and extract it to a fresh folder. Always extract fresh; never unzip a new version on top of an old folder.
- Double-click `install.bat`. It creates a private virtual environment in the folder, installs the Python dependencies, stops any older agent safely, registers the background agent to start at every logon, sets up the hourly failsafe, and starts the agent.
- Launch the app with `JRL-Messages.bat`. In Settings, enter the server address (the Mac's Tailscale name or 100.x address with the BlueBubbles port) and the server password. The password is stored in Windows Credential Manager, never in a file.
- Watch the connection indicator in the bottom-left: an orbiting arc means connected and checking. Send yourself a text; it should appear and alert.

## What gets installed on the PC

Everything lives in the folder you extracted plus standard per-user locations:

- `.venv/` inside the folder: the private Python environment.
- A Startup entry and an hourly scheduled task that keep the background agent alive.
- Per-user data (message database, config, attachment cache, logs) under the standard Windows application-data directory, separate from the app folder, so upgrading the app never touches your data.

`Stop-Agent.bat` pauses background collection until next logon; `Uninstall-Agent.bat` removes the startup registration. `Agent-Console.bat` and `Debug-Console.bat` run the agent and the window with visible output for troubleshooting.

## Upgrading

Extract the new version to a **new** folder and run its `install.bat`. The installer stops the old agent (even one running from a different folder), installs, and starts the new one. Your messages and settings carry over automatically because they live in the per-user data directory, not the app folder. The version in the window title bar is the ground truth for what is actually running.

## If something breaks

The in-app Activity window with its "Copy all" button is the first stop; [SETUP.md](https://github.com/crypto-frog/jrl-messages/blob/main/SETUP.md) has a troubleshooting section keyed to the connection indicator's states. When filing an issue, include the version from the title bar, the Activity log, and a screenshot, and redact your server address.
