# FritzTrack4U — Installations-Anleitung

Indoor-Positioning ueber FritzBox-WLAN. FritzTrack4U meldet, in welchem
Raum bzw. auf welcher Etage sich ein Handy gerade befindet — ganz ohne
App auf dem Handy, ohne Bluetooth-Beacons, nur ueber die Signalstaerke
(TR-064-Prozentwert), die der jeweils verbundene Access-Point meldet —
also die FritzBox/der Repeater, an dem das Geraet gerade haengt.
(Hinweis: ein Geraet ist immer nur mit *einer* Box verbunden; reine
Mesh-Repeater liefern fuer ihre Clients oft keinen eigenen Wert — dann
zaehlt, *welche* Box das Geraet haelt.)

Diese Anleitung fuehrt Schritt fuer Schritt durch die komplette
Einrichtung. Wer die Reihenfolge einhaelt, hat den Daemon in unter einer
Stunde laufen.

> **Kurz-Ueberblick der Schritte**
> 1. Voraussetzungen pruefen (Server + Python)
> 2. **Auf JEDER FritzBox/Repeater einen eigenen Benutzer anlegen** (wichtigster Schritt!)
> 3. Geraete-MACs der Handys herausfinden
> 4. `config.json` ausfuellen
> 5. Daemon starten (als systemd-Service)
> 6. Home Assistant verbinden (MQTT, optional)
> 7. Kalibrierung fuer Raumgenauigkeit (optional)
> 8. Troubleshooting

---

## 1. Voraussetzungen

### Hardware / Server

FritzTrack4U laeuft als Hintergrund-Dienst (Daemon). Er muss dauerhaft
laufen und sich im selben Heimnetz wie die FritzBoxen befinden. Geeignet
ist alles, was Linux und Python kann und 24/7 an bleibt:

- **Raspberry Pi** (Pi 3, 4 oder 5 — auch ein Pi Zero 2 W reicht), oder
- ein kleiner **Mini-Server / NUC / alter Laptop** mit Linux, oder
- jeder Rechner, der ohnehin durchlaeuft (z. B. der Home-Assistant-Host).

Der Daemon ist genuegsam: wenige MB RAM, kaum CPU-Last zwischen den
Abfragen. Wichtig ist nur, dass er die FritzBoxen im LAN erreicht.

### Software

- **Python 3.9 oder neuer** (empfohlen 3.11+).
  Pruefen:
  ```bash
  python3 --version
  ```
- **pip** (Python-Paketmanager). Falls nicht vorhanden:
  ```bash
  sudo apt update && sudo apt install -y python3 python3-pip python3-venv
  ```
- **SQLite** ist in Python eingebaut (`sqlite3`) — nichts zu installieren.
  Die Verlaufs-Datenbank (`fritztrack4u.db`) wird beim ersten Start
  automatisch angelegt.

### Optional: MQTT (nur fuer Home Assistant)

Wenn die Position in **Home Assistant** auftauchen soll, braucht es einen
MQTT-Broker. Am gaengigsten ist **Mosquitto**:

- als **Home-Assistant-Add-on** (HA OS / Supervised): Einstellungen →
  Add-ons → "Mosquitto broker" installieren, oder
- als eigenes Paket auf dem Server:
  ```bash
  sudo apt install -y mosquitto mosquitto-clients
  ```

Ohne MQTT laeuft FritzTrack4U trotzdem — Position und Verlauf landen dann
nur in der lokalen SQLite-Datenbank, aber nicht in Home Assistant.

### Optional: Home Assistant

Nur noetig, wenn Anwesenheit/Raum als Sensor in HA erscheinen und fuer
Automationen (Licht, Heizung, Szenen) genutzt werden soll. FritzTrack4U
meldet sich per **MQTT Auto-Discovery** selbststaendig an — in HA muss
nichts manuell konfiguriert werden, sobald MQTT eingerichtet ist (siehe
Schritt 6).

### Python-Abhaengigkeiten

Im Projektordner alles in einer virtuellen Umgebung installieren (sauber,
ohne das System-Python zu beruehren):

