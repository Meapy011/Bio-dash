import asyncio
import os
import glob
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
    <title>Polar H10 Live Telemetry</title>
    
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@3.9.1/dist/chart.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/luxon@3.0.1/build/global/luxon.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-luxon@1.2.0/dist/chartjs-adapter-luxon.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-streaming@2.0.0/dist/chartjs-plugin-streaming.min.js"></script>
</head>
<body class="bg-slate-900 text-white font-sans min-h-screen flex flex-col justify-between">

    <header class="p-6 bg-slate-800 border-b border-slate-700 shadow-md">
        <div class="container mx-auto flex justify-between items-center max-w-5xl">
            <div>
                <h1 class="text-2xl font-bold text-rose-500 tracking-wide">Polar H10 Signal Lab</h1>
                <p class="text-xs text-slate-400 mt-1">High-Fidelity ECG Telemetry (130Hz)</p>
            </div>
            <span id="status" class="px-3 py-1 rounded-full text-xs font-semibold bg-amber-500/20 text-amber-400 border border-amber-500/30 transition-colors duration-300">
                Awaiting Data...
            </span>
        </div>
    </header>

    <main class="container mx-auto px-4 py-8 flex-grow max-w-5xl flex flex-col gap-6">
        
        <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl w-full">
            <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider mb-4 flex items-center gap-2">
                <span class="w-2 h-2 rounded-full bg-rose-500 animate-pulse"></span>
                Real-Time PQRST Waveform
            </h2>
            <div class="relative h-72 w-full">
                <canvas id="ecgChart"></canvas>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl text-center flex flex-col justify-center">
                <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">Electrical Potential</h2>
                <div class="text-6xl font-extrabold text-teal-400 my-2 tracking-tight">
                    <span id="ecg-val">0.000</span><span class="text-2xl font-light text-slate-500 ml-2">mV</span>
                </div>
            </div>

            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 shadow-xl text-center flex flex-col justify-center relative overflow-hidden">
                <h2 class="text-sm font-medium text-slate-400 uppercase tracking-wider mb-2">Heart Rate</h2>
                <div class="text-6xl font-extrabold text-rose-500 my-2 tracking-tight">
                    <span id="hr-val">--</span><span class="text-2xl font-light text-slate-500 ml-2">BPM</span>
                </div>
            </div>
        </div>
    </main>

    <footer class="p-4 bg-slate-950 text-center text-xs text-slate-600 border-t border-slate-900">
        Local FastAPI WebSocket Environment
    </footer>

    <script>
        const ctx = document.getElementById('ecgChart').getContext('2d');
        const ecgChart = new Chart(ctx, {
            type: 'line',
            data: { 
                datasets: [{ 
                    label: 'ECG (mV)', 
                    borderColor: '#f43f5e',
                    borderWidth: 2, 
                    pointRadius: 0, 
                    data: [] 
                }] 
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                scales: {
                    x: { 
                        type: 'realtime', 
                        realtime: { duration: 4000, refresh: 40, delay: 100 } 
                    },
                    y: { 
                        grid: { color: '#334155' },
                        ticks: { color: '#94a3b8' }
                    }
                },
                plugins: { legend: { display: false } }
            }
        });

        const ws = new WebSocket(`ws://${location.host}/ws`);
        const statusEl = document.getElementById('status');
        const ecgValEl = document.getElementById('ecg-val');
        const hrValEl = document.getElementById('hr-val');

        ws.onopen = () => { 
            statusEl.innerText = "Connected 🟢"; 
            statusEl.className = "px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30";
        };
        
        ws.onclose = () => { 
            statusEl.innerText = "Disconnected 🔴"; 
            statusEl.className = "px-3 py-1 rounded-full text-xs font-semibold bg-red-500/20 text-red-400 border border-red-500/30";
        };

        ws.onmessage = (event) => {
            const parts = event.data.split(',');
            // Expecting 3 values: Epoch, ECG_mV, HR_BPM
            if (parts.length === 3) {
                const epoch_ms = parseInt(parts[0]);
                const mv_val = parseFloat(parts[1]);
                const hr_val = parseInt(parts[2]);

                ecgChart.data.datasets[0].data.push({ x: epoch_ms, y: mv_val });
                ecgValEl.innerText = mv_val.toFixed(3);
                
                if (hr_val > 0) {
                    hrValEl.innerText = hr_val;
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
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()

def get_latest_log_file():
    if not os.path.exists(LOGS_DIR):
        return None
    list_of_files = glob.glob(os.path.join(LOGS_DIR, 'polar_raw_ecg_*.csv'))
    return max(list_of_files, key=os.path.getctime) if list_of_files else None

async def tail_csv_and_broadcast():
    latest_file = get_latest_log_file()
    if not latest_file:
        print("⚠️ No log files found. Start advanced_worker.py first!")
        return

    print(f"📡 Web Server locked onto: {latest_file}")
    
    # Standard file open fixes the EOF caching bug
    with open(latest_file, 'r') as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                await asyncio.sleep(0.01)
                continue
            
            clean_line = line.strip()
            if clean_line and "Timestamp" not in clean_line:
                await manager.broadcast(clean_line)

@asynccontextmanager
async def lifespan(app: FastAPI):
    tail_task = asyncio.create_task(tail_csv_and_broadcast())
    yield
    print("\nHalting CSV tailing task...")
    tail_task.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def get():
    return HTMLResponse(HTML_TEMPLATE)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5001, reload=False)
