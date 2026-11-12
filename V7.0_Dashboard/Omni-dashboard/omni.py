"""Omni (Bio-dash V7.0): up to three people side by side, each with a Polar H10 and an SpO2 device (Checkme O2 ring).

Each person ("Person 1", or a name you give them) has at most one H10 and one O2 ring. Their H10 gets a Bluetooth
radio of its own when there is one (an H10 streaming ECG + accelerometer keeps its radio busy); the O2 rings go on
radios no H10 uses, else share their person's radio. The page compares everyone (heart rate, SpO2 and HRV on shared
charts, a table of the latest values) and has a card per person with their ECG, R-R and motion.

People: people.json next to this file (created from V6's last_device_*.json and radio_config.json, so a single
person carries on as before). Recordings: <Documents>/Bio-dash/Omni/<device>/<date>/<time>_<stream>.csv, one session
per device connection, as before, plus <time>_person.json saying whose session it was.
"""
import asyncio
import json
import logging
import math
import os
import sys
import threading
import time
import uuid
from collections import deque
from itertools import islice

from flask import Flask, Response, jsonify, request, send_from_directory

if os.environ.get("OMNI_FAKE") == "1":          # the stand-in devices (fake_omni.py), for the tests
    import fake_omni
    fake_omni.install()

from bleak import BleakClient, BleakScanner
from polar_python import PolarDevice
from importlib.metadata import version as _pkg_version

import bt_debug
from stream_clock import ClockAnchor, StreamClock

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("BIODASH_OMNI_PORT", "5000"))
ECG_HZ = 130
ACC_HZ = 200
MAX_PEOPLE = 3
# One colour per person; the page also gives each a line style, so colour never carries it alone
COLORS = ["#f43f5e", "#0ea5e9", "#d97706"]      # rose, sky, amber: readable on the light and the dark themes
ROLES = ("polar", "viatom")
ROLE_LABEL = {"polar": "Polar H10", "viatom": "O2 ring"}

POLAR_STREAMS = {
    "ecg": ["Timestamp_Epoch_ms", "ECG_mV"],
    "acc": ["Timestamp_Epoch_ms", "X_mg", "Y_mg", "Z_mg"],
    "rr":  ["Timestamp_Epoch_ms", "RR_ms"],
    "hrv": ["Timestamp_Epoch_ms", "HR_BPM", "RMSSD_ms", "SDNN_ms"],
}
O2_STREAMS = {"vitals": ["Timestamp_Epoch_ms", "SpO2_pct", "HR_BPM"]}
STREAMS = {"polar": POLAR_STREAMS, "viatom": O2_STREAMS}

# Optional: an O2 band known by address (bands are found by name / service anyway).
VIATOM_MAC = os.environ.get("BIODASH_VIATOM_MAC", "").strip().upper()
VIATOM_SVC_UUID = "14839ac4-7d7e-415c-9a42-167340cf2339"
VIATOM_WRITE_UUID = "8b00ace7-eb0a-49b0-b977-10a8d4d5e82f"
VIATOM_POLL = bytes([0xAA, 0x17, 0xE8, 0x00, 0x00, 0x00, 0x00, 0x1B])

_log_lock = threading.Lock()   # callbacks run on the BLE thread; keep each write whole


class SampleRing:
    """Thread-safe ring of recent samples with a running sequence number: the BLE thread appends, every SSE
    client keeps its own cursor and asks for everything since it last looked."""

    def __init__(self, maxlen):
        self._buf = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    def extend(self, items):
        with self._lock:
            self._buf.extend(items)
            self._seq += len(items)

    def since(self, seq):
        with self._lock:
            n = min(self._seq - seq, len(self._buf))
            items = list(islice(self._buf, len(self._buf) - n, None)) if n > 0 else []
            return items, self._seq

    @property
    def seq(self):
        with self._lock:
            return self._seq


def _first_attr(obj, names, default=None):
    for n in names:
        v = getattr(obj, n, None)
        if v:
            return v
    return default


def _acc_xyz(val):
    if isinstance(val, (list, tuple)):
        return val[0], val[1], val[2]
    if isinstance(val, dict):
        return val.get('x', val.get('X', 0)), val.get('y', val.get('Y', 0)), val.get('z', val.get('Z', 0))
    return getattr(val, 'x', 0), getattr(val, 'y', 0), getattr(val, 'z', 0)


def _norm(addr):
    return (addr or "").strip().upper()


