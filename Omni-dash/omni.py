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
from flask import Flask, Response, jsonify, request, render_template_string
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

# Shared Memory Matrix
omni_state = {
    "polar": {"status": "Disconnected", "hr": 0, "rmssd": 0.0, "ecg_mv": 0.0, "acc": [0,0,0]},
    "air": {"status": "Disconnected", "pm25": 0, "co2": 0, "temp": 0.0, "voc": 0},
    "viatom": {"status": "Disconnected", "spo2": 0, "hr": 0}
}

discovered_devices = {"polar": [], "air": [], "viatom": []}
active_targets = {"polar": None, "air": None, "viatom": None}

# THE FIX: Cache the physical device objects so we never have to double-scan!
ble_device_cache = {}

polar_ppi_history = []
polar_last_heartbeat = 0

# Hardware UUIDs
VIATOM_MAC = "F3:A0:A8:E3:F5:63"
VIATOM_SVC_UUID = "14839ac4-7d7e-415c-9a42-167340cf2339"
VIATOM_WRITE_UUID = "8b00ace7-eb0a-49b0-b977-10a8d4d5e82f"
AIR_UART_SVC_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
AIR_UART_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

# ==========================================
# 2. FLASK WEB DASHBOARD
# ==========================================
app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bio-Dash Omni-System</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@3.9.1/dist/chart.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/luxon@3.0.1/build/global/luxon.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-luxon@1.2.0/dist/chartjs-adapter-luxon.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-streaming@2.0.0/dist/chartjs-plugin-streaming.min.js"></script>
    <style>
        .glass-panel { background: #1e293b; border: 1px solid #334155; border-radius: 1rem; padding: 1.5rem; }
        .data-value { font-size: 2.5rem; font-weight: 800; letter-spacing: -0.05em; }
        .chart-container { height: 200px; width: 100%; position: relative; }
    </style>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen font-sans">
    <div class="max-w-7xl mx-auto px-4 py-6 flex flex-col gap-6">
        
        <header class="flex flex-col md:flex-row justify-between items-start md:items-center border-b border-slate-700 pb-4 gap-4">
            <div>
                <h1 class="text-3xl font-bold text-sky-400 flex items-center gap-3">
                    <span class="relative flex h-4 w-4"><span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-sky-400 opacity-75"></span><span class="relative inline-flex rounded-full h-4 w-4 bg-sky-500"></span></span>
                    OMNI-DASH COMMAND
                </h1>
                <p class="text-slate-400 text-sm mt-1">Multi-Threaded Hardware Telemetry</p>
            </div>
            
            <div class="flex gap-2 text-xs font-bold">
                <span id="stat-polar" class="px-3 py-1 rounded border border-rose-500/30 text-rose-400 bg-rose-500/10">POLAR: SCANNING</span>
                <span id="stat-air" class="px-3 py-1 rounded border border-emerald-500/30 text-emerald-400 bg-emerald-500/10">AIR: SCANNING</span>
                <span id="stat-viatom" class="px-3 py-1 rounded border border-indigo-500/30 text-indigo-400 bg-indigo-500/10">O2: SCANNING</span>
            </div>
        </header>

        <div id="scanner-grid" class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-2">
            <div id="scan-polar" class="glass-panel border-t-4 border-t-rose-500"><div class="text-slate-500 text-sm animate-pulse">Scanning for Polar H10...</div></div>
            <div id="scan-air" class="glass-panel border-t-4 border-t-emerald-500"><div class="text-slate-500 text-sm animate-pulse">Scanning for Air Monitors...</div></div>
            <div id="scan-viatom" class="glass-panel border-t-4 border-t-indigo-500"><div class="text-slate-500 text-sm animate-pulse">Scanning for Checkme O2...</div></div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
            <div class="glass-panel text-center flex flex-col justify-center border-l-2 border-indigo-500">
                <div class="text-xs font-bold text-slate-400 tracking-wider">BLOOD OXYGEN (SpO2)</div>
                <div class="data-value text-indigo-400 my-1"><span id="val-spo2">--</span><span class="text-lg text-slate-500 ml-1">%</span></div>
            </div>
            <div class="glass-panel text-center flex flex-col justify-center border-l-2 border-rose-500">
                <div class="text-xs font-bold text-slate-400 tracking-wider">HEART RATE (ECG)</div>
                <div class="data-value text-rose-500 my-1"><span id="val-hr">--</span><span class="text-lg text-slate-500 ml-1">BPM</span></div>
            </div>
            <div class="glass-panel text-center flex flex-col justify-center border-l-2 border-purple-500">
                <div class="text-xs font-bold text-slate-400 tracking-wider">AUTONOMIC STRESS (RMSSD)</div>
                <div class="data-value text-purple-400 my-1"><span id="val-rmssd">--</span><span class="text-lg text-slate-500 ml-1">ms</span></div>
            </div>
            <div class="glass-panel text-center flex flex-col justify-center border-l-2 border-amber-500">
                <div class="text-xs font-bold text-slate-400 tracking-wider">AMBIENT CO2</div>
                <div class="data-value text-amber-400 my-1"><span id="val-co2">--</span><span class="text-lg text-slate-500 ml-1">PPM</span></div>
            </div>
        </div>
        
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div class="glass-panel text-center flex flex-col justify-center border-l-2 border-cyan-500">
                <div class="text-xs font-bold text-slate-400 tracking-wider">PARTICULATE (PM 2.5)</div>
                <div class="data-value text-cyan-400 my-1"><span id="val-pm25">--</span><span class="text-lg text-slate-500 ml-1">µg</span></div>
            </div>
            <div class="glass-panel text-center flex flex-col justify-center border-l-2 border-emerald-500">
                <div class="text-xs font-bold text-slate-400 tracking-wider">ROOM TEMP</div>
                <div class="data-value text-emerald-400 my-1"><span id="val-temp">--</span><span class="text-lg text-slate-500 ml-1">°C</span></div>
            </div>
             <div class="glass-panel text-center flex flex-col justify-center border-l-2 border-blue-500">
                <div class="text-xs font-bold text-slate-400 tracking-wider">VOC INDEX</div>
                <div class="data-value text-blue-400 my-1"><span id="val-voc">--</span><span class="text-lg text-slate-500 ml-1">IDX</span></div>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div class="glass-panel"><div class="text-xs font-bold text-slate-400 tracking-wider mb-2">LIVE ECG WAVEFORM (130Hz)</div><div class="chart-container"><canvas id="ecgChart"></canvas></div></div>
            <div class="glass-panel"><div class="text-xs font-bold text-slate-400 tracking-wider mb-2">CHEST KINEMATICS (X,Y,Z)</div><div class="chart-container"><canvas id="accChart"></canvas></div></div>
        </div>

    </div>

    <script>
        // Charts Initialization
        const chartOpt = { responsive: true, maintainAspectRatio: false, animation: false, scales: { x: { type: 'realtime', realtime: { duration: 5000, refresh: 50, delay: 100 } }, y: { grid: { color: '#334155' }, ticks: { color: '#94a3b8' } } }, plugins: { legend: { display: false } } };
        const ecgChart = new Chart(document.getElementById('ecgChart').getContext('2d'), { type: 'line', data: { datasets: [{ borderColor: '#f43f5e', borderWidth: 2, pointRadius: 0, data: [] }] }, options: chartOpt });
        const accChart = new Chart(document.getElementById('accChart').getContext('2d'), { type: 'line', data: { datasets: [{ borderColor: '#38bdf8', borderWidth: 1.5, pointRadius: 0, data: [] }, { borderColor: '#a78bfa', borderWidth: 1.5, pointRadius: 0, data: [] }, { borderColor: '#fbbf24', borderWidth: 1.5, pointRadius: 0, data: [] } ]}, options: chartOpt });

        // Radar Scanner Logic
        async function fetchScanners() {
            try {
                const res = await fetch('/api/scanners');
                const state = await res.json();
                
                ['polar', 'air', 'viatom'].forEach(type => {
                    const statusEl = document.getElementById(`stat-${type}`);
                    const panel = document.getElementById(`scan-${type}`);
                    const deviceStatus = state.state[type].status;
                    
                    statusEl.innerText = `${type.toUpperCase()}: ${deviceStatus.toUpperCase()}`;
                    
                    if (deviceStatus === "Connected" || deviceStatus === "Connecting") {
                        panel.style.display = "none";
                        statusEl.className = "px-3 py-1 rounded border border-emerald-500/30 text-emerald-400 bg-emerald-500/10";
                    } else {
                        panel.style.display = "block";
                        statusEl.className = "px-3 py-1 rounded border border-rose-500/30 text-rose-400 bg-rose-500/10";
                        
                        let html = `<div class="text-xs font-bold text-slate-400 uppercase mb-3">${type} Radar</div>`;
                        if (state.devices[type].length === 0) {
                            html += `<div class="text-slate-600 text-sm italic">Searching airspace...</div>`;
                        } else {
                            state.devices[type].forEach(d => {
                                html += `<div class="flex justify-between items-center bg-slate-800 p-2 rounded mb-2 border border-slate-700">
                                    <div class="text-sm font-bold text-slate-300 truncate w-32">${d.name}</div>
                                    <button onclick="lockTarget('${type}', '${d.address}')" class="bg-sky-600 hover:bg-sky-500 text-white text-xs px-3 py-1 rounded font-bold transition">LOCK</button>
                                </div>`;
                            });
                        }
                        panel.innerHTML = html;
                    }
                });
            } catch(e) {}
        }
        setInterval(fetchScanners, 2000);

        async function lockTarget(type, mac) {
            await fetch('/api/connect', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({type: type, address: mac}) });
        }

        // Live Data SSE Pipeline
        const evtSource = new EventSource("/api/stream");
        evtSource.onmessage = (e) => {
            const data = JSON.parse(e.data);
            const now = Date.now();
            
            // Polar Updates
            if(data.polar.status === "Connected") {
                if(data.polar.hr > 0) document.getElementById('val-hr').innerText = data.polar.hr;
                document.getElementById('val-rmssd').innerText = data.polar.rmssd.toFixed(1);
                ecgChart.data.datasets[0].data.push({x: now, y: data.polar.ecg_mv});
                accChart.data.datasets[0].data.push({x: now, y: data.polar.acc[0]});
                accChart.data.datasets[1].data.push({x: now, y: data.polar.acc[1]});
                accChart.data.datasets[2].data.push({x: now, y: data.polar.acc[2]});
            }
            
            // Viatom Updates (HR falls back to Viatom if Polar is off)
            if(data.viatom.status === "Connected") {
                if(data.viatom.spo2 > 0) document.getElementById('val-spo2').innerText = data.viatom.spo2;
                if(data.polar.status !== "Connected" && data.viatom.hr > 0) {
                    document.getElementById('val-hr').innerText = data.viatom.hr;
                }
            }
            
            // Air Updates
            if(data.air.status === "Connected") {
                document.getElementById('val-co2').innerText = data.air.co2;
                document.getElementById('val-pm25').innerText = data.air.pm25;
                document.getElementById('val-temp').innerText = data.air.temp;
                document.getElementById('val-voc').innerText = data.air.voc;
            }
        };
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

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
        last_state = None
        while True:
            current_state = json.dumps(omni_state)
            if current_state != last_state:
                yield f"data: {current_state}\n\n"
                last_state = current_state
            time.sleep(0.05)
    return Response(event_stream(), mimetype="text/event-stream")


