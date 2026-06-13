# FritzTrack4U

**Indoor-Ortung mit den FritzBoxen, die du schon hast — kein ESP32, keine Beacons, keine Zusatz-Hardware.**
**Indoor positioning that runs on the FritzBoxes you already own — no ESP32, no beacons, no extra hardware.**

![status](https://img.shields.io/badge/status-active-success)
![version](https://img.shields.io/badge/version-6.1-blue)
![python](https://img.shields.io/badge/python-3.8%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![home%20assistant](https://img.shields.io/badge/Home%20Assistant-MQTT-41BDF5)

> Teil der **4U**-Projektfamilie — neben TA4U, FB4U, ASSIST4U, MC4U und WA4U.
> Part of the **4U** project family.

**🇩🇪 [Deutsch](#-deutsch)  ·  🇬🇧 [English](#-english)  ·  📖 [Installation (DE)](docs/INSTALL.de.md)**

---
---

# 🇩🇪 Deutsch

## Worum geht es?

Jeder "weiß", dass eine FritzBox kein Indoor-Tracking kann. Also klebt die ganze Bastler-Welt ESP32-Boards an die Wand, flasht ESPHome und pflegt ein Netz aus gelöteten Sensoren — nur um eine Frage zu beantworten: *In welchem Raum ist mein Handy gerade?*

Was dabei übersehen wird:

Deine FritzBoxen und Repeater **messen genau das schon längst.** Jede Box meldet die Signalstärke jedes verbundenen WLAN-Geräts. Wenn dein Handy durch die Wohnung läuft, sieht es die **Büro**-Box mit 35 % und die **Schlafzimmer**-Box gleichzeitig mit 10 %.

```
Handy "Pixel-9" → { Büro: 35%, Schlafzimmer: 10%, Küche: 4% }
```

Dieser Vektor **ist** eine Triangulation. Die stärkste Box sagt dir die Etage, der volle Fingerabdruck den Raum. **Keine Zusatz-Hardware. Kein Löten. Kein ESP32.**

Und es wird besser, je mehr Geräte du hast: Jede FritzBox und jeder Repeater ist ein weiterer Sensor im Raster. Die Hardware hängt schon an deinen Wänden — FritzTrack4U liest sie nur aus.

## Funktionen

| | |
|---|---|
| **Multi-Box-Triangulation** | Ein Handy von mehreren Boxen gleichzeitig gesehen → ein echter Signal-Vektor statt einer einzigen Schätzung |
| **Anwesenheits-Erkennung** | Keine Box sieht das Handy → **Abwesend**. Automatisch, sofort |
| **Gäste-Erkennung** | Erkennt unbekannte Geräte + Ausschluss-Liste, damit deine eigenen Geräte (TV, Drucker…) nicht als Gast zählen |
| **Raum-Fingerprinting** | Vergleicht den Live-Vektor mit eingelernten Raum-Signaturen (15 % Toleranz) |
| **Home Assistant** | MQTT-Auto-Discovery — Sensoren tauchen von selbst in HA auf |
| **SQLite-Verlauf** | Bewegungsverlauf in einer lokalen Datenbank, automatisches Aufräumen nach 60 Tagen |
| **Adaptiver Takt** | 60 s normal · 15 s bei Bewegung · 300 s in Ruhe · 3 s Live — schont den Router, bleibt scharf wenn nötig |
| **Skaliert 1..N** | Läuft mit einer einzigen Box; wird mit jeder weiteren Box/jedem Repeater genauer |

## Wie es funktioniert

```
┌─────────────┐   TR-064 Login      ┌──────────────┐
│  FritzBox 1 │◀── (pro Box) ──────│              │
├─────────────┤                     │ FritzTrack4U │
│  FritzBox 2 │◀───────────────────│   Daemon     │
├─────────────┤                     │              │
│  Repeater 3 │◀───────────────────│              │
└─────────────┘                     └──────┬───────┘
                                            │
   jede Box liefert: Gerät + Signalstärke %
                                            ▼
                          { Büro:35%, Schlafzimmer:10%, ... }
                                            │
                          stärkste Box → Etage
                          Fingerprint-Treffer → Raum
                                            ▼
                            SQLite-Verlauf  +  MQTT → Home Assistant
```

1. **TR-064-Login, pro Box.** Jede FritzBox bekommt ihren eigenen Login (MD5/UTF-16LE Challenge-Response). Genau diese eigene Sitzung pro Box schaltet die Signal-Daten frei, die das Netz sonst nicht herausgibt — **der Master allein reicht nicht.**
2. **Signal-Vektor.** Pro Box werden alle verbundenen Geräte mit ihrer Signalstärke (%) über WLANConfiguration 1/2/3 (2,4 GHz / 5 GHz / Gast) gelesen. Daraus entsteht ein Vektor pro Handy: `{ Boxname: Signal% }`.
3. **Position.** Die stärkste Box bestimmt die Etage, ein Fingerprint-Treffer (15 % Toleranz) den Raum. Sieht **keine** Box das Gerät → **Abwesend**.

## Schnellstart

**Voraussetzung:** Python 3.8+ (Linux / Raspberry Pi / Mini-Server)

```bash
# 1. Abhängigkeit installieren (nur für Home Assistant nötig)
pip install paho-mqtt

# 2. config.json aus der Vorlage anlegen
cp config.example.json config.json
nano config.json

# 3. Daemon starten
python3 fritztrack4u.py --config ./config.json
```

> **Wichtigster Schritt:** Auf **jeder** FritzBox/jedem Repeater einen eigenen Benutzer anlegen (Recht „FRITZ!Box Einstellungen“, nur Heimnetz). Erst dann liefert jede Box ihre eigenen Signalwerte. Die komplette deutsche Anleitung: **[docs/INSTALL.de.md](docs/INSTALL.de.md)**.

Beispiel-`config.json`:

```json
{
  "boxes": [
    { "name": "Büro",        "ip": "192.168.178.1", "floor": "Erdgeschoss", "floor_nr": 0, "user": "DEIN_USER", "password": "DEIN_PASSWORT" },
    { "name": "Schlafzimmer", "ip": "192.168.178.2", "floor": "Obergeschoss", "floor_nr": 1, "user": "DEIN_USER", "password": "DEIN_PASSWORT" }
  ],
  "devices": { "AA:BB:CC:DD:EE:FF": "Handy-Person-1" },
  "exclude_names": ["SmartTV", "Drucker"],
  "mqtt": { "enabled": true, "host": "192.168.178.50", "port": 1883, "user": "ha", "password": "..." },
  "retention_days": 60
}
```

Alle Optionen (Fingerprints, Intervalle, Zugangsdaten pro Box) stehen in [`config.example.json`](config.example.json).

> **Sicherheit:** Deine echte `config.json` enthält Passwörter und dein Wohnungs-Layout. Sie ist per `.gitignore` ausgeschlossen — **niemals** committen.

## Hardware-Skalierung

Je mehr FritzBoxen und Repeater im Mesh, desto feiner die Auflösung. Nichts zu kaufen — es skaliert mit dem, was du schon hast.

| Boxen / Repeater | Auflösung | Was du bekommst |
|---|---|---|
| **1** | Etagen-grob | „Zu Hause / Abwesend“ + welche Etage |
| **2–3** | Raum-genau | In welchem Raum das Handy ist |
| **4+** | Im-Raum | Position innerhalb des Raums über reichere Signal-Vektoren |

## Home Assistant

FritzTrack4U meldet sich per **MQTT Auto-Discovery** selbst an — Daemon mit MQTT starten, und die Entitäten erscheinen von allein in Home Assistant. Kein YAML von Hand.

Pro getracktem Handy bekommst du:
- einen **Anwesenheits**-Sensor (Zu Hause / Abwesend)
- einen **Raum**-Sensor (aktueller Raum aus dem Fingerprint-Treffer)
- einen **Etagen**-Sensor (aus der stärksten Box)

Dazu einen **Gäste**-Sensor, der anspringt, wenn ein unbekanntes Gerät auftaucht. Alles direkt in HA-Automationen nutzbar — Licht, Heizung, Benachrichtigungen.

## Roadmap

- [ ] **HA Custom Component** — native Integration statt nur MQTT
- [ ] **3D-Visualisierung** — Live-Etagen-Ansicht der georteten Geräte
- [ ] **Licht-Automation** — „Follow-me“-Licht, das auf Raumwechsel reagiert
- [ ] **Fingerprint-Trainer** — geführtes Einlernen der Raum-Signaturen per Klick (aktuell trägt man Fingerprints manuell in die Config ein)
- [ ] **Standby-Puffer** — kurze WLAN-Standby-Phasen von iPhones überbrücken, bevor „Abwesend“ gemeldet wird

## Mitmachen

Issues und Pull Requests willkommen. Der Daemon ist bewusst klein — eine Datei, eine Abhängigkeit. Wer einen Box-Hersteller, einen Sensor-Typ oder eine Fingerprint-Heuristik ergänzt: bitte diese Einfachheit erhalten. Für Änderungen an der `config.json`-Struktur bitte zuerst ein Issue öffnen.

---
---

# 🇬🇧 English

## Why?

Everyone "knows" a FritzBox can't do indoor tracking. So the whole hobbyist world glues ESP32 boards to walls, flashes ESPHome, and babysits a mesh of soldered sensors just to answer one question: *which room is my phone in?*

Here's the thing they missed.

Your FritzBoxes and repeaters are **already** measuring exactly that. Every box reports the signal strength of every Wi-Fi device associated with it. So when your phone walks through the apartment, the **Office** box sees it at 35% and the **Bedroom** box sees it at 10% — **at the same moment**.

```
phone "Pixel-9" → { Office: 35%, Bedroom: 10%, Kitchen: 4% }
```

That vector is a triangulation. Strongest box tells you the floor; the full fingerprint tells you the room. **No extra hardware. No soldering. No ESP32.**

And it gets better the more gear you have: every FritzBox or repeater you add is one more sensor in the grid. The hardware is already on your walls — FritzTrack4U just reads it.

## Features

| | |
|---|---|
| **Multi-box triangulation** | One phone seen by several boxes at once → a real signal vector, not a single guess |
| **Presence detection** | No box sees the phone → marked **Away**. Automatic, instant |
| **Guest detection** | Spots unknown devices + an exclude name-list so your own gear (TV, printer…) never trips it |
| **Room fingerprinting** | Matches the live vector against learned room signatures (15% tolerance) |
| **Home Assistant** | MQTT auto-discovery — sensors appear in HA by themselves |
| **SQLite history** | Movement history in a local database, automatic 60-day cleanup |
| **Adaptive polling** | 60s normal · 15s on movement · 300s at rest · 3s live — saves the router, stays sharp when it matters |
| **Scales 1..N** | Works with a single box; gets sharper with every box and repeater you add |

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

1. **TR-064 login, per box.** Each FritzBox gets its own MD5 / UTF-16LE challenge-response login. That dedicated per-box session is what unlocks the signal data the network won't give you otherwise — **the master box alone is not enough.**
2. **Signal vector.** Per box, every associated device and its `SignalStrength%` is read across `WLANConfiguration` 1/2/3 (2.4 GHz / 5 GHz / guest). This builds one vector per phone: `{ boxname: signal% }`.
3. **Position.** Strongest box → floor, then a fingerprint match (15% tolerance) → room. If no box sees the device at all → **Away**.

## Quick Start

**Requirements:** Python 3.8+ (Linux / Raspberry Pi / mini-server)

```bash
# 1. Install the dependency (only needed for Home Assistant)
pip install paho-mqtt

# 2. Create config.json from the template
cp config.example.json config.json
nano config.json

# 3. Run the daemon
python3 fritztrack4u.py --config ./config.json
```

> **Most important step:** create a dedicated user on **each** FritzBox/repeater ("FRITZ!Box settings" permission, home-network only). Only then does each box report its own signal values. Full setup guide (German): **[docs/INSTALL.de.md](docs/INSTALL.de.md)**.

See [`config.example.json`](config.example.json) for all options (fingerprints, intervals, per-box credentials).

> **Security:** your real `config.json` holds passwords and your home layout. It's excluded via `.gitignore` — never commit it.

## Hardware scaling

The more FritzBoxes and repeaters in the mesh, the finer the resolution. Nothing to buy — it scales with the gear you already have.

| Boxes / Repeaters | Resolution | What you get |
|---|---|---|
| **1** | Floor-level | "Home / Away" + which floor |
| **2–3** | Room-level | Which room the phone is in |
| **4+** | In-room | Sub-room position via richer signal vectors |

## Home Assistant

FritzTrack4U publishes over MQTT with **auto-discovery** — start the daemon with MQTT configured and the entities show up in Home Assistant on their own. No YAML to hand-write.

Per tracked phone you get a **presence** sensor (Home/Away), a **room** sensor, and a **floor** sensor — plus a **guest** sensor that flips when an unknown device appears. Wire any of these into HA automations — lights, heating, notifications.

## Roadmap

- [ ] **HA custom component** — native integration instead of MQTT-only
- [ ] **3D visualization** — live floor-plan view of tracked devices
- [ ] **Light automation** — follow-me lighting that reacts to room changes
- [ ] **Fingerprint trainer** — guided UI to record room signatures (currently fingerprints are entered manually in the config)
- [ ] **Standby buffer** — bridge short iPhone Wi-Fi standby gaps before reporting "Away"

## Contributing

Issues and PRs welcome. The daemon is intentionally small — one file, one dependency. If you add a box vendor, a sensor type, or a fingerprint heuristic, keep that simplicity. Open an issue first for anything that changes the `config.json` shape.

---

## License / Lizenz

MIT — Copyright © 2026 Murat Danis

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction. See [LICENSE](LICENSE) for the full text. The software is provided "as is", without warranty of any kind.