# ==========================================
# PEOPLE
# ==========================================
class Person:
    """One person: their devices (saved) and everything live about them (not saved)."""

    def __init__(self, pid, name, color, radio="auto", devices=None):
        self.id, self.name, self.color, self.radio = pid, name, color, radio or "auto"
        # role -> {"address", "name", "auto"} or None
        self.devices = {r: (devices or {}).get(r) for r in ROLES}
        self.target = {r: None for r in ROLES}           # the address a worker should connect to now
        self.retries = {r: 0 for r in ROLES}             # failed tries since target was set
        self.state = {"polar": {"status": "Disconnected", "hr": 0, "rr": 0, "rmssd": 0.0, "sdnn": 0.0},
                      "viatom": {"status": "Disconnected", "spo2": 0, "hr": 0}}
        self.ecg_ring, self.acc_ring, self.rr_ring = SampleRing(ECG_HZ * 10), SampleRing(ACC_HZ * 10), SampleRing(200)
        self.h10_clock = ClockAnchor()                   # ECG and accelerometer share the strap's clock
        self.ecg_clock = StreamClock(ECG_HZ, self.h10_clock)
        self.acc_clock = StreamClock(ACC_HZ, self.h10_clock)
        self.ppi = deque(maxlen=40)
        self.last_heartbeat = 0.0
        self.viatom_link_up = False
        self.sessions = {r: bt_debug.SessionFiles("Omni") for r in ROLES}
        self.meters = {k: bt_debug.RateMeter() for k in ("ecg", "acc", "ecg_frames", "acc_frames", "hr")}
        self.viatom_meters = {k: bt_debug.RateMeter(window=20) for k in ("notifications", "readings", "polls")}
        self.viatom_counters = {"fragments_skipped": 0, "calibrating_packets": 0, "out_of_range": 0, "write_failures": 0}
        self.link_failures = {r: 0 for r in ROLES}
        self.link_info = {r: {"adapter": None, "device": None, "connected_at": None, "recording": None} for r in ROLES}
        self.link_info["viatom"].update(last_reading_at=None, gatt={})
        self.route = {r: None for r in ROLES}            # hciN for this boot
        self.why = {r: "" for r in ROLES}
        self.tasks = {}

    # ---- saved part
    def saved(self):
        return {"id": self.id, "name": self.name, "color": self.color, "radio": self.radio, "devices": self.devices}

    def view(self):
        return {**self.saved(), "state": self.state, "routing": self.route, "why": self.why,
                "connecting_to": self.target}

    # ---- recordings
    def log_rows(self, role, stream, rows):
        if rows:
            with _log_lock:
                self.sessions[role].writerows(stream, rows)
                self.sessions[role].flush(stream)

    def start_session(self, role, name, address):
        with _log_lock:
            d = self.sessions[role].start(name, address, STREAMS[role])
            try:
                with open(os.path.join(d, f"{self.sessions[role].stamp}_person.json"), "w") as f:
                    json.dump({"person": self.name, "person_id": self.id, "role": role, "device": name,
                               "address": address, "started": time.time()}, f, indent=2)
            except OSError:
                pass
        self.link_info[role]["recording"] = d
        return d

    def close_session(self, role):
        with _log_lock:
            self.sessions[role].close()
        self.link_info[role]["recording"] = None

    # ---- Polar callbacks (BLE thread)
    def polar_hr_cb(self, data):
        self.last_heartbeat = time.time()
        self.meters["hr"].add()
        st = self.state["polar"]
        bpm = _first_attr(data, ("heartrate", "bpm", "heart_rate"))
        if bpm is not None:
            st["hr"] = int(bpm)
        new_rr, now_ms = [], int(time.time() * 1000)
        for rr in _first_attr(data, ("rr_intervals", "rrs", "rrs_ms"), []):
            try:
                v = int(round(float(rr)))
            except (ValueError, TypeError):
                continue
            if 200 < v < 2000:
                self.ppi.append(v)
                new_rr.append([now_ms, v])
        if not new_rr:
            return
        st["rr"] = new_rr[-1][1]
        self.rr_ring.extend(new_rr)
        self.log_rows("polar", "rr", new_rr)
        h = self.ppi
        if len(h) > 2:
            st["rmssd"] = math.sqrt(sum((h[i] - h[i - 1]) ** 2 for i in range(1, len(h))) / (len(h) - 1))
            mean = sum(h) / len(h)
            st["sdnn"] = math.sqrt(sum((x - mean) ** 2 for x in h) / (len(h) - 1))

    def polar_acc_cb(self, data):
        samples = _first_attr(data, ("data", "samples", "acc"), [])
        if not samples:
            return
        out = []
        for ts, val in zip(self.acc_clock.stamps(getattr(data, "timestamp", None), len(samples)), samples):
            try:
                x, y, z = _acc_xyz(val)
                out.append([ts, int(x), int(y), int(z)])
            except (ValueError, TypeError, IndexError):
                continue
        self.acc_ring.extend(out)
        self.log_rows("polar", "acc", out)
        self.meters["acc"].add(len(out))
        self.meters["acc_frames"].add()

    def polar_ecg_cb(self, data):
        samples = _first_attr(data, ("data", "samples", "ecg", "voltages"), [])
        if not samples:
            return
        out = []
        for ts, val in zip(self.ecg_clock.stamps(getattr(data, "timestamp", None), len(samples)), samples):
            try:
                out.append([ts, round(int(getattr(val, 'voltage', getattr(val, 'ecg_uv', val))) / 1000.0, 3)])
            except (ValueError, TypeError):
                continue
        if out:
            self.ecg_ring.extend(out)
            self.log_rows("polar", "ecg", out)
            self.meters["ecg"].add(len(out))
            self.meters["ecg_frames"].add()

    # ---- O2 ring callback (BLE thread)
    def viatom_rx_cb(self, sender, data):
        if len(data) == 0:
            return
        self.viatom_meters["notifications"].add()
        st = self.state["viatom"]
        # 0xA5 = sensor settling: a display state only; the link stays up
        if len(data) == 1 and data[0] == 0xA5:
            self.viatom_counters["calibrating_packets"] += 1
            st["status"] = "Calibrating"
            return
        # Real-time frames start with 0x55; continuation fragments don't (and read as garbage at fixed offsets)
        if data[0] != 0x55:
            self.viatom_counters["fragments_skipped"] += 1
            return
        if len(data) >= 9:
            spo2, hr = int(data[7]), int(data[8])
            if not 40 <= spo2 <= 100:
                self.viatom_counters["out_of_range"] += 1
            else:
                self.viatom_meters["readings"].add()
                self.link_info["viatom"]["last_reading_at"] = time.time()
                st.update(spo2=spo2, hr=hr, status="Connected")


