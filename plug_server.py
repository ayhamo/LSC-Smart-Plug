"""
Local controller for LSC Smart Connect plugs with power meter (Tuya, protocol 3.5).
One instance controls and monitors one or more plugs.

Runs entirely on your LAN: no Tuya cloud, no Home Assistant.

On-plug features (keep working when this program is off):
    countdown, inching (auto-off), cycle timer, random timer,
    power-on behaviour, LED mode, child lock, overcurrent protection
Program features (need this program running):
    energy log + cost, weekly on/off schedules, alerts, auto-rediscovery

Usage:
    python plug_server.py            # real plug(s), reads config.json
    python plug_server.py --demo     # two simulated plugs
"""

import base64
import copy
import csv
import io
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from calendar import monthrange
from datetime import datetime, timedelta

from flask import Flask, Response, jsonify, request, stream_with_context

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", BASE)      # config/state/db live here (a volume in Docker)
CONFIG_PATH = os.path.join(DATA, "config.json")
STATE_PATH = os.path.join(DATA, "state.json")
DB_PATH = os.path.join(DATA, "energy.db")
DEMO = "--demo" in sys.argv

# Silence the per-request access log ("GET /api/status 200"); keep warnings/errors
logging.getLogger("werkzeug").setLevel(logging.WARNING)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# Data points of "LSC Power Plug EU incl. Power meter" (verified on the device).
# A plug in config.json can override any of them with "dps": {"power": 19, ...}.
DEFAULT_DPS = {"switch": 1, "countdown": 9, "current": 18, "power": 19, "voltage": 20,
               "fault": 26, "power_on": 38, "overcurrent": 39, "led": 40,
               "child_lock": 41, "cycle": 42, "random": 43, "inching": 44}

POWER_ON_VALUES = ["off", "on", "memory"]
LED_VALUES = ["relay", "pos", "none", "on"]   # verified on the device
FAULT_BITS = ["overcurrent", "overvoltage", "overpower",
              "low current", "low voltage", "low power"]
DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]   # bit 0 .. bit 6