# ==========================================
# 3. BLUETOOTH ENGINE (ASYNCIO)
# ==========================================

# --- POLAR H10 HANDLERS ---
def polar_hr_cb(data):
    global polar_last_heartbeat
    polar_last_heartbeat = time.time()
    
    bpm = getattr(data, 'bpm', getattr(data, 'heart_rate', 0))
    if isinstance(bpm, (list, tuple)) and len(bpm) > 0: bpm = bpm[0]
    omni_state["polar"]["hr"] = int(bpm) if bpm else omni_state["polar"]["hr"]

    rrs = getattr(data, 'rr_intervals', getattr(data, 'rrs', []))
    if rrs:
        for rr in rrs:
            try:
                rr_val = int(rr)
                if 200 < rr_val < 2000:
                    polar_ppi_history.append(rr_val)
                    if len(polar_ppi_history) > 20: polar_ppi_history.pop(0)
                    if len(polar_ppi_history) > 2:
                        sq_diff = sum((polar_ppi_history[i] - polar_ppi_history[i-1])**2 for i in range(1, len(polar_ppi_history)))
                        omni_state["polar"]["rmssd"] = math.sqrt(sq_diff / (len(polar_ppi_history) - 1))
            except Exception: pass

def polar_acc_cb(data):
    samples = getattr(data, 'samples', getattr(data, 'acc', []))
    if samples:
        val = samples[-1]
        try:
            if isinstance(val, (list, tuple)): omni_state["polar"]["acc"] = [float(val[0]), float(val[1]), float(val[2])]
            elif isinstance(val, dict): omni_state["polar"]["acc"] = [float(val.get('x',0)), float(val.get('y',0)), float(val.get('z',0))]
            else: omni_state["polar"]["acc"] = [float(getattr(val,'x',0)), float(getattr(val,'y',0)), float(getattr(val,'z',0))]
        except Exception: pass

