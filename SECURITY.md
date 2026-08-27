# Security Policy

JRL Messages relays personal messages between your own devices. Nothing in this project sends your data to any third party: the path is iPhone, Apple, your Mac, your Tailscale network, your PC. Keeping it that way is a design requirement, not a feature.

## Reporting a vulnerability

Please report security vulnerabilities **privately** using GitHub's "Report a vulnerability" feature (Security tab, private advisory) on this repository. Do not open a public issue for a vulnerability.

You can expect an acknowledgment within a week. Coordinated disclosure is appreciated; credit will be given unless you prefer otherwise.

## In scope

- Anything that could expose message content, attachments, or the local database to another user or process.
- Credential handling. The BlueBubbles password must only ever live in the OS credential store via `keyring`; any path that writes it to disk in plaintext is a vulnerability.
- The local agent pipe: it must not be reachable or spoofable by other users of the machine.
- The Mac-side scripts in `mac/`.

## Out of scope

- The security of BlueBubbles, Tailscale, or macOS themselves (report upstream).
- Attacks requiring an already-compromised account on the same Windows user profile.

## Hygiene for issue filers

Before pasting logs or screenshots into a public issue:

- Redact your Tailscale hostname or 100.x address.
- Never include your BlueBubbles password.
- Blur or crop message content and phone numbers that are not yours.

Maintainers will edit or delete reports that expose someone's personal data, per the Code of Conduct.