DEFAULT_POLL = 5          # seconds between readings
GAP_POLLS = 3             # a gap longer than this many polls counts as "not measured"
ALERT_COOLDOWN_S = 600    # the same alert is sent at most once per 10 minutes
POWER_CLEAR = 0.95        # power alert clears below 95 % of the limit (no flapping)
SCHED_CATCHUP_MIN = 15    # schedules missed by up to 15 min (slow pass, DST) still run
SCHED_TRIES = 3           # a failed scheduled switch is retried this many times
SUMMARY_CACHE_S = 30      # page totals are recomputed at most every 30 s
URL_MASK = "•••"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
HM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ApiError(Exception):
    """Bad request data. Turned into {"ok": false, "error": msg} with this HTTP code."""
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def write_json(path, obj):
    """Write a JSON file so a crash mid-write never leaves it half written."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp, path)
    except OSError:
        # The file itself is a bind mount (can't be replaced): write it in place
        os.remove(tmp)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Input checks (shared by the API and by state.json loading)
# --------------------------------------------------------------------------

def check_hm(v, what="time"):
    if not isinstance(v, str) or not HM_RE.match(v):
        raise ApiError(f"{what} must be HH:MM (00:00–23:59)")
    return v


def check_days(v):
    if not isinstance(v, list) or not v or any(d not in DAYS for d in v):
        raise ApiError(f"days must be a non-empty list of {', '.join(DAYS)}")
    return [d for d in DAYS if d in v]


def check_int(v, lo, hi, what):
    if isinstance(v, bool):
        raise ApiError(f"{what} must be a whole number")
    try:
        n = int(v)
        if n != float(v):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ApiError(f"{what} must be a whole number") from None
    if not lo <= n <= hi:
        raise ApiError(f"{what} must be between {lo} and {hi}")
    return n


def check_num(v, what, lo=0.0):
    if isinstance(v, bool):
        raise ApiError(f"{what} must be a number")
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ApiError(f"{what} must be a number") from None
    if not math.isfinite(n) or n < lo:
        raise ApiError(f"{what} must be a number ≥ {lo:g}")
    return n


def check_name(v, what="name", maxlen=40):
    name = " ".join(str(v if v is not None else "").split())[:maxlen]
    if not name:
        raise ApiError(f"{what} is empty")
    return name


def clean_schedule(s):
    if not isinstance(s, dict):
        raise ApiError("schedule must be an object")
    if s.get("action") not in ("on", "off"):
        raise ApiError("action must be on or off")
    sid = s.get("id")
    return {"id": sid if isinstance(sid, str) and ID_RE.match(sid) else uuid.uuid4().hex[:8],
            "time": check_hm(s.get("time")), "action": s["action"],
            "days": check_days(s.get("days")), "enabled": bool(s.get("enabled", True))}


def clean_cycle(tasks):
    if not isinstance(tasks, list) or len(tasks) > 10:
        raise ApiError("cycle timers: a list of at most 10")
    out = []
    for t in tasks:
        if not isinstance(t, dict):
            raise ApiError("cycle timer must be an object")
        out.append({"enabled": bool(t.get("enabled", True)), "days": check_days(t.get("days")),
                    "start": check_hm(t.get("start"), "start"), "end": check_hm(t.get("end"), "end"),
                    "on_min": check_int(t.get("on_min"), 1, 1439, "on minutes"),
                    "off_min": check_int(t.get("off_min"), 1, 1439, "off minutes")})
    return out


def clean_random(tasks):
    if not isinstance(tasks, list) or len(tasks) > 16:
        raise ApiError("random timers: a list of at most 16")
    out = []
    for t in tasks:
        if not isinstance(t, dict):
            raise ApiError("random timer must be an object")
        out.append({"enabled": bool(t.get("enabled", True)), "days": check_days(t.get("days")),
                    "start": check_hm(t.get("start"), "start"), "end": check_hm(t.get("end"), "end")})
    return out


# --------------------------------------------------------------------------
# Binary formats of the on-plug timers (base64 strings)
# --------------------------------------------------------------------------

def _days_to_mask(days):
    return sum(1 << DAYS.index(d) for d in days)


def _mask_to_days(mask):
    return [d for i, d in enumerate(DAYS) if mask & (1 << i)]


def _hm_to_min(hm):
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def _min_to_hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def _b64(raw):
    return base64.b64encode(raw).decode() if raw else ""


def _unb64(s):
    try:
        return base64.b64decode(s) if isinstance(s, str) and s else b""
    except Exception:
        return b""


def encode_cycle(tasks):
    """10 bytes/task: enable, weekdays, start, end, on-min, off-min (big endian)."""
    raw = b""
    for t in tasks[:10]:
        raw += bytes([1 if t.get("enabled", True) else 0, _days_to_mask(t["days"])])
        raw += _hm_to_min(t["start"]).to_bytes(2, "big")
        raw += _hm_to_min(t["end"]).to_bytes(2, "big")
        raw += int(t["on_min"]).to_bytes(2, "big")
        raw += int(t["off_min"]).to_bytes(2, "big")
    return _b64(raw)


def decode_cycle(s):
    raw, out = _unb64(s), []
    for i in range(0, len(raw) - 9, 10):
        b = raw[i:i + 10]
        out.append({"enabled": bool(b[0] & 1), "days": _mask_to_days(b[1]),
                    "start": _min_to_hm(int.from_bytes(b[2:4], "big")),
                    "end": _min_to_hm(int.from_bytes(b[4:6], "big")),
                    "on_min": int.from_bytes(b[6:8], "big"),
                    "off_min": int.from_bytes(b[8:10], "big")})
    return out


def encode_random(tasks):
    """6 bytes/task: enable, weekdays, start, end (big endian minutes)."""
    raw = b""
    for t in tasks[:16]:
        raw += bytes([1 if t.get("enabled", True) else 0, _days_to_mask(t["days"])])
        raw += _hm_to_min(t["start"]).to_bytes(2, "big")
        raw += _hm_to_min(t["end"]).to_bytes(2, "big")
    return _b64(raw)


def decode_random(s):
    raw, out = _unb64(s), []
    for i in range(0, len(raw) - 5, 6):
        b = raw[i:i + 6]
        out.append({"enabled": bool(b[0] & 1), "days": _mask_to_days(b[1]),
                    "start": _min_to_hm(int.from_bytes(b[2:4], "big")),
                    "end": _min_to_hm(int.from_bytes(b[4:6], "big"))})
    return out


def encode_inching(enabled, seconds):
    return _b64(bytes([1 if enabled else 0]) + int(seconds).to_bytes(2, "big"))


def decode_inching(s):
    raw = _unb64(s)
    if len(raw) < 3:
        return {"enabled": False, "seconds": 5}
    return {"enabled": bool(raw[0] & 1), "seconds": int.from_bytes(raw[1:3], "big")}


# --------------------------------------------------------------------------
# Parsing raw data points into something readable
# --------------------------------------------------------------------------

def parse(d, dp):
    """Raw data points -> readable status. Unexpected value types never raise."""
    def scaled(name, scale):
        v = d.get(dp[name])
        if v is None or isinstance(v, bool):
            return None
        try:
            return round(float(v) / scale, 3)
        except (TypeError, ValueError):
            return None

    def whole(name):
        try:
            return int(d.get(dp[name]) or 0)
        except (TypeError, ValueError):
            return 0

    def text(name):
        v = d.get(dp[name])
        return v if isinstance(v, str) else ""

    fault = whole("fault")
    return {
        "on": d.get(dp["switch"]) is True,
        "power_w": scaled("power", 10),
        "voltage_v": scaled("voltage", 10),
        "current_a": scaled("current", 1000),
        "countdown_s": whole("countdown"),
        "faults": [name for i, name in enumerate(FAULT_BITS) if fault & (1 << i)],
        "power_on": d.get(dp["power_on"]),
        "overcurrent": d.get(dp["overcurrent"]),
        "led": d.get(dp["led"]),
        "child_lock": d.get(dp["child_lock"]),
        "inching": decode_inching(text("inching")),
        "cycle": decode_cycle(text("cycle")),
        "random": decode_random(text("random")),
    }


# --------------------------------------------------------------------------
# Plug backends: get_dps() -> dict, set_dp(dp, value) -> dict of confirmed dps
# --------------------------------------------------------------------------

class DemoBackend:
    """Simulated plug that behaves like the real one, for trying the UI."""
    def __init__(self, dp, watts=1400.0):
        self.dp, self.watts = dp, float(watts)
        self.d = {dp["switch"]: False, dp["countdown"]: 0, dp["fault"]: 0,
                  dp["power_on"]: "memory", dp["overcurrent"]: False, dp["led"]: "relay",
                  dp["child_lock"]: False, dp["cycle"]: "", dp["random"]: "", dp["inching"]: ""}
        self.t0 = time.time()
        self.cd_end = None
        self.inch_end = None
        self.lock = threading.Lock()

    def _tick(self):
        now = time.time()
        sw = self.dp["switch"]
        if self.cd_end and now >= self.cd_end:
            self.d[sw] = not self.d[sw]
            self.cd_end = None
        if self.inch_end and now >= self.inch_end:
            self.d[sw] = False
            self.inch_end = None
        self.d[self.dp["countdown"]] = int(self.cd_end - now) if self.cd_end else 0

    def get_dps(self):
        with self.lock:
            self._tick()
            on = self.d[self.dp["switch"]]
            w = self.watts * (1 + 0.08 * math.sin((time.time() - self.t0) / 9)) if on else 0
            self.d[self.dp["power"]] = int(w * 10)
            self.d[self.dp["voltage"]] = 2318
            self.d[self.dp["current"]] = int(w / 231.8 * 1000)
            return dict(self.d)

    def set_dp(self, dp, value):
        with self.lock:
            self.d[dp] = value
            if dp == self.dp["countdown"]:
                self.cd_end = time.time() + int(value) if int(value) else None
            if dp == self.dp["switch"] and value:
                inch = decode_inching(self.d[self.dp["inching"]])
                if inch["enabled"]:
                    self.inch_end = time.time() + inch["seconds"]
            return {dp: value}


class TuyaBackend:
    def __init__(self, pcfg, dp, on_new_ip):
        self.cfg = pcfg
        self.dp = dp
        self.on_new_ip = on_new_ip      # called with the new IP after rediscovery
        self.energy_dps = [int(dp["current"]), int(dp["power"]), int(dp["voltage"])]
        self.lock = threading.Lock()
        self._connect()

    def _version(self):
        return float(self.cfg.get("version", 3.5))

    def _connect(self):
        import tinytuya
        self.dev = tinytuya.OutletDevice(
            dev_id=self.cfg["device_id"], address=self.cfg["ip"],
            local_key=self.cfg["local_key"], version=self._version())
        # A fresh short connection per request. A long-lived connection collected
        # unread replies from the plug, so readings fell further and further behind.
        self.dev.set_socketPersistent(False)
        self.dev.set_socketRetryLimit(2)
        self.dev.set_socketTimeout(5)

    def _sweep(self):
        """Find the plug without UDP broadcasts (these don't reach a Docker bridge):
        try every host in the plug's /24 on Tuya's TCP port 6668, then confirm
        it answers with our device ID and key."""
        import socket
        import tinytuya
        from concurrent.futures import ThreadPoolExecutor
        prefix = self.cfg["ip"].rsplit(".", 1)[0]

        def port_open(ip):
            try:
                with socket.create_connection((ip, 6668), timeout=0.4):
                    return ip
            except OSError:
                return None

        with ThreadPoolExecutor(64) as ex:
            candidates = [ip for ip in ex.map(port_open, (f"{prefix}.{i}" for i in range(1, 255))) if ip]
        for ip in candidates:
            d = tinytuya.OutletDevice(self.cfg["device_id"], ip, self.cfg["local_key"],
                                      version=self._version())
            d.set_socketTimeout(3)
            d.set_socketRetryLimit(1)
            r = d.status()
            d.close()
            if r and "dps" in r:
                return ip
        return None

    def rediscover(self):
        """Find the plug by device ID if its IP changed. Returns new IP or None."""
        import tinytuya
        ip = None
        try:
            found = tinytuya.find_device(dev_id=self.cfg["device_id"])
            ip = found.get("ip") if found else None
        except Exception:
            pass
        if not ip:
            ip = self._sweep()
        if ip and ip != self.cfg["ip"]:
            with self.lock:
                self.cfg["ip"] = ip
                try:
                    self.dev.close()
                except Exception:
                    pass
                self._connect()
            self.on_new_ip(ip)
            return ip
        return None

    def get_dps(self):
        with self.lock:
            try:
                # Ask for fresh power readings. This plug never replies to the
                # request itself, so don't wait for one (waiting cost 5 s).
                self.dev.updatedps(self.energy_dps, nowait=True)
            except Exception:
                pass
            raw = self.dev.status()
        if not raw or "Error" in raw or "dps" not in raw:
            raise RuntimeError((raw or {}).get("Error", "no response"))
        return raw["dps"]

    def set_dp(self, dp, value):
        with self.lock:
            r = self.dev.set_value(int(dp), value)
        if r and "Error" in r:
            raise RuntimeError(r["Error"])
        # the plug confirms the new value right away
        return r["dps"] if r and isinstance(r.get("dps"), dict) else {}


# --------------------------------------------------------------------------
# Plugs
# --------------------------------------------------------------------------

class Plug:
    def __init__(self, pcfg, demo=False):
        self.id = pcfg["id"]
        self.cfg = pcfg
        self.config_name = pcfg.get("name") or f"Plug {self.id}"
        dps = dict(DEFAULT_DPS)
        for k, v in (pcfg.get("dps") or {}).items():
            if k not in DEFAULT_DPS:
                log(f"config.json: plug {self.id}: unknown dps entry '{k}' ignored "
                    f"(known: {', '.join(DEFAULT_DPS)})")
                continue
            dps[k] = int(v)
        self.dp = {k: str(v) for k, v in dps.items()}
        self.last = {"dps": {}, "error": None, "ts": 0, "fail_count": 0}
        self.sched_last = None      # last minute the schedules were checked for
        self.sched_retry = []       # (schedule, minute, tries) of failed scheduled switches
        self.power_alert = False
        if demo:
            self.backend = DemoBackend(self.dp, pcfg.get("demo_w", 1400))
        else:
            self.backend = TuyaBackend(pcfg, self.dp, lambda ip: persist_ip(self.id, ip))

    @property
    def name(self):
        """The name set in the page wins over the one in config.json."""
        return state["plugs"].get(self.id, {}).get("name") or self.config_name

    def status(self):
        return parse(self.last["dps"], self.dp) if self.last["dps"] else None

    def read(self):
        self.last["dps"].update(self.backend.get_dps())
        return parse(self.last["dps"], self.dp)

    def set(self, name, value):
        confirmed = self.backend.set_dp(self.dp[name], value)
        self.last["dps"].update(confirmed or {})

    def switch(self, on, reason=""):
        """Switch the relay. Returns None on success, else the error text."""
        try:
            self.set("switch", bool(on))
            log(f"{self.name}: plug -> {'ON' if on else 'OFF'} {reason}".rstrip())
            return None
        except Exception as e:
            log(f"{self.name}: switch {'on' if on else 'off'} failed {reason}: {e}")
            return str(e) or e.__class__.__name__

    def refresh(self):
        try:
            self.read()
            self.last.update(error=None, ts=time.time())
        except Exception as e:
            self.last.update(error=str(e))

    def overview(self):
        st = self.status() or {}
        return {"id": self.id, "name": self.name, "on": st.get("on"), "power_w": st.get("power_w"),
                "error": self.last["error"], "updated": self.last["ts"]}


PLUGS = {}                 # id -> Plug, in config order
cfg = {"poll_seconds": DEFAULT_POLL, "port": 8080, "title": "Smart Plug"}
config_lock = threading.Lock()
PLUG_KEYS = ("id", "name", "device_id", "local_key", "ip", "version", "dps",
             "product", "mac", "model")
DEMO_CONFIG = {"poll_seconds": 2, "port": 8081, "plugs": [
    {"id": "1", "name": "Demo heater", "demo_w": 1400},
    {"id": "2", "name": "Demo fridge", "demo_w": 90}]}


def plug_configs(raw, demo=False):
    """The plugs in config.json. An old single-plug config (device_id/local_key/ip at
    the top level) becomes plug "1"."""
    if "plugs" in raw:
        entries = raw["plugs"]
    else:
        entries = [{k: raw[k] for k in PLUG_KEYS if k in raw}]
    if not isinstance(entries, list) or not entries:
        raise ValueError('config.json: "plugs" must be a non-empty list')
    out, seen = [], set()
    for i, p in enumerate(entries):
        if not isinstance(p, dict):
            raise ValueError(f"config.json: plug #{i + 1} must be an object")
        pid = str(p.get("id") or i + 1)
        if not ID_RE.match(pid):
            raise ValueError(f"config.json: plug id '{pid}' may only use letters, digits, - and _")
        if pid in seen:
            raise ValueError(f"config.json: plug id '{pid}' is used twice")
        if not demo:
            missing = [k for k in ("device_id", "local_key", "ip") if not p.get(k)]
            if missing:
                raise ValueError(f"config.json: plug {pid} is missing {', '.join(missing)}")
        seen.add(pid)
        out.append({**p, "id": pid})
    return out


def persist_ip(pid, ip):
    """Store a plug's new IP in config.json so the next start uses it."""
    if DEMO:
        return
    with config_lock:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            disk = json.load(f)
        if "plugs" in disk:
            for i, p in enumerate(disk["plugs"]):
                if str(p.get("id") or i + 1) == pid:
                    p["ip"] = ip
        else:
            disk["ip"] = ip
        write_json(CONFIG_PATH, disk)
    log(f"{PLUGS[pid].name}: found at new IP {ip}, config.json updated")


# --------------------------------------------------------------------------
# State (settings, schedules, alerts) in state.json
# --------------------------------------------------------------------------

DEFAULT_STATE = {
    "price_per_kwh": 0.3205,
    "alerts": {"notify_url": "", "budget_eur": 0, "monthly_report": False},
    "last_report": "",       # "YYYY-MM" of the month the last report was sent in
    "budget_alerted": "",    # "YYYY-MM" of the month the budget alert was sent in
    "plugs": {},             # id -> {"name", "device", "schedules", "power_above_w", "confirm_switch"}
}
state_lock = threading.RLock()
state = copy.deepcopy(DEFAULT_STATE)


def _num_or(v, default):
    try:
        n = float(v)
        return n if math.isfinite(n) and n >= 0 else default
    except (TypeError, ValueError):
        return default


def load_state(raw_cfg):
    """Read state.json. A file from the single-plug version (schedules, device and
    the power limit at the top level) is moved to the first plug."""
    state.clear()
    state.update(copy.deepcopy(DEFAULT_STATE))
    if "price_per_kwh" in raw_cfg:
        state["price_per_kwh"] = _num_or(raw_cfg["price_per_kwh"], state["price_per_kwh"])
    saved = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            saved = json.load(f)
    saved.pop("timer", None)      # old server-side timer, replaced by on-plug countdown
    first = next(iter(PLUGS))
    legacy = {k: saved.pop(k) for k in ("schedules", "device") if k in saved}
    alerts = saved.pop("alerts", {}) or {}
    if "power_above_w" in alerts:
        legacy["power_above_w"] = alerts.pop("power_above_w")
    plugs = saved.pop("plugs", {}) or {}
    if legacy and first not in plugs:
        plugs[first] = legacy
    state["alerts"].update({k: v for k, v in alerts.items() if k in DEFAULT_STATE["alerts"]})
    state["alerts"]["budget_eur"] = _num_or(state["alerts"].get("budget_eur"), 0)
    state["alerts"]["monthly_report"] = bool(state["alerts"].get("monthly_report"))
    state["alerts"]["notify_url"] = str(state["alerts"].get("notify_url") or "")
    for k in ("price_per_kwh", "last_report", "budget_alerted"):
        if k in saved:
            state[k] = saved[k]
    state["price_per_kwh"] = _num_or(state["price_per_kwh"], DEFAULT_STATE["price_per_kwh"])
    # Keep entries of plugs that are not in config.json right now (e.g. commented out)
    state["plugs"] = {pid: dict(ps) for pid, ps in plugs.items() if isinstance(ps, dict)}
    for pid in PLUGS:
        ps = state["plugs"].setdefault(pid, {})
        ps.setdefault("device", "AC" if pid == first and "device" not in ps and legacy else
                      (ps.get("name") or PLUGS[pid].config_name))
        ps["power_above_w"] = _num_or(ps.get("power_above_w"), 0)
        # The page asks "Turn on/off?" before switching (on by default: safer for sensitive devices)
        # Only an explicit false turns it off (a missing or null value keeps asking)
        ps["confirm_switch"] = ps.get("confirm_switch") is not False
        good = []
        for s in ps.get("schedules") or []:
            try:
                good.append(clean_schedule(s))
            except ApiError as e:
                log(f"state.json: plug {pid}: dropped invalid schedule {s!r}: {e}")
        ps["schedules"] = sorted(good, key=lambda s: s["time"])
    save_state()


def save_state():
    write_json(STATE_PATH, state)


def plug_state(pid):
    return state["plugs"][pid]


# --------------------------------------------------------------------------
# Alerts and notifications
# --------------------------------------------------------------------------

alerts_lock = threading.Lock()
alerts_log = []          # recent alert messages shown on the page
_alerts = {}             # alert key -> {"active": bool, "sent": time of last notification}


def _record(key, message):
    """Show an alert on the page and send it, at most once per cooldown per key."""
    now = time.time()
    a = _alerts.setdefault(key, {"active": False, "sent": 0})
    alerts_log.insert(0, {"ts": now, "msg": message})
    del alerts_log[50:]
    log(f"ALERT: {message}")
    if now - a["sent"] >= ALERT_COOLDOWN_S:
        a["sent"] = now
        threading.Thread(target=notify, args=(message,), daemon=True).start()


def set_alert(key, active, message=""):
    """A condition alert: recorded once when it becomes active."""
    with alerts_lock:
        a = _alerts.setdefault(key, {"active": False, "sent": 0})
        if active and not a["active"]:
            a["active"] = True
            _record(key, message)
        elif not active:
            a["active"] = False


def alert_once(key, message):
    """A one-off event (back online, failed schedule, budget, …)."""
    with alerts_lock:
        _record(key, message)


def alert_active(key):
    with alerts_lock:
        return _alerts.get(key, {}).get("active", False)


def mask_url(url):
    """'https://discord.com/api/webhooks/123/abc' -> 'https://discord.com/•••'."""
    url = (url or "").strip()
    if not url:
        return ""
    p = urllib.parse.urlsplit(url)
    if not (p.scheme and p.hostname):
        return URL_MASK
    host = p.hostname + (f":{p.port}" if p.port else "")
    return f"{p.scheme}://{host}/{URL_MASK}"


def notify(message):
    """Send a message to the configured URL. Returns None on success, else the error.
    Discord webhook URLs get Discord's JSON format; anything else (e.g. ntfy)
    gets the message as plain text."""
    url = (state["alerts"].get("notify_url") or "").strip()
    if not url:
        return "no notification URL set"
    name = cfg.get("title", "Smart Plug")
    headers = {"User-Agent": "SmartPlug/1.0"}   # Discord rejects Python's default agent
    if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
        body = json.dumps({"username": name, "content": f"**{name}:** {message}"}).encode()
        headers["Content-Type"] = "application/json"
    else:
        body = message.encode()
        headers["Title"] = name
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers,
                                                      method="POST"), timeout=8)
        return None
    except Exception as e:
        log(f"notify failed: {e}")
        return str(e)