def polar_ecg_cb(data):
    samples = getattr(data, 'samples', getattr(data, 'ecg', []))
    if samples:
        try:
            val = samples[-1]
            uv = int(getattr(val, 'voltage', getattr(val, 'ecg_uv', val)))
            omni_state["polar"]["ecg_mv"] = round(uv / 1000.0, 3)
        except Exception: pass

# --- AIR MONITOR HANDLERS ---
air_buffer = ""
def air_rx_cb(sender, data):
    global air_buffer
    air_buffer += data.decode('utf-8')
    if '\n' in air_buffer:
        lines = air_buffer.split('\n')
        complete = lines[0].strip()
        air_buffer = '\n'.join(lines[1:])
        if complete:
            metrics = complete.split(',')
            if len(metrics) >= 10:
                try:
                    omni_state["air"]["pm25"] = int(metrics[1].strip())
                    omni_state["air"]["temp"] = float(metrics[5].strip())
                    omni_state["air"]["voc"] = int(metrics[6].strip())
                    omni_state["air"]["co2"] = int(metrics[9].strip())
                except Exception: pass

# --- VIATOM HANDLERS ---
def viatom_rx_cb(sender, data):
    if len(data) == 0: return
    if data.hex().upper() == "A5":
        omni_state["viatom"]["status"] = "Calibrating"
        return
    try:
        if len(data) >= 8: omni_state["viatom"]["spo2"] = int(data[7])
        if len(data) >= 9: omni_state["viatom"]["hr"] = int(data[8])
    except Exception: pass


