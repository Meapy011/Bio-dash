# Changelog

## V2.6

- **Removed in-tree version archives.** The `v1-code/` … `v4-code/` folders are gone from every dashboard: 10 folders, including old Polar session logs. Earlier versions now live as their own tagged releases, so each dashboard folder holds only the current code.

## V2.5

### Polar H10: bedside-monitor ECG
- **Hospital-style sweep display.** The live ECG no longer scrolls. It is drawn like a patient monitor: a pen sweeps left to right and wipes a small gap just ahead of itself, over an ECG-paper grid (1 mm / 5 mm squares at the standard 25 mm/s).
- Phosphor-green trace with glow on a black screen, with a large HR readout and a heart that flashes on each detected QRS.
- **QRS beep** (off by default). The toggle is needed because browsers only allow audio after a click.
- **Gain button** cycles AUTO → 5 → 10 → 20 mm/mV. 10 mm/mV is the clinical standard; AUTO fits the waveform to the screen.
- A 0.5 Hz high-pass filter on the display removes breathing and movement baseline wander. It only affects what's drawn; the CSV logs are untouched raw data.
- Samples are replayed at true speed ~0.9 s behind live, so the pen moves smoothly even though the H10 delivers ECG in ~0.5 s bursts. This also works when the browser is on a different machine than the Orin, even if their clocks don't match.
- Shows **NO SIGNAL** when data stops.
- New `static/js/ecg_monitor.js`, a standalone renderer that doesn't use Chart.js.

## V2.4

### Viatom O2 Ultra dashboard redesign
- **Rebuilt on the Polar H10 layout.** It now has the same Tailwind dark theme, header with a live status badge, scanner panel with a radar pulse and device cards, and metric tiles and chart cards. It uses a sky-blue accent so the two dashboards are easy to tell apart.
- Three tiles: **SpO₂**, **Pulse Rate** (with the session min/max), and **Device Battery**, whose bar turns amber at 40% and red at 20%.
- SpO₂ and pulse trend charts show 60 s windows sized for the O₂'s ~2 s update rate.
- The scanner panel follows the worker state. It shows "Handshaking…" while connecting, hides once streaming, and returns on disconnect. Device cards only rebuild when the set of devices changes.
- Calibration (`0xA5`) shows as "Sensor Settling…" instead of blanking the readings.
- If the stream goes quiet for 15 s while connected, the page shows a **Signal Lost** badge. The badge shows **Server Offline** if the web server stops.
- The old hand-written `style.css` is removed. The page now uses compiled Tailwind, and `fetch_vendor.sh --tailwind` rebuilds it.

## V2.3

- **`setup_env.sh`** creates an isolated `.venv/` (whole suite or a single dashboard), checks for Python ≥ 3.10, and verifies imports. The launch scripts use `.venv/` automatically when it exists. `.venv/` is gitignored.

## V2.2

- **Fully offline.** Chart.js 3.9.1, Luxon 3.0.1, chartjs-adapter-luxon 1.2.0 and chartjs-plugin-streaming 2.0.0 are included in each dashboard's `static/vendor/`. These are the same versions and files the CDN served.
- **Tailwind is compiled instead of loaded from the play CDN.** The play CDN downloaded a ~400 KB script and compiled CSS in the browser on every page load. Each page now loads a ~11 KB static `tailwind.css`, which is also lighter on the Orin.
- **New `fetch_vendor.sh`.** It downloads the libraries with `wget`, verifies them against pinned SHA-256 checksums, and rebuilds Tailwind with `--tailwind`. Set `CDN=` to use a mirror.

## V2.1

### Structure
- **HTML, CSS and JS moved out of Python.** Every dashboard now has `templates/index.html`, `static/js/dashboard.js`, and `static/css/style.css` where it has custom styles. The Python files contain only server and BLE logic.
- FastAPI dashboards (Polar, air) serve the page with `FileResponse` and mount `/static`. Flask dashboards (Viatom, Omni) use `send_from_directory` with an explicit `root_path`, so they work regardless of the directory you launch from.
- **Per-dashboard `requirements.txt`** for all four dashboards. The root `requirements.txt` pulls in all of them with `-r`. `bleak` and `polar-python` are capped below their next major version to prevent surprise upgrades.

### Fixes
- **Air dashboard page was broken.** Its script used `split('\n')` inside a non-raw Python string, so Python turned `\n` into a real newline and the browser hit a JS syntax error. The whole page script failed. Moving the JS into its own file fixes this, and the rest of that bug class along with it.

## V2 — 2026-09-22