# --------------------------------------------------------------------------
# Energy database
# --------------------------------------------------------------------------

SCHEMA = """CREATE TABLE IF NOT EXISTS readings (
    plug TEXT NOT NULL, ts INTEGER NOT NULL, on_state INTEGER, power_w REAL,
    voltage_v REAL, current_a REAL, wh REAL, device TEXT NOT NULL,
    PRIMARY KEY (plug, ts)) WITHOUT ROWID"""
_db_ready = False
_db_init_lock = threading.Lock()


def db():
    global _db_ready
    conn = sqlite3.connect(DB_PATH, timeout=15)
    if not _db_ready:
        with _db_init_lock:
            if not _db_ready:
                _init_db(conn)
                _db_ready = True
    return conn


def _init_db(conn):
    # WAL: page queries (readers) never block the poll loops (writers)
    conn.execute("PRAGMA journal_mode=WAL")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(readings)")]
    if cols and "plug" not in cols:
        # Database of the single-plug version: one row per second, no plug column.
        # Move every reading to the first plug.
        first = next(iter(PLUGS), "1")
        device = "COALESCE(device, 'AC')" if "device" in cols else "'AC'"
        t = time.time()
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ALTER TABLE readings RENAME TO readings_v1")
            conn.execute(SCHEMA)
            n = conn.execute(f"""INSERT INTO readings
                SELECT ?, ts, on_state, power_w, voltage_v, current_a, wh, {device}
                FROM readings_v1""", (first,)).rowcount
            conn.execute("DROP TABLE readings_v1")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.isolation_level = ""
        log(f"database upgraded for several plugs: {n} readings moved to plug {first} "
            f"in {time.time() - t:.1f} s")
        conn.execute("CREATE INDEX IF NOT EXISTS readings_ts ON readings(ts)")
        conn.commit()
        conn.execute("VACUUM")      # give back the old table's space (the file would double)
    conn.execute(SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS readings_ts ON readings(ts)")
    conn.commit()


def log_reading(pid, st, elapsed_s, device):
    p = st.get("power_w") or 0.0
    conn = db()
    try:
        conn.execute("INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?,?,?)",
                     (pid, int(time.time()), int(st["on"]), p, st.get("voltage_v"),
                      st.get("current_a"), p * elapsed_s / 3600.0, device))
        conn.commit()
    finally:
        conn.close()


def where(pid, device=""):
    """SQL fragment + params limiting a query to one plug and maybe one device label."""
    if device:
        return " AND plug = ? AND device = ?", (pid, device)
    return " AND plug = ?", (pid,)


def midnight(d, days_back=0):
    """Local midnight `days_back` days before d, as a timestamp (correct across DST)."""
    return int((datetime(d.year, d.month, d.day) - timedelta(days=days_back)).timestamp())


def period_stats(conn, pid, since, poll, price, device=""):
    """Runtime (relay on), energy and cost since a timestamp, plus per-day averages.
    Runtime adds the gap to the previous reading whenever the plug was on
    (capped, so time when the program was off is not counted)."""
    w, p = where(pid, device)
    runtime, wh, days = conn.execute(f"""
        SELECT COALESCE(SUM(CASE WHEN on_state = 1 AND prev IS NOT NULL
                                 THEN MIN(ts - prev, ?) ELSE 0 END), 0),
               COALESCE(SUM(wh), 0),
               COUNT(DISTINCT date(ts, 'unixepoch', 'localtime'))
        FROM (SELECT ts, on_state, wh, LAG(ts) OVER (ORDER BY ts) AS prev
              FROM readings WHERE ts >= ?{w})""", (poll * GAP_POLLS, since) + p).fetchone()
    d = max(days, 1)
    return {"runtime_s": int(runtime), "kwh": round(wh / 1000, 3),
            "eur": round(wh / 1000 * price, 2), "days_with_data": days,
            "avg_runtime_s": int(runtime / d), "avg_kwh": round(wh / 1000 / d, 3),
            "avg_eur": round(wh / 1000 * price / d, 2)}


def measured_seconds(conn, pid, since, poll, device=""):
    """Seconds covered by readings since a timestamp (gaps > 3 polls don't count)."""
    w, p = where(pid, device)
    return conn.execute(f"""
        SELECT COALESCE(SUM(MIN(ts - prev, ?)), 0)
        FROM (SELECT ts, LAG(ts) OVER (ORDER BY ts) AS prev FROM readings WHERE ts >= ?{w})
        WHERE prev IS NOT NULL""", (poll * GAP_POLLS, since) + p).fetchone()[0]


# All-time per-device totals of each plug. Scanning a year of readings takes seconds,
# so it is done once and then only the new rows are added.
_dev_lock = threading.Lock()
_dev = {}            # plug id -> {"upto": last ts included, "agg": {device: [runtime_s, wh, first, last]}}
_cache_gen = 0       # bumped on every invalidation; stale results are not cached
_summary_cache = {}  # (plug, device, price, poll, day0) -> (time, generation, totals)


def invalidate_energy(pid=None):
    """Forget cached totals (after relabel / reset)."""
    global _cache_gen
    with _dev_lock:
        if pid is None:
            _dev.clear()
        else:
            _dev.pop(pid, None)
        _cache_gen += 1
        _summary_cache.clear()


def _device_agg(conn, pid, poll):
    """[(device, runtime_s, wh, first_ts, last_ts)] of one plug, most recently used first."""
    gap = poll * GAP_POLLS
    with _dev_lock:
        c = _dev.get(pid)
        if c is None:
            agg = {}
            for dev, rt, wh, first, last_ts in conn.execute("""
                SELECT device,
                       SUM(CASE WHEN on_state = 1 AND prev IS NOT NULL THEN MIN(ts - prev, ?) ELSE 0 END),
                       SUM(wh), MIN(ts), MAX(ts)
                FROM (SELECT device, ts, on_state, wh,
                             LAG(ts) OVER (PARTITION BY device ORDER BY ts) AS prev
                      FROM readings WHERE plug = ?)
                GROUP BY device""", (gap, pid)):
                agg[dev] = [rt or 0, wh or 0, first, last_ts]
            c = _dev[pid] = {"agg": agg, "upto": max((a[3] for a in agg.values()), default=-1)}
        else:
            for dev, ts, on, wh in conn.execute(
                    "SELECT device, ts, on_state, wh FROM readings WHERE plug = ? AND ts > ? ORDER BY ts",
                    (pid, c["upto"])):
                a = c["agg"].get(dev)
                if a is None:
                    c["agg"][dev] = [0, wh or 0, ts, ts]
                else:
                    if on == 1:
                        a[0] += min(ts - a[3], gap)
                    a[1] += wh or 0
                    a[3] = ts
                c["upto"] = ts
        rows = sorted(c["agg"].items(), key=lambda kv: kv[1][3], reverse=True)
    return [(dev, *a) for dev, a in rows]


def device_totals(conn, pid, poll, price, agg=None):
    """One row per device label ever logged on a plug: runtime, kWh, cost, first and last day."""
    return [{"device": dev, "runtime_s": int(rt), "kwh": round(wh / 1000, 3),
             "eur": round(wh / 1000 * price, 2),
             "first": datetime.fromtimestamp(first).strftime("%d.%m.%Y"),
             "last": datetime.fromtimestamp(last_ts).strftime("%d.%m.%Y")}
            for dev, rt, wh, first, last_ts in (agg if agg is not None else _device_agg(conn, pid, poll))]


def energy_summary(pid, price, live_w, poll, device=""):
    """Totals for the page. `device` limits everything to one label ('' = all).
    The database part is cached for SUMMARY_CACHE_S: the page asks every 3 s and
    the 7/30-day queries take a second or more on a big database."""
    now = datetime.now()
    day0 = midnight(now)
    key = (pid, device, price, poll, day0)
    hit = _summary_cache.get(key)
    if hit and time.time() - hit[0] < SUMMARY_CACHE_S and hit[1] == _cache_gen:
        out = dict(hit[2])
    else:
        gen = _cache_gen
        out = _energy_totals(pid, now, day0, price, poll, device)
        with _dev_lock:
            if gen == _cache_gen:          # nothing was reset/relabeled meanwhile
                if len(_summary_cache) > 50:
                    _summary_cache.clear()
                _summary_cache[key] = (time.time(), gen, out)
        out = dict(out)
    out.update(now_eur_per_hour=round((live_w or 0) / 1000 * price, 3),
               now_eur_per_day=round((live_w or 0) / 1000 * 24 * price, 2),
               current_device=plug_state(pid)["device"])
    return out


def _energy_totals(pid, now, day0, price, poll, device):
    mon0 = int(datetime(now.year, now.month, 1).timestamp())
    w, p = where(pid, device)
    conn = db()
    try:
        q = f"SELECT COALESCE(SUM(wh),0) FROM readings WHERE ts >= ?{w}"
        today = conn.execute(q, (day0,) + p).fetchone()[0]
        month = conn.execute(q, (mon0,) + p).fetchone()[0]
        periods = {
            "today": period_stats(conn, pid, day0, poll, price, device),
            "d7": period_stats(conn, pid, midnight(now, 6), poll, price, device),
            "d30": period_stats(conn, pid, midnight(now, 29), poll, price, device),
        }
        measured_s = measured_seconds(conn, pid, mon0, poll, device)
        logged_today = measured_seconds(conn, pid, day0, poll)
        agg = _device_agg(conn, pid, poll)
    finally:
        conn.close()
    devices = device_totals(None, pid, poll, price, agg)
    # All-time total from the per-device totals (no full table scan)
    total = sum(r[2] for r in agg if not device or r[0] == device)

    # Month projection: average draw over the time actually measured this month,
    # applied to the whole month. Gaps (program off) are not counted as zero.
    days_in_month = monthrange(now.year, now.month)[1]
    proj_kwh = None
    if measured_s >= 3600:
        proj_kwh = month / 1000 / measured_s * days_in_month * 86400
    # Share of today that was actually logged (program running and plug reachable)
    secs_today = max(time.time() - day0, 1)
    coverage = min(100, round(logged_today / secs_today * 100))

    kwh = lambda wh: round(wh / 1000, 3)
    eur = lambda wh: round(wh / 1000 * price, 2)
    return {
        "price_per_kwh": price,
        "today_kwh": kwh(today), "today_eur": eur(today),
        "month_kwh": kwh(month), "month_eur": eur(month),
        "total_kwh": kwh(total), "total_eur": eur(total),
        "projected_month_kwh": None if proj_kwh is None else round(proj_kwh, 1),
        "projected_month_eur": None if proj_kwh is None else round(proj_kwh * price, 2),
        "coverage_today_pct": coverage,
        "measured_month_h": round(measured_s / 3600, 1),
        "periods": periods,
        "device": device, "devices": devices,
    }


def chart_data(pid, rng, date, price, device="", compare=True):
    """kWh per bucket like the Tuya app: day = 24 hours, week = Mon–Sun, month = 1..31.
    With compare, the previous day/week/month is included as `prev`."""
    d = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now()
    d = datetime(d.year, d.month, d.day)
    prev = None
    if compare:
        if rng == "day":
            pd = d - timedelta(days=1)
        elif rng == "week":
            pd = d - timedelta(days=7)
        else:
            pd = datetime(d.year, d.month, 1) - timedelta(days=1)
        prev = chart_data(pid, rng, pd.strftime("%Y-%m-%d"), price, device, compare=False)
        prev = {k: prev[k] for k in ("kwh", "title", "total_kwh", "total_eur")}
    if rng == "day":
        start, n = d, 24
        end = d + timedelta(days=1)
        key, labels = "CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER)", list(range(24))
        title = f"{d:%a %d.%m.%Y}"
    elif rng == "week":
        start = d - timedelta(days=d.weekday())
        end, n = start + timedelta(days=7), 7
        key = "CAST(julianday(date(ts, 'unixepoch', 'localtime')) - julianday(?) AS INTEGER)"
        labels = [(start + timedelta(days=i)).strftime("%a %d.%m") for i in range(7)]
        title = f"{start:%d.%m.} – {end - timedelta(days=1):%d.%m.%Y}"
    else:
        start = datetime(d.year, d.month, 1)
        n = monthrange(d.year, d.month)[1]
        end = start + timedelta(days=n)
        key = "CAST(strftime('%d', ts, 'unixepoch', 'localtime') AS INTEGER) - 1"
        labels = list(range(1, n + 1))
        title = f"{start:%B %Y}"
    w, p = where(pid, device)
    args = ((start.strftime("%Y-%m-%d"),) if rng == "week" else ()) + \
           (int(start.timestamp()), int(end.timestamp())) + p
    conn = db()
    try:
        rows = conn.execute(f"SELECT {key} k, SUM(wh) FROM readings "
                            f"WHERE ts >= ? AND ts < ?{w} GROUP BY k", args).fetchall()
    finally:
        conn.close()
    wh = [0.0] * n
    for k, v in rows:
        if k is not None and 0 <= k < n:
            wh[k] += v or 0
    total = sum(wh)
    return {"range": rng, "date": d.strftime("%Y-%m-%d"), "title": title,
            "labels": labels, "kwh": [round(x / 1000, 4) for x in wh],
            "total_kwh": round(total / 1000, 3), "total_eur": round(total / 1000 * price, 2),
            "has_data": [x > 0 for x in wh], "prev": prev}


def plug_name(pid):
    return PLUGS[pid].name if pid in PLUGS else plug_state_name(pid)


def plug_state_name(pid):
    return state["plugs"].get(pid, {}).get("name") or f"Plug {pid}"


def month_report(year, month, price, poll):
    """Text summary of one calendar month (all plugs) for the monthly notification."""
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), month % 12 + 1, 1)
    conn = db()
    try:
        rows = conn.execute("""
            SELECT plug, device, SUM(wh), SUM(CASE WHEN on_state = 1 AND prev IS NOT NULL
                                                   THEN MIN(ts - prev, ?) ELSE 0 END)
            FROM (SELECT plug, device, ts, on_state, wh,
                         LAG(ts) OVER (PARTITION BY plug, device ORDER BY ts) AS prev
                  FROM readings WHERE ts >= ? AND ts < ?)
            GROUP BY plug, device ORDER BY SUM(wh) DESC""",
            (poll * GAP_POLLS, int(start.timestamp()), int(end.timestamp()))).fetchall()
        busiest = conn.execute("""
            SELECT date(ts, 'unixepoch', 'localtime') d, SUM(wh) FROM readings
            WHERE ts >= ? AND ts < ? GROUP BY d ORDER BY SUM(wh) DESC LIMIT 1""",
            (int(start.timestamp()), int(end.timestamp()))).fetchone()
    finally:
        conn.close()
    if not rows:
        return None
    wh = sum(r[2] or 0 for r in rows)
    runtime = sum(r[3] or 0 for r in rows)
    eur = lambda x: f"{(x or 0) / 1000 * price:.2f} €"
    lines = [f"Report for {start:%B %Y}: {wh / 1000:.2f} kWh = {eur(wh)}, "
             f"switched on for {runtime / 3600:.1f} h."]
    if busiest:
        bd = datetime.strptime(busiest[0], "%Y-%m-%d")
        lines.append(f"Busiest day: {bd:%a %d.%m.} with {busiest[1] / 1000:.2f} kWh ({eur(busiest[1])}).")
    plugs = {}
    for pid, dev, w, rt in rows:
        t = plugs.setdefault(pid, [0, 0])
        t[0] += w or 0
        t[1] += rt or 0
    if len(plugs) > 1:
        lines.append("Per plug: " + ", ".join(
            f"{plug_name(pid)} {w / 1000:.2f} kWh ({eur(w)}), on {rt / 3600:.1f} h"
            for pid, (w, rt) in sorted(plugs.items(), key=lambda kv: -kv[1][0])))
    if len(rows) > len(plugs):
        lines.append("Per device: " + ", ".join(
            f"{dev}{f' ({plug_name(pid)})' if len(plugs) > 1 else ''} "
            f"{(w or 0) / 1000:.2f} kWh ({eur(w)})" for pid, dev, w, _ in rows))
    return "\n".join(lines)


