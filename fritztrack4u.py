#!/usr/bin/env python3
"""
FritzTrack4U - Indoor positioning daemon based on FritzBox WLAN signal strength.

Open source (MIT) by Murat Danis - https://aslan4u.de

How it works
------------
Every configured FritzBox / repeater is queried over TR-064 (SOAP). For each
box we read all associated WLAN devices and their per-device signal strength
(%) across WLANConfiguration 1/2/3 (2.4 GHz, 5 GHz, guest). Per tracked phone
we build a signal vector {box_name: signal_percent}. The strongest box decides
the floor; a per-floor fingerprint then matches the room (with a configurable
tolerance). If no box sees a phone, that phone is reported as "away".

Unknown MACs that are not in the tracked list and not excluded are treated as
guests. History (positions + guests) is stored in SQLite with an automatic
retention cleanup. The poll interval adapts to activity (idle / normal /
movement / live). Results can optionally be published to an MQTT broker with
Home Assistant auto-discovery; if MQTT is disabled the daemon only writes
SQLite and prints to stdout.

EVERYTHING is config-driven (see config.example.json). No box IPs, FritzBox
credentials, MACs or MQTT credentials are hardcoded. The daemon works with a
single box, a box + repeater, or N boxes - the more boxes, the more accurate
the room match (that is the selling point).

CLI-only, no paid APIs.

Usage
-----
    python fritztrack4u.py --config ./config.json
"""

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# --- Optional MQTT dependency (paho-mqtt) ----------------------------------
# MQTT is optional. We try to import it lazily and degrade gracefully so the
# daemon still runs (SQLite + stdout) without the package installed.
try:
    import paho.mqtt.client as mqtt  # type: ignore
    _HAS_MQTT = True
except ImportError:
    mqtt = None  # type: ignore
    _HAS_MQTT = False


# ===========================================================================
# Configuration loading
# ===========================================================================

def load_config(path):
    """Load and validate config.json. Returns a normalized dict.

    The config file may contain "_comment" keys anywhere for documentation;
    they are ignored by the daemon.
    """
    if not os.path.isfile(path):
        raise SystemExit(f"[FATAL] Config file not found: {path}")

    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[FATAL] Config file is not valid JSON: {exc}")

    # --- Boxes (at least one required) ---
    boxes = cfg.get("boxes") or []
    if not boxes:
        raise SystemExit("[FATAL] config 'boxes' must contain at least one box.")
    for box in boxes:
        for key in ("name", "ip"):
            if not box.get(key):
                raise SystemExit(f"[FATAL] Each box needs '{key}'. Offending: {box}")
        # Floor is free-text (config-driven); default to a single floor label.
        box.setdefault("floor", "Floor 1")
        box.setdefault("floor_nr", 1)

    # --- Tracked devices: MAC -> friendly name ---
    devices = cfg.get("devices") or {}
    # Normalize MAC keys to upper case for stable matching. Skip any
    # documentation keys (e.g. "_comment") - real MAC addresses contain ":".
    cfg["devices"] = {
        mac.upper(): name
        for mac, name in devices.items()
        if not mac.startswith("_") and ":" in mac
    }

    # --- MQTT block (optional) ---
    mqtt_cfg = cfg.get("mqtt") or {}
    mqtt_cfg.setdefault("enabled", False)
    mqtt_cfg.setdefault("host", "127.0.0.1")
    mqtt_cfg.setdefault("port", 1883)
    mqtt_cfg.setdefault("user", "")
    mqtt_cfg.setdefault("password", "")
    mqtt_cfg.setdefault("discovery_prefix", "homeassistant")
    mqtt_cfg.setdefault("base_topic", "fritztrack4u")
    cfg["mqtt"] = mqtt_cfg

    # --- Adaptive poll intervals (seconds) ---
    intervals = cfg.get("intervals") or {}
    intervals.setdefault("normal", 60)     # default cadence
    intervals.setdefault("movement", 15)   # someone is moving between boxes
    intervals.setdefault("idle", 300)      # nothing changed for a while
    intervals.setdefault("live", 3)        # live tracking burst
    cfg["intervals"] = intervals

    # --- Misc ---
    cfg.setdefault("retention_days", 60)
    cfg.setdefault("fingerprint_tolerance", 0.15)   # 15% tolerance
    cfg.setdefault("db_path", "fritztrack4u.db")
    cfg.setdefault("tr064_timeout", 10)
    # Names to ignore when detecting guests (e.g. smart-home devices).
    # Skip any documentation entries that start with "_comment".
    cfg["exclude_names"] = [
        n.upper() for n in (cfg.get("exclude_names") or [])
        if not str(n).lower().startswith("_comment")
    ]
    # Optional per-floor room fingerprints:
    #   { "Floor 1": { "Living room": {"BoxA": 80, "BoxB": 30}, ... }, ... }
    # Drop any "_comment*" documentation keys at the floor level.
    fingerprints = cfg.get("fingerprints") or {}
    cfg["fingerprints"] = {
        floor: rooms
        for floor, rooms in fingerprints.items()
        if not str(floor).startswith("_")
    }

    return cfg


