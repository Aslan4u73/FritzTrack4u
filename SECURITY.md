# Security Policy

## Scope & design

FritzTrack4U is a **local, LAN-only** tool. It talks to your FritzBoxes over TR-064 on your home network and (optionally) to a local MQTT broker. It makes **no outbound internet connections** and uses **no cloud service or API keys**.

## Your responsibility

- **Credentials live in `config.json`** (FritzBox users + MQTT) in plain text. This file is excluded via `.gitignore` — **never commit it**. Restrict its permissions: `chmod 600 config.json`.
- Create a **dedicated FritzBox user** for FritzTrack4U with only the "FRITZ!Box settings" permission, and **do not** allow internet access for that user. Don't reuse your admin password.
- The SQLite history (`*.db`) contains movement data of the people you track. Treat it as personal data. The 60-day auto-cleanup limits retention; lower `retention_days` if you want less.
- **Data protection:** use this only in your **own home**, for people who consent (your household). Tracking employees or customers raises serious GDPR/privacy issues and is explicitly **not** the purpose of this project.

## Reporting a vulnerability

Found a security issue? Please open a GitHub issue (for non-sensitive reports) or contact the author via [aslan4u.de](https://aslan4u.de). This is a hobby project maintained in spare time — please be patient.

## No warranty

As stated in the [DISCLAIMER](DISCLAIMER.md) and MIT [LICENSE](LICENSE): use at your own risk, no warranty, no liability.