people = []                       # Person, in page order
people_lock = threading.RLock()
PEOPLE_FILE = os.path.join(BASE_DIR, "people.json")


def _new_id():
    return "p" + uuid.uuid4().hex[:6]


def _free_color():
    used = {p.color for p in people}
    return next((c for c in COLORS if c not in used), COLORS[len(people) % len(COLORS)])


def _free_name():
    names = {p.name for p in people}
    return next(f"Person {i}" for i in range(1, 100) if f"Person {i}" not in names)


def save_people():
    tmp = PEOPLE_FILE + ".tmp"
    with people_lock:
        data = {"version": 1, "people": [p.saved() for p in people]}
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PEOPLE_FILE)


def load_people():
    """people.json, or (first V7 start) Person 1 made from V6's remembered H10 / O2 and the Polar's radio pin."""
    try:
        with open(PEOPLE_FILE) as f:
            saved = json.load(f).get("people") or []
        for d in saved[:MAX_PEOPLE]:
            people.append(Person(d.get("id") or _new_id(), d.get("name") or _free_name(), d.get("color") or _free_color(),
                                 d.get("radio"), d.get("devices")))
        return
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.error(f"Couldn't read {PEOPLE_FILE} ({e!r}); starting with one person")
    devices = {}
    for role in ROLES:
        old = bt_debug.LastDevice(os.path.join(BASE_DIR, f"last_device_{role}.json")).load()
        if old:
            devices[role] = {"address": old["address"], "name": old.get("name"), "auto": bool(old.get("auto", True))}
    radio = "auto"
    try:
        with open(os.path.join(BASE_DIR, "radio_config.json")) as f:
            radio = json.load(f).get("polar", "auto")
    except Exception:
        pass
    people.append(Person(_new_id(), "Person 1", COLORS[0], radio, devices))
    save_people()
    if devices:
        logging.info("People: Person 1 made from the last H10 / O2 ring (V6)")


def person(pid):
    with people_lock:
        return next((p for p in people if p.id == pid), None)


def owner_of(address):
    a = _norm(address)
    with people_lock:
        for p in people:
            for role in ROLES:
                d = p.devices.get(role)
                if d and _norm(d.get("address")) == a:
                    return p, role
    return None, None


def assign(p, role, address, name=None):
    """Give `p` this device (taking it from anyone who had it) and connect to it."""
    other, orole = owner_of(address)
    known = None
    if other is not None:
        known = (other.devices.get(orole) or {}).get("name")
        if other is not p or orole != role:
            other.devices[orole] = None
            other.target[orole] = None              # their worker lets go; ours retries until it's free
    old = p.devices.get(role) or {}
    same = _norm(old.get("address")) == _norm(address)
    seen = next((d["name"] for d in discovered.get(role, []) if _norm(d["address"]) == _norm(address)), None)
    p.devices[role] = {"address": address, "name": name or known or (old.get("name") if same else None) or seen,
                       "auto": True}
    p.target[role] = address
    p.retries[role] = 0
    save_people()
    resolve_routing()


# ==========================================
# RADIO ROUTING
# ==========================================
# Saved by adapter ADDRESS (stable), resolved to hciN here (hciN follows plug-in order). "auto":
#   H10s: one radio each, external radios first, in people order; more H10s than radios -> they share
#   O2 rings: a radio no H10 uses (the least busy one), else their own person's radio
#   scanning: a radio no H10 uses; if every radio carries an H10, the radio of an H10 that isn't streaming yet,
#   else the last person's (the scanner then sweeps less often: see omni_scanner)
ADAPTER_REFRESH_S = 10
adapters = []                      # latest bt_debug.list_adapters()
routing = {"scan": None, "scan_shared": False, "why": ""}


def resolve_routing():
    usable = [a for a in adapters if a.get("powered", True) and not a.get("error")]
    by_addr = {_norm(a.get("address")): a["name"] for a in usable}
    order = [a["name"] for a in usable if a.get("placement") == "external"] + \
            [a["name"] for a in usable if a.get("placement") != "external"]
    h10 = {n: 0 for n in order}
    with people_lock:
        ppl = list(people)
    for p in ppl:
        want = _norm(p.radio)
        if want and want != "AUTO" and want in by_addr:
            p.route["polar"], p.why["polar"] = by_addr[want], f"pinned to {want}"
        elif not order:
            p.route["polar"], p.why["polar"] = None, "no radio found: the system default"
        else:
            free = [n for n in order if h10[n] == 0]
            pick = free[0] if free else min(order, key=lambda n: h10[n])
            p.route["polar"] = pick
            p.why["polar"] = ("auto: a radio of its own" if free else "auto: shared with another H10 (more H10s than radios)") + \
                             (f" · pinned radio {want} not present" if want and want != "AUTO" else "")
        if p.route["polar"] in h10 and p.devices.get("polar"):
            h10[p.route["polar"]] += 1
    spare = [n for n in order if h10[n] == 0]
    o2 = {n: 0 for n in order}
    for p in ppl:
        if spare:
            pick = min(spare, key=lambda n: o2[n])
            p.route["viatom"], p.why["viatom"] = pick, "auto: a radio no H10 uses"
            o2[pick] += 1
        else:
            p.route["viatom"] = p.route["polar"]
            p.why["viatom"] = "auto: shared with this person's H10 (no spare radio)" if order else "the system default"
    if spare:
        routing.update(scan=min(spare, key=lambda n: o2[n]), scan_shared=False, why="a radio no H10 uses")
    elif order:
        idle = [p.route["polar"] for p in ppl if p.state["polar"]["status"] != "Connected" and p.route["polar"]]
        routing.update(scan=idle[0] if idle else (ppl[-1].route["polar"] if ppl else order[0]), scan_shared=True,
                       why="every radio carries an H10: sharing one" + ("" if idle else " that is streaming"))
    else:
        routing.update(scan=None, scan_shared=False, why="the system default")