```bash
cd /opt/fritztrack4u            # oder wohin der Code kopiert wurde
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Falls keine `requirements.txt` vorliegt, reicht ein einziges Paket
(TR-064/Netzwerk-Login laeuft ueber Pythons eingebautes `urllib`, nur MQTT
braucht `paho-mqtt` — und auch das nur, wenn Home Assistant genutzt wird):

```bash
pip install paho-mqtt
```

> Ohne MQTT (reiner SQLite-Betrieb) ist gar keine Zusatz-Bibliothek noetig —
> der Daemon laeuft dann mit der Python-Standardbibliothek allein.

---

## 2. WICHTIGSTER SCHRITT: Auf JEDER FritzBox und JEDEM Repeater einen eigenen Benutzer anlegen

**Das ist der Schritt, an dem die meiste Genauigkeit gewonnen oder
verloren wird. Bitte nicht ueberspringen.**

### Warum ueberhaupt mehrere Boxen?

FritzTrack4U bestimmt die Position aus der **Signalstaerke (in Prozent)**,
mit der ein Handy bei den einzelnen Funk-Knoten ankommt. Ein Handy im
Wohnzimmer wird von der Wohnzimmer-Box mit z. B. 90 % gesehen, vom
Repeater im Schlafzimmer aber nur mit 40 %. Aus diesem **Vektor**
`{Wohnzimmer: 90%, Schlafzimmer: 40%, Keller: 12%}` errechnet der Daemon
Etage und Raum.

Damit das funktioniert, muss **jede** Box (Master-FritzBox **und** jeder
Repeater) ihre eigenen Signalwerte einzeln melden. Genau hier ist die
Falle:

> **Es reicht NICHT, sich nur an der Master-FritzBox anzumelden.**
>
> Wenn FritzTrack4U nur die Master-Box per TR-064 abfragt, liefert diese
> zwar eine Geraete-Liste — aber die Signalstaerken, die ein **Repeater**
> sieht, kennt nur der Repeater selbst. Die Master-Box gibt sie nicht
> stellvertretend heraus. Ohne eigenen Zugang pro Box bekommt man also
> nur einen einzigen Messpunkt, und Indoor-Positioning mit einem
> einzigen Messpunkt ist unmoeglich — man weiss dann nur "Handy ist
> irgendwo im Haus", nicht in welchem Raum.

Deshalb gilt: **Pro Funk-Knoten ein eigener Benutzer, eine eigene
TR-064-Anmeldung.** Je mehr Boxen/Repeater, desto genauer die Ortung.

### So legst du den Benutzer an (pro Box gleich)

Diese Schritte fuer **jede** FritzBox und **jeden** Repeater einzeln
durchfuehren:

1. Im Browser die Oberflaeche des Geraets oeffnen:
   - Master-FritzBox: `http://fritz.box` oder `http://192.168.178.1`
   - Repeater: ueber **seine eigene IP** (siehe unten "Repeater erreichen")
2. Anmelden (mit dem vorhandenen Geraete-Passwort).
3. Menue **System → FRITZ!Box-Benutzer** oeffnen.
   - Beim Repeater heisst der Pfad meist **System → FRITZ!Repeater-Benutzer**
     oder unter **System → Benutzer**.
4. Auf **Benutzer hinzufuegen** klicken.
5. Felder ausfuellen:
   - **Benutzername:** z. B. `fritztrack` (gern auf jeder Box gleich,
     macht die `config.json` uebersichtlicher)
   - **Passwort:** ein starkes, eigenes Passwort. **Nicht** das
     Admin-Passwort wiederverwenden. Tipp: pro Box dasselbe Passwort
     ist okay, solange es nirgends sonst genutzt wird.
6. **Berechtigungen** — nur das Noetigste vergeben:
   - Haken bei **"FRITZ!Box Einstellungen"** setzen. Diese Berechtigung
     schaltet den TR-064-Zugriff frei, ueber den FritzTrack4U die
     WLAN-Geraeteliste und Signalstaerken liest.
   - **KEINE** weiteren Rechte noetig (kein VPN, keine Telefonie, keine
     externe Erreichbarkeit, kein Smart-Home-Zugriff).
7. **Zugriff nur aus dem Heimnetz** zulassen:
   - Den Haken fuer **"Zugang auch aus dem Internet erlaubt"** **NICHT**
     setzen bzw. deaktiviert lassen. FritzTrack4U arbeitet rein lokal —
     der Benutzer soll niemals von aussen erreichbar sein. Das ist eine
     wichtige Sicherheits-Vorgabe (Regel: keine unnoetige Angriffsflaeche).
