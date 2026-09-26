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
LOGS_DIR = os.path.join(BASE_DIR, "logs_air")
DEVICES_FILE = os.path.join(LOGS_DIR, "devices.json")
COMMAND_FILE = os.path.join(LOGS_DIR, "command.json")

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

POLL_S = 0.1
ROTATE_CHECK_S = 2.0

def get_latest_file():
    files = glob.glob(os.path.join(LOGS_DIR, 'sen69c_telemetry_*.csv'))
    return max(files, key=os.path.getmtime) if files else None

async def tail_file_and_broadcast():
    loop = asyncio.get_running_loop()
    current_file, f, partial = None, None, ""
    next_rotate_check = 0.0
    try:
        while True:
            # Only glob/stat the log dir every couple of seconds, not on every poll
            now = loop.time()
            if now >= next_rotate_check:
                next_rotate_check = now + ROTATE_CHECK_S
                latest_file = get_latest_file()
                if latest_file and latest_file != current_file:
                    first_lock = current_file is None
                    if f: f.close()
                    current_file, partial = latest_file, ""
                    print(f"📡 Router locked onto: {current_file}")
                    f = open(current_file, 'r')
                    if first_lock and os.path.getsize(current_file) > 16 * 1024:
                        f.seek(0, os.SEEK_END)

            if f is None:
                await asyncio.sleep(1)
                continue

            chunk = f.read()
            if not chunk:
                await asyncio.sleep(POLL_S)
                continue

            lines = (partial + chunk).split("\n")
            partial = lines.pop()
            good = [l.strip() for l in lines if l.strip() and not l.startswith("Timestamp")]
            if good and manager.active_connections:
                await manager.broadcast("\n".join(good))
    finally:
        if f: f.close()

# --- APP INITIALIZATION ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.path.exists(COMMAND_FILE):
        try: os.remove(COMMAND_FILE)
        except Exception: pass
        
    tail_task = asyncio.create_task(tail_file_and_broadcast())
    yield
    tail_task.cancel()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")), name="static")

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
    tmp = COMMAND_FILE + ".tmp"
    with open(tmp, 'w') as f:
        json.dump({"action": "connect", "address": req.address}, f)
    os.replace(tmp, COMMAND_FILE)
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
    uvicorn.run("app:app", host="0.0.0.0", port=5002, reload=False)