def _bluez(hci):
    return {"adapter": hci} if hci else {}


# ==========================================
# BLUETOOTH ENGINE (ASYNCIO)
# ==========================================
discovered = {"polar": [], "viatom": []}     # the last sweep, strongest first
ble_device_cache = {}
last_rssi = {}
scanner_state = {"scanning": False, "paused_reason": None, "last_sweep_at": None, "radio": None, "wanted_until": 0}
SHARED_SCAN_EVERY_S = 15                      # sweeps on an H10's radio while it streams: not more often
SCAN_WANTED_S = 60                            # "Find devices" on the page: scan this long even on a busy radio
versions = {}
for _pkg in ("bleak", "polar-python", "dbus-fast", "flask"):
    try:
        versions[_pkg] = _pkg_version(_pkg)
    except Exception:
        versions[_pkg] = None
versions["python"] = sys.version.split()[0]


def classify(name, addr, uuids):
    n = (name or "").upper()
    if "POLAR H10" in n:
        return "polar"
    if (VIATOM_MAC and addr == VIATOM_MAC) or VIATOM_SVC_UUID.lower() in uuids or \
            any(x in n for x in ["O2", "CHECKME", "VIATOM", "BAND-WU"]):
        return "viatom"
    return None


async def adapter_refresher():
    """Keeps the radio list and routing current (dongles can be plugged in any time)."""
    global adapters
    while True:
        try:
            adapters = await bt_debug.list_adapters()
        except Exception as e:
            logging.error(f"Adapter listing failed: {e!r}")
        resolve_routing()
        await asyncio.sleep(ADAPTER_REFRESH_S)


def _all_connected():
    with people_lock:
        ppl = list(people)
    return len(ppl) >= MAX_PEOPLE and all(p.state[r]["status"] in ("Connected", "Calibrating") for p in ppl for r in ROLES)


async def omni_scanner():
    last_shared = 0.0
    while True:
        resolve_routing()
        hci = routing["scan"]
        with people_lock:
            ppl = list(people)
        reason = None
        missing = any(p.devices.get(r) and p.devices[r].get("auto") and p.state[r]["status"] == "Disconnected"
                      for p in ppl for r in ROLES)
        wanted = time.time() < scanner_state["wanted_until"]
        busy = routing["scan_shared"] and any(p.route["polar"] == hci and p.state["polar"]["status"] == "Connected"
                                              for p in ppl)
        if any(p.state[r]["status"] == "Connecting" for p in ppl for r in ROLES):
            reason = "a link is connecting"
        elif _all_connected():
            reason = "everyone's devices are connected"
        elif busy and not (missing or wanted):
            reason = "nothing to look for, and the only radio free to scan carries a streaming H10 (Find devices scans anyway)"
        elif busy and time.time() - last_shared < SHARED_SCAN_EVERY_S:
            reason = f"sharing a streaming H10's radio: a sweep every {SHARED_SCAN_EVERY_S} s"
        if reason:
            scanner_state.update(scanning=False, paused_reason=reason)
            await asyncio.sleep(1)
            continue
        found = {"polar": {}, "viatom": {}}

        def scan_cb(device, adv):
            ble_device_cache[device.address] = device
            last_rssi[device.address] = adv.rssi
            addr = device.address.upper()
            role = classify(device.name or adv.local_name, addr, [u.lower() for u in (adv.service_uuids or [])])
            if role:
                found[role][addr] = {"name": device.name or adv.local_name or f"Device ({addr[-5:]})",
                                     "address": device.address, "rssi": adv.rssi or -100}

        scanner_state.update(scanning=True, paused_reason=None, radio=hci)
        try:
            async with BleakScanner(scan_cb, bluez=_bluez(hci)):
                await asyncio.sleep(2.5)
            if routing["scan_shared"]:
                last_shared = time.time()
            for role in ROLES:
                lst = []
                for addr, d in found[role].items():
                    p, r = owner_of(addr)
                    lst.append({**d, "role": role, "person": p.id if p else None})
                discovered[role] = sorted(lst, key=lambda x: x["rssi"], reverse=True)
            # auto-reconnect: someone's device is advertising, it's on auto, and nothing is connecting to it
            for p in ppl:
                for role in ROLES:
                    d = p.devices.get(role)
                    if d and d.get("auto") and not p.target[role] and _norm(d["address"]) in found[role]:
                        p.target[role] = found[role][_norm(d["address"])]["address"]
                        p.retries[role] = 0
                        logging.info(f"{p.name}: reconnecting the {ROLE_LABEL[role]} {d.get('name') or ''} [{d['address']}]")
            scanner_state["last_sweep_at"] = time.time()
        except Exception as e:
            logging.error(f"Scan failed on {hci or 'the default radio'}: {e!r}")
        await asyncio.sleep(2)