# --- ADAPTIVE OMNI SCANNER ---
async def omni_scanner():
    while True:
        # TRAFFIC CONTROLLER: Halt radar if ANY device is currently handshaking
        is_connecting = any(omni_state[k]["status"] == "Connecting" for k in omni_state)
        
        # BANDWIDTH SAVER: Halt radar completely if Polar is connected
        if is_connecting or omni_state["polar"]["status"] == "Connected":
            await asyncio.sleep(1)
            continue

        needs_scan = False
        for dev_type, mac in active_targets.items():
            if not mac or omni_state[dev_type]["status"] == "Disconnected":
                needs_scan = True
                
        if needs_scan:
            found = {"polar": {}, "air": {}, "viatom": {}}
            
            def scan_cb(device: BLEDevice, adv: AdvertisementData):
                # Cache the physical BLEDevice object instantly!
                ble_device_cache[device.address] = device
                
                name = (device.name or adv.local_name or "").upper()
                addr = device.address.upper()
                uuids = [u.lower() for u in (adv.service_uuids or [])]
                
                info = {"name": device.name or f"Device ({addr[-5:]})", "address": device.address, "rssi": adv.rssi or -100}
                
                if "POLAR H10" in name: found["polar"][addr] = info
                elif "SUPERMINI" in name or "SEN69" in name or "AIR" in name: found["air"][addr] = info
                elif addr == VIATOM_MAC or VIATOM_SVC_UUID.lower() in uuids or any(x in name for x in ["O2", "CHECKME", "VIATOM"]): found["viatom"][addr] = info

            try:
                async with BleakScanner(scan_cb, passive=False):
                    await asyncio.sleep(2.5) 

                for key in discovered_devices:
                    discovered_devices[key] = sorted(list(found[key].values()), key=lambda x: x["rssi"], reverse=True)
            except Exception as e:
                logging.error(f"Radar blocked by OS: {e}")
                
        await asyncio.sleep(2)


