#!/usr/bin/env python3
"""Live ECG / accelerometer forecasts for Polar-dash (Bio-dash V5.5).

Runs next to app.py as its own process, so a crash or a GPU stall can't freeze the dashboard and torch
stays out of the web server:

  * follows the session the Bluetooth worker is recording
    (<Documents>/Bio-dash/Polar H10/<device>/<date>/<time>_{ecg,acc}.csv) and tails the accelerometer and
    ECG files, accelerometer first;
  * builds model rows with waves.LiveAligner, exactly like the training data (the 125 Hz grid; the settings
    come from each model folder's config.json);
  * every refresh (0.5 s) and for each overlay that is switched on, forecasts the next second as 8 samples
    with the fast loop (KV cache + CUDA graph) and POSTs the band, the median and one representative sample
    to app.py's /api/forecast, which broadcasts them to the browsers;
  * sends a heartbeat once a second (state, device, timings) and gets the overlay switches back.

Two models: forecast/models/ecg (ECG only; the ECG overlay) and forecast/models/ecg_acc (ECG +
accelerometer; the accelerometer overlay). Each is loaded the first time its overlay is switched on.
Without torch / transformers / the model folders the service keeps running and reports why, so the
dashboard can show it. Run the models on a GPU with bfloat16 autocast, as trained (CPU works, slowly).

    python3 forecast_service.py [--url http://127.0.0.1:5001] [--models forecast/models] [--device auto]
    python3 forecast_service.py --bench          (no dashboard: load the models, time forecasts, GPU memory)
    python3 forecast_service.py --session <.../HH-MM-SS_ecg.csv> --record contexts.jsonl --seed 1   (checks)
"""
import argparse
import collections
import json
import os
import signal
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FC_DIR = os.path.join(HERE, "forecast")
for p in (HERE, os.path.join(HERE, "..", "analysis_dashboard"), FC_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import bt_debug  # noqa: E402  (session folders, standard library only)
import waves  # noqa: E402  (the shared interpolation: the same rows as the training data)

DEFAULTS = {"ecg": False, "acc": False, "horizon_s": 1.0, "refresh_s": 0.5, "samples": 8}
STALE_MS = 5000          # no new rows for this long: waiting for data
MIN_CONTEXT = 64         # rows (0.5 s) before the first forecast after a gap
TOP_K, TEMPERATURE = 100, 1.0   # as in the held-out evaluation (calibrated band)
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # localhost: never via a proxy


def log(msg):
    print(f"[forecast {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def post_json(url, obj, timeout=2.0):
    req = urllib.request.Request(url, data=json.dumps(obj, separators=(",", ":")).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with _opener.open(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body else None


# ------------------------------------------------------------------------------------------------ data
class Tail:
    """New complete lines of a growing CSV file (a half-written last line waits for the next read)."""

    def __init__(self, path, tail_bytes):
        self.path = path
        self.f = open(path, "r", errors="replace")
        self.partial = ""
        size = os.path.getsize(path)
        if size > tail_bytes:            # joined mid-session: start a few seconds back, not at the top
            self.f.seek(size - tail_bytes)
            self.f.readline()

    def lines(self):
        chunk = self.f.read()
        if not chunk:
            return []
        parts = (self.partial + chunk).split("\n")
        self.partial = parts.pop()
        return [s.strip() for s in parts if s.strip()]

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


def parse(line, n):
    """'ts,a,b[,c]' -> [int ts, floats...] or None (header, NUL bytes, garbage)."""
    parts = line.split(",")
    if len(parts) < n:
        return None
    try:
        return [int(parts[0])] + [float(x) for x in parts[1:n]]
    except ValueError:
        return None


def newest_session(fixed=None):
    """(ecg csv, acc csv or None) of the session being recorded (the newest ECG file)."""
    if fixed:
        ecg = fixed
    else:
        files = bt_debug.latest_session_files("Polar H10", "ecg", limit=1)
        if not files:
            return None, None
        ecg = files[0]
    acc = ecg[: -len("_ecg.csv")] + "_acc.csv" if ecg.endswith("_ecg.csv") else None
    return ecg, (acc if acc and os.path.exists(acc) else None)


def to_real(lane, v):
    """Integer lane value -> mV / mg (the lane's decode rule: value * step / scale)."""
    return v * float(lane.get("step", 1)) / float(lane.get("scale") or 1)


def summarize(samples, lanes):
    """samples: N x K x H integer values -> median, band (lo / hi) and the representative sample (the one
    closest to the median; averaging samples would smear a heartbeat whose timing is uncertain)."""
    N, K, H = len(samples), len(samples[0]), len(samples[0][0])
    real = [[[to_real(lanes[k], samples[n][k][h]) for h in range(H)] for k in range(K)] for n in range(N)]
    med = [[statistics.median(real[n][k][h] for n in range(N)) for h in range(H)] for k in range(K)]
    lo = [[min(real[n][k][h] for n in range(N)) for h in range(H)] for k in range(K)]
    hi = [[max(real[n][k][h] for n in range(N)) for h in range(H)] for k in range(K)]
    spread = [sum(hi[k][h] - lo[k][h] for h in range(H)) / H + 1e-9 for k in range(K)]
    dist = [sum(sum(abs(real[n][k][h] - med[k][h]) for h in range(H)) / spread[k] for k in range(K)) for n in range(N)]
    rep = min(range(N), key=dist.__getitem__)
    dec = [3 if lane.get("unit") == "mV" else 1 for lane in lanes]

    def rnd(rows):
        return [[round(v, d + 1) for v in row] for row, d in zip(rows, dec)]
    return {"median": rnd(med), "rep": rnd(real[rep]), "lo": rnd(lo), "hi": rnd(hi), "rep_index": rep}


class Overlay:
    """One overlay (ecg | acc): its model folder, LiveAligner and context ring."""

    def __init__(self, kind, model_dir):
        self.kind, self.model_dir = kind, model_dir
        self.cfg_error = None
        self.cfg = None
        try:
            with open(os.path.join(model_dir, "config.json")) as f:
                self.cfg = json.load(f)
            if not os.path.exists(os.path.join(model_dir, "pytorch_model.bin")) and \
                    not os.path.exists(os.path.join(model_dir, "model.safetensors")):
                self.cfg_error = f"no weights in {model_dir}"
        except OSError:
            self.cfg_error = f"model folder missing: {model_dir}"
        run_file = os.path.join(model_dir, "RUN.txt")
        self.run = open(run_file).read().strip() if os.path.exists(run_file) else os.path.basename(model_dir)
        cfg = self.cfg or {}
        self.lanes = cfg.get("lanes") or [{"column": "ecg_uv", "int_min": -2500, "scale": 1000, "unit": "mV"}]
        self.names = [ln["column"] for ln in self.lanes]
        self.units = [ln.get("unit", "") for ln in self.lanes]
        self.block = int(cfg.get("block_size", 256))
        st = (cfg.get("source") or {}).get("settings") or {"grid_ms": 8, "with_acc": kind == "acc"}
        self.settings = waves.Settings.from_dict(st)
        self.dt_ms = float(self.settings.grid_ms or round(1000.0 / float(cfg.get("sample_rate_hz") or 130), 3))
        self.aligner = waves.LiveAligner(self.settings)
        self.ring = collections.deque(maxlen=self.block)
        self.times = collections.deque(maxlen=self.block)
        self.last_t, self.fresh = None, 0
        self.model = self.fc = None
        self.load_error = None
        self.compute_ms, self.count, self.posted = None, 0, 0

    @property
    def needs_acc(self):
        return bool(self.settings.with_acc)

    def reset(self):
        self.aligner.reset()
        self.ring.clear()
        self.times.clear()
        self.last_t, self.fresh = None, 0

    def take_rows(self):
        for t, row, new_piece in self.aligner.rows():
            if new_piece:
                self.ring.clear()
                self.times.clear()
            self.ring.append(row)
            self.times.append(t)
            self.last_t = t
            self.fresh += 1

    def status(self, on):
        return {"on": on, "model": self.run, "loaded": self.model is not None,
                "error": self.cfg_error or self.load_error, "rows": len(self.ring),
                "compute_ms": None if self.compute_ms is None else round(self.compute_ms, 1),
                "forecasts": self.count, "last_t": self.last_t}


# ------------------------------------------------------------------------------------------------ service
class Service:
    def __init__(self, a):
        self.a = a
        self.url = a.url.rstrip("/")
        self.settings = dict(DEFAULTS)
        self.overlays = {"ecg": Overlay("ecg", a.ecg_model or os.path.join(a.models, "ecg")),
                         "acc": Overlay("acc", a.acc_model or os.path.join(a.models, "ecg_acc"))}
        self.session = None
        self.tails = {}
        self.state, self.detail = "starting", ""
        self.dashboard_ok = True
        self.stop = False
        self.record = open(a.record, "a") if a.record else None
        self.torch = self.dev = None
        self.torch_error = None
        self.device_name = "none"
        try:
            import torch
            self.torch = torch
            want = a.device
            if want == "cuda" or (want == "auto" and torch.cuda.is_available()):
                self.dev = torch.device("cuda")
                self.device_name = f"cuda ({torch.cuda.get_device_name(0)})"
            else:
                self.dev = torch.device("cpu")
                torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
                self.device_name = "cpu (slow: no CUDA GPU found)" if want == "auto" else "cpu"
        except Exception as e:     # noqa: BLE001
            self.torch_error = (f"PyTorch isn't available for {sys.executable} ({type(e).__name__}); "
                                f"see polar_dashboard/forecast/README.md")
        log(f"device: {self.device_name if self.torch else self.torch_error}"
            + (" · torch.compile on" if a.compile and self.torch else ""))
        for ov in self.overlays.values():
            log(f"{ov.kind}: {ov.model_dir} ({ov.run}) {ov.cfg_error or ''}".rstrip())

    # ---------------- dashboard link
    def heartbeat(self):
        if self.a.bench:
            return
        body = {"state": self.state, "detail": self.detail, "device": self.device_name, "pid": os.getpid(),
                "session": self.session, "overlays": {k: ov.status(bool(self.settings.get(k)))
                                                      for k, ov in self.overlays.items()},
                "horizon_s": self.settings.get("horizon_s"), "samples": self.settings.get("samples")}
        try:
            got = post_json(self.url + "/api/forecast/heartbeat", body)
            if isinstance(got, dict):
                self.settings = {**DEFAULTS, **{k: v for k, v in got.items() if k in DEFAULTS}}
            if not self.dashboard_ok:
                log("dashboard reachable again")
            self.dashboard_ok = True
        except (urllib.error.URLError, OSError, ValueError) as e:
            if self.dashboard_ok:
                log(f"dashboard not reachable at {self.url} ({e}); retrying")
            self.dashboard_ok = False

    # ---------------- data
    def follow(self):
        ecg, acc = newest_session(self.a.session)
        if ecg == self.session and (acc is None or "acc" in self.tails):
            return
        if ecg != self.session:
            for t in self.tails.values():
                t.close()
            self.tails = {}
            for ov in self.overlays.values():
                ov.reset()
            self.session = ecg
            if ecg:
                log(f"following {os.path.dirname(ecg)} ({os.path.basename(ecg)[:8]})")
                self.tails["ecg"] = Tail(ecg, 48 * 1024)
        if acc and "acc" not in self.tails and self.session:
            self.tails["acc"] = Tail(acc, 64 * 1024)

    def read(self):
        if "acc" in self.tails:            # accelerometer first, so ECG rows don't wait for it
            for line in self.tails["acc"].lines():
                r = parse(line, 4)
                if r:
                    for ov in self.overlays.values():
                        if ov.needs_acc:
                            ov.aligner.push_acc(r[0], r[1], r[2], r[3])
        if "ecg" in self.tails:
            for line in self.tails["ecg"].lines():
                r = parse(line, 2)
                if r:
                    for ov in self.overlays.values():
                        ov.aligner.push_ecg(r[0], r[1])
        for ov in self.overlays.values():
            ov.take_rows()

    # ---------------- models
    def ensure_model(self, ov):
        if ov.model is not None or ov.cfg_error or ov.load_error or self.torch is None:
            return ov.model is not None
        self.state, self.detail = "loading model", ov.run          # the heartbeat thread reports it
        try:
            from modeling_csv_mc_int import CsvMcIntForCausalLM
            t0 = time.time()
            ov.model = CsvMcIntForCausalLM.load_dir(ov.model_dir, map_location=self.dev)
            log(f"{ov.kind}: loaded {ov.run} on {self.dev} in {time.time() - t0:.1f} s")
        except Exception as e:     # noqa: BLE001
            ov.load_error = f"couldn't load {ov.run}: {type(e).__name__}: {e}"
            log(ov.load_error)
        return ov.model is not None

    def forecaster(self, ov, samples):
        if ov.fc is None or ov.fc.N != samples:
            from fast_forecast import FastForecaster

            def make():
                return FastForecaster(ov.model, num_samples=samples, top_k=TOP_K, temperature=TEMPERATURE,
                                      dtype=self.torch.bfloat16)
            fc = make()
            if self.a.compile and self.dev.type == "cuda":
                # Optional: fuse the decode step's ~300 small kernels (about 2.3x faster on an A100). Compiling
                # takes ~35 s per model there (longer on an Orin) and needs Triton; on any failure the plain
                # CUDA graph is used.
                self.state, self.detail = "loading model", f"{ov.run}: compiling (torch.compile)"
                t0 = time.time()
                try:
                    fc._step = self.torch.compile(fc._step, mode="max-autotune-no-cudagraphs", dynamic=False)
                    warm = self.torch.tensor([[-ln["int_min"]] * 8 for ln in ov.lanes], dtype=self.torch.long)
                    fc.forecast(warm, 2)                 # compiles, then captures the CUDA graph around it
                    log(f"{ov.kind}: compiled the decode step in {time.time() - t0:.0f} s")
                except Exception as e:     # noqa: BLE001
                    log(f"{ov.kind}: torch.compile failed ({type(e).__name__}: {str(e)[:200]}); "
                        f"using the plain CUDA graph")
                    fc = make()
            ov.fc = fc
        return ov.fc

    def forecast(self, ov):
        torch = self.torch
        horizon = max(1, int(round(float(self.settings["horizon_s"]) * 1000.0 / ov.dt_ms)))
        horizon = min(horizon, ov.block - MIN_CONTEXT + 1)
        samples = max(1, min(32, int(self.settings["samples"])))
        T = min(len(ov.ring), ov.block - horizon + 1)
        rows, times = list(ov.ring)[-T:], list(ov.times)[-T:]
        ctx = torch.tensor([[r[k] - ov.lanes[k]["int_min"] for r in rows] for k in range(len(ov.lanes))],
                           dtype=torch.long)
        fc = self.forecaster(ov, samples)
        t0 = time.perf_counter()
        toks = fc.forecast(ctx, horizon, generator_seed=self.a.seed)          # (N, K, H) token ids
        vals = (toks.cpu() + torch.tensor([ln["int_min"] for ln in ov.lanes]).view(1, -1, 1)).tolist()
        ov.compute_ms = (time.perf_counter() - t0) * 1000.0
        ov.count += 1
        ov.fresh = 0
        out = summarize(vals, ov.lanes)
        last = [to_real(ov.lanes[k], rows[-1][k]) for k in range(len(ov.lanes))]
        payload = {"kind": ov.kind, "model": ov.run, "t0": times[-1] + ov.dt_ms, "dt_ms": ov.dt_ms,
                   "lanes": ov.names, "units": ov.units, "last": last, "context_rows": T, "samples": samples,
                   "compute_ms": round(ov.compute_ms, 1), "made_at": int(time.time() * 1000), **out}
        if self.record:
            self.record.write(json.dumps({"kind": ov.kind, "t0": payload["t0"], "times": times, "rows": rows,
                                          "rep_index": out["rep_index"], "median": out["median"]}) + "\n")
            self.record.flush()
        try:
            post_json(self.url + "/api/forecast", payload)
            ov.posted += 1
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.dashboard_ok = False
            log(f"couldn't send the {ov.kind} forecast: {e}")

    # ---------------- main loop
    def _heartbeats(self):
        """Once a second, from its own thread: the dashboard keeps seeing the service (and its state) while a
        model loads or compiles or a slow forecast runs."""
        while not self.stop:
            t0 = time.time()
            self.heartbeat()
            time.sleep(max(0.05, 1.0 - (time.time() - t0)))

    def run(self):
        threading.Thread(target=self._heartbeats, name="heartbeat", daemon=True).start()
        next_follow = next_fc = 0.0
        while not self.stop:
            now = time.time()
            if now >= next_follow:
                next_follow = now + 2.0
                try:
                    self.follow()
                except OSError as e:
                    log(f"session files: {e}")
            try:
                self.read()
            except OSError as e:
                log(f"read: {e}")
                self.session = None
            on = [k for k in ("ecg", "acc") if self.settings.get(k)]
            newest = max([ov.last_t for ov in self.overlays.values() if ov.last_t is not None], default=None)
            stale = newest is None or now * 1000.0 - newest > STALE_MS
            if self.torch is None:
                self.state, self.detail = "unavailable", self.torch_error
            elif not on:
                self.state, self.detail = "idle", "both overlays are off"
            elif not self.session:
                self.state, self.detail = "waiting for data", "no Polar H10 recording yet"
            elif stale:
                self.state, self.detail = "waiting for data", "no new samples from the strap"
            else:
                self.state, self.detail = "forecasting", ""
            if self.state == "forecasting" and now >= next_fc:
                next_fc = now + max(0.1, float(self.settings["refresh_s"]))
                for k in on:
                    ov = self.overlays[k]
                    if len(ov.ring) >= MIN_CONTEXT and ov.fresh > 0 and self.ensure_model(ov):
                        try:
                            self.forecast(ov)
                        except Exception as e:     # noqa: BLE001  (keep serving; report it)
                            ov.load_error = f"forecast failed: {type(e).__name__}: {e}"
                            log(ov.load_error)
                    elif ov.cfg_error or ov.load_error:
                        self.detail = ov.cfg_error or ov.load_error
            time.sleep(0.02)
        for t in self.tails.values():
            t.close()
        if self.record:
            self.record.close()
        log("stopped")


def bench(svc, repeats):
    """Time warm forecasts for both models at the dashboard settings and report GPU memory (the Orin check:
    both overlays together must fit well inside the refresh interval)."""
    torch = svc.torch
    if torch is None:
        sys.exit(svc.torch_error)
    samples = int(svc.settings["samples"])
    sync = torch.cuda.synchronize if svc.dev.type == "cuda" else (lambda: None)
    total = 0.0
    for ov in svc.overlays.values():
        if ov.cfg_error or not svc.ensure_model(ov):
            log(f"{ov.kind}: {ov.cfg_error or ov.load_error}")
            continue
        H = max(1, int(round(float(svc.settings["horizon_s"]) * 1000.0 / ov.dt_ms)))
        T = ov.block - H + 1
        ctx = torch.tensor([[-ln["int_min"]] * T for ln in ov.lanes], dtype=torch.long)   # value 0 (timing only)
        fc = svc.forecaster(ov, samples)
        t0 = time.perf_counter()
        fc.forecast(ctx, H)
        sync()
        first = time.perf_counter() - t0
        times = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            toks = fc.forecast(ctx, H)
            summarize((toks.cpu() + torch.tensor([ln["int_min"] for ln in ov.lanes]).view(1, -1, 1)).tolist(),
                      ov.lanes)
            sync()
            times.append(time.perf_counter() - t0)
        med = statistics.median(times) * 1000.0
        total += med
        mem = (f", peak GPU memory {torch.cuda.max_memory_allocated() / 2 ** 20:.0f} MiB"
               if svc.dev.type == "cuda" else "")
        log(f"{ov.kind} ({ov.run}): {samples} samples x {H} rows from {T} context rows: median {med:.1f} ms, "
            f"max {max(times) * 1000.0:.1f} ms per forecast incl. summary (first one, with set-up: "
            f"{first * 1000.0:.0f} ms){mem}")
    if total:
        budget = float(svc.settings["refresh_s"]) * 1000.0
        log(f"both overlays: {total:.0f} ms of every {budget:.0f} ms refresh ("
            + ("OK" if total < 0.75 * budget else
               "tight: try --compile, a longer refresh_s or a shorter horizon_s; fewer samples barely help") + ")")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("BIODASH_FORECAST_URL", "http://127.0.0.1:5001"))
    ap.add_argument("--models", default=os.environ.get("BIODASH_FORECAST_MODELS", os.path.join(FC_DIR, "models")))
    ap.add_argument("--ecg-model", help="model folder for the ECG overlay (default <models>/ecg)")
    ap.add_argument("--acc-model", help="model folder for the accelerometer overlay (default <models>/ecg_acc)")
    ap.add_argument("--device", default=os.environ.get("BIODASH_FORECAST_DEVICE", "auto"), choices=("auto", "cuda", "cpu"))
    ap.add_argument("--compile", action="store_true", default=os.environ.get("BIODASH_FORECAST_COMPILE") == "1",
                    help="torch.compile the decode step (~2.3x faster on an A100; needs Triton; compiles for "
                         "~35 s per model when its overlay is first switched on). Env: BIODASH_FORECAST_COMPILE=1")
    ap.add_argument("--session", help="follow this *_ecg.csv instead of the newest recording")
    ap.add_argument("--record", help="append each forecast's context rows to this JSONL file (checks)")
    ap.add_argument("--seed", type=int, help="fixed sampling seed for every forecast (checks)")
    ap.add_argument("--bench", type=int, nargs="?", const=20, default=0, metavar="N",
                    help="no dashboard: load both models, time N forecasts each (default 20) and exit")
    ap.add_argument("--samples", type=int, help="with --bench: samples per forecast (default: the dashboard's 8)")
    a = ap.parse_args()
    svc = Service(a)
    if a.bench:
        if a.samples:
            svc.settings["samples"] = max(1, min(32, a.samples))
        bench(svc, a.bench)
        return

    def _stop(*_):
        svc.stop = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    svc.run()


if __name__ == "__main__":
    main()
