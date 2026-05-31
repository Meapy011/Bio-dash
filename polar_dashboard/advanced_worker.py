import asyncio
import csv
import datetime
import os
import sys
import json
import time
import logging
from bleak import BleakScanner
from bleak.exc import BleakError
from polar_python import PolarDevice

# Setup Logging
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S", 
    handlers=[logging.StreamHandler(sys.stdout)]
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs_advanced")
os.makedirs(LOGS_DIR, exist_ok=True)

DEVICES_FILE = os.path.join(LOGS_DIR, "devices.json")
COMMAND_FILE = os.path.join(LOGS_DIR, "command.json")

date_str = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
ecg_log_path = os.path.join(LOGS_DIR, f"polar_ecg_{date_str}.csv")
acc_log_path = os.path.join(LOGS_DIR, f"polar_acc_{date_str}.csv")
ppi_log_path = os.path.join(LOGS_DIR, f"polar_ppi_{date_str}.csv")

ecg_file = open(ecg_log_path, mode="a", newline="")
acc_file = open(acc_log_path, mode="a", newline="")
ppi_file = open(ppi_log_path, mode="a", newline="")

ecg_writer = csv.writer(ecg_file)
acc_writer = csv.writer(acc_file)
ppi_writer = csv.writer(ppi_file)

ecg_writer.writerow(["Timestamp_Epoch_ms", "ECG_mV", "HR_BPM"])
acc_writer.writerow(["Timestamp_Epoch_ms", "X_mg", "Y_mg", "Z_mg"])
ppi_writer.writerow(["Timestamp_Epoch_ms", "PPI_ms", "HR_BPM"])

current_hr = 0
ecg_counter = 0
selected_target_mac = None
last_heartbeat = 0

def hr_callback(data):
    global current_hr, last_heartbeat
    last_heartbeat = time.time()
    
    bpm = getattr(data, 'bpm', None) or getattr(data, 'heart_rate', None)
    if bpm is None and isinstance(data, (list, tuple)) and len(data) > 0: bpm = data[0]
    if bpm is None and hasattr(data, '__dict__'):
        for val in data.__dict__.values():
            if isinstance(val, int) and 30 < val < 220:
                bpm = val
                break
    if bpm is not None: current_hr = int(bpm)

    rr_list = getattr(data, 'rr_intervals', []) or getattr(data, 'rrs', []) or getattr(data, 'rrs_ms', [])
    if not rr_list and hasattr(data, '__dict__'):
        for val in data.__dict__.values():
            if isinstance(val, list):
                rr_list = val
                break
                
    if rr_list:
        now_ms = int(datetime.datetime.now().timestamp() * 1000)
        for rr in rr_list:
            try:
                rr_val = int(rr)
                if 200 < rr_val < 2000:
                    ppi_writer.writerow([now_ms, rr_val, current_hr])
            except (ValueError, TypeError): continue
    ppi_file.flush()

def acc_callback(data):
    samples = getattr(data, 'samples', []) or getattr(data, 'acc', [])
    if not samples and hasattr(data, '__dict__'):
        for val in data.__dict__.values():
            if isinstance(val, list):
                if len(val) > 0 and isinstance(val[0], int) and val[0] > 1000000000000000: continue
                samples = val
                break
    if not samples: return

    sample_spacing_ms = 1000.0 / 200.0
    start_time = datetime.datetime.now() - datetime.timedelta(milliseconds=len(samples) * sample_spacing_ms)

    for idx, val in enumerate(samples):
        try:
            if isinstance(val, (list, tuple)) and len(val) >= 3:
                x = float(val[0]); y = float(val[1]); z = float(val[2])
            elif isinstance(val, dict):
                x = float(val.get('x', val.get('X', 0)))
                y = float(val.get('y', val.get('Y', 0)))
                z = float(val.get('z', val.get('Z', 0)))
            else:
                x = float(getattr(val, 'x', 0)); y = float(getattr(val, 'y', 0)); z = float(getattr(val, 'z', 0))
            
            sample_time = start_time + datetime.timedelta(milliseconds=idx * sample_spacing_ms)
            acc_writer.writerow([int(sample_time.timestamp() * 1000), x, y, z])
        except (ValueError, TypeError, IndexError): continue
    acc_file.flush()

