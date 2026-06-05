import asyncio
import os
import glob
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs_advanced")
DEVICES_FILE = os.path.join(LOGS_DIR, "devices.json")
COMMAND_FILE = os.path.join(LOGS_DIR, "command.json")

class ConnectRequest(BaseModel):
    address: str

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polar H10 Advanced Research Lab</title>
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
                <h1 class="text-xl font-bold text-rose-500 tracking-wide">Polar H10 Biometric Lab</h1>
                <p class="text-xs text-slate-400 mt-1">ECG (130Hz) | ACC (200Hz) | Live HRV</p>
            </div>
            <span id="status" class="px-3 py-1 rounded-full text-xs font-semibold bg-amber-500/20 text-amber-400 border border-amber-500/30">
                Awaiting Data...
            </span>
        </div>
    </header>

    <main class="container mx-auto px-4 py-6 flex-grow max-w-7xl flex flex-col gap-6">
        
        <div id="scanner-panel" class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl w-full transition-all duration-500">
            <h2 class="text-lg font-bold text-slate-200 mb-4 flex items-center gap-3">
                <span class="relative flex h-3 w-3">
                  <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-sky-400 opacity-75"></span>
                  <span class="relative inline-flex rounded-full h-3 w-3 bg-sky-500"></span>
                </span>
                Scanning for Polar Units...
            </h2>
            <div id="device-list" class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-4">
                <div class="text-slate-500 text-sm animate-pulse">Initializing Bluetooth Radar...</div>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl text-center flex flex-col justify-center">
                <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">Heart Rate</h2>
                <div class="text-6xl font-extrabold text-rose-500 my-2 tracking-tight">
                    <span id="hr-val">--</span><span class="text-2xl font-light text-slate-500 ml-2">BPM</span>
                </div>
            </div>
            
            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl text-center flex flex-col justify-center">
                <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">ECG Potential</h2>
                <div class="text-6xl font-extrabold text-teal-400 my-2 tracking-tight">
                    <span id="ecg-val">0.00</span><span class="text-2xl font-light text-slate-500 ml-2">mV</span>
                </div>
            </div>

            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl text-center flex flex-col justify-center">
                <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">Autonomic Stress (RMSSD)</h2>
                <div class="text-6xl font-extrabold text-indigo-400 my-2 tracking-tight">
                    <span id="rmssd-val">--</span><span class="text-2xl font-light text-slate-500 ml-2">ms</span>
                </div>
                <div id="stress-label" class="text-xs font-bold mt-1 text-slate-500 animate-pulse">GATHERING BEATS...</div>
            </div>
        </div>

        <div class="bg-slate-800 border border-slate-700 rounded-2xl p-4 shadow-xl w-full">
            <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">Live PQRST Waveform</h2>
            <div class="relative h-48 w-full"><canvas id="ecgChart"></canvas></div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-4 shadow-xl w-full">
                <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">Chest Kinematics (mg)</h2>
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

        let uiIsConnected = false;
        
        async function updateScannerList() {
            if (uiIsConnected) return; 
            
            try {
                const response = await fetch('/api/scan-results');
                const devices = await response.json();
                const container = document.getElementById('device-list');
                container.innerHTML = '';
                
                if (devices.length === 0) {
                    container.innerHTML = '<div class="text-slate-500 text-sm animate-pulse col-span-3">No active straps detected... ensure pads are wet.</div>';
                    return;
                }
                
                devices.forEach(d => {
                    const div = document.createElement('div');
                    div.className = 'bg-slate-700/50 p-4 rounded-xl border border-slate-600 flex flex-col gap-3 justify-between';
                    div.innerHTML = `
                        <div>
                            <div class="font-bold text-slate-200">${d.name}</div>
                            <div class="text-xs text-slate-400 font-mono">${d.address}</div>
                        </div>
                        <button class="bg-sky-600 hover:bg-sky-500 text-white text-sm font-bold py-2 px-4 rounded w-full transition-colors" 
                                onclick="connectDevice('${d.address}')">
                            Connect Signal
                        </button>
                    `;
                    container.appendChild(div);
                });
            } catch (err) {}
        }
        
        async function connectDevice(mac) {
            document.getElementById('device-list').innerHTML = `<div class="text-emerald-400 font-bold py-4 col-span-3 animate-pulse">Handshaking with ${mac}...</div>`;
            await fetch('/api/connect', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({address: mac})
            });
        }
        
        setInterval(updateScannerList, 3000);

        const ws = new WebSocket(`ws://${location.host}/ws`);
        const statusEl = document.getElementById('status');
        const scannerPanel = document.getElementById('scanner-panel');
        
        let ppiHistory = []; 
        let lastEcgTs = 0;
        let lastAccTs = 0;

        ws.onopen = () => { 
            statusEl.innerText = "System Online"; 
        };
        
        ws.onclose = () => { 
            statusEl.innerText = "Disconnected 🔴"; 
            statusEl.className = "px-3 py-1 rounded-full text-xs font-semibold bg-red-500/20 text-red-400 border border-red-500/30";
            uiIsConnected = false;
            scannerPanel.style.display = 'block'; 
        };

        ws.onmessage = (event) => {
            if (!uiIsConnected) {
                uiIsConnected = true;
                scannerPanel.style.display = 'none';
                statusEl.innerText = "Telemetry Active 🟢"; 
                statusEl.className = "px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30";
            }

            const packet = JSON.parse(event.data);
            
            if (packet.type === 'ecg') {
                if (lastEcgTs === 0 || Math.abs(packet.ts - lastEcgTs) > 2000) {
                    lastEcgTs = packet.ts;
                } else {
                    lastEcgTs += 7.69;
                }
                ecgChart.data.datasets[0].data.push({ x: lastEcgTs, y: packet.mv });
                document.getElementById('ecg-val').innerText = packet.mv.toFixed(2);
                if (packet.hr > 0) document.getElementById('hr-val').innerText = packet.hr;
                
            } else if (packet.type === 'acc') {
                if (lastAccTs === 0 || Math.abs(packet.ts - lastAccTs) > 2000) {
                    lastAccTs = packet.ts;
                } else {
                    lastAccTs += 5.0;
                }
                accChart.data.datasets[0].data.push({ x: lastAccTs, y: packet.x });
                accChart.data.datasets[1].data.push({ x: lastAccTs, y: packet.y });
                accChart.data.datasets[2].data.push({ x: lastAccTs, y: packet.z });
                
            } else if (packet.type === 'ppi') {
                ppiChart.data.datasets[0].data.push({ x: packet.ts, y: packet.ppi });
                if (packet.hr > 0) document.getElementById('hr-val').innerText = packet.hr;
                
                ppiHistory.push(packet.ppi);
                if (ppiHistory.length > 20) ppiHistory.shift();
                
                if (ppiHistory.length > 2) {
                    let sumSquaredDiffs = 0;
                    for (let i = 1; i < ppiHistory.length; i++) {
                        let diff = ppiHistory[i] - ppiHistory[i-1];
                        sumSquaredDiffs += (diff * diff);
                    }
                    let rmssd = Math.sqrt(sumSquaredDiffs / (ppiHistory.length - 1));
                    
                    document.getElementById('rmssd-val').innerText = rmssd.toFixed(1);
                    let labelEl = document.getElementById('stress-label');
                    labelEl.classList.remove("animate-pulse");
                    
                    if (rmssd < 20) {
                        labelEl.innerText = "HIGH STRESS (SYMPATHETIC)";
                        labelEl.className = "text-xs font-bold mt-1 text-rose-500";
                    } else if (rmssd < 50) {
                        labelEl.innerText = "MODERATE (BALANCED)";
                        labelEl.className = "text-xs font-bold mt-1 text-amber-400";
                    } else {
                        labelEl.innerText = "RELAXED (PARASYMPATHETIC)";
                        labelEl.className = "text-xs font-bold mt-1 text-emerald-400";
                    }
                }
            }
        };
    </script>
