import asyncio
import csv
import datetime
import json
import logging
import os
import sys
import threading
import time
import math
from collections import deque
from itertools import islice
from flask import Flask, Response, jsonify, request, send_from_directory
from bleak import BleakClient, BleakScanner
from bleak.backends.scanner import AdvertisementData
from bleak.backends.device import BLEDevice
from polar_python import PolarDevice

# ==========================================
# 1. CONFIGURATION & GLOBAL STATE
# ==========================================
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)]
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs_omni")
os.makedirs(LOGS_DIR, exist_ok=True)

ECG_HZ = 130
ACC_HZ = 200

class SampleRing:
    """Thread-safe ring of recent samples with a running sequence number.

    The BLE thread appends; every SSE client keeps its own cursor and asks for
    everything since it last looked. Nothing is ever cleared out from under the
    writer, and N browser tabs each get the full stream.
    """
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

class StreamClock:
    """Sensor-clock -> wall-clock mapping for PMD frames (timestamp = last sample).
    Gives jitter-free sample spacing; re-anchors if drift exceeds 1 s."""
    def __init__(self, hz):
        self.period_ms = 1000.0 / hz
        self.offset_ms = None

    def reset(self):
        self.offset_ms = None

    def stamps(self, sensor_ts_ns, n):
        now_ms = time.time() * 1000.0
        if sensor_ts_ns:
            last_ms = sensor_ts_ns / 1e6
            if self.offset_ms is None or abs(last_ms + self.offset_ms - now_ms) > 1000.0:
                self.offset_ms = now_ms - last_ms
            end_ms = last_ms + self.offset_ms
        else:
            end_ms = now_ms
        return [int(end_ms - (n - 1 - i) * self.period_ms) for i in range(n)]

ecg_ring = SampleRing(ECG_HZ * 10)   # [ts_ms, mV]
acc_ring = SampleRing(ACC_HZ * 10)   # [ts_ms, x, y, z]
rr_ring = SampleRing(200)            # [ts_ms, rr_ms] -- one entry per beat
ecg_clock = StreamClock(ECG_HZ)
acc_clock = StreamClock(ACC_HZ)

# Shared Memory Matrix (scalars only; waveform data lives in the rings above)
omni_state = {
    "polar": {"status": "Disconnected", "hr": 0, "rr": 0, "rmssd": 0.0, "sdnn": 0.0},
    "viatom": {"status": "Disconnected", "spo2": 0, "hr": 0}
}

discovered_devices = {"polar": [], "viatom": []}
active_targets = {"polar": None, "viatom": None}

# DBus Cache
ble_device_cache = {}

polar_ppi_history = deque(maxlen=40)
polar_last_heartbeat = 0
viatom_link_up = False

# Hardware UUIDs
VIATOM_MAC = "F3:A0:A8:E3:F5:63"
VIATOM_SVC_UUID = "14839ac4-7d7e-415c-9a42-167340cf2339"
VIATOM_WRITE_UUID = "8b00ace7-eb0a-49b0-b977-10a8d4d5e82f"

# --- ADAPTER ROUTING ---
POLAR_ADAPTER = "hci0"    
GENERAL_ADAPTER = "hci1"  

# ==========================================
# 2. FLASK WEB DASHBOARD (STABILIZED UI)
# ==========================================
app = Flask(__name__, root_path=os.path.dirname(os.path.abspath(__file__)))  # works from any cwd


@app.route('/')
def home():
    return send_from_directory(os.path.join(app.root_path, "templates"), "index.html")

@app.route('/api/scanners')
def get_scanners():
    return jsonify({"devices": discovered_devices, "state": omni_state})

@app.route('/api/connect', methods=['POST'])
def command_connect():
    req = request.json
    t_type = req.get('type')
    t_mac = req.get('address')
    if t_type in active_targets:
        active_targets[t_type] = t_mac
    return jsonify({"status": "locked", "target": t_mac})

@app.route('/api/stream')
def stream_data():
    def event_stream():
        # Start each client at "now" so it doesn't replay the ring on connect
        ecg_seq, acc_seq, rr_seq = ecg_ring.seq, acc_ring.seq, rr_ring.seq
        while True:
            ecg, ecg_seq = ecg_ring.since(ecg_seq)
            acc, acc_seq = acc_ring.since(acc_seq)
            rr, rr_seq = rr_ring.since(rr_seq)
            payload = {
                "polar": {**omni_state["polar"], "ecg_buffer": ecg, "acc_buffer": acc, "rr_buffer": rr},
                "viatom": omni_state["viatom"],
            }
            yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
            time.sleep(0.1)
    return Response(event_stream(), mimetype="text/event-stream")


# ==========================================
# 3. BLUETOOTH ENGINE (ASYNCIO)
# ==========================================

def _first_attr(obj, names, default=None):
    for n in names:
        v = getattr(obj, n, None)
        if v:
            return v
    return default

