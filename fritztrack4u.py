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
    python fritztrack4u.py --once
    python fritztrack4u.py --live
    python fritztrack4u.py --calibrate --floor "Floor 1" --room "Living room" --samples 10 --duration 20
    python fritztrack4u.py --calibrate-floor "Floor 1"
"""

import argparse
import collections
import concurrent.futures
import hashlib
import http.server
import json
import os
import signal
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse, parse_qs, unquote, urlencode

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
    # Signal smoothing: rolling window size per mac (>=1; 1 = disabled)
    cfg.setdefault("smoothing_window", 3)
    # Penalty per missing box when comparing fingerprints (signal points)
    cfg.setdefault("fingerprint_missing_penalty", 5)
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

    # --- Debug / HTTP server ---
    debug_cfg = cfg.get("debug") or {}
    debug_cfg.setdefault("http_enabled", False)
    debug_cfg.setdefault("port", 8099)
    debug_cfg.setdefault("live_mode", False)
    # Mesh topology cache TTL in seconds. Mesh changes rarely; 30s is sufficient.
    debug_cfg.setdefault("mesh_ttl", 30)
    cfg["debug"] = debug_cfg

    # --- Polling method ---
    # method: "edit_device" = real RSSI dBm via data.lua page=edit_device (default)
    #         "homenet"     = legacy RX-rate proxy (0-100%, no real dBm)
    # Switching to "homenet" disables parallel polling and real dBm reporting.
    polling_cfg = cfg.get("polling") or {}
    polling_cfg.setdefault("method", "edit_device")
    # Per-box timeout for parallel edit_device calls (seconds). A hanging box
    # must not block the whole cycle. Default: tr064_timeout + 5 s headroom.
    polling_cfg.setdefault(
        "box_timeout",
        int(cfg.get("tr064_timeout", 10)) + 5,
    )
    cfg["polling"] = polling_cfg

    return cfg


def save_config(path, floor, room, fingerprint):
    """Atomically write one fingerprint into config.json.

    Loads the raw file (no normalization) so _comment keys and all
    existing structure are preserved. Modifies only
    cfg["fingerprints"][floor][room]. Writes to a .tmp file first, then
    os.replace() so a Ctrl-C mid-write never corrupts the config.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    if "fingerprints" not in raw or not isinstance(raw["fingerprints"], dict):
        raw["fingerprints"] = {}
    if floor not in raw["fingerprints"] or not isinstance(
            raw["fingerprints"][floor], dict):
        raw["fingerprints"][floor] = {}
    raw["fingerprints"][floor][room] = fingerprint

    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


# ===========================================================================
# TR-064 client (SOAP over HTTP, digest-style MD5 challenge-response)
# ===========================================================================