</body>
</html>
"""

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try: await connection.send_text(message)
            except Exception: pass

manager = ConnectionManager()

def get_latest_file(prefix):
    if not os.path.exists(LOGS_DIR): return None
    files = glob.glob(os.path.join(LOGS_DIR, f'{prefix}_*.csv'))
    return max(files, key=os.path.getctime) if files else None

async def tail_file_and_broadcast(prefix, stream_type):
    latest_file = None
    
    # Loop patiently until the hardware engine actually creates the file
    while not latest_file:
        latest_file = get_latest_file(prefix)
        if not latest_file:
            await asyncio.sleep(1)

    print(f"📡 Router locked onto {stream_type.upper()}: {latest_file}")
    
    with open(latest_file, 'r') as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                await asyncio.sleep(0.01)
                continue
            
            clean_line = line.strip()
            if clean_line and "Timestamp" not in clean_line:
                parts = clean_line.split(',')
                try:
                    payload = None
                    if stream_type == 'ecg' and len(parts) == 3:
                        payload = {"type": "ecg", "ts": int(parts[0]), "mv": float(parts[1]), "hr": int(parts[2])}
                    elif stream_type == 'acc' and len(parts) == 4:
                        payload = {"type": "acc", "ts": int(parts[0]), "x": float(parts[1]), "y": float(parts[2]), "z": float(parts[3])}
                    elif stream_type == 'ppi' and len(parts) == 3:
                        payload = {"type": "ppi", "ts": int(parts[0]), "ppi": int(parts[1]), "hr": int(parts[2])}
                    
                    if payload:
                        await manager.broadcast(json.dumps(payload))
                except Exception:
                    pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.path.exists(COMMAND_FILE):
        try: os.remove(COMMAND_FILE)
        except Exception: pass
        
    t1 = asyncio.create_task(tail_file_and_broadcast('polar_ecg', 'ecg'))
    t2 = asyncio.create_task(tail_file_and_broadcast('polar_acc', 'acc'))
    t3 = asyncio.create_task(tail_file_and_broadcast('polar_ppi', 'ppi'))
    yield
    t1.cancel(); t2.cancel(); t3.cancel()

# --- APP INITIALIZATION ---
app = FastAPI(lifespan=lifespan)

# --- ENDPOINTS ---
@app.get("/api/scan-results")
def get_scan_results():
    if os.path.exists(DEVICES_FILE):
        try:
            with open(DEVICES_FILE, 'r') as f: 
                return json.load(f)
        except Exception: pass
    return []

@app.post("/api/connect")
def connect_device(req: ConnectRequest):
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(COMMAND_FILE, 'w') as f:
        json.dump({"action": "connect", "address": req.address}, f)
    return {"status": "command_sent"}

@app.get("/")
async def get(): return HTMLResponse(HTML_TEMPLATE)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5001, reload=False)