def ecg_callback(data):
    global current_hr, ecg_counter
    samples = getattr(data, 'samples', []) or getattr(data, 'ecg', []) or getattr(data, 'voltages', [])
    if not samples and hasattr(data, '__dict__'):
        for key, val in data.__dict__.items():
            if isinstance(val, list) and len(val) > 0:
                if isinstance(val[0], int) and val[0] > 1000000000000000: continue
                samples = val
                break
    if not samples: return

    sample_spacing_ms = 1000.0 / 130.0
    start_time = datetime.datetime.now() - datetime.timedelta(milliseconds=len(samples) * sample_spacing_ms)

    for idx, val in enumerate(samples):
        try:
            uv_value = int(getattr(val, 'voltage', getattr(val, 'ecg_uv', val)))
            mv_value = round(uv_value / 1000.0, 3)
            sample_time = start_time + datetime.timedelta(milliseconds=idx * sample_spacing_ms)
            ecg_writer.writerow([int(sample_time.timestamp() * 1000), mv_value, current_hr])
            ecg_counter += 1
        except (ValueError, TypeError): continue
    ecg_file.flush()
        
    try:
        last_uv = int(getattr(samples[-1], 'voltage', getattr(samples[-1], 'ecg_uv', samples[-1])))
        sys.stdout.write(f"\r[ ❤️ HR: {current_hr:3d} BPM ] [ ECG: {ecg_counter:6d} ] || ⚡ ECG: {last_uv / 1000.0:>6.3f} mV    ")
        sys.stdout.flush()
    except (ValueError, TypeError, IndexError): pass

async def scan_and_list_devices():
    global selected_target_mac
    
    if os.path.exists(COMMAND_FILE):
        try: os.remove(COMMAND_FILE)
        except Exception: pass

    logging.info("Radar active. Sweeping for Polar H10 units...")
    found_devices = {}

    def detection_callback(device, adv_data):
        name = device.name or adv_data.local_name or ""
        if "Polar" in name:
            found_devices[device.address] = {
                "name": name,
                "address": device.address,
                "rssi": adv_data.rssi if adv_data.rssi else -100
            }

    async with BleakScanner(detection_callback, passive=False) as scanner:
        await asyncio.sleep(3.0)

    targets = sorted(list(found_devices.values()), key=lambda x: x["rssi"], reverse=True)
    with open(DEVICES_FILE + ".tmp", "w") as f:
        json.dump(targets, f)
    os.replace(DEVICES_FILE + ".tmp", DEVICES_FILE)

    if os.path.exists(COMMAND_FILE):
        try:
            with open(COMMAND_FILE, 'r') as f:
                cmd = json.load(f)
                if cmd.get("action") == "connect":
                    selected_target_mac = cmd.get("address")
                    logging.info(f"UI Command received! Target locked: {selected_target_mac}")
        except Exception: pass

async def main():
    global selected_target_mac, last_heartbeat
    logging.info("Hardware Engine Online.")
    
    while True:
        if not selected_target_mac:
            await scan_and_list_devices()
            await asyncio.sleep(1)
            continue

        try:
            device = await BleakScanner.find_device_by_filter(lambda d, ad: d.address == selected_target_mac)
            if not device:
                logging.warning("Target device vanished. Returning to radar scan.")
                selected_target_mac = None
                continue

            logging.info(f"Connecting to {device.name} [{device.address}]...")

            async with PolarDevice(device) as polar:
                logging.info("Connected! Starting standard Heart Rate + HRV engine...")
                await polar.start_hr_stream(hr_callback)
                await asyncio.sleep(0.5)
                
                logging.info("Initializing 200Hz Accelerometer stream...")
                await polar.start_acc_stream(acc_callback, 200, 16, 8)
                await asyncio.sleep(0.5)

                logging.info("Initializing 130Hz clean ECG stream...")
                await polar.start_ecg_stream(ecg_callback, 130, 14)
                
                logging.info(f"!!! TELEMETRY ACTIVE: Logging live to {LOGS_DIR} !!!\n")
                
                last_heartbeat = time.time()
                
                while time.time() - last_heartbeat < 10.0:
                    await asyncio.sleep(1)
                    
                logging.warning("\nConnection Dropped (Data flow stopped). Returning to Scanner...")
                selected_target_mac = None
                
        except (asyncio.TimeoutError, TimeoutError):
            logging.warning("\n⚠️ OS DBus Timeout. Resetting radar...")
            selected_target_mac = None
            await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"\n❌ Link Error: {e}. Resetting radar...")
            selected_target_mac = None
            await asyncio.sleep(2)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("\nHalting streaming contexts. Closing open file descriptors...")
        ecg_file.close(); acc_file.close(); ppi_file.close()
        logging.info("Logs closed cleanly. Safe to exit.")
