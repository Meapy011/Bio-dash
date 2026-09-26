# Bio-dash

Live browser dashboards for BLE biometric and environmental sensors, built for Linux (Jetson Orin Nano, x86 laptops) using BlueZ. Each dashboard has two parts. A **hardware worker** talks to the device over Bluetooth and logs to CSV, and a **web app** streams that data to charts in the browser.

| Dashboard | Device | Streams | Port | Launch |
|---|---|---|---|---|
| `polar_dashboard/` | Polar H10 chest strap | ECG 130 Hz, accelerometer 200 Hz, HR, R-R / RMSSD | 5001 | `./run_dashboard.sh` |
| `viatom_dashboard/` | Viatom Checkme O2 Ultra (advertises as `Band-WU …`) | SpO₂, pulse, battery | 5000 | `./run_dashboard.sh` |
| `nrf-sen_dashboard/` | Atmos-Mini (Sensirion SEN69C over Nordic UART) | PM1/2.5/4/10, RH, temp, VOC, NOx, HCHO, CO₂ | 5002 | `./start_air.sh` |
| `Omni-dashboard/` | Polar H10 + Viatom O2 at the same time | Both of the above in one view | 5000 | `./run_dashboard.sh` |

> ⚠️ **Not a medical device.** The stress/HRV labels are rough RMSSD thresholds for experimentation, not clinical interpretation.

## Requirements

- Linux with BlueZ and a working Bluetooth adapter
- Python **3.10+** (required by `polar-python`)
- Omni only: **two adapters** (`hci0` and `hci1`). Change `POLAR_ADAPTER` and `GENERAL_ADAPTER` in `omni.py` to match your setup.

```bash
pip3 install -r requirements.txt                    # all four dashboards
pip3 install -r polar_dashboard/requirements.txt    # or just one
```

## Usage

```bash
cd polar_dashboard
./run_dashboard.sh
# open http://localhost:5001
```

1. Wake the device. The H10 needs wet electrodes. The O2 Ultra only advertises while it's powered on and measuring.
2. Pick it from the scanner panel and click **Connect**.
3. Charts start as soon as data arrives. If the device goes quiet, the scanner comes back automatically.

`Ctrl+C` stops both the worker and the web server. The Polar and air launchers archive the previous session's CSVs into `logs_*/archive/` on each start.

> The Omni launcher runs `sudo systemctl restart bluetooth` to clear stale BlueZ state, so it will ask for your password.

## Project layout

Each dashboard keeps its Python and web front end separate:

```
polar_dashboard/
├── advanced_worker.py      # BLE worker: talks to the device, writes CSV
├── app.py                  # web server: API + live stream (no HTML inside)
├── requirements.txt
├── run_dashboard.sh
├── templates/index.html    # page markup
└── static/
    ├── js/dashboard.js     # charts, scanner panel, stream handling
    ├── css/tailwind.css    # compiled Tailwind (Polar, air, Omni)
    ├── css/style.css       # hand-written CSS (air, Viatom)
    └── vendor/             # Chart.js, Luxon, adapter, streaming plugin
```

`templates/index.html` is served as a plain file, not rendered through Jinja, so you can edit the HTML, JS and CSS directly and just refresh the browser. Only changes to the `.py` files need a restart.

## Offline use

The dashboards don't need internet access. Every library is included under each dashboard's `static/`:

| Library | Version | Used by |
|---|---|---|
| Chart.js | 3.9.1 | Polar, Viatom, Omni |
| Luxon + chartjs-adapter-luxon | 3.0.1 / 1.2.0 | Polar, Viatom, Omni |
| chartjs-plugin-streaming | 2.0.0 | Polar, Viatom, Omni |
| Tailwind CSS (compiled, not the play CDN) | 3.4 | Polar, air, Omni |

To re-download them, or after changing a version in the script:

```bash
./fetch_vendor.sh              # wget the JS libs from jsDelivr, verify SHA-256
./fetch_vendor.sh --tailwind   # also rebuild tailwind.css (needs Node/npx)
```

Tailwind CSS is compiled, so it only contains classes that already appear in `templates/` and `static/js/`. **If you add a new Tailwind class, run `./fetch_vendor.sh --tailwind`,** or the new class will have no effect.

## How it works

```
 BLE device ──► worker (bleak) ──► CSV log ──► web app (tails file) ──► browser
                     ▲                              │
                     └──── command.json ◄───────────┘  (Connect clicks)
```

- **Workers** own the Bluetooth link. They write every sample to CSV, write `devices.json` for the scanner panel, and read `command.json` when you click Connect. The Polar worker also writes `status.json` (scanning / connecting / streaming).
- **Web apps** follow the newest CSV and push batches to the browser. Polar and the air app use WebSockets; Viatom and Omni use Server-Sent Events.
- Logs are plain CSV with epoch-ms timestamps, so they open directly in pandas or a spreadsheet.

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| Scanner finds nothing | Device asleep, or still connected to a phone app or another host. Run `bluetoothctl devices Connected`. |
| O2 Ultra never appears | It stops advertising shortly after the finger comes off. Keep it measuring while you scan. |
| First Polar connect fails | Common BlueZ `le-connection-abort-by-local`. The worker retries 3× automatically. |
| New Tailwind class does nothing | Rebuild the CSS: `./fetch_vendor.sh --tailwind` |
| Port already in use | Viatom and Omni both use 5000, so don't run them at the same time. |

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