# ===========================================================================
# TR-064 client (SOAP over HTTP, digest-style MD5 challenge-response)
# ===========================================================================

class TR064Box:
    """Talks TR-064 to a single FritzBox / repeater.

    Authentication uses the FritzBox challenge-response scheme: the box sends a
    challenge, the client answers with MD5(challenge + "-" +
    MD5_UTF16LE(challenge + "-" + password)). All credentials come from config.
    """

    WLAN_SERVICES = (
        # (control URL, service type, config index)
        ("/upnp/control/wlanconfig1", "WLANConfiguration:1", 1),  # 2.4 GHz
        ("/upnp/control/wlanconfig2", "WLANConfiguration:2", 2),  # 5 GHz
        ("/upnp/control/wlanconfig3", "WLANConfiguration:3", 3),  # guest WLAN
    )

    def __init__(self, name, ip, floor, floor_nr, user, password, timeout=10):
        self.name = name
        self.ip = ip
        self.floor = floor
        self.floor_nr = floor_nr
        self.user = user
        self.password = password
        self.timeout = timeout
        self.base = f"http://{ip}:49000"

    # -- low level SOAP call -------------------------------------------------

    def _soap(self, control_url, service, action, body_args="", auth_header=""):
        """Send one SOAP action and return the raw XML response text."""
        service_type = f"urn:dslforum-org:service:{service}"
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f"<s:Header>{auth_header}</s:Header>"
            "<s:Body>"
            f'<u:{action} xmlns:u="{service_type}">{body_args}</u:{action}>'
            "</s:Body></s:Envelope>"
        )
        req = Request(
            self.base + control_url,
            data=envelope.encode("utf-8"),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SoapAction": f"{service_type}#{action}",
            },
            method="POST",
        )
        with urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _build_auth_header(self, challenge, realm):
        """Build the TR-064 client auth header from a challenge.

        secret = MD5_HEX( UTF-16LE( user + ":" + realm + ":" + password ) )
        auth   = MD5_HEX( secret + ":" + challenge )  (HTTP-digest style)
        """
        secret_src = f"{self.user}:{realm}:{self.password}".encode("utf-16-le")
        secret = hashlib.md5(secret_src).hexdigest()
        auth = hashlib.md5(f"{secret}:{challenge}".encode("utf-8")).hexdigest()
        nonce_id = "uuid:fritztrack4u"
        return (
            '<h:ClientAuth xmlns:h="http://soap-authentication.org/digest/2001/10/" '
            's:mustUnderstand="1">'
            f"<Nonce>{challenge}</Nonce>"
            f"<Auth>{auth}</Auth>"
            f"<UserID>{self.user}</UserID>"
            f"<Realm>{realm}</Realm>"
            f"<UUID>{nonce_id}</UUID>"
            "</h:ClientAuth>"
        )

    def _authed_call(self, control_url, service, action, body_args=""):
        """Perform an authenticated SOAP call (challenge then real request)."""
        # Step 1: initial call to obtain Challenge + Realm.
        init_header = (
            '<h:InitChallenge xmlns:h="http://soap-authentication.org/digest/2001/10/" '
            f's:mustUnderstand="1"><UserID>{self.user}</UserID></h:InitChallenge>'
        )
        try:
            first = self._soap(control_url, service, action, body_args, init_header)
        except HTTPError as exc:
            # 401/500 still carries the challenge envelope in its body.
            first = exc.read().decode("utf-8", errors="replace")

        challenge = self._xml_find(first, "Nonce")
        realm = self._xml_find(first, "Realm") or "F!Box SOAP-Auth"
        if not challenge:
            # No challenge -> already authorized or auth disabled; return body.
            return first

        # Step 2: answer the challenge.
        auth_header = self._build_auth_header(challenge, realm)
        return self._soap(control_url, service, action, body_args, auth_header)

    # -- XML helpers ---------------------------------------------------------

    @staticmethod
    def _xml_find(xml_text, local_name):
        """Find the first element whose tag ends with local_name; return text."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return None
        for elem in root.iter():
            tag = elem.tag.split("}")[-1]
            if tag == local_name:
                return (elem.text or "").strip()
        return None

    # -- public API ----------------------------------------------------------

    def box_geraete(self):
        """Return all associated WLAN devices on this box.

        German name kept from v6.1 for behavioral parity ("box devices").
        Returns a list of dicts:
            {"mac": "AA:BB:...", "signal": 78, "active": True}
        Signal is the per-device strength in percent across WLAN 1/2/3.
        """
        devices = []
        for control_url, service, _idx in self.WLAN_SERVICES:
            try:
                # 1) How many devices are associated on this WLAN config?
                resp = self._authed_call(
                    control_url, service, "GetTotalAssociations"
                )
                total_raw = self._xml_find(resp, "NewTotalAssociations")
                total = int(total_raw) if total_raw and total_raw.isdigit() else 0
            except (URLError, HTTPError, ValueError, OSError):
                # This radio (e.g. guest WLAN) may be off; skip silently.
                continue

            # 2) Pull each associated device by index.
            for i in range(total):
                try:
                    body = f"<NewAssociatedDeviceIndex>{i}</NewAssociatedDeviceIndex>"
                    dev = self._authed_call(
                        control_url, service,
                        "GetGenericAssociatedDeviceInfo", body,
                    )
                except (URLError, HTTPError, OSError):
                    continue

                mac = self._xml_find(dev, "NewAssociatedDeviceMACAddress")
                if not mac:
                    continue
                strength_raw = self._xml_find(dev, "NewX_AVM-DE_SignalStrength")
                authed_raw = self._xml_find(dev, "NewAssociatedDeviceAuthState")
                try:
                    signal_pct = int(strength_raw) if strength_raw else 0
                except ValueError:
                    signal_pct = 0
                devices.append({
                    "mac": mac.upper(),
                    "signal": signal_pct,
                    "active": authed_raw == "1",
                })
        return devices


# ===========================================================================
# SQLite history store (verlauf = positions, gaeste = guests)
# ===========================================================================

class HistoryStore:
    """SQLite-backed history with automatic retention cleanup.

    Tables (names kept from v6.1):
      - verlauf : per-phone position log (timestamp, name, floor, room, away)
      - gaeste  : detected guest devices (timestamp, mac, strongest box)
    """

    def __init__(self, db_path, retention_days):
        self.db_path = db_path
        self.retention_days = retention_days
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    def _init_schema(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS verlauf (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT NOT NULL,
                name      TEXT NOT NULL,
                floor     TEXT,
                room      TEXT,
                away      INTEGER NOT NULL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gaeste (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT NOT NULL,
                mac       TEXT NOT NULL,
                box       TEXT,
                signal    INTEGER
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_verlauf_ts ON verlauf(ts)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_gaeste_ts ON gaeste(ts)")
        self.conn.commit()

    def log_position(self, name, floor, room, away):
        self.conn.execute(
            "INSERT INTO verlauf (ts, name, floor, room, away) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), name, floor, room,
             1 if away else 0),
        )
        self.conn.commit()

    def log_guest(self, mac, box, signal):
        self.conn.execute(
            "INSERT INTO gaeste (ts, mac, box, signal) VALUES (?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), mac, box, signal),
        )
        self.conn.commit()

    def cleanup(self):
        """Delete rows older than retention_days from both tables."""
        cutoff = (datetime.now() - timedelta(days=self.retention_days))
        cutoff_iso = cutoff.isoformat(timespec="seconds")
        cur = self.conn.cursor()
        cur.execute("DELETE FROM verlauf WHERE ts < ?", (cutoff_iso,))
        deleted_v = cur.rowcount
        cur.execute("DELETE FROM gaeste WHERE ts < ?", (cutoff_iso,))
        deleted_g = cur.rowcount
        self.conn.commit()
        if deleted_v or deleted_g:
            print(f"[cleanup] removed {deleted_v} position + {deleted_g} guest "
                  f"rows older than {self.retention_days} days")

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass


# ===========================================================================
# MQTT publisher (optional) with Home Assistant auto-discovery
# ===========================================================================

class MqttPublisher:
    """Publishes positions to MQTT and registers HA auto-discovery sensors.

    When disabled (config mqtt.enabled=false) or paho-mqtt is missing, all
    methods become no-ops so the rest of the daemon is unaffected.
    """

    def __init__(self, mqtt_cfg):
        self.cfg = mqtt_cfg
        self.enabled = bool(mqtt_cfg.get("enabled")) and _HAS_MQTT
        self.client = None
        self.base_topic = mqtt_cfg.get("base_topic", "fritztrack4u")
        self.discovery_prefix = mqtt_cfg.get("discovery_prefix", "homeassistant")
        self._discovered = set()

        if mqtt_cfg.get("enabled") and not _HAS_MQTT:
            print("[mqtt] 'paho-mqtt' is not installed - MQTT disabled. "
                  "Install it with:  pip install paho-mqtt")

    def connect(self):
        if not self.enabled:
            return
        self.client = mqtt.Client(client_id="fritztrack4u")
        user = self.cfg.get("user")
        if user:
            self.client.username_pw_set(user, self.cfg.get("password", ""))
        try:
            self.client.connect(self.cfg["host"], int(self.cfg["port"]), keepalive=60)
            self.client.loop_start()
            print(f"[mqtt] connected to {self.cfg['host']}:{self.cfg['port']}")
        except (OSError, ValueError) as exc:
            print(f"[mqtt] connection failed ({exc}) - MQTT disabled for this run")
            self.enabled = False

    def _ensure_discovery(self, name):
        """Register a HA device_tracker-style sensor once per phone."""
        if not self.enabled or name in self._discovered:
            return
        slug = _slugify(name)
        topic = f"{self.discovery_prefix}/sensor/fritztrack4u_{slug}/config"
        payload = {
            "name": f"FritzTrack {name}",
            "unique_id": f"fritztrack4u_{slug}",
            "state_topic": f"{self.base_topic}/{slug}/state",
            "json_attributes_topic": f"{self.base_topic}/{slug}/attributes",
            "icon": "mdi:map-marker-account",
            "device": {
                "identifiers": ["fritztrack4u"],
                "name": "FritzTrack4U",
                "manufacturer": "4U",
                "model": "Indoor Positioning",
            },
        }
        self.client.publish(topic, json.dumps(payload), retain=True)
        self._discovered.add(name)

    def publish_position(self, name, floor, room, away, vector):
        if not self.enabled:
            return
        self._ensure_discovery(name)
        slug = _slugify(name)
        state = "away" if away else (room or floor or "home")
        self.client.publish(f"{self.base_topic}/{slug}/state", state, retain=True)
        attrs = {"floor": floor, "room": room, "away": away, "signals": vector}
        self.client.publish(
            f"{self.base_topic}/{slug}/attributes",
            json.dumps(attrs), retain=True,
        )

    def disconnect(self):
        if self.enabled and self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except (OSError, ValueError):
                pass


def _slugify(text):
    """Lowercase, keep alnum, turn the rest into single underscores."""
    out = "".join(c.lower() if c.isalnum() else "_" for c in text)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "device"


# ===========================================================================
# Positioning engine
# ===========================================================================

class PositionEngine:
    """Builds signal vectors, decides floor + room, presence and guests."""

    def __init__(self, config):
        self.cfg = config
        self.tolerance = float(config["fingerprint_tolerance"])
        self.tracked = config["devices"]                 # MAC -> name
        self.exclude = set(config["exclude_names"])       # upper-case names
        self.fingerprints = config["fingerprints"]        # floor -> room -> vec

        # Build the box clients from config.
        self.boxes = []
        for b in config["boxes"]:
            self.boxes.append(TR064Box(
                name=b["name"],
                ip=b["ip"],
                floor=b.get("floor", "Floor 1"),
                floor_nr=b.get("floor_nr", 1),
                user=b.get("user", config.get("fritzbox", {}).get("user", "")),
                password=b.get("password",
                               config.get("fritzbox", {}).get("password", "")),
                timeout=int(config["tr064_timeout"]),
            ))
        # Quick lookup: box name -> box object.
        self.box_by_name = {b.name: b for b in self.boxes}

    def sammle_alle(self):
        """Collect signal vectors for ALL seen MACs across all boxes.

        Returns:
            { mac: { box_name: signal_percent, ... }, ... }
        """
        vectors = {}
        for box in self.boxes:
            try:
                devices = box.box_geraete()
            except (URLError, HTTPError, OSError) as exc:
                print(f"[warn] box '{box.name}' ({box.ip}) unreachable: {exc}")
                continue
            for dev in devices:
                mac = dev["mac"]
                vectors.setdefault(mac, {})[box.name] = dev["signal"]
        return vectors

    def position(self, vector):
        """Determine (floor, room, away) for one signal vector.

        - away    : no box sees the phone.
        - floor   : floor of the strongest box.
        - room    : best fingerprint match for that floor within tolerance,
                    else None (floor-only resolution).
        """
        if not vector:
            return (None, None, True)

        # Strongest box wins the floor.
        strongest_box = max(vector, key=vector.get)
        box_obj = self.box_by_name.get(strongest_box)
        floor = box_obj.floor if box_obj else None

        room = self._match_room(floor, vector)
        return (floor, room, False)

    def _match_room(self, floor, vector):
        """Fingerprint match within +/- tolerance on the floor's fingerprints."""
        floor_prints = self.fingerprints.get(floor)
        if not floor_prints:
            return None

        best_room = None
        best_score = None
        for room, expected in floor_prints.items():
            score = self._fingerprint_distance(expected, vector)
            if score is None:
                continue
            if best_score is None or score < best_score:
                best_score = score
                best_room = room

        if best_room is None:
            return None

        # Tolerance gate: average per-box deviation must be within tolerance
        # of the signal scale (0..100 -> 15% == 15 points by default).
        if best_score <= self.tolerance * 100:
            return best_room
        return None

    @staticmethod
    def _fingerprint_distance(expected, observed):
        """Mean absolute deviation over the boxes present in the fingerprint."""
        shared = [b for b in expected if b in observed]
        if not shared:
            return None
        total = sum(abs(expected[b] - observed[b]) for b in shared)
        return total / len(shared)

    # -- guest detection -----------------------------------------------------

    def sammle_fremde(self, vectors):
        """Return vectors for MACs that are not in the tracked device list."""
        return {mac: vec for mac, vec in vectors.items()
                if mac not in self.tracked}

    def ist_gast(self, mac, name=None):
        """Decide whether an unknown MAC counts as a guest.

        A device is a guest when it is not tracked and not on the exclude list
        (the exclude list filters out smart-home gear, neighbours, etc.).
        """
        if mac in self.tracked:
            return False
        if name and name.upper() in self.exclude:
            return False
        return True


