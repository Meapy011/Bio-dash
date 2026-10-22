# Live forecasts in Polar-dash (Bio-dash V5.6)

The **🔮 Forecast** pills on the ECG card and the accelerometer card show the next second, predicted
by the H10 models, ahead of the live trace:

- **ECG** (bedside-monitor view and classic view): a dashed line (one representative sample) and a
  shaded band (the range of the 8 samples) ahead of the pen. The pen then draws the real ECG over it.
- **Accelerometer**: the same band and dashed line for X, Y and Z.
- **Status text** next to each pill, for example `1.0 s ahead · 8 samples · 140 ms · error 0.033 mV (hold 0.056)`.
  - The error is the mean |actual − forecast median| over the last 30 s.
  - "hold" is the same error for simply repeating the last value (the baseline to beat).

**V5.6:** what was forecast stays under the live trace after the pen passes (the pen draws the real ECG
over it), and the 🔮 Inference strip under the monitor shows it alone, on the same time axis. The rule: each
new forecast keeps the previous one up to its own start. `forecast_service.py` logs exactly that to
`<Documents>/Bio-dash/Forecasts/<day>/<session time>_forecast_<ecg|acc>[_replay].csv`: `t_ms`, `lead_ms` (from the
last row of context), then per lane the line drawn (the representative sample), median, lo and hi, in mV / mg
(raw, not high-passed). `--no-log` / `BIODASH_FORECAST_LOG=0` turns it off.

This is a research demo trained on one person's 7.2 h of H10 data. It is not a medical device.

## How it works

```
Polar H10 ──BLE──> advanced_worker.py ──> <Documents>/Bio-dash/Polar H10/<device>/<date>/<time>_{ecg,acc,rr}.csv
                                               │                         │
                                               │ tail                    │ tail
                                               ▼                         ▼
                                app.py (FastAPI :5001)      forecast_service.py  (own process, PyTorch)
                                  │   ▲                       waves.LiveAligner -> the 125 Hz model rows
                                  │   └── POST /api/forecast ─ FastForecaster (KV cache + CUDA graph)
                                  │       POST /api/forecast/heartbeat (state <-> pill settings)
                                  ▼
                   websocket /ws: ecg / acc / ppi rows + ecg_forecast / acc_forecast
                                  ▼
                   browser: static/js/forecast.js (pills, bands, errors) + ecg_monitor.js (sweep overlay)
```

- `forecast_service.py` runs next to `app.py`. `run_dashboard.sh` starts it and restarts it if it stops.
  - Because it is a separate process, torch never loads into the web server.
  - A crash or a GPU stall can't freeze the dashboard.
  - It follows the session the worker is recording and builds model rows with `analysis_dashboard/waves.py`
    (`LiveAligner`). These are the same 125 Hz grid rows the models were trained on (see the check below).
- Every 0.5 s, for each pill that is on, it forecasts 1 s ahead (125 rows) as 8 samples.
  - Sampling: top-k 100, temperature 1.0, from the last 132 rows of context.
  - Inference uses bf16 autocast, as in training.
  - It posts the band, the median and one representative sample. A representative sample is used
    because averaging samples would smear a heartbeat whose timing is uncertain.
- Models (each loads the first time its pill is switched on):

  | Overlay | Folder | Run | Inputs | Held-out loss |
  |---|---|---|---|---|
  | ECG | `models/ecg` | `hf_g125_ecg_d3_6L_384D_40k` | ECG | 2.4488 |
  | Accelerometer | `models/ecg_acc` | `hf_g125_ecg_acc_d4_6L_384D_40k` | ECG + acc X/Y/Z | 2.6472 |

  Both models are 6 layers × 384 dims with a 256-row context (2.048 s). They have 12.5 M and 17.2 M parameters.
- Without torch, transformers or the model folders, the service keeps running and reports why.
  The pills then show the reason, and everything else works as before.

## Files

| File | What |
|---|---|
| `../forecast_service.py` | The service. `--bench` times the models without a dashboard; `--record` / `--seed` are for checks |
| `../replay_session.py` | Replays a recorded session in real time as if the strap were connected |
| `check_replay.py` | Compares the service's context rows with the offline (training) rows of the replayed stretch |
| `fast_forecast.py` | Fast sampler: static KV cache + one CUDA graph per decode step |
| `modeling_csv_mc_int.py`, `configuration_csv_mc_int.py`, `processing_csv_mc_int.py` | The model code, copied from ReaLLM-Forge `hf_csv_mc_int/` |
| `models/ecg`, `models/ecg_acc` | `config.json`, `pytorch_model.bin`, `columns.json`, `training_state.json`, `RUN.txt` |
| `../static/js/forecast.js` | Pills, chart bands, status text and running error |
| `../static/js/ecg_monitor.js` | Bedside-monitor sweep, now with the forecast overlay canvas |
| `../app.py` | `GET/POST /api/forecast/settings`, `POST /api/forecast/heartbeat`, `POST /api/forecast` |
| `../forecast_settings.json` | The pill switches and settings (written by the page) |