_report_tries = {}


def monthly_checks(now=None):
    """Budget alert (all plugs together) and the monthly report."""
    n = now or datetime.now()
    month_key = n.strftime("%Y-%m")
    price = state["price_per_kwh"]
    poll = cfg["poll_seconds"]
    budget = state["alerts"].get("budget_eur") or 0
    if budget > 0 and state.get("budget_alerted") != month_key:
        conn = db()
        try:
            wh = conn.execute("SELECT COALESCE(SUM(wh), 0) FROM readings WHERE ts >= ?",
                              (int(datetime(n.year, n.month, 1).timestamp()),)).fetchone()[0]
        finally:
            conn.close()
        cost = wh / 1000 * price
        if cost >= budget:
            with state_lock:
                state["budget_alerted"] = month_key     # once per month, also across restarts
                save_state()
            alert_once(f"budget-{month_key}",
                       f"This month's cost is {cost:.2f} €, over your budget of {budget:.2f} €")
    if state["alerts"].get("monthly_report") and state.get("last_report") != month_key:
        prev_last_day = datetime(n.year, n.month, 1) - timedelta(days=1)
        text = month_report(prev_last_day.year, prev_last_day.month, price, poll)
        err = notify(text) if text else None
        tries = _report_tries.get(month_key, 0) + 1
        _report_tries[month_key] = tries
        # Retry a failed send on the next checks (every 5 min), for about an hour
        if err is None or err == "no notification URL set" or tries >= 12:
            with state_lock:
                state["last_report"] = month_key
                save_state()
            if text:
                log("monthly report " + ("sent" if err is None else f"not sent: {err}"))