A performance and reliability pass across all four dashboards. Verified on hardware: Polar H10 handshake and streaming, and Viatom O2 Ultra connect → SpO₂/HR logging.

### Polar H10 (`polar_dashboard/`)

**Performance**
- WebSocket traffic is batched. The server used to send one message per sample (~330 msgs/s per open tab) and now sends one message per poll per stream: 365 ECG samples arrive as 5 messages instead of 365.
- Samples are timestamped from the H10's own PMD frame clock, locked to wall-clock time on the first frame (it re-anchors if drift exceeds 1 s). This removes BLE delivery jitter at the source; ECG spacing is a steady ~7.7 ms. The browser-side `lastEcgTs += 7.69` workaround is gone.
- CSV rows are written with `writerows` per frame instead of one call per sample.

**Fixes**
- The scanner panel used to vanish permanently after the first packet. It now comes back after 5 s without data, so you can reconnect without reloading.
- Connecting no longer flickers. The worker reports scanning / connecting / streaming through a new `/api/status` endpoint, and the page shows "Handshaking… (attempt n/3)" instead of redrawing stale device cards.
- Clicking Connect in the middle of a scan no longer blanks the device list.
- Up to 3 connect attempts before falling back to scanning, which handles BlueZ's frequent first-attempt abort.
- The WebSocket reconnects automatically.
- The app now follows a new log file when the worker restarts.
- Callbacks read `polar_python`'s real fields (`heartrate`, `rr_intervals`, `timestamp`, `data`) instead of guessing through `__dict__`.
- Device names are HTML-escaped in the scanner list.

### Viatom O2 Ultra (`viatom_dashboard/`)
- **Continuous scanning.** Short 4 s scan windows kept missing the O2's slow adverts. The worker now runs one long-lived scanner and keeps devices listed for 30 s after their last advert. It logs `Spotted …` and `Lost …` events.
- The name filter now matches `Band-WU` (the O2 Ultra's advertised name), not just the hardcoded MAC.
- Removed a hardcoded `~/Forks/Bio-dash/...` path, so the dashboard runs from any checkout location.
- The notify fallback used to subscribe to the *service* UUID, which can never work. It now fails with a clear error instead.
- One failed poll write no longer drops the link. The worker flips the write mode and retries, giving up only after 3 misses.
- Notification fragments that don't start with the `0x55` frame header are ignored instead of being parsed into garbage readings.
- Removed dead `static/dashboard.js` and `templates/index.html`, which called a nonexistent `/api/data` endpoint.

### Atmos-Mini / SEN69C (`nrf-sen_dashboard/`)
- **Fixed dropped lines.** The UART handler processed only the first line per BLE notification, so readings lagged and backed up when two lines arrived together.
- The tailer used to glob and stat the log directory on every 50 ms poll; that now happens every 2 s.
- The scanner also matches `Atmos` and any device advertising the Nordic UART service, so board swaps and renames still show up.
- Reuses the BLE device found during the scan instead of running a second full scan to connect.
- `start_air.sh` pointed to port 8000; it now points to 5002, where the app actually runs.
- Auto-reconnect, stale-data detection and escaping, same as the Polar dashboard.

### Omni (`Omni-dashboard/`)
- **Fixed lost samples.** The SSE handler cleared the shared ECG/ACC buffers while the BLE thread was appending to them, which dropped samples and split data between open tabs. It's replaced by a thread-safe ring buffer with a cursor per client, so every tab gets the full stream.
- The R-R chart gets one point per heartbeat. It used to replot the last value ~20×/s.
- A `0xA5` calibration byte from the O2 no longer tears down a healthy connection, because link state and display state are now separate.
- Uses the same sensor-clock timestamps as the Polar dashboard.

### Repo
- `requirements.txt` now lists FastAPI and uvicorn, which were missing, and drops the unused `unicorn`, `numpy` and `flask-cors`.
- Added `.gitignore` for `__pycache__/`, `logs*/` and runtime JSON so session and health data don't get committed.
- `bleak` minimum raised to 0.22; tested on 3.0.2.

### Known limitations
- Viatom and Omni both use port 5000.
- Omni defines `POLAR_ADAPTER = "hci0"` but doesn't use it yet, so both devices share `hci1` and scanning pauses while the Polar streams.
- Pages load Tailwind and Chart.js from CDNs, so a fully offline machine won't render them.

## V1

The original dashboards: Polar H10 (v1 → v4 iterations), Viatom O2, SEN69C air monitor and Omni. Earlier versions are available as tagged releases and are no longer kept as `vN-code/` folders in the tree.
