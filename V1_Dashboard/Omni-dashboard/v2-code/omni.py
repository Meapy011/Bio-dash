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
    "polar": {"status": "Disconnected", "hr": 0, "rr": 0, "rmssd": 0.0, "sdnn": 0.0, "ecg_buffer": [], "acc_buffer": []},
    "viatom": {"status": "Disconnected", "spo2": 0, "hr": 0}
}

discovered_devices = {"polar": [], "viatom": []}
active_targets = {"polar": None, "viatom": None}

# THE FIX: Cache the physical device object to bypass DBus lookup errors
ble_device_cache = {}

polar_ppi_history = []
polar_last_heartbeat = 0

# Hardware UUIDs
VIATOM_MAC = "F3:A0:A8:E3:F5:63"
VIATOM_SVC_UUID = "14839ac4-7d7e-415c-9a42-167340cf2339"
VIATOM_WRITE_UUID = "8b00ace7-eb0a-49b0-b977-10a8d4d5e82f"

# ==========================================
# 2. FLASK WEB DASHBOARD (6-CARD TAILWIND)
# ==========================================
app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bio-Dash Advanced Lab</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@3.9.1/dist/chart.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/luxon@3.0.1/build/global/luxon.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-luxon@1.2.0/dist/chartjs-adapter-luxon.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-streaming@2.0.0/dist/chartjs-plugin-streaming.min.js"></script>
</head>
<body class="bg-slate-900 text-white font-sans min-h-screen flex flex-col">

    <header class="p-4 bg-slate-800 border-b border-slate-700 shadow-md">
        <div class="container mx-auto flex justify-between items-center max-w-7xl">
            <div>
                <h1 class="text-xl font-bold text-sky-400 tracking-wide flex items-center gap-3">
                    <span class="relative flex h-3 w-3">
                        <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-sky-400 opacity-75"></span>
                        <span class="relative inline-flex rounded-full h-3 w-3 bg-sky-500"></span>
                    </span>
                    Bio-Dash Omni-System
                </h1>
                <p class="text-xs text-slate-400 mt-1">Dual-Threaded Hardware Telemetry</p>
            </div>
            <div class="flex gap-2">
                <span id="stat-polar" class="px-3 py-1 rounded-full text-xs font-semibold bg-amber-500/20 text-amber-400 border border-amber-500/30">POLAR: AWAITING</span>
                <span id="stat-viatom" class="px-3 py-1 rounded-full text-xs font-semibold bg-amber-500/20 text-amber-400 border border-amber-500/30">O2: AWAITING</span>
            </div>
        </div>
    </header>

    <main class="container mx-auto px-4 py-6 flex-grow max-w-7xl flex flex-col gap-6">

        <div id="scanner-panel" class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl w-full transition-all duration-500">
            <h2 class="text-lg font-bold text-slate-200 mb-4">Radar Active: Scanning Airspace...</h2>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div id="wrapper-polar">
                    <h3 class="text-sm font-bold text-rose-400 mb-2 uppercase tracking-wider">Polar H10</h3>
                    <div id="list-polar" class="flex flex-col gap-3">
                        <div class="text-slate-500 text-sm animate-pulse">Initializing Bluetooth Radar...</div>
                    </div>
                </div>
                <div id="wrapper-viatom">
                    <h3 class="text-sm font-bold text-indigo-400 mb-2 uppercase tracking-wider">Checkme O2</h3>
                    <div id="list-viatom" class="flex flex-col gap-3">
                        <div class="text-slate-500 text-sm animate-pulse">Initializing Bluetooth Radar...</div>
                    </div>
                </div>
            </div>
        </div>

        <div class="grid grid-cols-2 lg:grid-cols-6 gap-6">
            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl text-center flex flex-col justify-center border-b-4 border-indigo-500">
                <h2 class="text-xs lg:text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">O2 Saturation</h2>
                <div class="text-4xl lg:text-5xl font-extrabold text-indigo-400 my-2 tracking-tight">
                    <span id="val-spo2">--</span><span class="text-xl font-light text-slate-500 ml-1">%</span>
                </div>
            </div>
            
            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl text-center flex flex-col justify-center border-b-4 border-cyan-500">
                <h2 class="text-xs lg:text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">Ring HR</h2>
                <div class="text-4xl lg:text-5xl font-extrabold text-cyan-400 my-2 tracking-tight">
                    <span id="val-viatom-hr">--</span><span class="text-xl font-light text-slate-500 ml-1">BPM</span>
                </div>
            </div>

            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl text-center flex flex-col justify-center border-b-4 border-rose-500">
                <h2 class="text-xs lg:text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">ECG HR</h2>
                <div class="text-4xl lg:text-5xl font-extrabold text-rose-500 my-2 tracking-tight">
                    <span id="val-polar-hr">--</span><span class="text-xl font-light text-slate-500 ml-1">BPM</span>
                </div>
            </div>

            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl text-center flex flex-col justify-center border-b-4 border-emerald-500">
                <h2 class="text-xs lg:text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">RR Interval</h2>
                <div class="text-4xl lg:text-5xl font-extrabold text-emerald-400 my-2 tracking-tight">
                    <span id="val-rr">--</span><span class="text-xl font-light text-slate-500 ml-1">ms</span>
                </div>
            </div>

            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl text-center flex flex-col justify-center relative border-b-4 border-purple-500">
                <h2 class="text-xs lg:text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">HRV (RMSSD)</h2>
                <div class="text-4xl lg:text-5xl font-extrabold text-purple-400 my-2 tracking-tight">
                    <span id="val-rmssd">--</span><span class="text-xl font-light text-slate-500 ml-1">ms</span>
                </div>
                <div id="stress-label" class="text-[10px] font-bold mt-1 text-slate-500 animate-pulse absolute bottom-2 w-full left-0">GATHERING BEATS...</div>
            </div>

            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl text-center flex flex-col justify-center relative border-b-4 border-violet-500">
                <h2 class="text-xs lg:text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">Stress (SDNN)</h2>
                <div class="text-4xl lg:text-5xl font-extrabold text-violet-400 my-2 tracking-tight">
                    <span id="val-sdnn">--</span><span class="text-xl font-light text-slate-500 ml-1">ms</span>
                </div>
            </div>
        </div>

        <div class="bg-slate-800 border border-slate-700 rounded-2xl p-4 shadow-xl w-full">
            <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">Live PQRST Waveform (130Hz)</h2>
            <div class="relative h-48 w-full"><canvas id="ecgChart"></canvas></div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-4 shadow-xl w-full">
                <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">Chest Kinematics (X,Y,Z)</h2>
                <div class="relative h-48 w-full"><canvas id="accChart"></canvas></div>
            </div>

            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-4 shadow-xl w-full">
                <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">Heart Rate Variability (R-R)</h2>
                <div class="relative h-48 w-full"><canvas id="ppiChart"></canvas></div>
            </div>
        </div>
    </main>

    <script>
        const chartOptions = (delayTime) => ({
            responsive: true, maintainAspectRatio: false, animation: false,
            scales: {
                x: { type: 'realtime', realtime: { duration: 5000, refresh: 40, delay: delayTime } },
                y: { grid: { color: '#334155' }, ticks: { color: '#94a3b8' } }
            },
            plugins: { legend: { display: false } }
        });

        const ecgChart = new Chart(document.getElementById('ecgChart').getContext('2d'), {
            type: 'line', data: { datasets: [{ borderColor: '#f43f5e', borderWidth: 2, pointRadius: 0, data: [] }] },
            options: chartOptions(100)
        });

        const accChart = new Chart(document.getElementById('accChart').getContext('2d'), {
            type: 'line', data: { datasets: [
                { label: 'X', borderColor: '#38bdf8', borderWidth: 1.5, pointRadius: 0, data: [] },
                { label: 'Y', borderColor: '#a78bfa', borderWidth: 1.5, pointRadius: 0, data: [] },
                { label: 'Z', borderColor: '#fbbf24', borderWidth: 1.5, pointRadius: 0, data: [] }
            ]},
            options: { ...chartOptions(100), plugins: { legend: { display: true, labels: { color: '#94a3b8' } } } }
        });

        const ppiChart = new Chart(document.getElementById('ppiChart').getContext('2d'), {
            type: 'line', data: { datasets: [{ borderColor: '#818cf8', backgroundColor: '#818cf8', borderWidth: 0, pointRadius: 4, data: [] }] },
            options: chartOptions(1000)
        });

        async function fetchScanners() {
            try {
                const res = await fetch('/api/scanners');
                const state = await res.json();
                
                let anyScanning = false;

                ['polar', 'viatom'].forEach(type => {
                    const statusEl = document.getElementById(`stat-${type}`);
                    const wrapper = document.getElementById(`wrapper-${type}`);
                    const list = document.getElementById(`list-${type}`);
                    const deviceStatus = state.state[type].status;
                    
                    statusEl.innerText = `${type.toUpperCase()}: ${deviceStatus.toUpperCase()}`;
                    
                    if (deviceStatus === "Connected") {
                        statusEl.className = "px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30";
                        wrapper.style.display = "none";
                    } else if (deviceStatus === "Connecting" || deviceStatus === "Calibrating") {
                        statusEl.className = "px-3 py-1 rounded-full text-xs font-semibold bg-sky-500/20 text-sky-400 border border-sky-500/30";
                        wrapper.style.display = "none";
                    } else {
                        anyScanning = true;
                        wrapper.style.display = "block";
                        statusEl.className = "px-3 py-1 rounded-full text-xs font-semibold bg-amber-500/20 text-amber-400 border border-amber-500/30";
                        
                        list.innerHTML = '';
                        if (state.devices[type].length === 0) {
                            list.innerHTML = `<div class="text-slate-500 text-sm italic py-2">Searching airspace...</div>`;
                        } else {
                            state.devices[type].forEach(d => {
                                const div = document.createElement('div');
                                div.className = 'bg-slate-700/50 p-4 rounded-xl border border-slate-600 flex justify-between items-center';
                                div.innerHTML = `
                                    <div>
                                        <div class="font-bold text-slate-200 text-sm">${d.name}</div>
                                        <div class="text-[10px] text-slate-400 font-mono mt-1">${d.address}</div>
                                    </div>
                                    <button class="bg-sky-600 hover:bg-sky-500 text-white text-xs font-bold py-2 px-5 rounded transition-colors" 
                                            onclick="lockTarget('${type}', '${d.address}')">
                                        LOCK
                                    </button>
                                `;
                                list.appendChild(div);
                            });
                        }
                    }
                });

                document.getElementById('scanner-panel').style.display = anyScanning ? "block" : "none";

            } catch(e) {}
        }
        setInterval(fetchScanners, 2000);

        async function lockTarget(type, mac) {
            document.getElementById(`list-${type}`).innerHTML = `<div class="text-emerald-400 font-bold py-4 animate-pulse">Handshaking with ${mac}...</div>`;
            await fetch('/api/connect', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({type: type, address: mac}) });
        }

        const evtSource = new EventSource("/api/stream");
        evtSource.onmessage = (e) => {
            const data = JSON.parse(e.data);
            const now = Date.now();
            
            if(data.polar.status === "Connected") {
                if(data.polar.hr > 0) document.getElementById('val-polar-hr').innerText = data.polar.hr;
                if(data.polar.rr > 0) {
                    document.getElementById('val-rr').innerText = data.polar.rr;
                    ppiChart.data.datasets[0].data.push({x: now, y: data.polar.rr});
                }
                
                let rmssd = data.polar.rmssd;
                document.getElementById('val-rmssd').innerText = rmssd.toFixed(1);
                
                let labelEl = document.getElementById('stress-label');
                if (rmssd > 0) {
                    labelEl.classList.remove("animate-pulse");
                    if (rmssd < 20) {
                        labelEl.innerText = "HIGH STRESS (SYMPATHETIC)";
                        labelEl.className = "text-[10px] font-bold mt-1 text-rose-500 absolute bottom-2 w-full left-0";
                    } else if (rmssd < 50) {
                        labelEl.innerText = "MODERATE (BALANCED)";
                        labelEl.className = "text-[10px] font-bold mt-1 text-amber-400 absolute bottom-2 w-full left-0";
                    } else {
                        labelEl.innerText = "RELAXED (PARASYMPATHETIC)";
                        labelEl.className = "text-[10px] font-bold mt-1 text-emerald-400 absolute bottom-2 w-full left-0";
                    }
                }

                document.getElementById('val-sdnn').innerText = data.polar.sdnn.toFixed(1);
                
                if (data.polar.ecg_buffer && data.polar.ecg_buffer.length > 0) {
                    let startT = now - (data.polar.ecg_buffer.length * 7.69); // 130Hz spacing
                    data.polar.ecg_buffer.forEach((pt, i) => {
                        ecgChart.data.datasets[0].data.push({x: startT + (i * 7.69), y: pt});
                    });
                }
                
                if (data.polar.acc_buffer && data.polar.acc_buffer.length > 0) {
                    let startT = now - (data.polar.acc_buffer.length * 5.0); // 200Hz spacing
                    data.polar.acc_buffer.forEach((pt, i) => {
                        let t = startT + (i * 5.0);
                        accChart.data.datasets[0].data.push({x: t, y: pt[0]});
                        accChart.data.datasets[1].data.push({x: t, y: pt[1]});
                        accChart.data.datasets[2].data.push({x: t, y: pt[2]});
                    });
                }
            }
            
            if(data.viatom.status === "Connected") {
                if(data.viatom.spo2 > 0) document.getElementById('val-spo2').innerText = data.viatom.spo2;
                if(data.viatom.hr > 0) document.getElementById('val-viatom-hr').innerText = data.viatom.hr;
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
        while True:
            # Safely capture the buffers then clear them for the next frame
            payload = json.dumps(omni_state)
            omni_state["polar"]["ecg_buffer"] = []
            omni_state["polar"]["acc_buffer"] = []
            
            yield f"data: {payload}\n\n"
            time.sleep(0.05)
    return Response(event_stream(), mimetype="text/event-stream")


# ==========================================
# 3. BLUETOOTH ENGINE (ASYNCIO)
# ==========================================

def polar_hr_cb(data):
    global polar_last_heartbeat
    polar_last_heartbeat = time.time()
    
    bpm = getattr(data, 'bpm', None) or getattr(data, 'heart_rate', None)
    if bpm is None and isinstance(data, (list, tuple)) and len(data) > 0: bpm = data[0]
    if bpm is None and hasattr(data, '__dict__'):
        for val in data.__dict__.values():
            if isinstance(val, int) and 30 < val < 220:
                bpm = val
                break
    if bpm is not None: omni_state["polar"]["hr"] = int(bpm)

    rr_list = getattr(data, 'rr_intervals', []) or getattr(data, 'rrs', []) or getattr(data, 'rrs_ms', [])
    if not rr_list and hasattr(data, '__dict__'):
        for val in data.__dict__.values():
            if isinstance(val, list):
                rr_list = val
                break
                
    if rr_list:
        for rr in rr_list:
            try:
                rr_val = int(rr)
                if 200 < rr_val < 2000:
                    omni_state["polar"]["rr"] = rr_val
                    polar_ppi_history.append(rr_val)
                    
                    if len(polar_ppi_history) > 40: 
                        polar_ppi_history.pop(0)
                        
                    if len(polar_ppi_history) > 2:
                        sq_diff = sum((polar_ppi_history[i] - polar_ppi_history[i-1])**2 for i in range(1, len(polar_ppi_history)))
                        omni_state["polar"]["rmssd"] = math.sqrt(sq_diff / (len(polar_ppi_history) - 1))
                        
                        mean_rr = sum(polar_ppi_history) / len(polar_ppi_history)
                        variance = sum((x - mean_rr)**2 for x in polar_ppi_history) / (len(polar_ppi_history) - 1)
                        omni_state["polar"]["sdnn"] = math.sqrt(variance)
            except Exception: pass

def polar_acc_cb(data):
    samples = getattr(data, 'samples', []) or getattr(data, 'acc', [])
    if not samples and hasattr(data, '__dict__'):
        for val in data.__dict__.values():
            if isinstance(val, list):
                if len(val) > 0 and isinstance(val[0], int) and val[0] > 1000000000000000: continue
                samples = val
                break
    if not samples: return
    
    new_acc = []
    for val in samples:
        try:
            if isinstance(val, (list, tuple)) and len(val) >= 3:
                new_acc.append([float(val[0]), float(val[1]), float(val[2])])
            elif isinstance(val, dict):
                new_acc.append([float(val.get('x', val.get('X', 0))), float(val.get('y', val.get('Y', 0))), float(val.get('z', val.get('Z', 0)))])
            else:
                new_acc.append([float(getattr(val, 'x', 0)), float(getattr(val, 'y', 0)), float(getattr(val, 'z', 0))])
        except Exception: pass
    
    if new_acc:
        omni_state["polar"]["acc_buffer"].extend(new_acc)

def polar_ecg_cb(data):
    samples = getattr(data, 'samples', []) or getattr(data, 'ecg', []) or getattr(data, 'voltages', [])
    if not samples and hasattr(data, '__dict__'):
        for key, val in data.__dict__.items():
            if isinstance(val, list) and len(val) > 0:
                if isinstance(val[0], int) and val[0] > 1000000000000000: continue
                samples = val
                break
    if not samples: return

    new_ecg = []
    for val in samples:
        try:
            uv_value = int(getattr(val, 'voltage', getattr(val, 'ecg_uv', val)))
            new_ecg.append(round(uv_value / 1000.0, 3))
        except Exception: pass

    if new_ecg:
        omni_state["polar"]["ecg_buffer"].extend(new_ecg)
        sys.stdout.write(f"\r[ POLAR ] ❤️ HR: {omni_state['polar']['hr']:3d} | ⚡ ECG: {new_ecg[-1]:>6.3f} mV    ")
        sys.stdout.flush()

def viatom_rx_cb(sender, data):
    if len(data) == 0: return
    
    raw_hex = data.hex().upper()
    if raw_hex == "A5":
        omni_state["viatom"]["status"] = "Calibrating"
        return
        
    try:
        if len(data) >= 9: 
            spo2_val = int(data[7])
            hr_val = int(data[8])
            
            omni_state["viatom"]["spo2"] = spo2_val
            omni_state["viatom"]["hr"] = hr_val
    except Exception: pass


async def omni_scanner():
    while True:
        is_connecting = any(omni_state[k]["status"] == "Connecting" for k in omni_state)
        
        if is_connecting or omni_state["polar"]["status"] == "Connected":
            await asyncio.sleep(1)
            continue

        needs_scan = False
        for dev_type, mac in active_targets.items():
            if not mac or omni_state[dev_type]["status"] == "Disconnected":
                needs_scan = True
                
        if needs_scan:
            found = {"polar": {}, "viatom": {}}
            
            def scan_cb(device: BLEDevice, adv: AdvertisementData):
                # THE FIX: Cache the physical object instantly
                ble_device_cache[device.address] = device
                
                name = (device.name or adv.local_name or "").upper()
                addr = device.address.upper()
                uuids = [u.lower() for u in (adv.service_uuids or [])]
                
                info = {"name": device.name or f"Device ({addr[-5:]})", "address": device.address, "rssi": adv.rssi or -100}
                
                if "POLAR H10" in name: found["polar"][addr] = info
                elif addr == VIATOM_MAC or VIATOM_SVC_UUID.lower() in uuids or any(x in name for x in ["O2", "CHECKME", "VIATOM"]): found["viatom"][addr] = info

            try:
                async with BleakScanner(scan_cb, passive=False):
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
            # THE FIX: Pull the actual device object from the cache instead of asking DBus to find it
            device = ble_device_cache.get(target_mac)
            
            if device:
                async with PolarDevice(device) as p:
                    omni_state["polar"]["status"] = "Connected"
                    polar_last_heartbeat = time.time()
                    
                    logging.info("Polar handshake complete. Activating Heart Rate Matrix...")
                    try:
                        await p.start_hr_stream(polar_hr_cb)
                    except Exception as e:
                        logging.error(f"[DIAGNOSTIC] HR Stream Error: {e}")
                    
                    await asyncio.sleep(1.5) 
                    
                    logging.info("Activating Kinematics...")
                    try:
                        try:
                            await p.start_acc_stream(polar_acc_cb, 200, 16, 8)
                        except TypeError:
                            await p.start_acc_stream(polar_acc_cb)
                    except Exception as e:
                        logging.error(f"[DIAGNOSTIC] ACC Stream Error: {e}")
                        
                    await asyncio.sleep(1.5)

                    logging.info("Activating ECG...")
                    try:
                        try:
                            await p.start_ecg_stream(polar_ecg_cb, 130, 14)
                        except TypeError:
                            await p.start_ecg_stream(polar_ecg_cb)
                    except Exception as e:
                        logging.error(f"[DIAGNOSTIC] ECG Stream Error: {e}")
                    
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
    def handle_disconnect(client):
        logging.warning("\n⚠️ VIATOM DROPPED: Hardware disconnect detected.")
        omni_state["viatom"]["status"] = "Disconnected"

    while True:
        target_mac = active_targets["viatom"]
        if not target_mac or omni_state["viatom"]["status"] in ["Connected", "Connecting", "Calibrating"]:
            await asyncio.sleep(1); continue
            
        omni_state["viatom"]["status"] = "Connecting"
        try:
            device = ble_device_cache.get(target_mac, target_mac)
            
            async with BleakClient(device, timeout=15.0, disconnected_callback=handle_disconnect) as client:
                omni_state["viatom"]["status"] = "Connected"
                
                logging.info("Viatom connected. Waiting 3s for GATT table to boot...")
                await asyncio.sleep(3.0) 
                
                try:
                    await client.clear_cache()
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

                services = client.services.get_service(VIATOM_SVC_UUID)
                
                target_notify_uuid = next((c.uuid for c in services.characteristics if "notify" in c.properties), "14839ac4-7d7e-415c-9a42-167340cf2339") if services else "14839ac4-7d7e-415c-9a42-167340cf2339"
                target_write_uuid = next((c.uuid for c in services.characteristics if "write" in c.properties or "write-without-response" in c.properties), VIATOM_WRITE_UUID) if services else VIATOM_WRITE_UUID

                try:
                    await client.start_notify(target_notify_uuid, viatom_rx_cb)
                except Exception as e:
                    logging.error(f"Viatom GATT resolution failed. Restarting link: {e}")
                    omni_state["viatom"]["status"] = "Disconnected"
                    continue 

                write_bytes = bytearray([0xAA, 0x17, 0xE8, 0x00, 0x00, 0x00, 0x00, 0x1B])
                
                while omni_state["viatom"]["status"] == "Connected":
                    try:
                        try:
                            await client.write_gatt_char(target_write_uuid, write_bytes, response=True)
                        except Exception:
                            await client.write_gatt_char(target_write_uuid, write_bytes, response=False)
                    except Exception as write_err:
                        pass
                    await asyncio.sleep(2)
        except Exception as e: 
            logging.error(f"Viatom Error: {repr(e)}")
        
        omni_state["viatom"]["status"] = "Disconnected"
        active_targets["viatom"] = None
        await asyncio.sleep(2)


async def async_master():
    await asyncio.gather(omni_scanner(), polar_worker(), viatom_worker())

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
