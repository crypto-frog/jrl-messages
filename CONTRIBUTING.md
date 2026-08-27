# Contributing to JRL Messages

Thank you for considering a contribution. This project has one governing value: **a message must never be silently missed.** Every convention below exists to protect that.

## Ground rules

1. **Behavior changes require tests.** Nearly every bug this project has ever fixed is pinned by a regression test named after the failure. If you fix something, pin it. If you change behavior, update the pin deliberately and say so in the pull request.
2. **The full battery must be green** before a pull request is opened. See "Running the tests" below.
3. **Do not touch the pinned invariants** (listed at the bottom) without opening an issue first and getting agreement. They encode hard-won field lessons; several of them look strange until you read the release note that created them.
4. **Small, reviewable pull requests.** One concern per PR.

## Development setup

You do not need a Mac, an iPhone, or even Windows to work on most of the codebase. The unit tests and three of the four harnesses run headless on Linux and macOS; Bluetooth modules import lazily and are exercised entirely through fakes.

```
git clone <this repository>
cd jrl-messages
python3.12 -m venv .venv

# Windows
.venv\Scripts\pip install -r requirements.txt

# macOS / Linux (the bleak dependency is Windows-only and skips itself)
.venv/bin/pip install -r requirements.txt
```

To run the actual app you need the full chain described in [SETUP.md](SETUP.md): a Mac with BlueBubbles, Tailscale on both machines, and a Windows PC.

## Running the tests

```
# Windows
set QT_QPA_PLATFORM=offscreen
set JRL_SMOKE=1
.venv\Scripts\python -m unittest discover -s tests -v

# macOS / Linux
QT_QPA_PLATFORM=offscreen JRL_SMOKE=1 .venv/bin/python -m unittest discover -s tests -v
```

`QT_QPA_PLATFORM=offscreen` lets the Qt-dependent tests run without a display. `JRL_SMOKE=1` keeps timers and network paths in their fast, fake-friendly modes.

The four suites, and what they guard:

| Suite | Guards |
|---|---|
| `tests/test_reliability.py` | The sync engine: ROWID windows, cursors, audits, gap repair, outbox, code extraction |
| `tests/test_storm_regressions.py` | Notification correctness: storms, self-texts, ledger, popup and sound rules |
| `tests/test_edge_regressions.py` | Assorted field-reported edge cases |
| `tests/test_phone_link.py` | The Bluetooth/ANCS stack: discovery, rotation, sidelining, wizard verdicts |
| `tests/test_desktop_upgrades.py` | The window upgrades: per-conversation drafts, the jump counter, transcript export |

Heavier end-to-end harnesses live in `tools/`:

- `harness_pipeline.py` runs the agent's pipeline against a mock BlueBubbles server.
- `harness_storm.py` replays notification storm scenarios.
- `harness_offscreen_window.py` drives the real window offscreen.
- `harness_agent_e2e.py` runs a live agent process end to end against the mock server.

Run them the same way (offscreen, `JRL_SMOKE=1`). All four must pass for a release.

## Code style

- Python, PEP 8, standard library first. The dependency list is deliberately short; adding a dependency needs a reason.
- Comments explain **why**, not what. The existing code leans heavily on this; follow it.
- House punctuation: plain ASCII. Do not use em dashes in code, comments, or documentation; use commas, colons, parentheses, or separate sentences.
- User-facing strings are complete sentences in plain language. This app coaches non-technical users through hard situations; wording is part of the product.

## Where help is most valuable

1. **The Bluetooth/ANCS notification mirroring feature.** Read [docs/known-issues.md](docs/known-issues.md) first. This is genuinely difficult territory (rotating anonymous addresses, per-connection ANCS authorization, Windows GATT caching) and the project's main open problem.
2. **Field reports.** Running the app in daily use and filing precise issues (version from the title bar, Activity log "Copy all", screenshots) is a first-class contribution.
3. **Mac-side hardening.** The `mac/` keepalive scripts fight macOS sleep and App Nap; there is always more to learn there.
4. **Packaging.** A signed installer or a PyInstaller build that preserves the agent/supervisor arrangement would lower the entry barrier considerably.

## Pinned invariants

These behaviors are load-bearing. Tests enforce most of them; treat the rest as if they were.

**Process architecture**

- The agent never imports UI code, and the window never opens network connections to the Mac. The pipe is the only channel between them.
- The agent must survive and keep collecting with the window closed, crashed, or never opened.
- A worker restart is never dropped: when a delayed stop is in flight, the exiting thread hands off the respawn.

**Sync engine**

- ROWID windows are fixed numeric intervals; never reintroduce mutable OFFSET pagination.
- A transiently short server response must never become a permanent cursor hole (recheck passes stay).
- Notifications never fire for messages older than the freshness window, and never for messages sent from this app's own composer.

**Bluetooth (the radio doctrine)**

- Never unpair automatically. Never pair in the background. The default setup flow never initiates pairing at all; only the explicit advanced flow may, and it must request every ceremony kind.
- Never trust the Windows GATT cache: every client that inspects services sets `use_cached_services=False`.
- When identity proof is pending, stored and paired shortcuts are skipped.
- The Windows paired-device **list** is the authority for "is an iPhone paired"; the per-address query is only trusted when it answers yes, because it is blind behind a rotating anonymous address.
- ANCS missing on a known-paired iPhone means the phone is withholding the service, not that the wrong device answered.

If a change makes one of these tests fail, the default assumption is that the change is wrong, not the test.

## Filing issues

Use the issue templates. For anything involving logs: open the Activity window in the app, press "Copy all", and paste. **Redact your server address and never include your BlueBubbles password.** See [SECURITY.md](SECURITY.md).

## Release procedure (maintainers)

1. Bump `VERSION` in `app/constants.py`.
2. Run the entire battery and all four harnesses; everything green.
3. Write `RELEASE-NOTES-<version>.txt` in the established candid style: what was reported, what was proven, what is now pinned.
4. Update `CHANGELOG.md`.
5. Tag `v<version>`, build the release zip (exclude `.venv` and runtime data), attach it to a GitHub release, and state the exact byte size in the release description.
