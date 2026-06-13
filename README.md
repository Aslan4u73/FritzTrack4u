# FritzTrack4U

**Indoor positioning that runs on the FritzBoxes you already own — no ESP32, no beacons, no extra hardware.**

![status](https://img.shields.io/badge/status-active-success)
![version](https://img.shields.io/badge/version-6.1-blue)
![python](https://img.shields.io/badge/python-3.8%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![home%20assistant](https://img.shields.io/badge/Home%20Assistant-MQTT-41BDF5)

> Part of the **4U** project family — alongside TA4U, FB4U, ASSIST4U, MC4U and WA4U.
> *German install docs in [`docs/`](docs/).*

---

## Why?

Everyone "knows" a FritzBox can't do indoor tracking. So the whole hobbyist world glues ESP32 boards to walls, flashes ESPHome, and babysits a mesh of soldered sensors just to answer one question: *which room is my phone in?*

Here's the thing they missed.

Your FritzBoxes and repeaters are **already** measuring exactly that. Every box reports the signal strength of every Wi-Fi device associated with it. So when your phone walks through the apartment, the **Office** box sees it at 35% and the **Bedroom** box sees it at 10% — **at the same moment**.

```
phone "Pixel-9" → { Office: 35%, Bedroom: 10%, Kitchen: 4% }
```

That vector is a triangulation. Strongest box tells you the floor; the full fingerprint tells you the room. **No extra hardware. No soldering. No ESP32.**

And it gets better the more gear you have: every FritzBox or repeater you add is one more sensor in the grid. The hardware is already on your walls — FritzTrack4U just reads it.

---

## Features

| | |
|---|---|
| **Multi-box triangulation** | One phone seen by several boxes at once → a real signal vector, not a single guess |
| **Presence detection** | No box sees the phone → marked **Away**. Automatic, instant |
| **Guest detection** | Spots unknown devices via `sammle_fremde()` + an exclude name-list so your own gear never trips it |
| **Room fingerprinting** | Matches the live vector against learned room signatures (15% tolerance) |
| **Home Assistant** | MQTT auto-discovery — sensors appear in HA by themselves |
| **SQLite history** | `verlauf` + `gaeste` tables, 60-day automatic cleanup |
| **Adaptive polling** | 60s idle · 15s on movement · 300s at rest · 3s live mode — saves the router, stays sharp when it matters |
| **Scales 1..N** | Works with a single box; gets sharper with every box and repeater you add |

---

## How it works

```
┌─────────────┐   TR-064 login      ┌──────────────┐
│  FritzBox 1 │◀── (per box) ──────│              │
├─────────────┤                     │ FritzTrack4U │
│  FritzBox 2 │◀───────────────────│   daemon     │
├─────────────┤                     │              │
│  Repeater 3 │◀───────────────────│              │
└─────────────┘                     └──────┬───────┘
                                            │
   each box returns: device + SignalStrength%
                                            ▼
                          { Office:35%, Bedroom:10%, ... }
                                            │
                          strongest box → floor
                          fingerprint match → room
                                            ▼
                            SQLite history  +  MQTT → Home Assistant
```

1. **TR-064 login, per box.** Each FritzBox gets its own MD5 / UTF-16LE challenge-response login. That dedicated session is what unlocks the per-device signal data the rest of the network can't give you.
2. **Signal vector.** `box_geraete()` pulls every associated device and its `SignalStrength%` across `WLANConfiguration` 1/2/3. `sammle_alle()` assembles one vector per phone: `{ boxname: signal% }`.
3. **Position.** `position()` takes the strongest box → floor, then runs a fingerprint match (15% tolerance) → room. If no box sees the device at all → **Away**.

---

## Quick Start

**Requirements:** Python 3.8+

```bash
# 1. Install the one dependency
pip install paho-mqtt

# 2. Create your config.json (boxes, credentials, MQTT)
cp config.example.json config.json
nano config.json

# 3. Run the daemon
python3 fritztrack4u.py
```

Example `config.json`:

```json
{
  "boxes": [
    { "name": "Office",  "ip": "192.168.178.1", "floor": "Ground floor", "floor_nr": 0, "user": "YOUR_USER", "password": "YOUR_PASSWORD" },
    { "name": "Bedroom", "ip": "192.168.178.2", "floor": "Upper floor",  "floor_nr": 1, "user": "YOUR_USER", "password": "YOUR_PASSWORD" }
  ],
  "devices": { "AA:BB:CC:DD:EE:FF": "Person1" },
  "exclude_names": ["SmartTV", "Printer"],
  "mqtt": { "enabled": true, "host": "192.168.178.50", "port": 1883, "user": "ha", "password": "..." },
  "retention_days": 60
}
```

See [`config.example.json`](config.example.json) for all options (fingerprints, intervals, per-box credentials).

> Until v6.1, boxes/devices/credentials were hardcoded. They now live in external `config.json` — never commit yours.

---

## Hardware scaling

The more FritzBoxes and repeaters in the mesh, the finer the resolution. Nothing to buy — it scales with the gear you already have.

| Boxes / Repeaters | Resolution | What you get |
|---|---|---|
| **1** | Floor-level | "Home / Away" + which floor |
| **2–3** | Room-level | Which room the phone is in |
| **4+** | In-room | Sub-room position via richer signal vectors |

---

## Home Assistant

FritzTrack4U publishes over MQTT with **auto-discovery** — start the daemon with MQTT configured and the entities show up in Home Assistant on their own. No YAML to hand-write.

You get, per tracked phone:
- a **presence** sensor (Home / Away)
- a **room** sensor (current room from the fingerprint match)
- a **floor** sensor (from the strongest box)

Plus a **guest** sensor that flips when an unknown device appears. Wire any of these straight into HA automations — lights, heating, notifications.

---

## Roadmap

- [ ] **HA custom component** — native integration instead of MQTT-only
- [ ] **3D visualization** — live floor-plan view of tracked devices
- [ ] **Light automation** — follow-me lighting that reacts to room changes
- [ ] **Fingerprint trainer** — guided UI to record room signatures

---

## Contributing

Issues and PRs welcome. The daemon is intentionally small — one file, one dependency. If you add a box vendor, a sensor type, or a fingerprint heuristic, keep that simplicity. Open an issue first for anything that changes `config.json` shape.

---

## License

MIT — Copyright © Murat Danis

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
```