8. Speichern.

Diese Benutzerdaten (Box-IP, Benutzername, Passwort) kommen spaeter in die
`config.json` (Schritt 4).

> **Hinweis zum Login-Verfahren (Hintergrund, nichts zu tun):**
> FritzTrack4U meldet sich bei jeder Box per TR-064 an und nutzt das
> sichere Challenge-Response-Verfahren von AVM (MD5 ueber die
> UTF-16LE-kodierte Kombination aus Challenge und Passwort). Das
> Passwort wird also **nie** im Klartext uebertragen. Pro Box wird eine
> eigene Session (SID) geholt.

### Repeater erreichen (eigene IP finden)

Mesh-Repeater haben oft keinen offensichtlichen eigenen Namen unter
`fritz.box`. So findest du ihre IP:

1. In der **Master-FritzBox**: **Heimnetz → Netzwerk → Netzwerkverbindungen**
   (oder **Heimnetz → Mesh**). Dort sind alle Repeater mit ihrer
   **IP-Adresse** gelistet (z. B. `192.168.178.20`, `192.168.178.21`).
2. Diese IP direkt im Browser aufrufen: `http://192.168.178.20`.
3. Dort wie oben den Benutzer anlegen.

> **Wichtig:** Den Benutzer **direkt ueber die IP des Repeaters** anlegen
> und in der `config.json` auch genau diese Repeater-IP eintragen — nicht
> die der Master-Box. Nur so fragt FritzTrack4U die Signalwerte ab, die
> wirklich am Repeater gemessen wurden.

### Empfehlung fuer gute Ergebnisse

- **Mindestens 2 Boxen** (Master + 1 Repeater) fuer eine grobe
  Etagen-/Bereichs-Trennung.
- **3 oder mehr Knoten** fuer raumgenaue Ortung. Faustregel: ein Knoten
  pro Etage, und in grossen Etagen je ein Knoten pro Gebaeudefluegel.
- Repeater dort platzieren, wo ohnehin WLAN gebraucht wird — die
  Positionierung profitiert automatisch davon.

---

## 3. Geraete-MACs der Handys herausfinden

FritzTrack4U identifiziert Handys (und andere Geraete) an ihrer
**MAC-Adresse**. Diese muss in die `config.json`, damit der Daemon weiss,
welches Geraet zu welcher Person gehoert.

### MAC ueber die FritzBox finden (einfachster Weg)

1. In der **Master-FritzBox**: **Heimnetz → Netzwerk → Netzwerkverbindungen**.
2. Das gesuchte Handy in der Liste suchen (es muss dafuer im WLAN sein).
3. Auf den Geraetenamen / das Detail-Symbol klicken — dort steht die
   **MAC-Adresse** im Format `AA:BB:CC:DD:EE:FF`.

### MAC am Handy selbst finden

- **Android:** Einstellungen → Ueber das Telefon → Status → WLAN-MAC-Adresse.
- **iPhone:** Einstellungen → Allgemein → Info → WLAN-Adresse.

### Wichtig: Zufaellige / private MAC-Adresse deaktivieren

Moderne Handys nutzen pro WLAN eine **zufaellige (private) MAC**. Aendert
sich diese, "verschwindet" das Handy fuer FritzTrack4U. Damit die Ortung
stabil bleibt, fuer das **Heim-WLAN** die private Adresse abschalten:

- **iPhone:** Einstellungen → WLAN → beim Heimnetz auf das (i) tippen →
  **"Private WLAN-Adresse"** ausschalten. Dann steht oben die feste
  WLAN-Adresse, die in die Config kommt.
- **Android:** Einstellungen → WLAN → Heimnetz lange gedrueckt / Zahnrad →
  **Privatsphaere / MAC-Typ** → auf **"Geraete-MAC verwenden"** stellen.

Danach die (jetzt feste) MAC wie oben ablesen und notieren.

---

## 4. config.json ausfuellen

Bisher waren Boxen, Geraete und Zugangsdaten im Code fest verdrahtet. Ab
dieser Version liegt **alles in einer externen `config.json`** im
Projektordner. So sieht sie aus:

```json
{
  "boxes": [
    {
      "name": "Wohnzimmer",
      "ip": "192.168.178.1",
      "floor": "Erdgeschoss",
      "floor_nr": 0,
      "user": "fritztrack",
      "password": "DEIN_BOX_PASSWORT"
    },
    {
      "name": "Schlafzimmer",
      "ip": "192.168.178.20",
      "floor": "Obergeschoss",
      "floor_nr": 1,
      "user": "fritztrack",
      "password": "DEIN_REPEATER_PASSWORT"
    },
    {
      "name": "Keller",
      "ip": "192.168.178.21",
      "floor": "Untergeschoss",
      "floor_nr": -1,
      "user": "fritztrack",
      "password": "DEIN_REPEATER_PASSWORT_2"
    }
  ],

  "fritzbox": {
    "user": "fritztrack",
    "password": "GLOBAL_FALLBACK_PASSWORT"
  },

  "devices": {
    "AA:BB:CC:DD:EE:01": "Handy-Person-1",
    "AA:BB:CC:DD:EE:02": "Handy-Person-2"
  },

  "exclude_names": ["Drucker", "TV-Wohnzimmer", "Saugroboter", "Thermostat"],

  "intervals": {
    "normal": 60,
    "movement": 15,
    "idle": 300,
    "live": 3
  },

  "retention_days": 60,
  "db_path": "fritztrack4u.db",

  "fingerprints": {
    "Erdgeschoss": {
      "Wohnzimmer": { "Wohnzimmer": 85, "Schlafzimmer": 40 },
      "Kueche": { "Wohnzimmer": 45, "Schlafzimmer": 25 }
    }
  },
  "fingerprint_tolerance": 0.15,
  "tr064_timeout": 10,

  "mqtt": {
    "enabled": true,
    "host": "192.168.178.50",
    "port": 1883,
    "user": "mqtt-user",
    "password": "MQTT_PASSWORT",
    "discovery_prefix": "homeassistant",
    "base_topic": "fritztrack4u"
  }
}
```

> **Hinweis:** Eine fertige Vorlage mit allen Feldern liegt als
> [`config.example.json`](../config.example.json) bei. Einfach kopieren
> (`cp config.example.json config.json`) und die eigenen Werte eintragen.
> Alle `_comment`-Felder darin werden vom Daemon ignoriert.

### Felder erklaert

**`boxes`** — eine Liste, ein Eintrag pro FritzBox/Repeater:
- `name`: frei waehlbarer Name des Standorts (taucht in HA und im Verlauf
  auf, z. B. der Raum, in dem die Box steht).
- `ip`: IP-Adresse der Box/des Repeaters (bei Repeatern die **eigene**
  IP aus Schritt 2).
- `floor`: Etagen-Name als Freitext (`Untergeschoss`, `Erdgeschoss`,
  `Obergeschoss` …). Die Etage wird aus der **staerksten Box** abgeleitet —
  daher sollte sie stimmen.
- `floor_nr`: Etagen-Nummer fuer die Sortierung/3D-Anzeige (z. B. -1=Keller,
  0=EG, 1=OG, 2=DG).
- `user` / `password`: die in Schritt 2 angelegten Zugangsdaten. Optional —
  wenn weggelassen, greift der globale `fritzbox`-Block (siehe unten).

**`fritzbox`** (optional) — globale Zugangsdaten als Fallback fuer alle
Boxen, die kein eigenes `user`/`password` haben. Praktisch, wenn auf allen
Boxen derselbe Benutzer angelegt wurde:
- `user` / `password`: Standard-Login fuer alle Boxen ohne eigene Angabe.

**`devices`** — die zu ortenden Handys (aus Schritt 3), als
**MAC → Anzeigename**:
- Schluessel = feste MAC-Adresse (`AA:BB:CC:DD:EE:01`).
- Wert = Anzeigename (`Murat-iPhone`). Taucht so in HA und im Verlauf auf.

**`exclude_names`** — Fremderkennung / Gaeste-Filter:
- Eine Namens-Liste von Geraeten, die **kein** Gast sind (Drucker,
  Smart-TV, Saugroboter, Thermostate …). Jedes WLAN-Geraet, das **nicht**
  in `devices` steht und **nicht** auf dieser Liste, wird als **Gast**
  gemeldet. So bleibt die Gaeste-Zaehlung sauber. (Gaeste-Erkennung ist
  immer aktiv; diese Liste steuert nur, was ausgefiltert wird.)

