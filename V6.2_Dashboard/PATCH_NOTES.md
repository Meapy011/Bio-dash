# Hydro sees bottles with a newer bleak; forecasts work on a Jetson Orin
- Hydro-dash: the scan callback takes exactly (device, advertisement), as bleak 1.x+ requires; before, every scan failed with "callback must be callable with 2 parameters" and no bottle was listed
- tools/ble_scan.py --adapter works with a newer bleak
- tools/orin_setup.sh installs transformers 4.x and Pillow 9.1+ into the forecast environment, then checks transformers imports (with the Jetson's own PyTorch, the old system Pillow broke every model load)
- Running tools/orin_setup.sh again fixes an environment made before
- The forecast's "couldn't load" message shows how to see the real error and the usual fix
