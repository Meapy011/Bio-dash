import asyncio
import os
import glob
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs_air")

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SuperMini Air Monitor</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .metric-card {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 1rem;
            padding: 1.5rem;
            transition: all 0.3s ease;
        }
    </style>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen font-sans flex flex-col">

    <div class="max-w-4xl mx-auto px-4 py-8 flex-grow w-full">
        <header class="flex flex-col sm:flex-row justify-between items-center border-b border-slate-700 pb-6 mb-8 gap-4">
            <div>
                <h1 class="text-3xl font-bold tracking-tight text-emerald-400">SEN69C Environmental Monitor</h1>
                <p class="text-slate-400 text-sm mt-1">Local CSV Logging Dashboard</p>
            </div>
            <div id="status-badge" class="px-4 py-2 rounded-full text-sm font-semibold bg-amber-500/20 text-amber-400 border border-amber-500/30">
                Awaiting Telemetry...
            </div>
        </header>

        <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-6">
            <div class="metric-card md:col-span-3 border-l-4 border-cyan-500">
                <h3 class="text-xs font-bold uppercase tracking-wider text-cyan-400 mb-4">Particulate Matter (µg/m³)</h3>
                <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
                    <div><span class="text-slate-400 text-xs block">PM 1.0</span><span id="pm1" class="text-2xl font-semibold">--</span></div>
                    <div><span class="text-slate-400 text-xs block">PM 2.5</span><span id="pm25" class="text-2xl font-semibold text-cyan-300">--</span></div>
                    <div><span class="text-slate-400 text-xs block">PM 4.0</span><span id="pm4" class="text-2xl font-semibold">--</span></div>
                    <div><span class="text-slate-400 text-xs block">PM 10.0</span><span id="pm10" class="text-2xl font-semibold">--</span></div>
                </div>
            </div>

            <div class="metric-card border-l-4 border-amber-500">
                <span class="text-xs font-bold uppercase tracking-wider text-amber-400 block mb-1">Carbon Dioxide</span>
                <span id="co2" class="text-4xl font-extrabold block my-2">--</span>
                <span class="text-xs text-slate-400">PPM</span>
            </div>

            <div class="metric-card border-l-4 border-red-500">
                <span class="text-xs font-bold uppercase tracking-wider text-red-400 block mb-1">Formaldehyde (HCHO)</span>
                <span id="hcho" class="text-4xl font-extrabold block my-2">--</span>
                <span class="text-xs text-slate-400">PPB</span>
            </div>

            <div class="metric-card border-l-4 border-emerald-500">
                <span class="text-xs font-bold uppercase tracking-wider text-emerald-400 block mb-1">Temperature</span>
                <span id="temp" class="text-4xl font-extrabold block my-2">--</span>
                <span class="text-xs text-slate-400">°C</span>
            </div>

            <div class="metric-card border-l-4 border-blue-500">
                <span class="text-xs font-bold uppercase tracking-wider text-blue-400 block mb-1">Relative Humidity</span>
                <span id="humidity" class="text-4xl font-extrabold block my-2">--</span>
                <span class="text-xs text-slate-400">% RH</span>
            </div>

            <div class="metric-card border-l-4 border-purple-500">
                <span class="text-xs font-bold uppercase tracking-wider text-purple-400 block mb-1">VOC Index</span>
                <span id="voc" class="text-4xl font-extrabold block my-2">--</span>
                <span class="text-xs text-slate-400">1 - 500</span>
            </div>

            <div class="metric-card border-l-4 border-indigo-500">
                <span class="text-xs font-bold uppercase tracking-wider text-indigo-400 block mb-1">NOx Index</span>
                <span id="nox" class="text-4xl font-extrabold block my-2">--</span>
                <span class="text-xs text-slate-400">1 - 500</span>
            </div>
        </div>
    </div>

    <script>
        const ws = new WebSocket(`ws://${location.host}/ws`);
        const statusBadge = document.getElementById('status-badge');

        ws.onopen = () => {
            statusBadge.innerText = "System Online";
            statusBadge.className = "px-4 py-2 rounded-full text-sm font-semibold bg-sky-500/20 text-sky-400 border border-sky-500/30";
        };

        ws.onclose = () => {
            statusBadge.innerText = "Disconnected 🔴";
            statusBadge.className = "px-4 py-2 rounded-full text-sm font-semibold bg-red-500/20 text-red-400 border border-red-500/30";
        };

        ws.onmessage = (event) => {
            statusBadge.innerText = "Telemetry Active 🟢";
            statusBadge.className = "px-4 py-2 rounded-full text-sm font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30";
            
            const parts = event.data.split(',');
            // Expecting 11 parts: Timestamp + 10 metrics
            if (parts.length >= 11) {
                document.getElementById('pm1').innerText      = parts[1];
                document.getElementById('pm25').innerText     = parts[2];
                document.getElementById('pm4').innerText      = parts[3];
                document.getElementById('pm10').innerText     = parts[4];
                document.getElementById('humidity').innerText = parts[5];
                document.getElementById('temp').innerText     = parts[6];
                document.getElementById('voc').innerText      = parts[7];
                document.getElementById('nox').innerText      = parts[8];
                document.getElementById('hcho').innerText     = parts[9];
                document.getElementById('co2').innerText      = parts[10];
                
                const co2Val = parseInt(parts[10]);
                const co2Card = document.getElementById('co2').parentElement;
                if (co2Val > 1000) {
                    co2Card.style.borderColor = '#ef4444';
                } else {
                    co2Card.style.borderColor = '#f59e0b';
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

def get_latest_file():
    if not os.path.exists(LOGS_DIR): return None
    files = glob.glob(os.path.join(LOGS_DIR, 'sen69c_telemetry_*.csv'))
    return max(files, key=os.path.getctime) if files else None

async def tail_file_and_broadcast():
    current_file = None
    f = None
    try:
        while True:
            latest_file = get_latest_file()
            if not latest_file:
                await asyncio.sleep(1)
                continue

            if latest_file != current_file:
                if f: f.close()
                current_file = latest_file
                print(f"📡 Router locked onto: {current_file}")
                f = open(current_file, 'r')
                f.seek(0, os.SEEK_END)

            line = f.readline()
            if not line:
                await asyncio.sleep(0.05)
                continue
            
            clean_line = line.strip()
            if clean_line and "Timestamp" not in clean_line:
                await manager.broadcast(clean_line)
    finally:
        if f: f.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    tail_task = asyncio.create_task(tail_file_and_broadcast())
    yield
    tail_task.cancel()

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
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