# ===========================================================================
# Adaptive cadence
# ===========================================================================

class AdaptiveClock:
    """Chooses the next sleep interval based on recent activity.

    movement : a tracked phone changed box/floor since last cycle.
    normal   : phones present but stable.
    idle     : no change for several cycles in a row.
    live     : forced fast burst (set externally, e.g. via a live flag).
    """

    def __init__(self, intervals):
        self.i = intervals
        self.idle_streak = 0
        self.idle_threshold = 5          # cycles of no change -> idle cadence
        self.live = False

    def next_interval(self, changed):
        if self.live:
            return self.i["live"]
        if changed:
            self.idle_streak = 0
            return self.i["movement"]
        self.idle_streak += 1
        if self.idle_streak >= self.idle_threshold:
            return self.i["idle"]
        return self.i["normal"]


# ===========================================================================
# Main daemon loop
# ===========================================================================

class FritzTrack4U:
    def __init__(self, config):
        self.cfg = config
        self.engine = PositionEngine(config)
        self.store = HistoryStore(config["db_path"], int(config["retention_days"]))
        self.mqtt = MqttPublisher(config["mqtt"])
        self.clock = AdaptiveClock(config["intervals"])
        self.tracked = config["devices"]            # MAC -> name
        self._last_state = {}                       # name -> (floor, room, away)
        self._last_cleanup = datetime.min
        self._running = True

    def _changed_since_last(self, current):
        """Did any tracked phone change floor/room/presence?"""
        if current != self._last_state:
            return True
        return False

    def cycle(self):
        """One full poll cycle. Returns True if anything changed."""
        vectors = self.engine.sammle_alle()

        # --- Tracked phones: position + presence ---
        current_state = {}
        for mac, name in self.tracked.items():
            vec = vectors.get(mac, {})
            floor, room, away = self.engine.position(vec)
            current_state[name] = (floor, room, away)

            self.store.log_position(name, floor, room, away)
            self.mqtt.publish_position(name, floor, room, away, vec)

            where = "AWAY" if away else f"{floor or '?'} / {room or 'floor-only'}"
            print(f"[{datetime.now():%H:%M:%S}] {name:<12} -> {where}")

        # --- Guests: unknown, non-excluded MACs ---
        for mac, vec in self.engine.sammle_fremde(vectors).items():
            if not self.engine.ist_gast(mac):
                continue
            if not vec:
                continue
            strongest_box = max(vec, key=vec.get)
            self.store.log_guest(mac, strongest_box, vec[strongest_box])
            print(f"[{datetime.now():%H:%M:%S}] GUEST {mac} near {strongest_box} "
                  f"({vec[strongest_box]}%)")

        changed = self._changed_since_last(current_state)
        self._last_state = current_state
        return changed

    def maybe_cleanup(self):
        """Run retention cleanup at most once per day."""
        if (datetime.now() - self._last_cleanup) > timedelta(hours=24):
            self.store.cleanup()
            self._last_cleanup = datetime.now()

    def run(self):
        self.mqtt.connect()
        print(f"[start] FritzTrack4U tracking {len(self.tracked)} device(s) "
              f"across {len(self.engine.boxes)} box(es).")
        try:
            while self._running:
                try:
                    changed = self.cycle()
                except Exception as exc:  # never let one bad cycle kill the daemon
                    print(f"[error] cycle failed: {exc}")
                    changed = False
                self.maybe_cleanup()
                wait = self.clock.next_interval(changed)
                # Sleep in small steps so SIGTERM is honoured promptly.
                slept = 0
                while slept < wait and self._running:
                    time.sleep(min(1, wait - slept))
                    slept += 1
        finally:
            self.shutdown()

    def shutdown(self):
        self._running = False
        self.mqtt.disconnect()
        self.store.close()
        print("[stop] FritzTrack4U shut down cleanly.")


# ===========================================================================
# Entry point
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="FritzTrack4U - FritzBox WLAN indoor positioning daemon."
    )
    parser.add_argument(
        "--config", default="./config.json",
        help="Path to config.json (default: ./config.json)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single poll cycle and exit (useful for testing/cron).",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    daemon = FritzTrack4U(config)

    # Graceful shutdown on SIGINT/SIGTERM.
    def _handle_signal(signum, _frame):
        print(f"\n[signal] received {signum}, shutting down...")
        daemon._running = False

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    if args.once:
        daemon.mqtt.connect()
        try:
            daemon.cycle()
            daemon.maybe_cleanup()
        finally:
            daemon.shutdown()
    else:
        daemon.run()


if __name__ == "__main__":
    main()