async def _device_on(p, role, mac):
    """A BLEDevice for `mac` as the person's radio for `role` sees it (BlueZ connects through the radio that
    discovered a device, so a device seen by the scan radio is looked up on the right one first)."""
    hci = p.route[role]
    cached = ble_device_cache.get(mac)
    if cached is not None and (hci is None or bt_debug.adapter_of(cached) in (None, hci)):
        return cached
    logging.info(f"{p.name}: looking up {mac} on {hci} for the {ROLE_LABEL[role]}...")
    return await BleakScanner.find_device_by_address(mac, timeout=10.0, bluez=_bluez(hci))


QUICK_RETRIES = 3          # tries straight after a device is given to someone (it may still be letting go of its
                           # previous person); after that, the scanner reconnects it when it next sees it


def settle(p, role, mac):
    """After a link ended or failed: try again soon a few times, then leave it to the scanner."""
    if _norm(p.target[role]) != _norm(mac):
        return                                    # someone changed the target meanwhile
    if p.retries[role] < QUICK_RETRIES and p.state[role]["status"] != "Connected" and not p.link_info[role]["connected_at"]:
        p.retries[role] += 1
    else:
        p.target[role] = None


async def polar_worker(p):
    st, info = p.state["polar"], p.link_info["polar"]
    while True:
        mac = p.target["polar"]
        if not mac:
            await asyncio.sleep(1)
            continue
        st["status"] = "Connecting"
        try:
            device = await _device_on(p, "polar", mac)
            if device:
                info.update(adapter=bt_debug.adapter_of(device) or p.route["polar"],
                            device={"name": device.name, "address": device.address, "rssi": last_rssi.get(device.address)})
                logging.info(f"{p.name}: Polar {device.name} -> radio {info['adapter'] or 'default'}")
                async with PolarDevice(device) as dev:
                    p.ecg_clock.reset()
                    p.acc_clock.reset()
                    for m in p.meters.values():
                        m.reset()
                    p.ppi.clear()
                    info["connected_at"] = time.time()
                    st["status"] = "Connected"
                    p.retries["polar"] = QUICK_RETRIES      # a link that ends later goes back to the scanner
                    d = p.devices.get("polar")
                    if d and _norm(d.get("address")) == _norm(device.address) and device.name:
                        d["name"] = device.name
                        save_people()
                    rec = p.start_session("polar", getattr(device, "name", None), device.address)
                    logging.info(f"{p.name}: Polar recording to {rec}")
                    p.last_heartbeat = time.time()
                    try:
                        await dev.start_hr_stream(p.polar_hr_cb)
                    except Exception as e:
                        logging.error(f"{p.name}: HR stream: {e}")
                    await asyncio.sleep(1.5)
                    try:
                        try:
                            await dev.start_acc_stream(p.polar_acc_cb, 200, 16, 8)
                        except TypeError:
                            await dev.start_acc_stream(p.polar_acc_cb)
                    except Exception as e:
                        logging.error(f"{p.name}: accelerometer stream: {e}")
                    await asyncio.sleep(1.5)
                    try:
                        try:
                            await dev.start_ecg_stream(p.polar_ecg_cb, 130, 14)
                        except TypeError:
                            await dev.start_ecg_stream(p.polar_ecg_cb)
                    except Exception as e:
                        logging.error(f"{p.name}: ECG stream: {e}")
                    while time.time() - p.last_heartbeat < 10.0 and _norm(p.target["polar"]) == _norm(mac):
                        await asyncio.sleep(1)
                    if _norm(p.target["polar"]) == _norm(mac):
                        logging.warning(f"{p.name}: Polar went quiet for 10 s (watchdog)")
            else:
                logging.warning(f"{p.name}: Polar {mac} not found")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            p.link_failures["polar"] += 1
            logging.error(f"{p.name}: Polar error: {e!r}")
        finally:
            info["connected_at"] = None
            p.close_session("polar")
            st.update(status="Disconnected", hr=0, rr=0, rmssd=0.0, sdnn=0.0)
        settle(p, "polar", mac)
        await asyncio.sleep(2)


