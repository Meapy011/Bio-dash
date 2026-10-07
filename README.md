# Bio-dash

Live browser dashboards for BLE biometric and environmental sensors -- Polar H10 chest strap,
Viatom / Checkme O2 ring, Atmos air-quality monitors -- built for Linux (Jetson Orin Nano, x86
laptops) with BlueZ. Each dashboard pairs a **hardware worker** (talks to the device, records CSV)
with a **web app** (live charts in the browser).

> ⚠️ **Not a medical device.** Stress / HRV labels are rough thresholds for experimentation, not clinical interpretation.

## Versions

Each release lives in its own folder with its own README; **use the newest**. Every release is
also tagged (`v1`, `v2.0`, `v2.2`, `v2.3` … `v2.8`, `v3.0`), so you can check out any of them.

| Folder | Release | What it brought |
|---|---|---|
| [`V3.0_Dashboard/`](V3.0_Dashboard) | **V3.0 (latest)** | Hydro-dash for the HidrateSpark PRO 2 (new, still being tested), overnight SpO₂ report, shutdown / reboot buttons, architecture diagrams |
| [`V2.8_Dashboard/`](V2.8_Dashboard) | V2.8 | Debug panels, radio routing, general Atmos dashboard (Mini / Sphere S4 / S5, Wi-Fi), tile trends, ECG Monitor/Classic toggle, services (`biodash.py`), keep-awake, auto-reconnect, recordings in `Documents/Bio-dash` |
| [`V2.7_Dashboard/`](V2.7_Dashboard) | V2.7 | ECG monitor follows the dashboard's slate / rose theme |
| [`V2.6_Dashboard/`](V2.6_Dashboard) | V2.6 | In-tree `vN-code` archive folders removed |
| [`V2.5_Dashboard/`](V2.5_Dashboard) | V2.5 | Bedside-monitor ECG sweep for the Polar H10 |
| [`V2.4_Dashboard/`](V2.4_Dashboard) | V2.4 | Viatom dashboard redesigned on the Polar layout |
| [`V2.3_Dashboard/`](V2.3_Dashboard) | V2.3 | `setup_env.sh` isolated venv; launchers use it automatically |
| [`V2.2_Dashboard/`](V2.2_Dashboard) | V2.2 | Offline assets (vendored libraries, compiled Tailwind), HTML/CSS/JS split out of Python |
| [`V2.0_Dashboard/`](V2.0_Dashboard) | V2.0 | First optimisation pass: batched streaming, sensor-clock timestamps, BLE reliability fixes |
| [`V1_Dashboard/`](V1_Dashboard) | V1 | Original Polar H10, Viatom O2, SEN69C air and Omni dashboards |

## Quick start

```bash
cd V3.0_Dashboard
cat README.md            # full documentation for this release
```

Full history of changes: [`CHANGELOG.md`](CHANGELOG.md).

## Keeping data out of the repo

Recordings (CSV) and Bluetooth MAC addresses don't belong in this repository. `scrub_repo.py`
(standard-library Python) finds and removes them:

```bash
./scrub_repo.py                 # check the current files AND the full history (screenshots too, if tesseract is installed)
./scrub_repo.py fix-files       # clean the current files, then commit as usual
./scrub_repo.py fix-history     # rewrite all history (needs git-filter-repo; writes a backup bundle first; then force-push)
./scrub_repo.py install-hook    # pre-commit hook: refuse commits that add CSVs or MAC addresses
```

Screenshots that show an address can't be cleaned automatically -- `check` lists them so they can be retaken.

## Acknowledgements

Bio-dash started from **[klei22/viatom-ble](https://github.com/klei22/viatom-ble)** (a fork of
[ecostech/viatom-ble](https://github.com/ecostech/viatom-ble), MIT), the Python script for reading
Viatom / Wellue oximeters over Bluetooth that kicked off these dashboards. Thank you!

## License

Apache 2.0 -- see [`LICENSE`](LICENSE).