def polar_hr_cb(data):
    # polar_python HRData: heartrate (int BPM), rr_intervals (list[float] ms)
    global polar_last_heartbeat
    polar_last_heartbeat = time.time()

    bpm = _first_attr(data, ("heartrate", "bpm", "heart_rate"))
    if bpm is not None:
        omni_state["polar"]["hr"] = int(bpm)

    rr_list = _first_attr(data, ("rr_intervals", "rrs", "rrs_ms"), [])
    new_rr = []
    now_ms = int(time.time() * 1000)
    for rr in rr_list:
        try:
            rr_val = int(round(float(rr)))
        except (ValueError, TypeError):
            continue
        if 200 < rr_val < 2000:
            polar_ppi_history.append(rr_val)
            new_rr.append([now_ms, rr_val])

    if not new_rr:
        return
    omni_state["polar"]["rr"] = new_rr[-1][1]
    rr_ring.extend(new_rr)

    h = polar_ppi_history
    if len(h) > 2:
        sq_diff = sum((h[i] - h[i - 1]) ** 2 for i in range(1, len(h)))
        omni_state["polar"]["rmssd"] = math.sqrt(sq_diff / (len(h) - 1))
        mean_rr = sum(h) / len(h)
        omni_state["polar"]["sdnn"] = math.sqrt(sum((x - mean_rr) ** 2 for x in h) / (len(h) - 1))

def _acc_xyz(val):
    if isinstance(val, (list, tuple)):
        return val[0], val[1], val[2]
    if isinstance(val, dict):
        return val.get('x', val.get('X', 0)), val.get('y', val.get('Y', 0)), val.get('z', val.get('Z', 0))
    return getattr(val, 'x', 0), getattr(val, 'y', 0), getattr(val, 'z', 0)

def polar_acc_cb(data):
    # polar_python ACCData: timestamp (ns, last sample), data (list[(x, y, z)] mG)
    samples = _first_attr(data, ("data", "samples", "acc"), [])
    if not samples: return
    stamps = acc_clock.stamps(getattr(data, "timestamp", None), len(samples))
    out = []
    for ts, val in zip(stamps, samples):
        try:
            x, y, z = _acc_xyz(val)
            out.append([ts, int(x), int(y), int(z)])
        except (ValueError, TypeError, IndexError):
            continue
    acc_ring.extend(out)

def polar_ecg_cb(data):
    # polar_python ECGData: timestamp (ns, last sample), data (list[int] µV)
    samples = _first_attr(data, ("data", "samples", "ecg", "voltages"), [])
    if not samples: return
    stamps = ecg_clock.stamps(getattr(data, "timestamp", None), len(samples))
    out = []
    for ts, val in zip(stamps, samples):
        try:
            out.append([ts, round(int(getattr(val, 'voltage', getattr(val, 'ecg_uv', val))) / 1000.0, 3)])
        except (ValueError, TypeError):
            continue
    if out:
        ecg_ring.extend(out)
        sys.stdout.write(f"\r[ POLAR ] ❤️ HR: {omni_state['polar']['hr']:3d} | ⚡ ECG: {out[-1][1]:>6.3f} mV    ")
        sys.stdout.flush()

def viatom_rx_cb(sender, data):
    if len(data) == 0: return
    
    # 0xA5 = sensor settling. This is a DISPLAY state only -- it must not touch the
    # link flag, or the write loop would exit and drop a perfectly good connection.
    if len(data) == 1 and data[0] == 0xA5:
        omni_state["viatom"]["status"] = "Calibrating"
        return

    # Real-time frames start with 0x55; continuation fragments of a longer frame
    # don't, and parsing them at fixed offsets produces garbage readings.
    if len(data) >= 9 and data[0] == 0x55:
        spo2_val, hr_val = int(data[7]), int(data[8])
        if 40 <= spo2_val <= 100:
            omni_state["viatom"]["spo2"] = spo2_val
            omni_state["viatom"]["hr"] = hr_val
            omni_state["viatom"]["status"] = "Connected"


async def omni_scanner():
    while True:
        # Scanning is paused while any link is being set up, and while the Polar is
        # streaming (active scans on the same adapter cause ECG/ACC drops).
        is_connecting = any(omni_state[k]["status"] == "Connecting" for k in omni_state)
        if is_connecting or omni_state["polar"]["status"] == "Connected":
            await asyncio.sleep(1)
            continue

        if any(not mac for mac in active_targets.values()):
            found = {"polar": {}, "viatom": {}}
            
            def scan_cb(device: BLEDevice, adv: AdvertisementData):
                ble_device_cache[device.address] = device
                name = (device.name or adv.local_name or "").upper()
                addr = device.address.upper()
                uuids = [u.lower() for u in (adv.service_uuids or [])]
                
                info = {"name": device.name or f"Device ({addr[-5:]})", "address": device.address, "rssi": adv.rssi or -100}
                if "POLAR H10" in name: found["polar"][addr] = info
                elif addr == VIATOM_MAC or VIATOM_SVC_UUID.lower() in uuids or any(x in name for x in ["O2", "CHECKME", "VIATOM", "BAND-WU"]): found["viatom"][addr] = info

            try:
                async with BleakScanner(scan_cb, passive=False, bluez={"adapter": GENERAL_ADAPTER}):
                    await asyncio.sleep(2.5) 
                for key in discovered_devices:
                    discovered_devices[key] = sorted(list(found[key].values()), key=lambda x: x["rssi"], reverse=True)
            except Exception as e:
                logging.error(f"Radar blocked by OS: {repr(e)}")
        await asyncio.sleep(2)