# --- CONNECTION WORKERS ---
async def polar_worker():
    global polar_last_heartbeat
    
    def handle_disconnect(client):
        logging.warning("\n⚠️ POLAR DROPPED: Hardware disconnect detected.")
        omni_state["polar"]["status"] = "Disconnected"
        
    while True:
        target_mac = active_targets["polar"]
        if not target_mac or omni_state["polar"]["status"] in ["Connected", "Connecting"]:
            await asyncio.sleep(1); continue
            
        omni_state["polar"]["status"] = "Connecting"
        try:
            # THE FIX: Pull the device directly from the cache! No double-scanning!
            device = ble_device_cache.get(target_mac)
            
            if device:
                async with PolarDevice(device) as p:
                    p.client.set_disconnected_callback(handle_disconnect)
                    omni_state["polar"]["status"] = "Connected"
                    polar_last_heartbeat = time.time()
                    
                    await p.start_hr_stream(polar_hr_cb)
                    await p.start_acc_stream(polar_acc_cb, 200, 16, 8)
                    await p.start_ecg_stream(polar_ecg_cb, 130, 14)
                    
                    while time.time() - polar_last_heartbeat < 10.0 and omni_state["polar"]["status"] == "Connected":
                        await asyncio.sleep(1)
                    
                    logging.warning("\n⚠️ POLAR DROPPED: Watchdog timeout.")
            else:
                logging.warning("Polar device lost from cache. Retrying scan...")
                
        except Exception as e: logging.error(f"Polar Error: {e}")
        
        omni_state["polar"]["status"] = "Disconnected"
        active_targets["polar"] = None
        await asyncio.sleep(2)


async def air_worker():
    def handle_disconnect(client):
        logging.warning("\n⚠️ AIR MONITOR DROPPED: Hardware disconnect detected.")
        omni_state["air"]["status"] = "Disconnected"

    while True:
        target_mac = active_targets["air"]
        if not target_mac or omni_state["air"]["status"] in ["Connected", "Connecting"]:
            await asyncio.sleep(1); continue
            
        omni_state["air"]["status"] = "Connecting"
        try:
            # THE FIX: Connect using the cached device object to skip DBus MAC resolution
            device = ble_device_cache.get(target_mac, target_mac)
            
            async with BleakClient(device, timeout=10.0) as client:
                client.set_disconnected_callback(handle_disconnect)
                omni_state["air"]["status"] = "Connected"
                await client.start_notify(AIR_UART_TX_UUID, air_rx_cb)
                
                while omni_state["air"]["status"] == "Connected": 
                    await asyncio.sleep(1)
        except Exception as e: logging.error(f"Air Error: {e}")
        
        omni_state["air"]["status"] = "Disconnected"
        active_targets["air"] = None
        await asyncio.sleep(2)


async def viatom_worker():
    def handle_disconnect(client):
        logging.warning("\n⚠️ VIATOM DROPPED: Hardware disconnect detected.")
        omni_state["viatom"]["status"] = "Disconnected"

    while True:
        target_mac = active_targets["viatom"]
        if not target_mac or omni_state["viatom"]["status"] in ["Connected", "Connecting", "Calibrating"]:
            await asyncio.sleep(1); continue
            
        omni_state["viatom"]["status"] = "Connecting"
        try:
            # THE FIX: Connect using the cached device object to skip DBus MAC resolution
            device = ble_device_cache.get(target_mac, target_mac)
            
            async with BleakClient(device, timeout=10.0) as client:
                client.set_disconnected_callback(handle_disconnect)
                services = client.services.get_service(VIATOM_SVC_UUID)
                
                target_notify_uuid = next((c.uuid for c in services.characteristics if "notify" in c.properties), "14839ac4-7d7e-415c-9a42-167340cf2339") if services else "14839ac4-7d7e-415c-9a42-167340cf2339"
                target_write_uuid = next((c.uuid for c in services.characteristics if "write" in c.properties), VIATOM_WRITE_UUID) if services else VIATOM_WRITE_UUID

                await client.start_notify(target_notify_uuid, viatom_rx_cb)
                omni_state["viatom"]["status"] = "Connected"
                write_bytes = bytearray([0xAA, 0x17, 0xE8, 0x00, 0x00, 0x00, 0x00, 0x1B])
                
                while omni_state["viatom"]["status"] == "Connected":
                    await client.write_gatt_char(target_write_uuid, write_bytes, response=True)
                    await asyncio.sleep(2)
        except Exception as e: logging.error(f"Viatom Error: {e}")
        
        omni_state["viatom"]["status"] = "Disconnected"
        active_targets["viatom"] = None
        await asyncio.sleep(2)


async def async_master():
    await asyncio.gather(omni_scanner(), polar_worker(), air_worker(), viatom_worker())

def start_ble_engine():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_master())

# ==========================================
# 4. STARTUP SEQUENCE
# ==========================================
if __name__ == "__main__":
    ble_thread = threading.Thread(target=start_ble_engine, daemon=True)
    ble_thread.start()
    logging.info("OMNI-DASH LIVE: http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