# --------------------------------------------------------------------------
# Background loops: one per plug, plus one for the monthly checks
# --------------------------------------------------------------------------

def poll_once(plug, now, elapsed_s):
    """Read the plug, store the reading, update alerts."""
    try:
        st = plug.read()
    except Exception as e:
        plug.last["fail_count"] += 1
        plug.last.update(error=str(e) or e.__class__.__name__, ts=now)
        fc = plug.last["fail_count"]
        if fc == GAP_POLLS:
            set_alert(f"offline-{plug.id}", True, f"{plug.name}: plug is not reachable")
        if (fc == GAP_POLLS or fc % 30 == 0) and hasattr(plug.backend, "rediscover"):
            try:
                plug.backend.rediscover()
            except Exception as e2:
                log(f"{plug.name}: rediscover failed: {e2}")
        return
    if alert_active(f"offline-{plug.id}"):
        set_alert(f"offline-{plug.id}", False)
        alert_once(f"online-{plug.id}", f"{plug.name}: plug is reachable again")
    plug.last.update(error=None, ts=now, fail_count=0)
    # Database and alert problems are logged, not counted as the plug failing
    try:
        log_reading(plug.id, st, elapsed_s, plug_state(plug.id)["device"])
    except Exception as e:
        log(f"{plug.name}: saving reading failed: {e}")
    try:
        set_alert(f"fault-{plug.id}", bool(st["faults"]),
                  f"{plug.name}: plug reports fault: " + ", ".join(st["faults"]))
        limit = plug_state(plug.id).get("power_above_w") or 0
        power = st["power_w"] or 0
        if not limit or power < limit * POWER_CLEAR:
            plug.power_alert = False
            set_alert(f"power-{plug.id}", False)
        elif power > limit and not plug.power_alert:
            plug.power_alert = True
            set_alert(f"power-{plug.id}", True,
                      f"{plug.name}: power {power:.0f} W is above your limit of {limit:g} W")
    except Exception as e:
        log(f"{plug.name}: alert check failed: {e}")