## Setup

The service needs Python 3.10+ with **PyTorch** and **transformers 4.x**, ideally on an NVIDIA GPU. It was tested
with torch 2.7.1 + CUDA 11.8 and transformers 4.53.3. CPU works, but slowly.

### Jetson Orin (JetPack 6)

1. Install NVIDIA's PyTorch build for your JetPack version, following NVIDIA's *Installing PyTorch for
   Jetson Platform* guide or the Jetson AI Lab pip index.
   - A plain `pip install torch` gives a CPU-only build on Jetson.
2. Install transformers into the same Python. Keep it below 5:

   ```bash
   pip install "transformers>=4.40,<5" numpy
   ```

   - A plain `pip install transformers` now gets 5.x.
   - Model code older than 2026-10-21 can't even build the model on 5.x, so the service would show "⚠ couldn't load …".
   - The current `forecast/modeling_csv_mc_int.py` has the fix, but the service hasn't run on an Orin with 5.x yet.

3. Choose which Python runs the service. Either works:
   - **The dashboard's own venv** (the release folder's `.venv`, made by `setup_env.sh`): install torch and
     transformers into it. `run_dashboard.sh` then uses it automatically.
   - **A separate env**: point the launcher at it.

     ```bash
     export BIODASH_FORECAST_PYTHON=/path/to/env/bin/python3
     ./run_dashboard.sh
     ```

4. Check it, without the dashboard or the strap:

   ```bash
   cd polar_dashboard
   $BIODASH_FORECAST_PYTHON forecast_service.py --bench
   ```

   - It should say `device: cuda (Orin)`, print the time per forecast for both models, and end with `OK`.
   - Both overlays together must fit well inside the 500 ms refresh.
   - Reference on nv03 (A100): see the numbers at the end of this file.
5. Start the dashboard as usual, open `http://<orin>:5001`, and switch the 🔮 pills on.

If the Orin is too slow:

- Most of the time goes into 124 sequential decode steps, each about 300 small GPU kernels.
  So the horizon and the refresh matter, while the number of samples barely does (4 instead of 8 saves ~4%).
- **Compile the decode step.** On the A100 this is 2.3× faster (ECG 140 → 59 ms, joint 192 → 83 ms).
  - It needs Triton for the Orin's PyTorch.
  - Each model compiles for ~35 s (longer on the Orin) the first time its pill is switched on.
  - If compiling fails, the service logs it and uses the plain CUDA graph.

  ```bash
  BIODASH_FORECAST_COMPILE=1 ./run_dashboard.sh
  $BIODASH_FORECAST_PYTHON forecast_service.py --bench --compile      # check it first
  ```

- Use the maximum power mode: `sudo nvpmodel -m 0`, then `sudo jetson_clocks`.
- Or ask for less work, such as a slower refresh or a shorter horizon:

  ```bash
  curl -X POST localhost:5001/api/forecast/settings -H 'Content-Type: application/json' \
       -d '{"refresh_s": 1.0, "horizon_s": 0.5}'
  ```

### Settings

`polar_dashboard/forecast_settings.json` is written when you click a pill. It can also be set through
`POST /api/forecast/settings`. The service applies changes within 1 s.

| Key | Default | Range |
|---|---|---|
| `ecg`, `acc` | `false` | the pills |
| `horizon_s` | 1.0 | 0.25–1.0 (the 256-row context limit) |
| `refresh_s` | 0.5 | 0.25–5 |
| `samples` | 8 | 1–32 |

### Environment variables

| Variable | Effect |
|---|---|
| `BIODASH_FORECAST=0` | `run_dashboard.sh` doesn't start the service |
| `BIODASH_FORECAST_PYTHON` | The Python that has torch (default: `python3` from the release `.venv` / PATH) |
| `BIODASH_FORECAST_DEVICE` | `auto` (default), `cuda` or `cpu` |
| `BIODASH_FORECAST_COMPILE=1` | torch.compile the decode step (see above) |
| `BIODASH_FORECAST_MODELS` | Models folder (default `polar_dashboard/forecast/models`) |
| `BIODASH_FORECAST_URL` | Dashboard address for the service (default `http://127.0.0.1:5001`) |
| `BIODASH_FORECAST_REMOTE=1` | (for `app.py`) Accept forecasts / heartbeats from another computer |
| `BIODASH_DATA_DIR` | Where recordings live (default `<Documents>/Bio-dash`); used by the worker, app, service and replay |

## Testing without the strap: replay

`replay_session.py` writes a new session (`Polar H10 REPLAY`) in real time:

- The ECG goes out in 73-sample frames, the accelerometer frame by frame, and each frame lands 40 ms after its last sample.
- The dashboard and the service follow it as if the strap were connected.
- Timestamps move to "now" by whole 8 ms steps, so the model rows are exactly the offline rows, shifted.
- Older recordings (like the Drive export) have no HR column in the ECG / R-R files. The replay fills it
  from the strap's 1 Hz HR file, so the HR tiles work.

Use a separate data folder, so the test stays out of your recordings and a connected strap can't take
over. Start `app.py` on its own: `run_dashboard.sh` also starts the Bluetooth worker.

```bash
cd polar_dashboard
export BIODASH_DATA_DIR=/tmp/biodash_test
python3 app.py &                                    # the dashboard: http://localhost:5001
python3 forecast_service.py --record /tmp/contexts.jsonl --seed 1 &
curl -X POST localhost:5001/api/forecast/settings -H 'Content-Type: application/json' -d '{"ecg": true, "acc": true}'
python3 replay_session.py "<Drive export>.zip" --member "Polar H10 <strap ID>/<date>/<HH-MM-SS>" \
        --start-s 600 --duration-s 100
kill %2                                             # stop the service (flushes the record), then:
python3 forecast/check_replay.py /tmp/contexts.jsonl \
        "$BIODASH_DATA_DIR/Polar H10/Polar H10 REPLAY/<date>/<time>_replay_info.json"
```

The session 2026-10-14 14-42-36 is in both models' held-out split, so its forecasts are a fair test.
`check_replay.py` exits non-zero if the live rows aren't the training rows.

## Known limits

- **Live alignment.** The live aligner finalises a joint row once the accelerometer has run 10 ms past it.
  Around the strap's clock re-syncs, a joint row can then differ from the offline row by a few mg
  (0.04% of rows over the whole dataset; none in the 100 s replay test). ECG-only rows are identical.
- **Accelerometer timestamps.** The strap samples at ~205.6 Hz, but the worker stamps samples 5 ms apart.
  This leaves a small timing error (~3 mg RMS). It is the same in training and live, so the models
  match their data. A fix (re-spacing each frame) would need a retrained joint model.
- **Horizon.** The context limit is 256 rows, so a 1 s forecast uses 132 rows (1.06 s) of context.
  Longer horizons would need a model with a longer context.
- **How far ahead you see.** The H10 sends ECG in 73-sample frames (0.56 s), so the newest real sample is
  already up to ~0.6 s old.
  - In the test, an ECG forecast started a median 0.44 s before "now" and reached 0.55 s past it.
  - Accelerometer forecasts started 0.65 s back and reached 0.34 s ahead, because joint rows also wait
    for the accelerometer frames.

## Reference numbers (nv03, A100-SXM4-40GB, torch 2.7.1 + CUDA 11.8, 2026-10-21)

End-to-end test setup:

- `app.py` + `forecast_service.py` + a websocket client + headless Chrome.
- Replay of 100 s of the held-out session 2026-10-14 14-42-36 (from 600 s).
- Default settings: 1 s ahead, every 0.5 s, 8 samples.

| | ECG overlay | Accelerometer overlay |
|---|---|---|
| Model load | 0.6 s | 0.4 s |
| One forecast (8 × 125 rows from 132 rows), incl. summary | 140 ms | 187–191 ms |
| Prefill / 124 decode steps / summary | 8.5 ms / 131 ms (1.06 ms each) / 1.4 ms | 9.3 ms / 179 ms (1.44 ms each) / 5.6 ms |
| With 4 samples instead of 8 | 134 ms | 176 ms |
| With `--compile` (warm cache; cold: ~35 s per model) | 61 ms | 88 ms |
| Peak GPU memory | 141 MiB | 278 MiB (both models loaded) |
| First forecast (CUDA-graph capture) | ~0.5 s | ~0.35 s |
| Forecasts reaching the page | every 0.505 s (median); 3 ms from service to websocket | every 0.504 s; 5 ms |
| Live context rows vs training rows (`check_replay.py`) | 11,070 of 11,070 identical | 11,991 of 11,991 identical |
| Forecast median, mean \|error\| vs "hold the last value" | 0.033 mV vs 0.056 mV | x 9.4 / y 14.4 / z 11.5 mg vs 9.2 / 15.5 / 11.3 mg |

Other results:

- Both overlays together used 331 ms of every 500 ms refresh (148 ms with `--compile`).
- **Pill off / on.** No ECG forecasts arrived while the pill was off (from 1.5 s after the click).
  They resumed 0.6–1.2 s after switching it back on.
- **Service stopped.** Both status texts said "⚠ forecast service offline" within 7 s.
- **Without PyTorch.** The service keeps running and the pills show why.
- **Browser console.** No errors from the page, apart from the sidebar's probes of dashboards that
  weren't running and a missing favicon.