async def viatom_worker(p):
    st, info = p.state["viatom"], p.link_info["viatom"]

    def on_disconnect(client):
        logging.warning(f"{p.name}: O2 ring disconnected")
        p.viatom_link_up = False

    while True:
        mac = p.target["viatom"]
        if not mac:
            await asyncio.sleep(1)
            continue
        st["status"] = "Connecting"
        try:
            device = await _device_on(p, "viatom", mac) or mac
            info.update(adapter=(bt_debug.adapter_of(device) if not isinstance(device, str) else None) or p.route["viatom"],
                        device={"name": getattr(device, "name", None), "address": mac, "rssi": last_rssi.get(mac)},
                        gatt={}, last_reading_at=None)
            for m in p.viatom_meters.values():
                m.reset()
            for k in p.viatom_counters:
                p.viatom_counters[k] = 0
            async with BleakClient(device, timeout=15.0, disconnected_callback=on_disconnect,
                                   bluez=_bluez(p.route["viatom"])) as client:
                p.viatom_link_up = True
                info["connected_at"] = time.time()
                await asyncio.sleep(3.0)                      # the ring's GATT table needs a moment
                svc = client.services.get_service(VIATOM_SVC_UUID)
                chars = svc.characteristics if svc else []
                notify_uuid = next((c.uuid for c in chars if "notify" in c.properties), None)
                write_uuid = next((c.uuid for c in chars if "write" in c.properties or "write-without-response" in c.properties),
                                  VIATOM_WRITE_UUID)
                if not notify_uuid:
                    raise RuntimeError("no notify characteristic in the O2 ring's service (GATT not resolved)")
                info["gatt"] = {"notify_uuid": notify_uuid, "write_uuid": write_uuid,
                                "mtu": getattr(client, "mtu_size", None), "write_mode": "with response"}
                await client.start_notify(notify_uuid, p.viatom_rx_cb)
                name = (info.get("device") or {}).get("name")
                d = p.devices.get("viatom")
                if d and _norm(d.get("address")) == _norm(mac) and name:
                    d["name"] = name
                    save_people()
                p.retries["viatom"] = QUICK_RETRIES
                rec = p.start_session("viatom", name, mac)
                logging.info(f"{p.name}: O2 recording to {rec}")
                st["status"] = "Connected"
                use_response = True
                while p.viatom_link_up and client.is_connected and _norm(p.target["viatom"]) == _norm(mac):
                    try:
                        await client.write_gatt_char(write_uuid, VIATOM_POLL, response=use_response)
                        p.viatom_meters["polls"].add()
                    except Exception:
                        p.viatom_counters["write_failures"] += 1
                        use_response = not use_response       # some firmwares take only one write mode
                        info["gatt"]["write_mode"] = "with response" if use_response else "without response"
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            p.link_failures["viatom"] += 1
            logging.error(f"{p.name}: O2 ring error: {e!r}")
        finally:
            info["connected_at"] = None
            p.close_session("viatom")
            p.viatom_link_up = False
            st.update(status="Disconnected", spo2=0, hr=0)
        settle(p, "viatom", mac)
        await asyncio.sleep(2)


async def vitals_logger():
    """One row a second per connected device: H10 -> hrv (HR, RMSSD, SDNN), O2 ring -> vitals (SpO2, HR)."""
    num = lambda x, d=0: (round(x, d) if d else int(x)) if x else ""
    while True:
        await asyncio.sleep(1)
        now_ms = int(time.time() * 1000)
        with people_lock:
            ppl = list(people)
        for p in ppl:
            ps, vs = p.state["polar"], p.state["viatom"]
            if ps["status"] == "Connected":
                p.log_rows("polar", "hrv", [[now_ms, num(ps["hr"]), num(ps["rmssd"], 1), num(ps["sdnn"], 1)]])
            if vs["status"] == "Connected":
                p.log_rows("viatom", "vitals", [[now_ms, num(vs["spo2"]), num(vs["hr"])]])


async def supervisor():
    """A Polar worker and an O2 worker per person; a removed person's workers are stopped (which disconnects)."""
    while True:
        with people_lock:
            ppl = list(people)
            gone = [p for p in _running if p not in ppl]
        for p in ppl:
            if not p.tasks:
                p.tasks = {"polar": asyncio.ensure_future(polar_worker(p)),
                           "viatom": asyncio.ensure_future(viatom_worker(p))}
                _running.append(p)
        for p in gone:
            for t in p.tasks.values():
                t.cancel()
            for t in p.tasks.values():
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            p.tasks = {}
            _running.remove(p)
            logging.info(f"{p.name} removed: their devices are disconnected")
        await asyncio.sleep(0.5)


_running = []


async def async_master():
    global adapters
    try:
        adapters = await bt_debug.list_adapters()
    except Exception:
        adapters = []
    resolve_routing()
    await asyncio.gather(adapter_refresher(), omni_scanner(), supervisor(), vitals_logger())


def start_ble_engine():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_master())


# ==========================================
# WEB
# ==========================================
app = Flask(__name__, root_path=BASE_DIR)


@app.route('/')
def home():
    return send_from_directory(os.path.join(app.root_path, "templates"), "index.html")


def people_view():
    with people_lock:
        ppl = [p.view() for p in people]
    found = {}
    for role in ROLES:
        lst = []
        for d in discovered[role]:
            p, r = owner_of(d["address"])
            if p is not None and p.state[r]["status"] in ("Connected", "Calibrating", "Connecting"):
                continue                              # seen before it connected: it isn't advertising now
            lst.append({**d, "person": p.id if p else None})
        found[role] = lst
    usable = [a for a in adapters if a.get("powered", True) and not a.get("error")]
    return {"people": ppl, "max": MAX_PEOPLE, "devices": found, "radios": len(usable),
            "scanner": scanner_state, "routing": routing}


def _err(msg, code=400):
    return jsonify({"error": msg}), code


@app.route('/api/people', methods=['GET', 'POST'])
def people_api():
    """GET: everyone, the devices found and the radios. POST {"name"?}: add a person (up to 3)."""
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        with people_lock:
            if len(people) >= MAX_PEOPLE:
                return _err(f"up to {MAX_PEOPLE} people (one H10 per radio)")
            name = str(body.get("name") or "").strip()[:40] or _free_name()
            p = Person(_new_id(), name, _free_color())
            people.append(p)
        save_people()
        resolve_routing()
        return jsonify({**people_view(), "added": p.id})
    return jsonify(people_view())