def run_schedules(plug, now=None):
    """Fire the weekly schedules that are due. Minutes missed since the last check
    (slow pass, rediscovery, DST jump) are caught up for up to 15 minutes; failed
    switches are retried and alerted."""
    cur = (now or datetime.now()).replace(second=0, microsecond=0)
    if plug.sched_last is None or cur < plug.sched_last - timedelta(hours=2):
        plug.sched_last = cur - timedelta(minutes=1)    # start / clock set back by hand
    due = []
    if cur > plug.sched_last:
        with state_lock:
            scheds = [dict(s) for s in plug_state(plug.id)["schedules"] if s.get("enabled", True)]
        m = max(plug.sched_last + timedelta(minutes=1), cur - timedelta(minutes=SCHED_CATCHUP_MIN))
        while m <= cur:
            day, hm = DAYS[(m.weekday() + 1) % 7], f"{m:%H:%M}"
            due += [(s, m, 0) for s in scheds if s["time"] == hm and day in s["days"]]
            m += timedelta(minutes=1)
        plug.sched_last = cur
    todo, plug.sched_retry = plug.sched_retry + due, []
    if len(todo) > 1:
        # Only the newest due switch counts: running an older one (a retry, or several
        # caught up at once) after it would leave the plug in the wrong state
        newest = max(m for _, m, _ in todo)
        for s, m, _ in todo:
            if m < newest:
                log(f"{plug.name}: schedule {s['time']} (turn {s['action']}) skipped, "
                    f"a newer one is due")
        todo = [t for t in todo if t[1] == newest][-1:]
    for s, m, tries in todo:
        err = plug.switch(s["action"] == "on", f"(schedule {s['time']})")
        if err is None:
            continue
        if tries + 1 < SCHED_TRIES:
            plug.sched_retry.append((s, m, tries + 1))
        else:
            alert_once(f"sched-{plug.id}",
                       f"{plug.name}: schedule {s['time']} (turn {s['action']}) failed: {err}")


def poll_loop(plug, interval):
    last_ts = time.time()
    while True:
        t0 = time.time()
        # Never let one bad pass end the thread: log it and carry on
        try:
            # Cap the step so a sleep/hibernate gap is not counted as usage
            poll_once(plug, t0, min(t0 - last_ts, interval * GAP_POLLS))
        except Exception as e:
            log(f"{plug.name}: poll failed: {e}")
        last_ts = t0
        try:
            run_schedules(plug)
        except Exception as e:
            log(f"{plug.name}: schedules failed: {e}")
        # Keep the real interval close to poll_seconds however long the plug took
        time.sleep(max(0.5, interval - (time.time() - t0)))


