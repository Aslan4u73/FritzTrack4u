# Changelog

All notable changes to FritzTrack4U are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]
### Planned
- HACS / Home Assistant custom component (one-click install)
- 3D "X-ray house" web visualization bundled with the repo
- Guided calibration UI (record room fingerprints by button press)
- 2-minute presence buffer (bridge short iPhone Wi-Fi standby gaps)

## [0.1.0] - 2026-06-13
First public release. Config-driven, anonymized, scales from 1 to N boxes.

### Added
- Multi-box signal collection over TR-064 (`GetGenericAssociatedDeviceInfo` per box)
- Floor detection (strongest box) and room detection (full-vector fingerprint match)
- Presence/absence detection — a phone seen by no box is "away"
- Guest detection (unknown devices) with an exclude name-list
- SQLite history with 60-day automatic cleanup
- Adaptive polling (fast on movement, slow at rest)
- Home Assistant MQTT auto-discovery (optional)
- Single-file daemon, standard library only (paho-mqtt optional)

### History before the public release
The project evolved through several private iterations:
- **v1** — single box, floor-level only ("which box is the phone on")
- **v5** — per-room fingerprint calibration (1 box + dBm)
- **v6** — multi-box data collector, presence check, SQLite history, adaptive polling
- **v6.1** — guest detection + 60-day auto-cleanup
- **0.1.0** — public, config-driven (1..N boxes), full-vector match across all boxes
