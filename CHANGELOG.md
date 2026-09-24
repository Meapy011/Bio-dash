# Changelog

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

The original dashboards: Polar H10 (v1 → v4 iterations), Viatom O2, SEN69C air monitor and Omni. See `V1_Dashboard/`.