def housekeeping_loop():
    while True:
        try:
            monthly_checks()
        except Exception as e:
            log(f"monthly checks failed: {e}")
        time.sleep(300)


# --------------------------------------------------------------------------
# Web API
# --------------------------------------------------------------------------

app = Flask(__name__)
app.json.ensure_ascii = False


@app.errorhandler(ApiError)
def _api_error(e):
    return jsonify(ok=False, error=str(e)), e.code


def body():
    b = request.get_json(silent=True)
    if not isinstance(b, dict):
        raise ApiError("send a JSON object")
    return b


def pick_plug(b=None):
    """The plug a request is about: ?plug=ID or "plug" in the body; default the first."""
    pid = request.args.get("plug")
    if pid is None:
        pid = (b or {}).get("plug")
    if pid is None:
        pid = next(iter(PLUGS))
    # An empty or wrong id is an error, never "the first plug": a switch must not
    # reach a plug other than the one asked for
    pid = str(pid)
    if pid not in PLUGS:
        raise ApiError(f"unknown plug '{pid}'", 404)
    return PLUGS[pid]


def plug_error(e):
    return jsonify(ok=False, error=f"plug did not respond: {e}"), 502


@app.get("/api/plugs")
def api_plugs():
    return jsonify(plugs=[p.overview() for p in PLUGS.values()])


@app.get("/api/status")
def api_status():
    plug = pick_plug()
    st = plug.status()
    with state_lock:   # copy the state; the slow database part runs without the lock
        ps = plug_state(plug.id)
        schedules = [dict(s) for s in ps["schedules"]]
        alerts_cfg = dict(state["alerts"], power_above_w=ps["power_above_w"])
        price = state["price_per_kwh"]
        confirm_switch = ps["confirm_switch"]
    # The webhook / topic URL works as a password: never send it back
    alerts_cfg["notify_url"] = mask_url(alerts_cfg.get("notify_url"))
    energy = energy_summary(plug.id, price, st["power_w"] if st and st["on"] else 0,
                            cfg["poll_seconds"], request.args.get("device", ""))
    return jsonify(plug={"id": plug.id, "name": plug.name, "confirm_switch": confirm_switch},
                   plugs=[p.overview() for p in PLUGS.values()],
                   status=st, error=plug.last["error"], updated=plug.last["ts"],
                   schedules=schedules, alerts_cfg=alerts_cfg, alerts=alerts_log[:10],
                   energy=energy)


@app.post("/api/switch")
def api_switch():
    b = body()
    plug = pick_plug(b)
    if not isinstance(b.get("on"), bool):
        raise ApiError('send {"on": true} or {"on": false}')
    plug.sched_retry = []      # a manual switch wins over a failed schedule still being retried
    err = plug.switch(b["on"], "(manual)")
    plug.refresh()
    if err:
        return plug_error(err)
    return jsonify(ok=True)


@app.post("/api/device")
def api_device():
    """Write an on-plug setting. Body: {"plug": id, "setting": name, "value": ...}"""
    b = body()
    plug = pick_plug(b)
    name, v = b.get("setting"), b.get("value")
    if name == "countdown":
        dp, val = "countdown", check_int(v, 0, 86400, "countdown seconds")
    elif name == "power_on":
        if v not in POWER_ON_VALUES:
            raise ApiError(f"value must be one of {POWER_ON_VALUES}")
        dp, val = "power_on", v
    elif name == "led":
        if v not in LED_VALUES:
            raise ApiError(f"value must be one of {LED_VALUES}")
        dp, val = "led", v
    elif name in ("child_lock", "overcurrent"):
        if not isinstance(v, bool):
            raise ApiError("value must be true or false")
        dp, val = name, v
    elif name == "inching":
        if not isinstance(v, dict):
            raise ApiError('value must be {"enabled": bool, "seconds": n}')
        dp, val = "inching", encode_inching(bool(v.get("enabled")),
                                            check_int(v.get("seconds"), 1, 65535, "seconds"))
    elif name == "cycle":
        dp, val = "cycle", encode_cycle(clean_cycle(v))
    elif name == "random":
        dp, val = "random", encode_random(clean_random(v))
    else:
        raise ApiError("unknown setting")
    try:
        plug.set(dp, val)
    except Exception as e:
        log(f"{plug.name}: setting {name} failed: {e}")
        return plug_error(e)
    time.sleep(0.5)
    plug.refresh()
    return jsonify(ok=True)


@app.post("/api/schedules")
def api_schedules():
    """Add {"time","action","days"}, or {"delete": id} / {"toggle": id} (id or list index)."""
    b = body()
    plug = pick_plug(b)
    with state_lock:
        scheds = plug_state(plug.id)["schedules"]
        old = copy.deepcopy(scheds)
        if "delete" in b or "toggle" in b:
            ref = b.get("delete", b.get("toggle"))
            idx = next((i for i, s in enumerate(scheds) if s["id"] == ref), None)
            if idx is None and isinstance(ref, int) and not isinstance(ref, bool) and 0 <= ref < len(scheds):
                idx = ref
            if idx is None:
                raise ApiError("no such schedule", 404)
            if "delete" in b:
                scheds.pop(idx)
            else:
                scheds[idx]["enabled"] = not scheds[idx].get("enabled", True)
        else:
            if len(scheds) >= 50:
                raise ApiError("at most 50 schedules per plug")
            s = clean_schedule({k: b.get(k) for k in ("time", "action", "days")})
            scheds.append(s)
            scheds.sort(key=lambda s: s["time"])
        try:
            save_state()
        except OSError as e:
            scheds[:] = old       # not saved: don't run it until the next restart either
            raise ApiError(f"could not save schedules: {e}", 500) from None
    return jsonify(ok=True)


@app.post("/api/settings")
def api_settings():
    """Any subset of: price_per_kwh, notify_url, budget_eur, monthly_report (all plugs);
    plug_name, device, power_above_w, confirm_switch (the plug given by "plug")."""
    b = body()
    plug = pick_plug(b)
    # Check everything first, so a bad field changes nothing
    new = {}
    if "price_per_kwh" in b:
        new["price_per_kwh"] = check_num(b["price_per_kwh"], "price")
    if "budget_eur" in b:
        new["budget_eur"] = check_num(b["budget_eur"] or 0, "budget")
    if "power_above_w" in b:
        new["power_above_w"] = check_num(b["power_above_w"] or 0, "power limit")
    if "monthly_report" in b:
        new["monthly_report"] = bool(b["monthly_report"])
    if "confirm_switch" in b:
        if not isinstance(b["confirm_switch"], bool):
            raise ApiError("confirm_switch must be true or false")
        new["confirm_switch"] = b["confirm_switch"]
    if "notify_url" in b:
        url = str(b["notify_url"] or "").strip()
        if URL_MASK not in url:     # the page shows the masked URL; sending it back keeps the saved one
            if url and not re.match(r"^https?://[^\s/]+", url):
                raise ApiError("notification URL must start with http:// or https://")
            new["notify_url"] = url
    if "device" in b:
        new["device"] = check_name(b["device"], "device name")
    if "plug_name" in b:
        new["plug_name"] = check_name(b["plug_name"], "plug name", 30)
    with state_lock:
        old = copy.deepcopy(state)
        ps = plug_state(plug.id)
        for k in ("price_per_kwh",):
            if k in new:
                state[k] = new[k]
        for k in ("budget_eur", "monthly_report", "notify_url"):
            if k in new:
                state["alerts"][k] = new[k]
        if "power_above_w" in new:
            ps["power_above_w"] = new["power_above_w"]
        if "confirm_switch" in new:
            if new["confirm_switch"] != ps["confirm_switch"]:
                log(f"{plug.name}: confirm before switching "
                    f"{'enabled' if new['confirm_switch'] else 'disabled'}")
            ps["confirm_switch"] = new["confirm_switch"]
        if "device" in new:
            if new["device"] != ps["device"]:
                log(f"{plug.name}: connected device: {ps['device']} -> {new['device']}")
            ps["device"] = new["device"]
        if "plug_name" in new:
            if new["plug_name"] != plug.name:
                log(f"plug {plug.id} renamed: {plug.name} -> {new['plug_name']}")
            ps["name"] = new["plug_name"]
        try:
            save_state()
        except OSError as e:
            state.clear()
            state.update(old)
            raise ApiError(f"could not save settings: {e}", 500) from None
    return jsonify(ok=True)


