# advanced_worker.py
import asyncio
import csv
import datetime
import os
import sys
import logging
from bleak import BleakScanner

# Pure pip-installed package import
from polar_python import PolarDevice

# Setup Logging
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S", 
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Dynamic Path Tracking for Dashboard
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs_advanced")
os.makedirs(LOGS_DIR, exist_ok=True)

date_str = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
acc_log_path = os.path.join(LOGS_DIR, f"polar_raw_acc_{date_str}.csv")
ecg_log_path = os.path.join(LOGS_DIR, f"polar_raw_ecg_{date_str}.csv")

# Open log files with immediate flushing (buffering=1)
acc_file = open(acc_log_path, mode="a", newline="", buffering=1)
acc_writer = csv.writer(acc_file)
acc_writer.writerow(["Timestamp_ms", "X_mg", "Y_mg", "Z_mg"])

ecg_file = open(ecg_log_path, mode="a", newline="", buffering=1)
ecg_writer = csv.writer(ecg_file)
ecg_writer.writerow(["Timestamp_ms", "ECG_uV"])

# Global tracking variables for terminal layout
current_hr = 0
ecg_counter = 0

def hr_callback(data):
    """Parses heart rate telemetry from pip data models safely."""
    global current_hr
    
    # Try common property names used across pip versions
    bpm = getattr(data, 'bpm', None) or getattr(data, 'heart_rate', None)
    
    # List/tuple unpacking fallback
    if bpm is None and isinstance(data, (list, tuple)) and len(data) > 0:
        bpm = data[0]
        
    # Dictionary lookup fallback
    if bpm is None and hasattr(data, '__dict__'):
        for val in data.__dict__.values():
            if isinstance(val, int) and 30 < val < 220:
                bpm = val
                break

    if bpm is not None:
        current_hr = int(bpm)
        sys.stdout.write(f"\r[ ❤️ HR: {current_hr:3d} BPM ] [ Samples Written: {ecg_counter} ] | Streaming raw ECG... ")
        sys.stdout.flush()

def ecg_callback(data):
    """Extracts high-resolution sample arrays and flushes them to the CSV log."""
    global current_hr, ecg_counter
    ts = datetime.datetime.now().isoformat()
    samples = []
    
    # Extract list data structures from the pip object
    if hasattr(data, 'samples') and isinstance(data.samples, list):
        samples = data.samples
    elif hasattr(data, 'ecg_voltage_uv'):
        samples = [data.ecg_voltage_uv]
    elif hasattr(data, 'voltage'):
        samples = [data.voltage]
    elif isinstance(data, list):
        samples = data
    elif hasattr(data, '__dict__'):
        for val in data.__dict__.values():
            if isinstance(val, list):
                samples = val
                break
            elif isinstance(val, int) and abs(val) > 10:
                samples = [val]
                break

    # Commit unpacked elements to the CSV file
    if samples:
        for val in samples:
            try:
                uv_value = int(getattr(val, 'voltage', val))
                ecg_writer.writerow([ts, uv_value])
                ecg_counter += 1
            except (ValueError, TypeError):
                continue
        
        # Display the trailing microvolt amplitude on the CLI console
        try:
            last_uv = int(getattr(samples[-1], 'voltage', samples[-1]))
            sys.stdout.write(
                f"\r[ ❤️ HR: {current_hr:3d} BPM ] [ Samples Written: {ecg_counter:6d} ] || ⚡ RAW: {last_uv:6d} µV    "
            )
            sys.stdout.flush()
        except (ValueError, TypeError, IndexError):
            pass

async def main():
    logging.info("Scanning for your Polar H10 via pip package discovery...")
    
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.name and "Polar H10" in d.name
    )

    if not device:
        logging.error("Could not find a Polar H10 strap. Make sure it's snug and damp!")
        return

    logging.info(f"Target locked: {device.name} [{device.address}]. Connecting...")

    # Establish context manager according to the pip version structure
    async with PolarDevice(device) as polar:
        logging.info("Connected successfully! Starting heart rate engine...")
        await polar.start_hr_stream(hr_callback)
        await asyncio.sleep(0.5)
        
        # Pass callback, sample_rate, and resolution as explicit positional args
        logging.info("Initializing 130Hz raw ECG stream hardware...")
        await polar.start_ecg_stream(ecg_callback, 130, 14)
        
        logging.info("!!! TELEMETRY ACTIVE: Logging live to logs_advanced/ !!!\n")
        
        while True:
            await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("\nHalting streaming contexts. Closing open file descriptors...")
        acc_file.close()
        ecg_file.close()
        logging.info("Logs closed cleanly. Safe to exit.")
