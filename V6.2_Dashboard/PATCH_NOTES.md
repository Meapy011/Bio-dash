# Forecasts work on a Jetson Orin out of the box
- tools/orin_setup.sh installs transformers 4.x and Pillow 9.1+ into the forecast environment, then checks transformers imports (with the Jetson's own PyTorch, the old system Pillow broke every model load)
- Running tools/orin_setup.sh again fixes an environment made before
- The forecast's "couldn't load" message shows how to see the real error and the usual fix
