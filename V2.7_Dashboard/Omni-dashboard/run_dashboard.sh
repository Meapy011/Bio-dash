#!/bin/bash

# Use the repo's .venv automatically if ./setup_env.sh has been run (no need to activate)
_VENV="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.venv"
[ -x "$_VENV/bin/python3" ] && export PATH="$_VENV/bin:$PATH"

echo "========================================"
echo "    OMNI-DASH IGNITION SEQUENCE"
echo "========================================"

echo "[1/4] Hunting for lingering Python processes..."
# Force-kill any ghost threads still running the dashboard
pkill -9 -f "python3 omni.py" 2>/dev/null || echo "  -> No ghost Python processes found."

echo "[2/4] Forcing Port 5000 open..."
# Aggressively kill anything holding the Flask web port
fuser -k 5000/tcp 2>/dev/null || echo "  -> Port 5000 is clean."

echo "[3/4] Flushing Linux Bluetooth Cache..."
# This prevents the DBus "ghost locks" so you never have to reboot the Orin again!
# (It will ask for your Orin password here)
sudo systemctl restart bluetooth
sleep 2 # Give the BlueZ daemon a moment to wake back up

echo "[4/4] FIRING MAIN ENGINE..."
echo "========================================"
# Launch the app natively
python3 omni.py