@app.route('/api/people/<pid>', methods=['POST', 'DELETE'])
def person_api(pid):
    """POST {"name"?, "color"?, "radio"?: adapter address | "auto", "move"?: -1 | 1}. DELETE: remove them."""
    p = person(pid)
    if p is None:
        return _err("no such person", 404)
    if request.method == 'DELETE':
        with people_lock:
            if len(people) == 1:
                return _err("Omni keeps at least one person")
            people.remove(p)
            p.target = {r: None for r in ROLES}
        save_people()
        resolve_routing()
        return jsonify(people_view())
    body = request.get_json(silent=True) or {}
    if "name" in body:
        name = str(body["name"] or "").strip()[:40]
        if not name:
            return _err("a name can't be empty")
        p.name = name
    if "color" in body:
        c = str(body["color"] or "")
        if not (len(c) == 7 and c.startswith("#") and all(ch in "0123456789abcdefABCDEF" for ch in c[1:])):
            return _err("color must look like #38bdf8")
        p.color = c
    if "radio" in body:
        p.radio = str(body["radio"] or "auto")
    if body.get("move") in (-1, 1):
        with people_lock:
            i = people.index(p)
            j = min(max(i + body["move"], 0), len(people) - 1)
            people[i], people[j] = people[j], people[i]
    save_people()
    resolve_routing()
    return jsonify(people_view())


@app.route('/api/people/<pid>/device', methods=['POST'])
def person_device_api(pid):
    """{"role", "address", "name"?}: give this person the device and connect.
    {"role", "auto": bool}: auto-reconnect on / off (off also disconnects). {"role", "disconnect": true}: disconnect
    now and stay off until Connect / auto. {"role", "connect": true}: connect when in range. {"role", "forget": true}."""
    p = person(pid)
    if p is None:
        return _err("no such person", 404)
    body = request.get_json(silent=True) or {}
    role = body.get("role")
    if role not in ROLES:
        return _err("role must be polar or viatom")
    d = p.devices.get(role)
    if body.get("address"):
        assign(p, role, body["address"], body.get("name"))
    elif body.get("forget"):
        p.devices[role] = None
        p.target[role] = None
        save_people()
    elif not d:
        return _err(f"{p.name} has no {ROLE_LABEL[role]} yet")
    elif body.get("disconnect") or body.get("auto") is False:
        d["auto"] = False
        p.target[role] = None
        save_people()
    elif body.get("connect") or body.get("auto") is True:
        d["auto"] = True
        save_people()                                # the scanner connects it when it's in range
    resolve_routing()
    return jsonify(people_view())


@app.route('/api/scan', methods=['POST'])
def scan_api():
    """The page's "Find devices": scan for the next minute, even on a radio a streaming H10 uses."""
    scanner_state["wanted_until"] = time.time() + SCAN_WANTED_S
    return jsonify({"scanner": scanner_state})


# ---- V6 calls, kept for scripts: they act on the first person (or the one given)
@app.route('/api/scanners')
def get_scanners():
    with people_lock:
        p = people[0]
    return jsonify({"devices": discovered, "state": p.state})


@app.route('/api/connect', methods=['POST'])
def command_connect():
    body = request.get_json(silent=True) or {}
    role, addr = body.get("type"), body.get("address")
    if role not in ROLES or not addr:
        return _err("type must be polar or viatom, with an address")
    with people_lock:
        p = person(body.get("person")) or next((q for q in people if not q.devices.get(role)), people[0])
    assign(p, role, addr)
    return jsonify({"status": "locked", "target": addr, "person": p.id})


@app.route('/api/last-device')
def last_device_api():
    """What nicknames.js asks every dashboard for (it shows the remembered devices beside the status). Omni
    shows each device in its person's card instead, so: none here."""
    return jsonify({"devices": {}})


HISTORY_MAX_POINTS = 1500
# metric -> (role, stream, column)
_HISTORY_SRC = {"rr": ("polar", "rr", 1), "polar_hr": ("polar", "hrv", 1), "rmssd": ("polar", "hrv", 2),
                "sdnn": ("polar", "hrv", 3), "spo2": ("viatom", "vitals", 1), "viatom_hr": ("viatom", "vitals", 2)}


def _tail_lines(path, max_bytes):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > max_bytes else lines


def _device_files(device, stream):
    import glob
    folder = os.path.join(bt_debug.dashboard_dir("Omni"), bt_debug.device_folder(device.get("name"), device.get("address")))
    return sorted(glob.glob(os.path.join(folder, "*", f"*_{stream}.csv")), key=os.path.getmtime, reverse=True)