**`intervals`** — adaptiver Abfrage-Rhythmus in Sekunden (wie oft der
Daemon die Boxen fragt):
- `normal` (Standard 60): Grundtakt.
- `movement` (15): schneller, sobald sich ein Geraet zwischen Raeumen/Etagen
  bewegt — damit der Raumwechsel zeitnah erkannt wird.
- `idle` (300): langsamer, wenn sich mehrere Takte lang nichts aendert —
  spart Last auf den Boxen.
- `live` (3): Sehr schneller Modus fuer Kalibrierung/Tests (siehe
  Schritt 7). Nicht dauerhaft nutzen — belastet die Boxen.

**`retention_days`** (Standard 60) — SQLite-Bewegungsverlauf:
- Eintraege aelter als X Tage werden automatisch geloescht (taeglicher
  Cleanup). So waechst die DB nicht endlos und alte Bewegungsdaten
  verschwinden datenschutzfreundlich von selbst.

**`db_path`** (Standard `fritztrack4u.db`) — Pfad/Name der Datenbank
(Tabellen `verlauf` und `gaeste`). Wird beim ersten Start automatisch
angelegt.

**`fingerprints`** — Ortungs-Feintuning fuer Raumgenauigkeit (siehe
Schritt 7): pro Etage eine Liste von Raeumen mit erwartetem Signal-Vektor,
Struktur `{ "Etage": { "Raum": { "Boxname": Signal%, ... } } }`. Ohne
Fingerprints loest der Daemon nur etagengenau auf.

**`fingerprint_tolerance`** (Standard 0.15 = 15 %) — Wie stark ein
gemessener Signal-Vektor vom gespeicherten Fingerprint abweichen darf und
trotzdem noch als derselbe Raum gilt. Groesser = unschaerfer, aber
stabiler; kleiner = praeziser, aber empfindlicher.

**`tr064_timeout`** (Standard 10) — Timeout in Sekunden pro Box-Abfrage.

**`mqtt`** — Home-Assistant-Anbindung (nur wenn `"enabled": true`):
- `host`/`port`: Adresse des MQTT-Brokers (oft der HA-Server, Port 1883).
- `user`/`password`: MQTT-Zugangsdaten (im Broker/Mosquitto-Add-on
  angelegt).
- `discovery_prefix`: muss zum HA-MQTT-Setup passen, Standard
  `homeassistant`.
- `base_topic`: MQTT-Topic-Praefix, Standard `fritztrack4u`.

> **Sicherheit:** Die `config.json` enthaelt Klartext-Passwoerter und das
> Wohnungs-Layout. Sie ist in der `.gitignore` ausgeschlossen und darf
> **niemals** in ein Git-Repository gepusht werden. Dateirechte
> einschraenken:
> ```bash
> chmod 600 config.json
> ```

---

## 5. Daemon starten

### Erster Test im Vordergrund

Vor dem Dauerbetrieb einmal manuell starten und mitlesen, ob alle Boxen
antworten:

```bash
cd /opt/fritztrack4u
source .venv/bin/activate
python3 fritztrack4u.py --config ./config.json
```

Tipp: Mit `--once` laeuft nur **ein** Abfrage-Zyklus und der Daemon beendet
sich wieder — ideal zum schnellen Testen der Config:

```bash
python3 fritztrack4u.py --config ./config.json --once
```

Erwartete Ausgabe (sinngemaess):
- pro Box eine erfolgreiche Anmeldung,
- ein Signal-Vektor pro erkanntem Handy, z. B.
  `Murat-iPhone -> Erdgeschoss / Wohnzimmer`,
- ggf. Hinweise zu Gaesten (`GUEST ...`).

Meldet eine Box "Login fehlgeschlagen" oder gar nichts → Schritt 8
(Troubleshooting), insbesondere Benutzer/Recht/IP pruefen.

Mit `Strg+C` beenden.

### Dauerbetrieb als systemd-Service (empfohlen)

Damit FritzTrack4U beim Boot automatisch startet und nach einem Absturz
oder Strom-Aus von selbst neu hochfaehrt, einen systemd-Service anlegen.

Datei `/etc/systemd/system/fritztrack4u.service` erstellen:

```ini
[Unit]
Description=FritzTrack4U Indoor-Positioning Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/fritztrack4u
ExecStart=/opt/fritztrack4u/.venv/bin/python3 /opt/fritztrack4u/fritztrack4u.py --config /opt/fritztrack4u/config.json
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Anpassen:
- `User=pi` → der Linux-Benutzer, dem der Projektordner gehoert.
- Pfade unter `WorkingDirectory` und `ExecStart` → an den tatsaechlichen
  Installationsort.

`Restart=always` sorgt dafuer, dass der Daemon **immer** wieder anlaeuft —
auch wenn z. B. kurz das Netz weg war oder eine FritzBox neu gestartet
ist. `RestartSec=10` wartet 10 Sekunden zwischen Neustart-Versuchen.

Service aktivieren und starten:

```bash
sudo systemctl daemon-reload
sudo systemctl enable fritztrack4u.service
sudo systemctl start fritztrack4u.service
```

Status und Logs:

```bash
sudo systemctl status fritztrack4u.service
journalctl -u fritztrack4u.service -f      # Live-Log mitlesen
```

> Nach jeder Aenderung an `config.json` den Service neu starten:
> `sudo systemctl restart fritztrack4u.service`

---

## 6. Home Assistant verbinden (MQTT)

Nur noetig, wenn Raum/Etage/Anwesenheit in Home Assistant sichtbar sein
sollen. Voraussetzung: MQTT-Broker laeuft (Schritt 1) und der
`mqtt`-Block in `config.json` ist mit `"enabled": true` ausgefuellt.

### Ablauf

1. **MQTT-Integration in HA** sicherstellen: Einstellungen → Geraete &
   Dienste → Integration **MQTT** muss vorhanden und mit dem Broker
   verbunden sein. (Beim Mosquitto-Add-on richtet HA das meist von selbst
   ein.)
2. **FritzTrack4U starten** (Schritt 5). Der Daemon meldet jedes Geraet
   per **MQTT Auto-Discovery** selbststaendig an — es muss in HA **nichts**
   manuell als Sensor angelegt werden.
3. In HA unter **Einstellungen → Geraete & Dienste → MQTT** erscheinen
   nach kurzer Zeit neue Entitaeten, z. B.:
   - `sensor.fritztrack_murat_iphone_raum` → aktueller Raum
   - `sensor.fritztrack_murat_iphone_etage` → aktuelle Etage
   - `binary_sensor.fritztrack_murat_iphone_anwesend` → zu Hause / abwesend
   - `sensor.fritztrack_gaeste` → Anzahl erkannter Gaeste

   (Die genauen Namen richten sich nach `name`/`person` in der Config.)

4. Diese Entitaeten lassen sich wie jeder andere Sensor in Dashboards,
   Automationen und Szenen verwenden — z. B. "Licht im Raum an, sobald
   das Handy den Raum betritt" oder "Heizung runter, wenn niemand
   anwesend".

> **Hinweis:** Die exakten Entity-Namen ergeben sich aus dem `base_topic`
> und dem Anzeigenamen aus `devices`. Welche Sensoren genau entstehen,
> haengt von der MQTT-Discovery-Konfiguration des Daemons ab — sie tauchen
> aber automatisch unter dem Geraet **FritzTrack4U** in HA auf.
>
> Wenn keine Entitaeten erscheinen: `discovery_prefix` in der Config muss
> mit dem in HA eingestellten MQTT-Discovery-Prefix uebereinstimmen
> (Standard `homeassistant`), und die MQTT-Zugangsdaten muessen stimmen.
> Mit `mosquitto_sub -h <broker> -u <user> -P <pass> -t '#' -v` laesst
> sich live mitlesen, ob FritzTrack4U publiziert.

---

## 7. Kalibrierung (optional, fuer Raumgenauigkeit)

Ohne Kalibrierung ordnet FritzTrack4U ein Handy grob der **staerksten Box
und damit der Etage** zu — das funktioniert sofort. Fuer **raumgenaue**
Ortung innerhalb einer Etage braucht es **Fingerprints**: gespeicherte
Signal-Vektoren je Raum.

### Was ein Fingerprint ist

Ein Fingerprint ist die "Signal-Unterschrift" eines Raumes — der erwartete
Signal-Vektor ueber die Boxen, gemessen in diesem Raum, z. B.:

```
Kueche  = { "Wohnzimmer": 72, "Schlafzimmer": 33, "Keller": 9 }
```

Im Betrieb vergleicht FritzTrack4U den aktuell gemessenen Vektor mit allen
gespeicherten Fingerprints **der erkannten Etage** und waehlt den
aehnlichsten — sofern die Abweichung innerhalb der
`fingerprint_tolerance` aus der Config liegt (Standard 0.15 = 15 %).

Fingerprints stehen im `fingerprints`-Block der `config.json`, gegliedert
nach **Etage → Raum → { Boxname: Signal% }** (siehe Schritt 4).

### So kalibrierst du (manuell, robust)

Der Daemon misst, du traegst die Werte ein:

1. **Live messen:** Mit dem Handy in der Hand in den Raum gehen, dort
   stehen bleiben und einen Einzel-Durchlauf starten:
   ```bash
   python3 fritztrack4u.py --config ./config.json --once
   ```
   Die Ausgabe zeigt den Signal-Vektor des Handys, z. B.
   `Murat-iPhone -> {Wohnzimmer: 72, Schlafzimmer: 33, Keller: 9}`.
   (Tipp: 2–3 Mal messen und mitteln — das Signal schwankt ein paar
   Prozent.)
2. **Eintragen:** Diesen Vektor unter dem Raumnamen in den
   `fingerprints`-Block der `config.json` schreiben, unter der passenden
   Etage:
   ```json
   "fingerprints": {
     "Erdgeschoss": {
       "Wohnzimmer": { "Wohnzimmer": 85, "Schlafzimmer": 40 },
       "Kueche":     { "Wohnzimmer": 72, "Schlafzimmer": 33 }
     }
   }
   ```
3. **Raum fuer Raum wiederholen** — moeglichst an der Stelle messen, wo man
   sich im Raum typischerweise aufhaelt.
4. **Service neu starten** (`sudo systemctl restart fritztrack4u.service`),
   damit die neuen Fingerprints geladen werden.

> **Roadmap-Hinweis:** Eine gefuehrte Live-Kalibrierung per Knopfdruck
> (statt manuellem Eintragen) ist als kuenftiges Feature geplant. Bis dahin
> ist das manuelle Eintragen der robuste Weg und gibt dir volle Kontrolle.

### Tipps fuer gute Fingerprints

- Pro Raum ggf. mehrere Messungen mitteln, das macht die Erkennung
  robuster.
- Immer mit demselben Handy(-Typ) kalibrieren, mit dem spaeter geortet
  wird — verschiedene Geraete funken unterschiedlich stark.
- Bei unsicherer Trennung zweier Raeume: `fingerprint_tolerance` testweise
  senken (z. B. auf 0.10) fuer mehr Praezision, oder erhoehen fuer mehr
  Stabilitaet.
- Die `config.json` (inkl. Fingerprints) enthaelt das Wohnungs-Layout und
  ist deshalb per `.gitignore` vom Git-Push ausgeschlossen.

---

## 8. Troubleshooting

### FritzBox ueberlastet / wird langsam

**Symptom:** WLAN ruckelt, Box reagiert traege, Logs zeigen Timeouts.

**Ursache:** Zu haeufige TR-064-Abfragen, besonders im Live-Takt (3 s) im
Dauerbetrieb, oder sehr viele Boxen gleichzeitig.

**Loesung:**
- `intervals.live` (3 s) nur zur Kalibrierung/Test nutzen, nicht dauerhaft.
- `intervals.normal` erhoehen (z. B. 90 oder 120 s), wenn sekundengenaue
  Ortung nicht noetig ist.
- `intervals.idle` grosszuegig setzen (z. B. 300–600 s) — wenn niemand
  daheim ist, muss kaum abgefragt werden.

### SID-Verweigerung / Login fehlgeschlagen

**Symptom:** Eine Box meldet beim Start "Login fehlgeschlagen", "SID
ungueltig" oder liefert keine Geraeteliste.

**Pruefen in dieser Reihenfolge:**
1. **Recht fehlt:** Hat der angelegte Benutzer die Berechtigung
   **"FRITZ!Box Einstellungen"**? Ohne dieses Recht ist TR-064 gesperrt
   (Schritt 2).
2. **Falsche Zugangsdaten:** Benutzername/Passwort in `config.json` exakt
   wie angelegt? Keine Leerzeichen, kein altes Admin-Passwort.
3. **Falsche IP:** Bei Repeatern muss die **eigene** Repeater-IP
   eingetragen sein, nicht die der Master-Box. IP in der Master-Box unter
   Heimnetz/Mesh nachsehen — Repeater koennen nach Neustart eine neue IP
   per DHCP bekommen. Tipp: in der FritzBox eine **feste IP** fuer jeden
   Repeater vergeben.
4. **TR-064 deaktiviert:** In der FritzBox unter **Heimnetz →
   Netzwerk → Netzwerkeinstellungen** muss **"Zugriff fuer Anwendungen
   zulassen"** (TR-064) aktiv sein.
5. Nach jeder Korrektur: `sudo systemctl restart fritztrack4u.service`.

### iPhone "verschwindet" / Anwesenheit flackert (Standby)

**Symptom:** Ein iPhone (seltener Android) wird zeitweise als **abwesend**
gemeldet, obwohl es im Haus liegt — typischerweise nachts oder wenn es
laenger ungenutzt herumliegt.

**Ursache:** iPhones gehen im Standby in einen WLAN-Schlafmodus und melden
sich von der Box ab, um Strom zu sparen. Fuer ein paar Minuten sieht dann
**keine** Box das Geraet.

**Loesung — kurzer Abfrage-Takt + feste MAC:** Halte `intervals.normal`
moderat (60 s), damit ein "Aufwach-Fenster" des iPhones zuverlaessig
getroffen wird, und stelle die **feste WLAN-MAC** am iPhone sicher
(Schritt 3) — wechselt die MAC, wird das Geraet ohnehin nicht erkannt.

> **Hinweis:** Ein eingebauter Karenz-Puffer (Geraet erst nach ~2 Minuten
> ohne Sichtung als abwesend melden, um kurzes Standby-Wegnicken zu
> ueberbruecken) ist als Verbesserung geplant. Bis dahin gilt: kurzer
> Normaltakt + feste MAC halten das Flackern gering.

Falls das Flackern trotzdem stoert:
- Die feste WLAN-MAC am iPhone sicherstellen (Schritt 3) — wechselt die
  MAC, hilft kein Puffer.
- `intervals.normal` nicht zu hoch setzen: Wird seltener abgefragt als der
  Standby-Zyklus, kann ein "Aufwach-Fenster" verpasst werden.

### Handy wird gar nicht erkannt

- MAC in `config.json` stimmt **exakt** (Gross-/Kleinschreibung egal, aber
  alle Bytes richtig)?
- Private/zufaellige WLAN-Adresse am Handy fuer das Heimnetz **aus**
  (Schritt 3)? Sonst funkt das Handy mit einer anderen MAC als in der
  Config.
- Handy aktuell ueberhaupt im WLAN (nicht nur Mobilfunk)?

### Raum stimmt nicht / springt zwischen Raeumen

- Fingerprints vorhanden und sauber eingelernt (Schritt 7)? Ohne
  Fingerprints gibt es nur Etagen-Genauigkeit.
- `fingerprint_tolerance` zu hoch → Raeume verschwimmen; testweise senken.
- Zu wenige Boxen fuer die Raumzahl → einen weiteren Repeater ergaenzen
  und neu kalibrieren.

### Keine Daten in Home Assistant

- `"enabled": true` im `mqtt`-Block?
- MQTT-Broker erreichbar, Zugangsdaten korrekt? Test:
  `mosquitto_sub -h <broker> -u <user> -P <pass> -t '#' -v` — kommen
  FritzTrack4U-Nachrichten an?
- `discovery_prefix` identisch zu HA (Standard `homeassistant`)?
- MQTT-Integration in HA verbunden?

---

## Anhang: Wartung & gute Praxis

- **Backups:** `config.json` sichern (enthaelt die ganze Einrichtung inkl.
  Fingerprints). Die Verlaufs-DB ist optional sicherbar.
- **Updates:** Vor Code-Updates Service stoppen
  (`sudo systemctl stop fritztrack4u.service`), Code aktualisieren,
  wieder starten.
- **Datenschutz:** Bewegungsdaten sind sensibel. Der Auto-Cleanup loescht
  alte Eintraege automatisch (`retention_days`, Standard 60). Wer kuerzere
  Aufbewahrung will, setzt den Wert herunter.
- **Sicherheit:** Benutzer auf den Boxen ohne Internet-Zugriff, nur
  "FRITZ!Box Einstellungen"-Recht, `config.json` mit `chmod 600`,
  niemals Zugangsdaten ins Git.