class TR064Box:
    """Talks to a single FritzBox / repeater via SID/data.lua (web interface).

    Authentication: SID-based challenge-response (login_sid.lua).
    Device data: POST /data.lua page=homeNet -> topology JSON.
    Mesh topology: derived from the same homeNet call (parent chains).

    SID is cached per instance (TTL 1100 s, safely inside the 1200 s
    Fritz!OS session window). A second call within the TTL reuses the
    existing SID without a login round-trip — that is the key advantage
    over per-request TR-064 SOAP auth.

    Signal reporting:
      - conninfo.bandinfo[0].speed_rx is the receive rate in Mbit/s.
        It is converted to a 0-100 percent proxy using 867 Mbit/s (5 GHz
        max for common AVM hardware) as the ceiling. The result is labelled
        "signal_proxy_from_rx_rate" in code comments because it is NOT dBm
        RSSI — it reflects link throughput, not radio sensitivity. It is
        good enough for room fingerprinting (strong link near the box vs.
        weak link far away). True RSSI would require a separate wSet page
        call; omitted to keep the poll to one POST per cycle.
      - If speed_rx is 0 or absent, signal is reported as 1 (minimum
        non-zero so the device still shows up in vectors).
      - active = stateinfo.active (bool from homeNet JSON).

    Port: 80 (http://<ip>/...), not 49000 (TR-064).
    """

    # SID TTL: Fritz!OS sessions expire after 1200 s (20 min).
    # We renew at 1100 s to keep a safety margin.
    _SID_TTL = 1100
    # Rate ceiling used for signal percent calculation (Mbit/s).
    # 867 Mbit/s covers 802.11ac MCS9 on 5 GHz 2x2. Adjust if needed.
    _RATE_CEIL = 867.0

    def __init__(self, name, ip, floor, floor_nr, user, password, timeout=10):
        self.name = name
        self.ip = ip
        self.floor = floor
        self.floor_nr = floor_nr
        self.user = user
        self.password = password
        self.timeout = timeout
        # Web interface base URL (port 80, not TR-064 port 49000).
        self.web_base = f"http://{ip}"
        # SID cache: (sid_string, timestamp_of_login).
        self._sid = None
        self._sid_ts = 0.0
        # auth_error set to True when SID login fails (wrong credentials).
        self.auth_error = False
        # reachable: last known reachability (updated by box_geraete).
        self.reachable = True
        # UID cache for edit_device path: MAC (upper) -> landevice UID string.
        # Populated by _resolve_uid(). Cached for the lifetime of the instance
        # because MACs do not change between polls (device stays on the network).
        self._uid_cache = {}

    # -- SID authentication --------------------------------------------------

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

    def _login_once(self):
        """Fetch a fresh SID from login_sid.lua.

        Flow (AVM standard, works on all Fritz!OS versions):
          1. GET /login_sid.lua  -> XML with <Challenge>
          2. MD5( UTF-16LE(challenge + "-" + password) ) -> response_hash
          3. GET /login_sid.lua?username=<user>&response=<challenge-hash>
             -> XML with <SID>
          4. SID "0000000000000000" means wrong credentials.

        Returns the SID string on success.
        Raises URLError/OSError on network error.
        Raises PermissionError when credentials are rejected.
        """
        # Step 1: get challenge.
        url1 = f"{self.web_base}/login_sid.lua"
        req1 = Request(url1, method="GET")
        with urlopen(req1, timeout=self.timeout) as r:
            xml1 = r.read().decode("utf-8", errors="replace")

        challenge = self._xml_find(xml1, "Challenge")
        if not challenge:
            raise ValueError(
                f"box '{self.name}': login_sid.lua returned no Challenge"
            )

        # Step 2: compute response.
        # AVM spec: MD5( UTF-16LE( challenge + "-" + password ) )
        resp_src = (challenge + "-" + self.password).encode("utf-16-le")
        resp_hash = hashlib.md5(resp_src).hexdigest()
        response = f"{challenge}-{resp_hash}"

        # Step 3: authenticate.
        from urllib.parse import quote as _quote
        url2 = (
            f"{self.web_base}/login_sid.lua"
            f"?username={_quote(self.user)}&response={response}"
        )
        req2 = Request(url2, method="GET")
        with urlopen(req2, timeout=self.timeout) as r:
            xml2 = r.read().decode("utf-8", errors="replace")

        sid = self._xml_find(xml2, "SID")
        if not sid or sid == "0000000000000000":
            raise PermissionError(
                f"box '{self.name}' ({self.ip}): SID login failed "
                "(wrong username/password or user has no web-interface rights)"
            )
        return sid

    def _get_sid(self):
        """Return a valid SID, reusing the cached one when still fresh.

        Logs [warn] and sets self.auth_error=True on credential failure.
        Raises URLError/OSError on network-level errors (box unreachable).
        """
        now = time.time()
        if self._sid and (now - self._sid_ts) < self._SID_TTL:
            return self._sid  # still valid, no login needed

        # Cache expired or not yet initialized — log in.
        try:
            sid = self._login_once()
        except PermissionError as exc:
            self.auth_error = True
            print(f"[warn] {exc}")
            return None

        self._sid = sid
        self._sid_ts = now
        self.auth_error = False
        return sid

    # -- data.lua POST -------------------------------------------------------

    def _data_lua(self, page, extra_params=None):
        """POST to /data.lua and return the parsed JSON dict.

        On SID expiry (data.lua responds with a redirect to login or an
        empty/error JSON), clears the cached SID and retries once with a
        fresh login. Raises URLError/OSError on network errors.
        Returns None when the response cannot be parsed as JSON.
        """
        url = f"{self.web_base}/data.lua"

        def _post(sid):
            params = {"sid": sid, "page": page, "xhr": "1", "no_sidrenew": ""}
            if extra_params:
                params.update(extra_params)
            body = urlencode(params).encode("utf-8")
            req = Request(
                url,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            # data.lua (esp. page=homeNet) returns a large payload the box
            # assembles on the fly — it needs more time than the short
            # login/SID round-trips. Use a generous floor of 20 s.
            post_timeout = max(self.timeout, 20)
            with urlopen(req, timeout=post_timeout) as r:
                return r.read().decode("utf-8", errors="replace")

        sid = self._get_sid()
        if not sid:
            return None  # auth_error already logged

        raw = _post(sid)

        # data.lua returns a redirect (3xx Location: /login.lua) or an empty
        # body when the SID has expired. Detect and retry once.
        if not raw.strip() or '"sid"' in raw[:120]:
            # Possibly a session-expired JSON with "sid":"0000..." — clear and retry.
            self._sid = None
            self._sid_ts = 0.0
            sid = self._get_sid()
            if not sid:
                return None
            raw = _post(sid)

        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[warn] box '{self.name}' data.lua/{page} JSON parse error: {exc}")
            return None

    # -- homeNet device parsing ----------------------------------------------

    @staticmethod
    def _rate_to_signal(speed_rx_str):
        """Convert a speed_rx string like '433' (Mbit/s) to a 1-100 percent proxy.

        Signal here is link throughput converted to percent, NOT RSSI dBm.
        Used as a room-fingerprinting proxy: strong near the box, weak far away.
        Returns 1 (not 0) when no rate is available so the device still appears
        in signal vectors.
        """
        try:
            rate = float(str(speed_rx_str).replace(",", ".").strip())
        except (ValueError, TypeError):
            return 1
        if rate <= 0:
            return 1
        pct = int(round(rate / TR064Box._RATE_CEIL * 100))
        return max(1, min(100, pct))

    def _parse_homenet_devices(self, data, uid_to_mac=None):
        """Extract WLAN client list from a homeNet data.lua response.

        Returns list of dicts:
            {"mac": str, "signal": int (1-100), "active": bool,
             "name": str, "band": str, "parent_uid": str|None}

        Only devices with conn=="wlan" are returned (LAN devices excluded).
        homeNet does NOT carry the MAC inside the device object (only the
        landevice UID + IP). The MAC is resolved via uid_to_mac, a
        {landevice_uid: MAC} map built from the wSet page (see box_geraete).
        Devices whose MAC cannot be resolved are skipped.
        """
        uid_to_mac = uid_to_mac or {}
        try:
            devices_dict = data["data"]["topology"]["devices"]
        except (KeyError, TypeError):
            return []

        result = []
        for uid, dev in devices_dict.items():
            if not isinstance(dev, dict):
                continue
            if dev.get("conn") != "wlan":
                continue  # LAN or unknown connection type

            # MAC: homeNet carries no MAC in the device object — resolve it
            # from the wSet-derived uid->mac map (keyed by the landevice UID).
            mac = (uid_to_mac.get(uid) or "").upper().strip()
            if not mac or mac == "00:00:00:00:00:00":
                continue

            # Device name.
            nameinfo = dev.get("nameinfo") or {}
            raw_name = nameinfo.get("name") or dev.get("name") or ""
            # "lwip0" and "none" are Fritz!OS placeholders for unnamed devices.
            name = "" if raw_name in ("lwip0", "none", "lwip") else raw_name

            # Signal: derive from RX rate (see _rate_to_signal docstring).
            conninfo = dev.get("conninfo") or {}
            bandinfo_list = conninfo.get("bandinfo") or []
            speed_rx = None
            band = conninfo.get("desc") or ""
            if bandinfo_list and isinstance(bandinfo_list, list):
                bi = bandinfo_list[0]
                if isinstance(bi, dict):
                    speed_rx = bi.get("speed_rx")
                    if not band:
                        band = bi.get("radio") or ""
            if speed_rx is None:
                # Fall back to conninfo.speed_rx if present.
                speed_rx = conninfo.get("speed_rx")
            signal = self._rate_to_signal(speed_rx)

            # Active status.
            stateinfo = dev.get("stateinfo") or {}
            active = bool(stateinfo.get("active") or stateinfo.get("online"))

            # Parent UID (which box/repeater this device is connected to).
            parent_uid = dev.get("parent") or None

            result.append({
                "mac":        mac,
                "signal":     signal,
                "active":     active,
                "name":       name,
                "band":       band,
                "parent_uid": parent_uid,
                "_uid":       uid,
            })
        return result

    # -- edit_device / real RSSI path ----------------------------------------

    def _resolve_uid(self, mac):
        """Resolve a MAC address to its landevice UID via data.lua page=wSet.

        The UID is cached in self._uid_cache so subsequent calls within the
        same daemon instance do not pay the wSet round-trip again.

        Returns the UID string, or None when the MAC is not found on this box
        (device is unknown here or wSet failed).
        """
        mac_upper = mac.upper()
        cached = self._uid_cache.get(mac_upper)
        if cached:
            return cached

        data = self._data_lua("wSet")
        if not data:
            return None

        found = {}

        def _walk(obj):
            if isinstance(obj, dict):
                obj_mac = str(obj.get("mac") or obj.get("macAddress") or "").upper()
                if obj_mac == mac_upper:
                    uid = obj.get("UID") or obj.get("uid")
                    if uid:
                        found["uid"] = uid
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    _walk(v)

        _walk(data)
        uid = found.get("uid")
        if uid:
            self._uid_cache[mac_upper] = uid
        return uid

    def read_device(self, mac):
        """Read real RSSI dBm for one MAC from this box via page=edit_device.

        Returns a dict:
            {
                "mac":          str,
                "connected":    bool,    # True only if this box currently serves the device
                "rssi":         int|None, # real dBm value (negative), None if not connected
                "band":         str,      # "2.4GHz" | "5GHz" | ""
                "phy_rate_tx":  int|None, # TX PHY rate in Mbit/s
                "phy_rate_rx":  int|None, # RX PHY rate in Mbit/s
                "quality":      int|None, # link quality 0-100
                "state":        str,      # "CONNECTED" | other wlan.state value | ""
            }

        A device that is not associated to THIS box returns connected=False,
        rssi=None. This is normal: every box is queried independently and at
        most one box per cycle should return connected=True for a given MAC.

        Raises URLError / OSError on network-level failure so the caller can
        mark this box as unreachable without crashing the whole cycle.
        """
        mac_upper = mac.upper()
        result = {
            "mac":         mac_upper,
            "connected":   False,
            "rssi":        None,
            "band":        "",
            "phy_rate_tx": None,
            "phy_rate_rx": None,
            "quality":     None,
            "state":       "",
        }

        uid = self._resolve_uid(mac_upper)
        if not uid:
            # MAC not known to this box — it has never connected here.
            return result

        data = self._data_lua("edit_device", {"dev": uid})
        if not data:
            return result

        try:
            dev = data["data"]["vars"]["dev"]
        except (KeyError, TypeError):
            return result

        wlan = dev.get("wlan") or {}
        state = str(wlan.get("state") or "").strip()
        result["state"] = state

        show_list = wlan.get("show") or []
        show = show_list[0] if show_list and isinstance(show_list, list) else {}

        # rssi: edit_device returns the real RSSI dBm as a string like "-58"
        # or as an integer. 0 / "" means the box does not currently serve this
        # device (not associated here right now).
        raw_rssi = show.get("rssi")
        try:
            rssi_int = int(raw_rssi)
        except (TypeError, ValueError):
            rssi_int = 0

        # phy rates (Mbit/s)
        raw_tx = show.get("speed")
        raw_rx = show.get("speed_rx")
        try:
            tx_int = int(raw_tx) if raw_tx not in (None, "") else None
        except (TypeError, ValueError):
            tx_int = None
        try:
            rx_int = int(raw_rx) if raw_rx not in (None, "") else None
        except (TypeError, ValueError):
            rx_int = None

        # quality (0-100 percent)
        raw_q = show.get("quality")
        try:
            q_int = int(raw_q) if raw_q not in (None, "") else None
        except (TypeError, ValueError):
            q_int = None

        # Band: Fritz!OS exposes frequency in MHz as "freq" inside show[0],
        # or as a band label. Fall back to guessing from TX rate when absent.
        raw_freq = show.get("freq") or show.get("frequency") or ""
        try:
            freq_mhz = int(str(raw_freq).strip())
        except (TypeError, ValueError):
            freq_mhz = 0
        if freq_mhz >= 5000:
            band = "5GHz"
        elif 2400 <= freq_mhz < 2500:
            band = "2.4GHz"
        elif tx_int and tx_int >= 200:
            # Heuristic: 5 GHz rates are typically >=200 Mbit/s for 2x2 MIMO.
            band = "5GHz"
        elif tx_int and tx_int > 0:
            band = "2.4GHz"
        else:
            band = ""

        # "connected" means this box is currently the serving AP for this device.
        # Criteria: state == "CONNECTED" AND rssi is a non-zero negative number.
        connected = (state == "CONNECTED") and (rssi_int < 0)

        result.update({
            "connected":   connected,
            "rssi":        rssi_int if connected else None,
            "band":        band,
            "phy_rate_tx": tx_int,
            "phy_rate_rx": rx_int,
            "quality":     q_int,
        })
        return result

    # -- public API ----------------------------------------------------------

    def box_geraete(self):
        """Return all associated WLAN devices on this box via data.lua/homeNet.

        Returns a list of dicts (same schema as before, for caller compatibility):
            {"mac": "AA:BB:...", "signal": int (1-100), "active": bool}

        Signal is a throughput-derived percent proxy (1-100), NOT RSSI dBm.
        active = stateinfo.active from the homeNet JSON.

        Sets self.reachable and self.auth_error based on the result.
        Raises URLError/OSError when the box is completely network-unreachable
        (so sammle_alle can catch it and mark the box as unreachable).
        """
        data = self._data_lua("homeNet")
        if data is None:
            # Either auth error (already logged) or JSON parse failure.
            self.reachable = not self.auth_error
            return []

        # homeNet has no MAC per device — build a {landevice_uid: MAC} map
        # from the wSet page and use it to resolve MACs while parsing.
        uid_to_mac = self._build_uid_mac_map()
        devices_full = self._parse_homenet_devices(data, uid_to_mac)
        self.reachable = True

        # Return only the fields the rest of the daemon uses.
        return [
            {"mac": d["mac"], "signal": d["signal"], "active": d["active"]}
            for d in devices_full
        ]

    def _build_uid_mac_map(self):
        """Build {landevice_uid: MAC} from the wSet data.lua page.

        homeNet identifies devices only by their landevice UID; wSet lists
        every known device with both its UID and MAC. Joining on the UID
        gives each homeNet WLAN client its MAC. Returns {} on failure (the
        caller then yields no devices rather than crashing).
        """
        mapping = {}
        data = self._data_lua("wSet")
        if not data:
            return mapping

        def _walk(obj):
            if isinstance(obj, dict):
                uid = obj.get("UID") or obj.get("uid")
                mac = obj.get("mac") or obj.get("macAddress")
                if uid and mac and isinstance(mac, str) and ":" in mac:
                    mapping[uid] = mac
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    _walk(v)

        _walk(data)
        return mapping

    def is_reachable(self):
        """Quick reachability check: try to reach the login page.

        Returns True if the box answers HTTP, False on network error.
        Does not consume a SID or make data.lua calls.
        """
        try:
            req = Request(
                f"{self.web_base}/login_sid.lua", method="GET"
            )
            with urlopen(req, timeout=self.timeout):
                pass
            return True
        except (URLError, HTTPError, OSError, Exception):
            return False

    def get_mesh(self):
        """Return mesh topology from the homeNet data.lua call.

        Uses the same homeNet page that box_geraete() uses — no extra poll.
        Derives topology from parent-UID chains in the devices dict.

        Returns:
            {
                "nodes": [
                    {
                        "name":      str,
                        "mac":       str | None,
                        "is_master": bool,
                        "type":      "router" | "repeater" | "client" | "unknown",
                        "model":     None   # not available via homeNet
                    }
                ],
                "links": [
                    {
                        "from_mac": str | None,   # parent device MAC
                        "to_mac":   str | None,   # child device MAC
                        "rx_rate":  int | None,   # Mbit/s (int), null if unknown
                        "tx_rate":  int | None,   # Mbit/s (int), null if unknown
                        "rssi":     None           # not available via homeNet
                    }
                ]
            }

        Returns {"nodes": [], "links": []} on any failure. Never raises.
        """
        empty = {"nodes": [], "links": []}
        try:
            data = self._data_lua("homeNet")
        except Exception as exc:
            print(f"[warn] mesh: box '{self.name}' homeNet fetch failed: {exc}")
            return empty

        if data is None:
            return empty

        try:
            devices_dict = data["data"]["topology"]["devices"]
        except (KeyError, TypeError):
            print(
                f"[warn] mesh: box '{self.name}' homeNet missing "
                "data.topology.devices"
            )
            return empty

        # Build UID -> device entry map with MAC resolution.
        uid_map = {}  # uid -> {"mac", "name", "conn", "type", "parent_uid", "conninfo"}
        for uid, dev in devices_dict.items():
            if not isinstance(dev, dict):
                continue
            mac = (
                dev.get("mac")
                or dev.get("macAddress")
                or dev.get("mac_address")
                or ""
            ).upper().strip()
            nameinfo = dev.get("nameinfo") or {}
            raw_name = nameinfo.get("name") or dev.get("name") or ""
            name = "" if raw_name in ("lwip0", "none", "lwip") else raw_name
            conn = dev.get("conn") or ""
            category = (dev.get("category") or "").lower()
            uid_map[uid] = {
                "mac":        mac or None,
                "name":       name,
                "conn":       conn,
                "category":   category,
                "parent_uid": dev.get("parent") or None,
                "conninfo":   dev.get("conninfo") or {},
                "stateinfo":  dev.get("stateinfo") or {},
                "dist":       dev.get("dist"),
            }

        # Determine device type.
        # Fritz!OS homeNet topology:
        #   - The mesh master (and repeaters acting as nodes) appear as
        #     parent of client devices. Devices with dist==0 or no parent
        #     and category containing "box"/"repeater" are infrastructure nodes.
        #   - Clients have conn=="wlan" or "lan" and dist >= 1.
        def _node_type(entry):
            cat = entry.get("category") or ""
            conn = entry.get("conn") or ""
            if "box" in cat or "router" in cat:
                return "router"
            if "repeater" in cat or "extender" in cat:
                return "repeater"
            if conn in ("wlan", "lan"):
                return "client"
            return "unknown"

        # Collect infrastructure nodes (boxes + repeaters).
        # A node is infrastructure when it is a parent of at least one other
        # device, OR when its category says so.
        child_parent_uids = {
            e["parent_uid"]
            for e in uid_map.values()
            if e["parent_uid"]
        }
        infra_uids = {
            uid
            for uid, e in uid_map.items()
            if uid in child_parent_uids
            or "box" in e.get("category", "")
            or "repeater" in e.get("category", "")
        }

        nodes_out = []
        for uid in infra_uids:
            e = uid_map[uid]
            ntype = _node_type(e)
            # is_master: box with dist==0 or no parent, or the only router.
            is_master = (
                ntype == "router"
                and (e.get("dist") in (0, None) or not e.get("parent_uid"))
            )
            nodes_out.append({
                "name":      e["name"],
                "mac":       e["mac"],
                "is_master": is_master,
                "type":      ntype,
                "model":     None,  # not exposed via homeNet
            })

        # Build links: for each WLAN client, link parent_uid -> client.
        links_out = []
        seen_links = set()
        for uid, e in uid_map.items():
            parent_uid = e.get("parent_uid")
            if not parent_uid or parent_uid not in uid_map:
                continue
            parent = uid_map[parent_uid]
            from_mac = parent["mac"]
            to_mac = e["mac"]

            # De-duplicate symmetric links.
            key = tuple(sorted([from_mac or uid, to_mac or parent_uid]))
            if key in seen_links:
                continue
            seen_links.add(key)

            # Rate from conninfo.
            conninfo = e.get("conninfo") or {}
            bandinfo_list = conninfo.get("bandinfo") or []
            speed_rx = speed_tx = None
            if bandinfo_list and isinstance(bandinfo_list, list):
                bi = bandinfo_list[0]
                if isinstance(bi, dict):
                    try:
                        speed_rx = int(float(str(bi.get("speed_rx") or 0)))
                    except (ValueError, TypeError):
                        speed_rx = None
                    try:
                        speed_tx = int(float(str(bi.get("speed_tx") or 0)))
                    except (ValueError, TypeError):
                        speed_tx = None

            links_out.append({
                "from_mac": from_mac,
                "to_mac":   to_mac,
                "rx_rate":  speed_rx if speed_rx else None,
                "tx_rate":  speed_tx if speed_tx else None,
                "rssi":     None,  # not available in homeNet; wSet has it
            })

        return {"nodes": nodes_out, "links": links_out}


# ===========================================================================
# SQLite history store (verlauf = positions, gaeste = guests)
# ===========================================================================

class HistoryStore:
    """SQLite-backed history with automatic retention cleanup.

    Tables (names kept from v6.1):
      - verlauf       : per-phone position log (timestamp, name, floor, room, away)
      - gaeste        : detected guest devices (timestamp, mac, strongest box)
      - live_snapshot : current position per MAC (upserted each cycle)
      - box_snapshot  : current per-box device list (upserted each cycle)
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS live_snapshot (
                mac         TEXT PRIMARY KEY,
                name        TEXT,
                ts          TEXT,
                floor       TEXT,
                room        TEXT,
                score       REAL,
                away        INTEGER NOT NULL DEFAULT 0,
                vector_json TEXT
            )
        """)
        # Per-box device snapshot written every cycle.
        # reachable=0 when the box was unreachable during the poll.
        # devices_json: JSON array of {mac, signal, active, name} objects.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS box_snapshot (
                box_name     TEXT PRIMARY KEY,
                ts           TEXT NOT NULL,
                reachable    INTEGER NOT NULL DEFAULT 1,
                device_count INTEGER NOT NULL DEFAULT 0,
                devices_json TEXT NOT NULL DEFAULT '[]'
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

    def upsert_snapshot(self, mac, name, ts, floor, room, score, away, vector):
        """Insert or replace the current position snapshot for one MAC."""
        vector_json = json.dumps(vector, ensure_ascii=False) if vector else "{}"
        self.conn.execute(
            """INSERT OR REPLACE INTO live_snapshot
               (mac, name, ts, floor, room, score, away, vector_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (mac, name, ts, floor, room, score, 1 if away else 0, vector_json),
        )
        self.conn.commit()

    def upsert_box_snapshot(self, box_name, ts, reachable, devices):
        """Insert or replace the current device list for one box.

        devices is a list of dicts: {"mac", "signal", "active", "name"}.
        reachable=False means the box was unreachable this cycle; devices=[]
        in that case - we store the fact explicitly instead of silently keeping
        stale data (audit finding: unreachable must be visible, not silent 0).
        """
        devices_json = json.dumps(devices, ensure_ascii=False)
        self.conn.execute(
            """INSERT OR REPLACE INTO box_snapshot
               (box_name, ts, reachable, device_count, devices_json)
               VALUES (?,?,?,?,?)""",
            (box_name, ts, 1 if reachable else 0, len(devices), devices_json),
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
# HTTP debug server (stdlib only, localhost only, read-only)
# ===========================================================================

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png":  "image/png",
    ".ico":  "image/x-icon",
    ".svg":  "image/svg+xml",
}


class _DebugHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for the debug server. All routes are GET, read-only.

    New routes added:
      GET /api/mesh         - Mesh topology (cached, mesh_ttl seconds TTL)
      GET /api/box_live     - Per-box live device list from box_snapshot table
      GET /api/box_toggle   - In-memory display-visibility flag per box
                              (display-only, no daemon poll interaction)
    """

    db_path = None   # set by DebugServer before spawning threads

    def log_message(self, fmt, *args):
        # Suppress default access log to keep stdout clean.
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/debug.html"):
            self._serve_static("debug.html")
        elif path == "/api/live":
            self._api_live()
        elif path == "/api/history":
            self._api_history(qs)
        elif path == "/api/boxes":
            self._api_boxes()
        elif path == "/api/mesh":
            self._api_mesh()
        elif path == "/api/box_live":
            self._api_box_live()
        elif path == "/api/box_toggle":
            self._api_box_toggle(qs)
        elif path.startswith("/"):
            # Try static file from web/
            fname = path.lstrip("/")
            self._serve_static(fname)
        else:
            self._send_text("Not found", 404)

    def _serve_static(self, fname):
        # Decode percent-encoded sequences (e.g. %2e%2e -> ..) before the
        # path-traversal check so that encoded dot-dot segments cannot bypass
        # the realpath comparison.
        fname = unquote(fname)
        requested = os.path.normpath(os.path.join(_WEB_DIR, fname))
        web_root = os.path.realpath(_WEB_DIR)
        full = os.path.realpath(requested)
        # Block any path that escapes the web/ directory.
        if full != web_root and not full.startswith(web_root + os.sep):
            self._send_text("403 Forbidden", 403)
            return
        if not os.path.isfile(full):
            self._send_text(
                f"404 - File not found: web/{fname}\n"
                "Place your debug.html in the web/ subdirectory next to fritztrack4u.py.",
                404,
            )
            return
        ext = os.path.splitext(fname)[1].lower()
        ctype = _CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_live(self):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT mac, name, ts, floor, room, score, away, vector_json "
                "FROM live_snapshot ORDER BY name"
            ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            self._send_json({"error": str(exc)}, 500)
            return

        persons = []
        latest_ts = None
        for row in rows:
            try:
                vec = json.loads(row["vector_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                vec = {}
            ts = row["ts"] or ""
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
            persons.append({
                "name":  row["name"],
                "floor": row["floor"],
                "room":  row["room"],
                "score": row["score"],
                "away":  bool(row["away"]),
                "ts":    ts,
                "vector": vec,
            })

        self._send_json({
            "updated": latest_ts or "",
            "persons": persons,
        })

    def _api_history(self, qs):
        name = (qs.get("name") or [None])[0]
        try:
            limit = min(int((qs.get("limit") or ["200"])[0]), 1000)
        except (ValueError, IndexError):
            limit = 200

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            if name:
                rows = conn.execute(
                    "SELECT ts, floor, room, away FROM verlauf "
                    "WHERE name=? ORDER BY ts DESC LIMIT ?",
                    (name, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT ts, floor, room, away FROM verlauf "
                    "ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            self._send_json({"error": str(exc)}, 500)
            return

        entries = [
            {"ts": r["ts"], "floor": r["floor"],
             "room": r["room"], "away": bool(r["away"])}
            for r in rows
        ]
        self._send_json({"name": name, "entries": entries})

    def _api_boxes(self):
        cfg = self.server.config  # injected by DebugServer
        boxes_out = []
        floors_seen = {}
        persons = list(cfg.get("devices", {}).values())

        for b in cfg.get("boxes", []):
            boxes_out.append({
                "name":     b.get("name", ""),
                "floor":    b.get("floor", ""),
                "floor_nr": b.get("floor_nr", 1),
            })
            fl = b.get("floor", "")
            if fl not in floors_seen:
                floors_seen[fl] = b.get("floor_nr", 1)

        floors_out = sorted(
            [{"floor": f, "floor_nr": nr} for f, nr in floors_seen.items()],
            key=lambda x: x["floor_nr"],
        )
        self._send_json({
            "boxes":   boxes_out,
            "floors":  floors_out,
            "persons": persons,
        })

    def _api_mesh(self):
        """GET /api/mesh

        Returns cached mesh topology. The cache is refreshed by the daemon
        cycle every mesh_ttl seconds (default 30). This endpoint is read-only.

        Response schema:
            {
                "updated":   string | "",   // ISO timestamp of last successful fetch
                "available": boolean,       // false when no mesh data (repeater-only, error)
                "nodes": [
                    {
                        "name":      string,
                        "mac":       string | null,
                        "is_master": boolean,
                        "type":      "router" | "repeater" | "client" | "unknown",
                        "model":     string | null
                    }
                ],
                "links": [
                    {
                        "from_mac": string | null,
                        "to_mac":   string | null,
                        "rx_rate":  integer | null,  // kbit/s
                        "tx_rate":  integer | null,  // kbit/s
                        "rssi":     integer | null   // dBm, present only if reported
                    }
                ]
            }
        """
        cache = self.server.mesh_cache  # {"data": {...}, "updated": iso, "ts": float}
        data = cache.get("data") or {"nodes": [], "links": []}
        nodes = data.get("nodes") or []
        links = data.get("links") or []
        self._send_json({
            "updated":   cache.get("updated") or "",
            "available": bool(nodes or links),
            "nodes":     nodes,
            "links":     links,
        })

    def _api_box_live(self):
        """GET /api/box_live

        Returns the latest per-box device list from the box_snapshot table,
        ordered by floor_nr then box name. Includes all boxes from config even
        if not yet polled (device_count=0, reachable=null until first write).

        Response schema:
            {
                "updated": string | "",   // ISO timestamp of the most recent snapshot
                "boxes": [
                    {
                        "name":         string,
                        "floor":        string,
                        "floor_nr":     integer,
                        "reachable":    boolean | null,   // null = not yet polled
                        "device_count": integer,
                        "ts":           string | null,
                        "devices": [
                            {
                                "mac":    string,
                                "signal": integer,         // percent 0-100
                                "active": boolean,
                                "name":   string | null    // friendly name if tracked
                            }
                        ]
                    }
                ]
            }
        """
        cfg = self.server.config

        # Build a name->config map for all configured boxes.
        box_cfg = {b["name"]: b for b in cfg.get("boxes", [])}

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT box_name, ts, reachable, device_count, devices_json "
                "FROM box_snapshot"
            ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            self._send_json({"error": str(exc)}, 500)
            return

        # Index DB rows by box_name.
        db_by_name = {}
        for row in rows:
            db_by_name[row["box_name"]] = row

        latest_ts = None
        boxes_out = []

        # Iterate in config order; sort by floor_nr then name for stable output.
        sorted_boxes = sorted(
            cfg.get("boxes", []),
            key=lambda b: (b.get("floor_nr", 1), b.get("name", "")),
        )

        for b in sorted_boxes:
            bname = b["name"]
            row = db_by_name.get(bname)

            if row is None:
                # Box exists in config but has not been polled yet.
                boxes_out.append({
                    "name":         bname,
                    "floor":        b.get("floor", ""),
                    "floor_nr":     b.get("floor_nr", 1),
                    "reachable":    None,
                    "device_count": 0,
                    "ts":           None,
                    "devices":      [],
                })
                continue

            ts = row["ts"]
            if latest_ts is None or (ts and ts > latest_ts):
                latest_ts = ts

            try:
                devices = json.loads(row["devices_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                devices = []

            boxes_out.append({
                "name":         bname,
                "floor":        b.get("floor", ""),
                "floor_nr":     b.get("floor_nr", 1),
                "reachable":    bool(row["reachable"]),
                "device_count": row["device_count"],
                "ts":           ts,
                "devices":      devices,
            })

        self._send_json({
            "updated": latest_ts or "",
            "boxes":   boxes_out,
        })

    def _api_box_toggle(self, qs):
        """GET /api/box_toggle?box=<name>&show=true|false

        Sets an in-memory display-visibility flag for a box. This flag is
        display-only: the daemon continues polling all boxes regardless. It
        serves as a shared state store when multiple browser tabs are open.

        The flag is NOT persisted to DB and NOT reflected in /api/box_live or
        /api/mesh data (those always return full data). Filtering by visibility
        is a frontend responsibility.

        Response schema:
            {
                "box":     string,
                "visible": boolean
            }

        Error responses:
            400 {"error": "..."} - missing/unknown box or invalid show value
        """
        cfg = self.server.config
        known_boxes = {b["name"] for b in cfg.get("boxes", [])}

        box_name = (qs.get("box") or [None])[0]
        show_raw = (qs.get("show") or [None])[0]

        if not box_name:
            self._send_json({"error": "Missing required parameter: box"}, 400)
            return
        if box_name not in known_boxes:
            self._send_json(
                {"error": f"Unknown box '{box_name}'. "
                           f"Known: {sorted(known_boxes)}"},
                400,
            )
            return
        if show_raw is None:
            self._send_json({"error": "Missing required parameter: show (true|false)"}, 400)
            return
        if show_raw.lower() not in ("true", "false"):
            self._send_json({"error": "Parameter 'show' must be 'true' or 'false'"}, 400)
            return

        visible = show_raw.lower() == "true"
        # box_visibility is an in-memory dict on the DebugServer instance.
        self.server.box_visibility[box_name] = visible
        self._send_json({"box": box_name, "visible": visible})


class DebugServer:
    """Wraps ThreadingHTTPServer in a daemon thread. Localhost-only."""

    def __init__(self, config):
        self.config = config
        self.port = int(config["debug"].get("port", 8099))
        self.db_path = config["db_path"]
        self._server = None
        self._thread = None
        # In-memory mesh cache. Updated by FritzTrack4U.cycle() via update_mesh_cache().
        # Structure: {"data": {nodes,links}, "updated": iso_str, "ts": float}
        self.mesh_cache = {"data": None, "updated": "", "ts": 0.0}
        # In-memory display-visibility flags per box name.
        # True = show, False = hide. Default: all visible.
        # Purpose: display-only state shared across browser tabs. The daemon
        # poll loop is NOT affected by this flag - read-only principle.
        self.box_visibility = {}

    def update_mesh_cache(self, data):
        """Called by the daemon cycle thread to refresh mesh data."""
        self.mesh_cache = {
            "data":    data,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "ts":      time.time(),
        }

    def start(self):
        # Inject db_path + config into the handler class (thread-safe: set
        # before the server thread starts, never mutated afterwards).
        _DebugHandler.db_path = self.db_path

        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self.port), _DebugHandler
        )
        self._server.config = self.config     # accessible via self.server in handler
        self._server.mesh_cache = self.mesh_cache
        self._server.box_visibility = self.box_visibility

        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="debug-http"
        )
        self._thread.start()
        print(f"[debug] HTTP server on http://127.0.0.1:{self.port}/")

    def stop(self):
        if self._server:
            self._server.shutdown()


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
        self.smoothing_window = max(1, int(config.get("smoothing_window", 3)))
        self.missing_penalty = float(config.get("fingerprint_missing_penalty", 5))

        # Rolling signal history: mac -> deque of raw vectors (dict box->%)
        self._smooth_history = {}

        # Polling method: "edit_device" (real dBm) or "homenet" (legacy proxy).
        polling_cfg = config.get("polling") or {}
        self.polling_method = polling_cfg.get("method", "edit_device")
        self.box_timeout = float(polling_cfg.get("box_timeout", 15))

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

    def _smooth(self, mac, raw_vector):
        """Return a smoothed vector (per-box mean over the last K observations).

        The history deque holds at most smoothing_window raw vectors. When K=1
        smoothing is effectively disabled (returns the raw vector unchanged).
        """
        if mac not in self._smooth_history:
            self._smooth_history[mac] = collections.deque(
                maxlen=self.smoothing_window
            )
        self._smooth_history[mac].append(raw_vector)

        history = self._smooth_history[mac]
        if not history:
            return raw_vector

        # Collect all box names seen across the window.
        all_boxes = set()
        for v in history:
            all_boxes.update(v.keys())

        smoothed = {}
        for box in all_boxes:
            values = [v[box] for v in history if box in v]
            if values:
                smoothed[box] = sum(values) / len(values)
        return smoothed

    def _poll_box_homenet(self, box):
        """Poll one box via legacy homeNet path. Returns (box, devices_list|None)."""
        try:
            devices = box.box_geraete()
            return (box, devices)
        except (URLError, HTTPError, OSError) as exc:
            print(f"[warn] box '{box.name}' ({box.ip}) unreachable: {exc}")
            return (box, None)

    def _poll_box_edit_device(self, box, tracked_macs):
        """Poll one box via edit_device to get real RSSI dBm for all tracked MACs.

        Also falls back to homeNet for guest detection (non-tracked devices).
        Returns:
            (box, homenet_devices_list|None, detail_results_dict)
            homenet_devices_list: [{mac, signal, active}] for box_snapshot + guests
            detail_results_dict:  {mac: read_device result} for tracked MACs
        """
        detail = {}

        # homeNet first (for box_snapshot / guest detection) — seriell innerhalb
        # des bereits parallelisierten Box-Calls, also kein zusätzlicher Overhead.
        homenet_devices = None
        try:
            homenet_devices = box.box_geraete()
        except (URLError, HTTPError, OSError) as exc:
            print(f"[warn] box '{box.name}' ({box.ip}) unreachable (homeNet): {exc}")
            # Box unreachable — skip edit_device too.
            return (box, None, detail)

        # edit_device per tracked MAC.
        for mac in tracked_macs:
            try:
                detail[mac] = box.read_device(mac)
            except (URLError, HTTPError, OSError) as exc:
                print(
                    f"[warn] box '{box.name}' read_device({mac}) failed: {exc}"
                )
                detail[mac] = {
                    "mac": mac, "connected": False, "rssi": None,
                    "band": "", "phy_rate_tx": None, "phy_rate_rx": None,
                    "quality": None, "state": "error",
                }
            except Exception as exc:
                # Unexpected error (JSON parse, etc.) — log and continue.
                print(
                    f"[warn] box '{box.name}' read_device({mac}) unexpected: {exc}"
                )
                detail[mac] = {
                    "mac": mac, "connected": False, "rssi": None,
                    "band": "", "phy_rate_tx": None, "phy_rate_rx": None,
                    "quality": None, "state": "error",
                }

        return (box, homenet_devices, detail)

    def sammle_alle(self):
        """Collect signal vectors for ALL seen MACs across all boxes — PARALLEL.

        Polling method is controlled by config["polling"]["method"]:
          "edit_device"  — all boxes queried in parallel via ThreadPoolExecutor;
                           real RSSI dBm captured per tracked MAC per box.
          "homenet"      — legacy serial path (RX-rate proxy, no real dBm).

        Returns a tuple:
            (
                vectors:    { mac: { box_name: signal_percent } },
                box_raw:    { box_name: [{"mac","signal","active"}, ...] | None },
                detail_map: { mac: { "rssi_dbm": int|None, "band": str,
                                     "phy_rate_tx": int|None,
                                     "phy_rate_rx": int|None,
                                     "quality": int|None,
                                     "connected_ap": str|None } }
            )

        Callers that only care about the legacy (vectors, box_raw) pair can
        ignore the third element — existing code is unaffected.

        box_raw[box.name] is None when the box was unreachable this cycle.
        """
        vectors = {}
        box_raw = {}
        # detail_map: per-tracked-MAC aggregated best result across all boxes.
        detail_map = {mac: {
            "rssi_dbm": None, "band": "", "phy_rate_tx": None,
            "phy_rate_rx": None, "quality": None, "connected_ap": None,
        } for mac in self.tracked}

        n_workers = max(1, len(self.boxes))

        if self.polling_method == "homenet":
            # --- Legacy serial path (unchanged behaviour) ---
            for box in self.boxes:
                _, devices = self._poll_box_homenet(box)
                box_raw[box.name] = devices
                if devices is None:
                    continue
                for dev in devices:
                    mac = dev["mac"]
                    vectors.setdefault(mac, {})[box.name] = dev["signal"]
            return vectors, box_raw, detail_map

        # --- Parallel edit_device path ---
        tracked_macs = list(self.tracked.keys())  # upper-case MACs

        def _worker(box):
            return self._poll_box_edit_device(box, tracked_macs)

        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(_worker, box): box for box in self.boxes}
            # Collect results as they complete. Each future.result() gets its
            # own per-box timeout so one hanging box does not stall the others.
            # as_completed() without a global timeout iterates until all futures
            # are done (or cancelled by the context manager exit).
            for future in concurrent.futures.as_completed(futures):
                box = futures[future]
                try:
                    _box, homenet_devs, detail = future.result(
                        timeout=self.box_timeout
                    )
                except concurrent.futures.TimeoutError:
                    print(
                        f"[warn] box '{box.name}' ({box.ip}) timed out "
                        f"after {self.box_timeout:.0f}s — skipped this cycle"
                    )
                    box_raw[box.name] = None
                    continue
                except Exception as exc:
                    print(
                        f"[warn] box '{box.name}' ({box.ip}) worker error: {exc}"
                    )
                    box_raw[box.name] = None
                    continue

                box_raw[_box.name] = homenet_devs

                if homenet_devs is None:
                    continue

                # Build vectors from homeNet data (signal proxy for all MACs,
                # tracked + guests — fingerprinting / position() uses this).
                for dev in homenet_devs:
                    mac = dev["mac"]
                    vectors.setdefault(mac, {})[_box.name] = dev["signal"]

                # Aggregate edit_device detail: prefer the box where connected=True
                # AND rssi is strongest (most negative = weakest, closest to 0 =
                # strongest). In AVM Mesh-Roaming multiple APs report connected=True
                # simultaneously; last-writer-wins was non-deterministic. Fix: only
                # accept the new AP's values when its rssi beats the current winner.
                for mac, dev_result in detail.items():
                    dm = detail_map[mac]
                    if dev_result.get("connected"):
                        new_rssi = dev_result["rssi"]
                        # Take this box only if no winner yet, or signal is stronger.
                        if dm.get("rssi_dbm") is None or new_rssi > dm["rssi_dbm"]:
                            dm["rssi_dbm"]    = new_rssi
                            dm["band"]        = dev_result["band"]
                            dm["phy_rate_tx"] = dev_result["phy_rate_tx"]
                            dm["phy_rate_rx"] = dev_result["phy_rate_rx"]
                            dm["quality"]     = dev_result["quality"]
                            dm["connected_ap"] = _box.name

        elapsed = time.time() - t0
        print(
            f"[poll] {len(self.boxes)} box(es) polled in parallel — "
            f"{elapsed:.2f}s"
        )

        return vectors, box_raw, detail_map

    def position(self, vector):
        """Determine (floor, room, away, score) for one signal vector.

        Returns a 4-tuple:
            (floor: str|None, room: str|None, away: bool, score: float|None)

        - away  : no box sees the phone.
        - floor : floor of the strongest box.
        - room  : best fingerprint match for that floor within tolerance,
                  else None (floor-only resolution).
        - score : raw mean deviation of the best match (None when no fingerprints).
        """
        if not vector:
            return (None, None, True, None)

        # Strongest box wins the floor.
        strongest_box = max(vector, key=vector.get)
        box_obj = self.box_by_name.get(strongest_box)
        floor = box_obj.floor if box_obj else None

        room, score = self._match_room(floor, vector)
        return (floor, room, False, score)

    def _match_room(self, floor, vector):
        """Fingerprint match within +/- tolerance on the floor's fingerprints.

        Returns (room: str|None, best_score: float|None).
        """
        floor_prints = self.fingerprints.get(floor)
        if not floor_prints:
            return (None, None)

        best_room = None
        best_score = None
        for room, expected in floor_prints.items():
            score = self._fingerprint_distance(expected, vector, self.missing_penalty)
            if score is None:
                continue
            if best_score is None or score < best_score:
                best_score = score
                best_room = room

        if best_room is None:
            return (None, None)

        # Tolerance gate: average per-box deviation must be within tolerance
        # of the signal scale (0..100 -> 15% == 15 points by default).
        if best_score <= self.tolerance * 100:
            return (best_room, best_score)
        return (None, best_score)

    @staticmethod
    def _fingerprint_distance(expected, observed, missing_penalty=5.0):
        """Mean absolute deviation + penalty for missing boxes.

        Shared boxes must be at least min(2, len(expected)) to avoid a
        single-box fingerprint matching unfairly against a complete one.
        The penalty adds `missing_penalty` points per box that is in the
        fingerprint but not observed, so a more complete match wins at
        equal mean deviation.
        """
        shared = [b for b in expected if b in observed]
        min_overlap = min(2, len(expected))
        if len(shared) < min_overlap:
            return None
        mean_dev = sum(abs(expected[b] - observed[b]) for b in shared) / len(shared)
        missing = len(expected) - len(shared)
        return mean_dev + missing_penalty * missing

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
    live     : forced fast burst (set via --live flag or debug.live_mode).
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
        self._debug_server = None
        # Mesh cache: timestamp of last successful fetch.
        self._mesh_last_fetched = 0.0
        self._mesh_ttl = float(config.get("debug", {}).get("mesh_ttl", 30))

    def _changed_since_last(self, current):
        """Did any tracked phone change floor/room/presence?"""
        if current != self._last_state:
            return True
        return False

    def _refresh_mesh_if_due(self):
        """Fetch mesh topology if the cache TTL has expired.

        Only queries the first box that is a potential mesh master (box with
        floor_nr==1 or the first box in config). Repeaters return empty data
        which is logged as a warning by TR064Box.get_mesh() - never crashes.
        """
        now = time.time()
        if now - self._mesh_last_fetched < self._mesh_ttl:
            return  # cache still valid

        # Try boxes in order; use the first non-empty result.
        mesh_data = {"nodes": [], "links": []}
        for box in self.engine.boxes:
            try:
                result = box.get_mesh()
            except Exception as exc:
                print(f"[warn] mesh: unexpected error from box '{box.name}': {exc}")
                result = {"nodes": [], "links": []}
            if result.get("nodes") or result.get("links"):
                mesh_data = result
                break  # got data from the master box; no need to query others

        self._mesh_last_fetched = now

        if self._debug_server:
            self._debug_server.update_mesh_cache(mesh_data)

    def cycle(self):
        """One full poll cycle. Returns True if anything changed."""
        # Single poll per box - sammle_alle returns vectors, raw per-box data,
        # and (new) real-dBm detail_map per tracked MAC.
        raw_vectors, box_raw, detail_map = self.engine.sammle_alle()

        # --- Tracked phones: smooth vector -> position -> snapshot ---
        current_state = {}
        ts_now = datetime.now().isoformat(timespec="seconds")

        for mac, name in self.tracked.items():
            raw_vec = raw_vectors.get(mac, {})
            smooth_vec = self.engine._smooth(mac, raw_vec)

            floor, room, away, score = self.engine.position(smooth_vec)
            current_state[name] = (floor, room, away)

            self.store.log_position(name, floor, room, away)
            self.store.upsert_snapshot(
                mac, name, ts_now, floor, room, score, away, smooth_vec
            )
            self.mqtt.publish_position(name, floor, room, away, smooth_vec)

            where = "AWAY" if away else f"{floor or '?'} / {room or 'floor-only'}"
            dm = detail_map.get(mac) or {}
            dbm_str = ""
            if dm.get("rssi_dbm") is not None:
                dbm_str = (
                    f"  {dm['rssi_dbm']} dBm"
                    f"  {dm.get('band','')}"
                    f"  Q:{dm.get('quality','?')}%"
                    f"  AP:{dm.get('connected_ap','?')}"
                )
            print(f"[{datetime.now():%H:%M:%S}] {name:<12} -> {where}{dbm_str}")

        # --- Guests: unknown, non-excluded MACs ---
        for mac, vec in self.engine.sammle_fremde(raw_vectors).items():
            if not self.engine.ist_gast(mac):
                continue
            if not vec:
                continue
            strongest_box = max(vec, key=vec.get)
            self.store.log_guest(mac, strongest_box, vec[strongest_box])
            print(f"[{datetime.now():%H:%M:%S}] GUEST {mac} near {strongest_box} "
                  f"({vec[strongest_box]}%)")

        # --- Box snapshots: write one row per box using already-fetched data ---
        # Reuse the per-box device lists from sammle_alle (no second poll).
        # Enrich each device entry with a friendly name if it is a tracked MAC.
        for box in self.engine.boxes:
            raw_devs = box_raw.get(box.name)
            reachable = raw_devs is not None

            if reachable:
                enriched = []
                for dev in raw_devs:
                    friendly = self.tracked.get(dev["mac"])
                    enriched.append({
                        "mac":    dev["mac"],
                        "signal": dev["signal"],
                        "active": dev["active"],
                        "name":   friendly,        # None for untracked devices
                    })
                self.store.upsert_box_snapshot(box.name, ts_now, True, enriched)
            else:
                # Box was unreachable this cycle. Store reachable=0 + empty
                # device list so the API can surface the outage explicitly.
                self.store.upsert_box_snapshot(box.name, ts_now, False, [])

        # --- Mesh cache refresh (TTL-gated, not every cycle) ---
        self._refresh_mesh_if_due()

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

        # Start HTTP debug server if enabled.
        if self.cfg["debug"].get("http_enabled"):
            self._debug_server = DebugServer(self.cfg)
            self._debug_server.start()

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
        if self._debug_server:
            self._debug_server.stop()
        self.mqtt.disconnect()
        self.store.close()
        print("[stop] FritzTrack4U shut down cleanly.", flush=True)


# ===========================================================================
# CLI calibration helpers
# ===========================================================================

def _valid_floors(config):
    """Return sorted list of unique floor labels from box definitions."""
    seen = []
    for b in config.get("boxes", []):
        fl = b.get("floor", "")
        if fl and fl not in seen:
            seen.append(fl)
    return seen


def _collect_samples(engine, floor_boxes, n_samples, duration_sec):
    """Collect n_samples signal readings from floor_boxes over duration_sec.

    floor_boxes : list of box names that belong to the target floor.
    Returns list of dicts {box_name: signal%}.
    Prints live feedback to stdout per sample.
    """
    interval = duration_sec / max(n_samples, 1)
    samples = []
    for i in range(n_samples):
        raw, _box_raw, _detail = engine.sammle_alle()
        sample = {}
        for mac_vec in raw.values():
            for box_name, pct in mac_vec.items():
                if box_name in floor_boxes:
                    # Keep max signal seen across all MACs for this box.
                    if box_name not in sample or pct > sample[box_name]:
                        sample[box_name] = pct

        # Also include boxes that were unreachable (0).
        for b in floor_boxes:
            sample.setdefault(b, 0)

        samples.append(sample)
        parts = " | ".join(f"{b} {sample[b]}%" for b in sorted(floor_boxes))
        print(f"  [{i+1}/{n_samples}] {parts}")
        if i < n_samples - 1:
            time.sleep(interval)
    return samples


def _median_of_samples(samples, floor_boxes):
    """Per-box median across all samples. Returns {box_name: median_int}."""
    result = {}
    for box in floor_boxes:
        vals = sorted(s.get(box, 0) for s in samples)
        mid = len(vals) // 2
        if len(vals) % 2 == 0 and len(vals) > 0:
            result[box] = int((vals[mid - 1] + vals[mid]) / 2)
        elif vals:
            result[box] = vals[mid]
        else:
            result[box] = 0
    return result


def calibrate_room(config_path, config, floor, room, n_samples, duration_sec):
    """Measure and save a single room fingerprint."""
    valid = _valid_floors(config)
    if floor not in valid:
        print(f"[error] Floor '{floor}' not found in config.")
        print(f"        Valid floors: {', '.join(valid) or '(none)'}")
        sys.exit(1)

    floor_boxes = [b["name"] for b in config["boxes"] if b.get("floor") == floor]
    if not floor_boxes:
        print(f"[error] No boxes found for floor '{floor}'.")
        sys.exit(1)

    engine = PositionEngine(config)
    print(f"Calibrating  floor='{floor}'  room='{room}'")
    print(f"Boxes on this floor: {', '.join(floor_boxes)}")
    print(f"Taking {n_samples} samples over {duration_sec}s ...")
    print()

    samples = _collect_samples(engine, floor_boxes, n_samples, duration_sec)
    fingerprint = _median_of_samples(samples, floor_boxes)

    print()
    print(f"Fingerprint: {json.dumps(fingerprint, ensure_ascii=False)}")
    save_config(config_path, floor, room, fingerprint)
    print(f"[ok] Saved to config: fingerprints['{floor}']['{room}']")


def calibrate_floor(config_path, config, floor):
    """Guided loop: measure every room on one floor until empty input."""
    valid = _valid_floors(config)
    if floor not in valid:
        print(f"[error] Floor '{floor}' not found in config.")
        print(f"        Valid floors: {', '.join(valid) or '(none)'}")
        sys.exit(1)

    floor_boxes = [b["name"] for b in config["boxes"] if b.get("floor") == floor]
    if not floor_boxes:
        print(f"[error] No boxes found for floor '{floor}'.")
        sys.exit(1)

    engine = PositionEngine(config)
    print(f"Floor calibration for '{floor}'")
    print(f"Boxes: {', '.join(floor_boxes)}")
    print("Enter room name and press ENTER to start measuring. Empty name = done.")
    print()

    while True:
        try:
            room = input("Room name (empty = stop): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[abort] Calibration stopped.")
            break
        if not room:
            print("[done] Floor calibration finished.")
            break

        try:
            input(f"  Go to '{room}' and press ENTER to start measuring...")
        except (EOFError, KeyboardInterrupt):
            print("\n[abort] Calibration stopped.")
            break

        samples = _collect_samples(engine, floor_boxes, n_samples=10,
                                   duration_sec=20)
        fingerprint = _median_of_samples(samples, floor_boxes)
        print(f"  Fingerprint: {json.dumps(fingerprint, ensure_ascii=False)}")
        save_config(config_path, floor, room, fingerprint)
        print(f"  [ok] Saved fingerprints['{floor}']['{room}']")
        print()


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
    parser.add_argument(
        "--live", action="store_true",
        help="Force live cadence (3s) and enable the HTTP debug server.",
    )
    # --- Calibration arguments ---
    parser.add_argument(
        "--calibrate", action="store_true",
        help="Calibrate a single room fingerprint (requires --floor and --room).",
    )
    parser.add_argument(
        "--calibrate-floor", metavar="FLOOR",
        help="Guided floor-wide calibration loop for the given floor label.",
    )
    parser.add_argument(
        "--floor", metavar="FLOOR",
        help="Floor label for --calibrate.",
    )
    parser.add_argument(
        "--room", metavar="ROOM",
        help="Room name for --calibrate.",
    )
    parser.add_argument(
        "--samples", type=int, default=10,
        help="Number of samples for --calibrate (default: 10).",
    )
    parser.add_argument(
        "--duration", type=int, default=20,
        help="Total measurement duration in seconds for --calibrate (default: 20).",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    # --- Calibration modes (exit after completion) ---
    if args.calibrate:
        if not args.floor or not args.room:
            print("[error] --calibrate requires --floor and --room.")
            sys.exit(1)
        calibrate_room(
            args.config, config,
            args.floor, args.room, args.samples, args.duration,
        )
        return

    if args.calibrate_floor:
        calibrate_floor(args.config, config, args.calibrate_floor)
        return

    # --- Normal daemon / once mode ---
    daemon = FritzTrack4U(config)

    # --live: force live cadence + enable HTTP debug server.
    if args.live:
        daemon.clock.live = True
        daemon.cfg["debug"]["http_enabled"] = True

    # debug.live_mode in config also activates live cadence.
    if config["debug"].get("live_mode"):
        daemon.clock.live = True

    # Graceful shutdown on SIGINT/SIGTERM.
    def _handle_signal(signum, _frame):
        print(f"\n[signal] received {signum}, shutting down...")
        daemon._running = False

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    if args.once:
        daemon.mqtt.connect()
        if daemon.cfg["debug"].get("http_enabled"):
            daemon._debug_server = DebugServer(daemon.cfg)
            daemon._debug_server.start()
        try:
            daemon.cycle()
            daemon.maybe_cleanup()
        finally:
            daemon.shutdown()
    else:
        daemon.run()


if __name__ == "__main__":
    main()