@app.route('/api/history')
def history():
    """Trend history from the newest recording of a person's device:
    ?metric=spo2|viatom_hr|polar_hr|rmssd|sdnn|rr&minutes=N&person=<id> (default: the first person)"""
    metric = request.args.get("metric", "")
    try:
        minutes = max(1, min(int(request.args.get("minutes", 15)), 60))
    except ValueError:
        minutes = 15
    if metric not in _HISTORY_SRC:
        return jsonify({})
    role, stream, col = _HISTORY_SRC[metric]
    with people_lock:
        p = person(request.args.get("person")) or people[0]
    dev = p.devices.get(role)
    live = p.sessions[role].paths.get(stream)
    files = [live] if live else (_device_files(dev, stream)[:1] if dev else [])
    cutoff, pts = time.time() * 1000 - minutes * 60_000, []
    if files:
        try:
            for line in _tail_lines(files[0], minutes * 60 * (4 * 24 if stream == "rr" else 40) + 4096):
                f = line.split(",")
                try:
                    ts, y = int(f[0]), float(f[col])
                except (ValueError, IndexError):
                    continue
                if ts >= cutoff and y > 0:
                    pts.append({"x": ts, "y": y})
        except OSError:
            pass
    step = max(1, len(pts) // HISTORY_MAX_POINTS)
    return jsonify({metric: pts[::step]})


@app.route('/api/radios', methods=['GET', 'POST'])
def radios():
    """GET: radios + routing. POST {"person", "address" | "auto"}: pin a person's H10 to a radio."""
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        p = person(body.get("person"))
        if p is None:
            return _err("person must be one of /api/people")
        p.radio = body.get("address") or "auto"
        save_people()
        resolve_routing()
    with people_lock:
        per = {p.id: {"name": p.name, "radio": p.radio, "routing": p.route, "why": p.why} for p in people}
    return jsonify({"adapters": adapters, "people": per, "routing": routing})


@app.route('/api/debug')
def debug_info():
    out = []
    with people_lock:
        ppl = list(people)
    for p in ppl:
        pi, vi = p.link_info["polar"], p.link_info["viatom"]
        out.append({
            "id": p.id, "name": p.name, "color": p.color, "radio": p.radio, "routing": p.route, "why": p.why,
            "polar": {**pi, "state": p.state["polar"]["status"], "link_failures": p.link_failures["polar"],
                      "nominal": {"ecg_hz": ECG_HZ, "acc_hz": ACC_HZ},
                      "rates": {"ecg_hz": round(p.meters["ecg"].rate(), 1), "acc_hz": round(p.meters["acc"].rate(), 1),
                                "ecg_frames_per_s": round(p.meters["ecg_frames"].rate(), 2),
                                "ecg_samples_per_frame": p.ecg_clock.last_frame_len,
                                "hr_notifications_per_s": round(p.meters["hr"].rate(), 2),
                                "ecg_dropped": p.ecg_clock.dropped, "acc_dropped": p.acc_clock.dropped,
                                "acc_hz_measured": round(p.acc_clock.measured_hz, 2),
                                "clock_reanchors": p.h10_clock.reanchors}},
            "viatom": {**vi, "state": p.state["viatom"]["status"], "link_failures": p.link_failures["viatom"],
                       "counters": p.viatom_counters, "nominal": {"readings_per_s": 0.5},
                       "rates": {"readings_per_s": round(p.viatom_meters["readings"].rate(), 2),
                                 "notifications_per_s": round(p.viatom_meters["notifications"].rate(), 2),
                                 "polls_per_s": round(p.viatom_meters["polls"].rate(), 2)}},
        })
    return jsonify({"adapters": adapters, "routing": routing, "scanner": scanner_state, "versions": versions,
                    "people": out})


@app.route('/api/stream')
def stream_data():
    """Server-sent events, 10 a second: {"people": {id: {"polar": {...state, ecg_buffer, acc_buffer, rr_buffer},
    "viatom": {...state}}}}. Each client starts at "now" (no replay of the rings)."""
    def event_stream():
        cursors = {}
        while True:
            with people_lock:
                ppl = list(people)
            payload = {}
            for p in ppl:
                c = cursors.get(p.id)
                if c is None:
                    c = cursors[p.id] = [p.ecg_ring.seq, p.acc_ring.seq, p.rr_ring.seq]
                ecg, c[0] = p.ecg_ring.since(c[0])
                acc, c[1] = p.acc_ring.since(c[1])
                rr, c[2] = p.rr_ring.since(c[2])
                payload[p.id] = {"polar": {**p.state["polar"], "ecg_buffer": ecg, "acc_buffer": acc, "rr_buffer": rr},
                                 "viatom": p.state["viatom"]}
            yield f"data: {json.dumps({'people': payload}, separators=(',', ':'))}\n\n"
            time.sleep(0.1)
    return Response(event_stream(), mimetype="text/event-stream")


# ---------- shutdown / reboot (sidebar "Power") ----------
@app.route("/api/system/power", methods=["GET", "POST"])
def system_power():
    host = request.remote_addr or ""
    if request.method == "GET":
        return jsonify(bt_debug.power_info(host))
    action = (request.get_json(silent=True) or {}).get("action")
    code, out = bt_debug.system_power(action, host, request.headers.get(bt_debug.POWER_HEADER))
    return jsonify(out), code


# ---------- updates (sidebar "Updates"; the work is done by ../updater.py) ----------
@app.route("/api/system/update", methods=["GET", "POST"])
def system_update():
    host = request.remote_addr or ""
    if request.method == "GET":
        return jsonify(bt_debug.update_info(host, fetch=request.args.get("check") == "1"))
    action = (request.get_json(silent=True) or {}).get("action")
    code, out = bt_debug.system_update(action, host, request.headers.get(bt_debug.POWER_HEADER))
    return jsonify(out), code


# ---------- restart everything (sidebar "Power"; the work is done by ../restart_all.py) ----------
@app.route("/api/system/restart", methods=["GET", "POST"])
def system_restart():
    host = request.remote_addr or ""
    if request.method == "GET":
        return jsonify(bt_debug.restart_info(host))
    code, out = bt_debug.system_restart(request.get_json(silent=True) or {}, host, request.headers.get(bt_debug.POWER_HEADER))
    return jsonify(out), code


# ---------- names you give devices ("Mine", "Boss's"), shared by every dashboard ----------
@app.route("/api/device-names", methods=["GET", "POST"])
def device_names_api():
    if request.method == "GET":
        return jsonify({"names": bt_debug.device_names()})
    body = request.get_json(silent=True) or {}
    code, out = bt_debug.set_device_name(body.get("key"), body.get("name"))
    return jsonify(out), code


load_people()

if __name__ == "__main__":
    threading.Thread(target=start_ble_engine, daemon=True).start()
    logging.info(f"OMNI-DASH LIVE: http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
