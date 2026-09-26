import asyncio
import os
import glob
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

WEB_DIR = os.path.dirname(os.path.abspath(__file__))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs_advanced")
DEVICES_FILE = os.path.join(LOGS_DIR, "devices.json")
COMMAND_FILE = os.path.join(LOGS_DIR, "command.json")
STATUS_FILE = os.path.join(LOGS_DIR, "status.json")

class ConnectRequest(BaseModel):
    address: str


class ConnectionManager:
    def __init__(self):
        self.active_connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)

    async def broadcast(self, message: str):
        conns = list(self.active_connections)
        results = await asyncio.gather(*(c.send_text(message) for c in conns), return_exceptions=True)
        for c, r in zip(conns, results):
            if isinstance(r, Exception):
                self.disconnect(c)

manager = ConnectionManager()

# CSV row layouts written by advanced_worker.py -> (column count, per-column converters)
ROW_SCHEMAS = {
    "ecg": (3, (int, float, int)),   # ts_ms, mV, hr
    "acc": (4, (int, float, float, float)),  # ts_ms, x, y, z (mG)
    "ppi": (3, (int, int, int)),     # ts_ms, ppi_ms, hr
}

POLL_S = 0.02          # how often to check the file for new bytes
ROTATE_CHECK_S = 2.0   # how often to look for a newer log file (worker restart)

def get_latest_file(prefix):
    files = glob.glob(os.path.join(LOGS_DIR, f"{prefix}_*.csv"))
    return max(files, key=os.path.getmtime) if files else None

def parse_rows(lines, stream_type):
    ncols, conv = ROW_SCHEMAS[stream_type]
    rows = []
    for line in lines:
        parts = line.split(",")
        if len(parts) != ncols:
            continue
        try:
            rows.append([c(p) for c, p in zip(conv, parts)])
        except ValueError:
            continue  # header row or garbage
    return rows

async def tail_file_and_broadcast(prefix, stream_type):
    """Follows the newest `prefix_*.csv`, sending everything that arrived since the
    last poll as ONE websocket message instead of one message per sample."""
    loop = asyncio.get_running_loop()
    current, f, partial = None, None, ""
    next_rotate_check = 0.0
    try:
        while True:
            now = loop.time()
            if now >= next_rotate_check:
                next_rotate_check = now + ROTATE_CHECK_S
                latest = get_latest_file(prefix)
                if latest and latest != current:
                    first_lock = current is None
                    if f:
                        f.close()
                    f = open(latest, "r")
                    # On startup, skip past a file that already holds a lot (don't replay an old
                    # session), but read a fresh one from the top so nothing is dropped.
                    if first_lock and os.path.getsize(latest) > 64 * 1024:
                        f.seek(0, os.SEEK_END)
                    current, partial = latest, ""
                    print(f"📡 Router locked onto {stream_type.upper()}: {latest}")

            if f is None:
                await asyncio.sleep(1)
                continue

            chunk = f.read()
            if not chunk:
                await asyncio.sleep(POLL_S)
                continue

            # Keep any half-written trailing line for the next read
            data = partial + chunk
            lines = data.split("\n")
            partial = lines.pop()

            if manager.active_connections:
                rows = parse_rows((l.strip() for l in lines if l.strip()), stream_type)
                if rows:
                    await manager.broadcast(json.dumps({"type": stream_type, "rows": rows}, separators=(",", ":")))
    finally:
        if f:
            f.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.path.exists(COMMAND_FILE):
        try: os.remove(COMMAND_FILE)
        except Exception: pass

    tasks = [
        asyncio.create_task(tail_file_and_broadcast("polar_ecg", "ecg")),
        asyncio.create_task(tail_file_and_broadcast("polar_acc", "acc")),
        asyncio.create_task(tail_file_and_broadcast("polar_ppi", "ppi")),
    ]
    yield
    for t in tasks:
        t.cancel()

# --- APP INITIALIZATION ---
app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")), name="static")

# --- ENDPOINTS ---
@app.get("/api/scan-results")
def get_scan_results():
    if os.path.exists(DEVICES_FILE):
        try:
            with open(DEVICES_FILE, "r") as f:
                return json.load(f)
        except Exception: pass
    return []

@app.get("/api/status")
def get_status():
    try:
        with open(STATUS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"state": "scanning"}

@app.post("/api/connect")
def connect_device(req: ConnectRequest):
    os.makedirs(LOGS_DIR, exist_ok=True)
    tmp = COMMAND_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"action": "connect", "address": req.address}, f)
    os.replace(tmp, COMMAND_FILE)  # atomic, so the worker never reads a half-written command
    return {"status": "command_sent"}

@app.get("/")
async def index(): return FileResponse(os.path.join(WEB_DIR, "templates", "index.html"))

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5001, reload=False)
