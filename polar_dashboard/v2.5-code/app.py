import asyncio
import os
import glob
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs_advanced")

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polar H10 Multi-Stream Lab</title>
    
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
                <h1 class="text-xl font-bold text-rose-500 tracking-wide">Polar H10 Signal Lab</h1>
                <p class="text-xs text-slate-400 mt-1">ECG (130Hz) | ACC (200Hz) | HRV</p>
            </div>
            <div class="flex items-center gap-4">
                <div class="text-right">
                    <div class="text-2xl font-bold text-rose-500" id="hr-val">-- <span class="text-sm font-light text-slate-500">BPM</span></div>
                </div>
                <span id="status" class="px-3 py-1 rounded-full text-xs font-semibold bg-amber-500/20 text-amber-400 border border-amber-500/30">
                    Awaiting Data...
                </span>
            </div>
        </div>
    </header>

    <main class="container mx-auto px-4 py-6 flex-grow max-w-7xl flex flex-col gap-6">
        
        <div class="bg-slate-800 border border-slate-700 rounded-2xl p-4 shadow-xl w-full">
            <div class="flex justify-between items-center mb-2">
                <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider flex items-center gap-2">
                    <span class="w-2 h-2 rounded-full bg-rose-500 animate-pulse"></span> Electrical Potential (mV)
                </h2>
                <div class="text-xl font-bold text-teal-400"><span id="ecg-val">0.000</span></div>
            </div>
            <div class="relative h-48 w-full"><canvas id="ecgChart"></canvas></div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            
            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-4 shadow-xl w-full">
                <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2 flex items-center gap-2">
                    Kinematics (mg)
                </h2>
                <div class="relative h-48 w-full"><canvas id="accChart"></canvas></div>
            </div>

            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-4 shadow-xl w-full">
                <div class="flex justify-between items-center mb-2">
                    <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider flex items-center gap-2">
                        Heart Rate Variability (R-R)
                    </h2>
                    <div class="text-xl font-bold text-sky-400"><span id="ppi-val">0</span> ms</div>
                </div>
                <div class="relative h-48 w-full"><canvas id="ppiChart"></canvas></div>
            </div>

        </div>
    </main>

    <script>
        // Shared Chart Configuration
        const chartOptions = (delayTime) => ({
            responsive: true, maintainAspectRatio: false, animation: false,
            scales: {
                x: { type: 'realtime', realtime: { duration: 5000, refresh: 40, delay: delayTime } },
                y: { grid: { color: '#334155' }, ticks: { color: '#94a3b8' } }
            },
            plugins: { legend: { display: false } }
        });

        // Initialize ECG Chart
        const ecgChart = new Chart(document.getElementById('ecgChart').getContext('2d'), {
            type: 'line',
            data: { datasets: [{ borderColor: '#f43f5e', borderWidth: 2, pointRadius: 0, data: [] }] },
            options: chartOptions(100)
        });

        // Initialize ACC Chart (3 Lines)
        const accChart = new Chart(document.getElementById('accChart').getContext('2d'), {
            type: 'line',
            data: { 
                datasets: [
                    { label: 'X', borderColor: '#38bdf8', borderWidth: 1.5, pointRadius: 0, data: [] },
                    { label: 'Y', borderColor: '#a78bfa', borderWidth: 1.5, pointRadius: 0, data: [] },
                    { label: 'Z', borderColor: '#fbbf24', borderWidth: 1.5, pointRadius: 0, data: [] }
                ] 
            },
            options: { ...chartOptions(100), plugins: { legend: { display: true, labels: { color: '#94a3b8' } } } }
        });

        // Initialize PPI Chart (Scatter points look better for beat-to-beat variability)
        const ppiChart = new Chart(document.getElementById('ppiChart').getContext('2d'), {
            type: 'line',
            data: { datasets: [{ borderColor: '#38bdf8', backgroundColor: '#38bdf8', borderWidth: 0, pointRadius: 4, data: [] }] },
            options: chartOptions(1000) // Longer delay for lower frequency data
        });

        // WebSocket Routing
        const ws = new WebSocket(`ws://${location.host}/ws`);
        const statusEl = document.getElementById('status');

        ws.onopen = () => { 
            statusEl.innerText = "Connected 🟢"; 
            statusEl.className = "px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30";
        };
        ws.onclose = () => { 
            statusEl.innerText = "Disconnected 🔴"; 
            statusEl.className = "px-3 py-1 rounded-full text-xs font-semibold bg-red-500/20 text-red-400 border border-red-500/30";
        };

        ws.onmessage = (event) => {
            const packet = JSON.parse(event.data);
            
            if (packet.type === 'ecg') {
                ecgChart.data.datasets[0].data.push({ x: packet.ts, y: packet.mv });
                document.getElementById('ecg-val').innerText = packet.mv.toFixed(3);
                if (packet.hr > 0) document.getElementById('hr-val').innerHTML = `${packet.hr} <span class="text-sm font-light text-slate-500">BPM</span>`;
            
            } else if (packet.type === 'acc') {
                accChart.data.datasets[0].data.push({ x: packet.ts, y: packet.x });
                accChart.data.datasets[1].data.push({ x: packet.ts, y: packet.y });
                accChart.data.datasets[2].data.push({ x: packet.ts, y: packet.z });
            
            } else if (packet.type === 'ppi') {
                ppiChart.data.datasets[0].data.push({ x: packet.ts, y: packet.ppi });
                document.getElementById('ppi-val').innerText = packet.ppi;
                if (packet.hr > 0) document.getElementById('hr-val').innerHTML = `${packet.hr} <span class="text-sm font-light text-slate-500">BPM</span>`;
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
    latest_file = get_latest_file(prefix)
    if not latest_file:
        print(f"⚠️ No {stream_type.upper()} log found. Waiting...")
        return

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
                        payload = {"type": "acc", "ts": int(parts[0]), "x": int(parts[1]), "y": int(parts[2]), "z": int(parts[3])}
                    elif stream_type == 'ppi' and len(parts) == 3:
                        payload = {"type": "ppi", "ts": int(parts[0]), "ppi": int(parts[1]), "hr": int(parts[2])}
                    
                    if payload:
                        await manager.broadcast(json.dumps(payload))
                except Exception:
                    pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Spawn three concurrent tailing tasks
    t1 = asyncio.create_task(tail_file_and_broadcast('polar_ecg', 'ecg'))
    t2 = asyncio.create_task(tail_file_and_broadcast('polar_acc', 'acc'))
    t3 = asyncio.create_task(tail_file_and_broadcast('polar_ppi', 'ppi'))
    yield
    print("\nHalting router tasks...")
    t1.cancel(); t2.cancel(); t3.cancel()

app = FastAPI(lifespan=lifespan)

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
