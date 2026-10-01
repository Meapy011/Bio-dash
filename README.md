# Bio-dash

Live browser dashboards for BLE biometric and environmental sensors -- Polar H10 chest strap,
Viatom / Checkme O2 ring, Atmos air-quality monitors -- built for Linux (Jetson Orin Nano, x86
laptops) with BlueZ. Each dashboard pairs a **hardware worker** (talks to the device, records CSV)
with a **web app** (live charts in the browser).

> ⚠️ **Not a medical device.** Stress / HRV labels are rough thresholds for experimentation, not clinical interpretation.

## Versions

Each release lives in its own folder with its own README; **use the newest**. Every release is
also tagged (`v1`, `v2`, `v2.2`, `v2.3` … `v2.4`), so you can check out any of them.

| Folder | Release | What it brought |
|---|---|---|
| [`V2.4_Dashboard/`](V2.4_Dashboard) | **V2.4 (latest)** | Viatom dashboard redesigned on the Polar layout |
| [`V2.3_Dashboard/`](V2.3_Dashboard) | V2.3 | `setup_env.sh` isolated venv; launchers use it automatically |
| [`V2.2_Dashboard/`](V2.2_Dashboard) | V2.2 | Offline assets (vendored libraries, compiled Tailwind), HTML/CSS/JS split out of Python |
| [`V2_Dashboard/`](V2_Dashboard) | V2 | First optimisation pass: batched streaming, sensor-clock timestamps, BLE reliability fixes |
| [`V1_Dashboard/`](V1_Dashboard) | V1 | Original Polar H10, Viatom O2, SEN69C air and Omni dashboards |

## Quick start

```bash
cd V2.4_Dashboard
cat README.md            # full documentation for this release
```

Full history of changes: [`CHANGELOG.md`](CHANGELOG.md).

## License

Apache 2.0 -- see [`LICENSE`](LICENSE).
