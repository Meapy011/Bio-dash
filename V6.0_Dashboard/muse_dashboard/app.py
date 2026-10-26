"""Muse-dash web server (port 5006): follows the worker's session files and sends new rows to the page.

As the other dashboards: muse_worker.py records <Documents>/Bio-dash/Muse/<headband>/<date>/<time>_<stream>.csv
and this tails the newest of each stream, sending what arrived since the last look as one websocket message
{"type": stream, "rows": [...]} (eeg, optics, imu, bands, heart, fnirs, battery). Commands go to the worker
through logs_muse/command.json; its state comes back through status.json.
"""
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import bt_debug
import muse_protocol as mp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs_muse")
DEVICES_FILE = os.path.join(LOGS_DIR, "devices.json")
COMMAND_FILE = os.path.join(LOGS_DIR, "command.json")
STATUS_FILE = os.path.join(LOGS_DIR, "status.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "muse_settings.json")
PORT = int(os.environ.get("BIODASH_MUSE_PORT", "5006"))
STREAMS = ("eeg", "optics", "imu", "bands", "heart", "fnirs", "battery")
POLL_S, ROTATE_CHECK_S = 0.02, 2.0


class Clients:
    def __init__(self):
        self.ws = set()

    async def broadcast(self, text):
        conns = list(self.ws)
        results = await asyncio.gather(*(c.send_text(text) for c in conns), return_exceptions=True)
        for c, r in zip(conns, results):
            if isinstance(r, Exception):
                self.ws.discard(c)


clients = Clients()


def latest(stream):
    files = bt_debug.latest_session_files("Muse", stream, limit=1)
    return files[0] if files else None


def parse_rows(lines):
    """CSV lines -> [[ts_ms, v, ...]]: the header and broken lines are skipped; empty cells -> null."""
    rows = []
    for line in lines:
        parts = line.split(",")
        try:
            row = [int(parts[0])]
            row += [float(p) if p != "" else None for p in parts[1:]]
        except ValueError:
            continue
        rows.append(row)
    return rows


async def follow(stream):
    """The newest <time>_<stream>.csv, as it grows (a new session's file is picked up within 2 s)."""
    loop = asyncio.get_running_loop()
    current, f, partial, next_check = None, None, "", 0.0
    try:
        while True:
            if loop.time() >= next_check:
                next_check = loop.time() + ROTATE_CHECK_S
                path = await run_in_threadpool(latest, stream)
                if path and path != current:
                    first = current is None
                    if f:
                        f.close()
                    f = open(path, "r")
                    if first and os.path.getsize(path) > 64 * 1024:
                        f.seek(0, os.SEEK_END)            # an old session at startup: don't replay it
                    current, partial = path, ""
            if f is None:
                await asyncio.sleep(1)
                continue
            chunk = f.read()
            if not chunk:
                await asyncio.sleep(POLL_S)
                continue
            lines = (partial + chunk).split("\n")
            partial = lines.pop()
            if clients.ws:
                rows = parse_rows(l.strip() for l in lines if l.strip())
                if rows:
                    await clients.broadcast(json.dumps({"type": stream, "rows": rows}, separators=(",", ":")))
    finally:
        if f:
            f.close()


@asynccontextmanager
async def lifespan(_app):
    if os.path.exists(COMMAND_FILE):
        try:
            os.remove(COMMAND_FILE)
        except OSError:
            pass
    tasks = [asyncio.create_task(follow(s)) for s in STREAMS]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.middleware("http")
async def _proxied_is_not_local(request, call_next):
    """Behind a proxy on this computer (tailscale serve) every request comes from 127.0.0.1: use the forwarded
    address, so "this computer only" (power, updates, restart) still means this computer."""
    h = request.headers
    if h.get("tailscale-user-login") or h.get("x-forwarded-for") or h.get("forwarded"):
        first = (h.get("x-forwarded-for") or "").split(",")[0].strip()
        request.scope["client"] = (first if first and first not in ("127.0.0.1", "::1") else "proxied", 0)
    return await call_next(request)


@app.middleware("http")
async def _revalidate_pages(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _command(obj):
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(COMMAND_FILE + ".tmp", "w") as f:
        json.dump(obj, f)
    os.replace(COMMAND_FILE + ".tmp", COMMAND_FILE)       # atomic: the worker never reads half a command


class ConnectRequest(BaseModel):
    address: str


@app.get("/")
async def index():
    return FileResponse(os.path.join(BASE_DIR, "templates", "index.html"))


@app.get("/api/scan-results")
def scan_results():
    try:
        with open(DEVICES_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


@app.get("/api/status")
def status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"state": "scanning"}


@app.post("/api/connect")
def connect(req: ConnectRequest):
    _command({"action": "connect", "address": req.address})
    return {"status": "command_sent"}


@app.post("/api/disconnect")
def disconnect():
    _command({"action": "disconnect"})
    return {"status": "command_sent"}


@app.post("/api/fnirs/zero")
def fnirs_zero():
    """A new fNIRS baseline: the next 10 s become "zero" (e.g. after settling in, before a task)."""
    _command({"action": "zero"})
    return {"status": "command_sent"}


@app.get("/api/preset")
def get_preset():
    try:
        with open(SETTINGS_FILE) as f:
            preset = json.load(f).get("preset") or mp.DEFAULT_PRESET
    except (OSError, ValueError, AttributeError):
        preset = mp.DEFAULT_PRESET
    return {"preset": preset, "presets": mp.PRESETS, "default": mp.DEFAULT_PRESET}


class PresetRequest(BaseModel):
    preset: str


@app.post("/api/preset")
def set_preset(req: PresetRequest):
    """Which streams the headband sends. A live session restarts with it (new files: the columns change)."""
    if req.preset not in mp.PRESETS:
        return JSONResponse({"error": f"unknown preset {req.preset}"}, status_code=400)
    with open(SETTINGS_FILE + ".tmp", "w") as f:
        json.dump({"preset": req.preset}, f)
    os.replace(SETTINGS_FILE + ".tmp", SETTINGS_FILE)
    _command({"action": "preset"})
    return get_preset()


# ---------- tile trends, pre-filled from this session's files ----------
HISTORY_MAX_POINTS = 1500


def _tail_lines(path, max_bytes):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > max_bytes else lines


def _thin(points):
    return points[::max(1, len(points) // HISTORY_MAX_POINTS)]


@app.get("/api/history")
def history(metric: str, minutes: int = 15):
    """hr (from the beats), alpha (alpha's share of the power, mean of the channels in contact) or battery."""
    minutes = max(1, min(minutes, 60))
    cutoff = time.time() * 1000 - minutes * 60_000
    stream = {"hr": "heart", "alpha": "bands", "battery": "battery"}.get(metric)
    path = latest(stream) if stream else None
    if not path:
        return {metric: []}
    rows = [r for r in parse_rows(_tail_lines(path, minutes * 60 * (400 if stream == "bands" else 40) + 8192)) if r[0] >= cutoff]
    if metric == "hr":
        return {"hr": _thin([{"x": r[0], "y": r[2]} for r in rows if r[2]])}
    if metric == "battery":
        return {"battery": _thin([{"x": r[0], "y": r[1]} for r in rows])}
    out = []
    for r in rows:                      # bands: 4 channels x 5 bands (log10 uV^2), then 4 contact codes
        shares = []
        for c in range(4):
            if r[21 + c] in (1, 2):
                p = [10 ** v for v in r[1 + 5 * c:6 + 5 * c]]
                shares.append(p[2] / sum(p))
        if shares:
            out.append({"x": r[0], "y": round(100 * sum(shares) / len(shares), 1)})
    return {"alpha": _thin(out)}


# ---------- auto-reconnect, radio, names (shared with the other dashboards) ----------
LAST = {"device": bt_debug.LastDevice(os.path.join(BASE_DIR, "last_device.json"))}


class LastDeviceRequest(BaseModel):
    role: str = "device"
    auto: bool | None = None
    forget: bool = False


@app.get("/api/last-device")
def get_last_device():
    return {"devices": {role: ld.load() for role, ld in LAST.items()}}


@app.post("/api/last-device")
def set_last_device(req: LastDeviceRequest):
    ld = LAST.get(req.role)
    if ld is None:
        return {"error": f"unknown role {req.role}"}
    if req.forget:
        ld.forget()
    elif req.auto is not None:
        ld.set_auto(req.auto)
    return get_last_device()


class RadioRequest(BaseModel):
    address: str = "auto"


radio_pin = bt_debug.RadioPin(os.path.join(BASE_DIR, "radio_config.json"))


@app.get("/api/radio")
def get_radio():
    return {"radio": radio_pin.config()}


@app.post("/api/radio")
def set_radio(req: RadioRequest):
    radio_pin.save(req.address)
    return {"radio": radio_pin.config()}


@app.get("/api/device-names")
def get_device_names():
    return {"names": bt_debug.device_names()}


@app.post("/api/device-names")
async def post_device_name(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    code, out = bt_debug.set_device_name(body.get("key"), body.get("name"))
    return JSONResponse(out, status_code=code)


# ---------- the sidebar's System card: power, updates, restart (this computer only) ----------
def _host(request):
    return request.client.host if request.client else ""


async def _body(request):
    try:
        b = await request.json()
    except Exception:
        b = {}
    return b if isinstance(b, dict) else {}


@app.get("/api/system/power")
def power_info(request: Request):
    return bt_debug.power_info(_host(request))


@app.post("/api/system/power")
async def power(request: Request):
    code, out = bt_debug.system_power((await _body(request)).get("action"), _host(request), request.headers.get(bt_debug.POWER_HEADER))
    return JSONResponse(out, status_code=code)


@app.get("/api/system/update")
def update_info(request: Request, check: int = 0):
    return bt_debug.update_info(_host(request), fetch=bool(check))


@app.post("/api/system/update")
async def update(request: Request):
    action = (await _body(request)).get("action")
    code, out = await run_in_threadpool(bt_debug.system_update, action, _host(request), request.headers.get(bt_debug.POWER_HEADER))
    return JSONResponse(out, status_code=code)


@app.get("/api/system/restart")
def restart_info(request: Request):
    return bt_debug.restart_info(_host(request))


@app.post("/api/system/restart")
async def restart_all(request: Request):
    body = await _body(request)
    code, out = await run_in_threadpool(bt_debug.system_restart, body, _host(request), request.headers.get(bt_debug.POWER_HEADER))
    return JSONResponse(out, status_code=code)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.ws.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.ws.discard(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