async def polar_worker():
    global polar_last_heartbeat
    while True:
        target_mac = active_targets["polar"]
        if not target_mac or omni_state["polar"]["status"] in ["Connected", "Connecting"]:
            await asyncio.sleep(1); continue
            
        omni_state["polar"]["status"] = "Connecting"
        try:
            device = ble_device_cache.get(target_mac)
            if device:
                async with PolarDevice(device) as p:
                    ecg_clock.reset()
                    acc_clock.reset()
                    omni_state["polar"]["status"] = "Connected"
                    polar_last_heartbeat = time.time()
                    
                    logging.info("Polar handshake complete. Activating Heart Rate Matrix...")
                    try: await p.start_hr_stream(polar_hr_cb)
                    except Exception as e: logging.error(f"[DIAGNOSTIC] HR Error: {e}")
                    await asyncio.sleep(1.5) 
                    
                    logging.info("Activating Kinematics...")
                    try:
                        try: await p.start_acc_stream(polar_acc_cb, 200, 16, 8)
                        except TypeError: await p.start_acc_stream(polar_acc_cb)
                    except Exception as e: logging.error(f"[DIAGNOSTIC] ACC Error: {e}")
                    await asyncio.sleep(1.5)

                    logging.info("Activating ECG...")
                    try:
                        try: await p.start_ecg_stream(polar_ecg_cb, 130, 14)
                        except TypeError: await p.start_ecg_stream(polar_ecg_cb)
                    except Exception as e: logging.error(f"[DIAGNOSTIC] ECG Error: {e}")
                    
                    while time.time() - polar_last_heartbeat < 10.0 and omni_state["polar"]["status"] == "Connected":
                        await asyncio.sleep(1)
                    logging.warning("\n⚠️ POLAR DROPPED: Watchdog timeout.")
            else:
                logging.warning(f"\n⚠️ Target not found in radar cache. Retrying...")
                
        except Exception as e: logging.error(f"Polar Error: {repr(e)}")
        
        omni_state["polar"]["status"] = "Disconnected"
        active_targets["polar"] = None
        sys.stdout.write("\n")
        await asyncio.sleep(2)


async def viatom_worker():
    global viatom_link_up

    def handle_disconnect(client):
        global viatom_link_up
        logging.warning("\n⚠️ VIATOM DROPPED: Hardware disconnect detected.")
        viatom_link_up = False

    write_bytes = bytearray([0xAA, 0x17, 0xE8, 0x00, 0x00, 0x00, 0x00, 0x1B])

    while True:
        target_mac = active_targets["viatom"]
        if not target_mac or omni_state["viatom"]["status"] != "Disconnected":
            await asyncio.sleep(1); continue

        omni_state["viatom"]["status"] = "Connecting"
        try:
            device = ble_device_cache.get(target_mac, target_mac)
            async with BleakClient(device, timeout=15.0, disconnected_callback=handle_disconnect) as client:
                viatom_link_up = True
                logging.info("Viatom connected. Waiting 3s for GATT table to boot...")
                await asyncio.sleep(3.0)

                svc = client.services.get_service(VIATOM_SVC_UUID)
                chars = svc.characteristics if svc else []
                notify_uuid = next((c.uuid for c in chars if "notify" in c.properties), None)
                write_uuid = next((c.uuid for c in chars if "write" in c.properties or "write-without-response" in c.properties), VIATOM_WRITE_UUID)
                if not notify_uuid:
                    raise RuntimeError("no notify characteristic in Viatom service (GATT not resolved)")

                await client.start_notify(notify_uuid, viatom_rx_cb)
                omni_state["viatom"]["status"] = "Connected"

                use_response = True
                while viatom_link_up and client.is_connected:
                    try:
                        await client.write_gatt_char(write_uuid, write_bytes, response=use_response)
                    except Exception:
                        use_response = not use_response  # some firmwares only accept one mode
                    await asyncio.sleep(2)
        except Exception as e: logging.error(f"Viatom Error: {repr(e)}")

        viatom_link_up = False
        omni_state["viatom"]["status"] = "Disconnected"
        active_targets["viatom"] = None
        await asyncio.sleep(2)

async def async_master():
    await asyncio.gather(omni_scanner(), polar_worker(), viatom_worker())

def start_ble_engine():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_master())

if __name__ == "__main__":
    ble_thread = threading.Thread(target=start_ble_engine, daemon=True)
    ble_thread.start()
    logging.info("OMNI-DASH LIVE: http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