@app.post("/api/relabel")
def api_relabel():
    """Change the device label of a plug's readings between two local times.
    Body: {"plug": id, "from": "YYYY-MM-DDTHH:MM", "to": "YYYY-MM-DDTHH:MM", "device": name}"""
    b = body()
    plug = pick_plug(b)
    try:
        t0 = datetime.strptime(str(b.get("from")), "%Y-%m-%dT%H:%M")
        t1 = datetime.strptime(str(b.get("to")), "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ApiError("from/to must be YYYY-MM-DDTHH:MM") from None
    name = check_name(b.get("device"), "device name")
    if t1 <= t0:
        raise ApiError("to must be after from")
    conn = db()
    try:
        cur = conn.execute("UPDATE readings SET device = ? WHERE plug = ? AND ts >= ? AND ts < ?",
                           (name, plug.id, int(t0.timestamp()), int(t1.timestamp())))
        conn.commit()
    finally:
        conn.close()
    invalidate_energy(plug.id)
    log(f"{plug.name}: relabeled {cur.rowcount} readings {t0:%d.%m. %H:%M}–{t1:%d.%m. %H:%M} as {name}")
    return jsonify(ok=True, changed=cur.rowcount)


@app.post("/api/test-report")
def api_test_report():
    """Send last month's report now (or this month so far if last month is empty)."""
    n = datetime.now()
    price, poll = state["price_per_kwh"], cfg["poll_seconds"]
    pl = datetime(n.year, n.month, 1) - timedelta(days=1)
    text = month_report(pl.year, pl.month, price, poll) or month_report(n.year, n.month, price, poll)
    if not text:
        return jsonify(ok=False, error="no data yet")
    err = notify(text)
    return jsonify(ok=err is None, error=err, text=text)


@app.post("/api/reset")
def api_reset():
    """Delete every stored reading of one plug. Settings, schedules and the price stay."""
    b = body()
    plug = pick_plug(b)
    if b.get("confirm") != "RESET":
        raise ApiError('send {"confirm": "RESET"}')
    conn = db()
    try:
        n = conn.execute("DELETE FROM readings WHERE plug = ?", (plug.id,)).rowcount
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    invalidate_energy(plug.id)
    log(f"{plug.name}: energy log reset: {n} readings deleted")
    return jsonify(ok=True, deleted=n)


@app.post("/api/test-notify")
def api_test_notify():
    err = notify("Test notification from the smart plug page")
    return jsonify(ok=err is None, error=err)


@app.get("/api/info")
def api_info():
    plug = pick_plug()
    c = plug.cfg
    return jsonify(
        id=plug.id, name=plug.name, product=c.get("product"),
        device_id=c.get("device_id", "demo"), mac=c.get("mac"),
        ip=c.get("ip", "demo"), protocol=c.get("version", None if DEMO else 3.5),
        model=c.get("model"), poll_seconds=cfg["poll_seconds"])


@app.get("/api/export/readings.csv")
def export_readings():
    """Every stored reading (one row per poll), streamed so a big log needs little memory.
    ?plug=ID limits it to one plug, ?device=NAME (with plug) to one label."""
    pid, device = request.args.get("plug", ""), request.args.get("device", "")
    if pid and pid not in PLUGS:
        raise ApiError(f"unknown plug '{pid}'", 404)
    sql, args = "", ()
    if pid:
        sql, args = where(pid, device)
    names = {p.id: p.name for p in PLUGS.values()}

    def rows():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "plug", "device", "on", "power_w", "voltage_v", "current_a", "energy_wh"])
        conn = db()
        try:
            cur = conn.execute(f"""SELECT datetime(ts,'unixepoch','localtime'), plug, device, on_state,
                                          power_w, voltage_v, current_a, ROUND(wh, 5)
                                   FROM readings WHERE 1=1{sql} ORDER BY ts, plug""", args)
            while True:
                chunk = cur.fetchmany(2000)
                if not chunk:
                    break
                w.writerows((r[0], names.get(r[1], r[1])) + r[2:] for r in chunk)
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        finally:
            conn.close()
        yield buf.getvalue()

    label = "-".join(x for x in (names.get(pid, "") if pid else "", device) if x)
    label = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_")
    filename = f"plug-readings{'-' + label if label else ''}-{datetime.now():%Y-%m-%d}.csv"
    return Response(stream_with_context(rows()), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/api/chart")
def api_chart():
    plug = pick_plug()
    rng = request.args.get("range", "day")
    if rng not in ("day", "week", "month"):
        raise ApiError("range must be day, week or month")
    try:
        return jsonify(chart_data(plug.id, rng, request.args.get("date"), state["price_per_kwh"],
                                  request.args.get("device", "")))
    except ValueError:
        raise ApiError("date must be YYYY-MM-DD") from None


@app.get("/")
def index():
    with open(os.path.join(BASE, "index.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


# --------------------------------------------------------------------------

def init(raw_cfg=None, demo=False, data_dir=None):
    """Load config, plugs, state and the database. Separate from main() for the tests."""
    global DATA, CONFIG_PATH, STATE_PATH, DB_PATH, DEMO, _db_ready
    DEMO = demo
    DATA = data_dir or DATA
    CONFIG_PATH = os.path.join(DATA, "config.json")
    STATE_PATH = os.path.join(DATA, "demo_state.json" if demo else "state.json")
    DB_PATH = os.path.join(DATA, "demo_energy.db" if demo else "energy.db")
    if raw_cfg is None:
        if demo:
            raw_cfg = DEMO_CONFIG
        else:
            if not os.path.exists(CONFIG_PATH):
                raise ValueError("config.json missing. Copy config.example.json and fill it in.")
            with open(CONFIG_PATH, encoding="utf-8") as f:
                raw_cfg = json.load(f)
    cfg.clear()
    cfg.update(poll_seconds=int(raw_cfg.get("poll_seconds", DEFAULT_POLL)),
               port=int(raw_cfg.get("port", 8080)),
               title=raw_cfg.get("title") or ("Smart Plug" if "plugs" in raw_cfg
                                              else raw_cfg.get("name") or "Smart Plug"))
    if cfg["poll_seconds"] < 1:
        raise ValueError("config.json: poll_seconds must be at least 1")
    PLUGS.clear()
    for pc in plug_configs(raw_cfg, demo):
        PLUGS[pc["id"]] = Plug(pc, demo)
    with alerts_lock:
        _alerts.clear()
        alerts_log.clear()
    _db_ready = False
    invalidate_energy()
    load_state(raw_cfg)
    db().close()          # create / upgrade the database now, not on the first request


def start_threads():
    for plug in PLUGS.values():
        threading.Thread(target=poll_loop, args=(plug, cfg["poll_seconds"]), daemon=True,
                         name=f"poll-{plug.id}").start()
    threading.Thread(target=housekeeping_loop, daemon=True, name="housekeeping").start()

    def warm():
        # The first full scan of a big database takes a few seconds: do it now,
        # not on the first page load
        conn = db()
        try:
            for pid in PLUGS:
                _device_agg(conn, pid, cfg["poll_seconds"])
        finally:
            conn.close()
    threading.Thread(target=warm, daemon=True, name="warm-cache").start()


if __name__ == "__main__":
    try:
        init(demo=DEMO)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        sys.exit(str(e))
    if DEMO:
        log("DEMO MODE: simulated plugs (separate demo_* data files)")
    start_threads()
    for p in PLUGS.values():
        log(f"plug {p.id} '{p.name}' at {p.cfg.get('ip', 'demo')}")
    log(f"listening on port {cfg['port']}")
    app.run(host="0.0.0.0", port=cfg["port"], debug=False, use_reloader=False, threaded=True)
