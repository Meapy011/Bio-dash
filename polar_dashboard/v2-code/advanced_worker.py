import asyncio
import csv
import datetime
import os
import sys
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

# Dynamic Path Tracking
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs_advanced")
os.makedirs(LOGS_DIR, exist_ok=True)

date_str = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
ecg_log_path = os.path.join(LOGS_DIR, f"polar_raw_ecg_{date_str}.csv")

# Open log file with immediate flushing
ecg_file = open(ecg_log_path, mode="a", newline="", buffering=1)
ecg_writer = csv.writer(ecg_file)
# Added HR_BPM to the CSV header
ecg_writer.writerow(["Timestamp_Epoch_ms", "ECG_mV", "HR_BPM"])

current_hr = 0
ecg_counter = 0

def hr_callback(data):
    """Parses heart rate telemetry from pip data models safely."""
    global current_hr
    
    bpm = getattr(data, 'bpm', None) or getattr(data, 'heart_rate', None)
    
    if bpm is None and isinstance(data, (list, tuple)) and len(data) > 0:
        bpm = data[0]
        
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
    """Extracts sample arrays, converts to mV, and writes Epoch ms + HR."""
    global current_hr, ecg_counter
    samples = []
    
    if hasattr(data, 'samples') and isinstance(data.samples, list):
        samples = data.samples
    elif hasattr(data, 'ecg') and isinstance(data.ecg, list):
        samples = data.ecg
    elif hasattr(data, 'voltages') and isinstance(data.voltages, list):
        samples = data.voltages
    elif hasattr(data, '__dict__'):
        for key, val in data.__dict__.items():
            if isinstance(val, list) and len(val) > 0:
                first_el = val[0]
                if isinstance(first_el, int) and first_el > 1000000000000000:
                    continue # Skip the 64-bit timestamp array
                samples = val
                break

    if not samples:
        return

    sample_spacing_ms = 1000.0 / 130.0
    start_time = datetime.datetime.now() - datetime.timedelta(milliseconds=len(samples) * sample_spacing_ms)

    for idx, val in enumerate(samples):
        try:
            uv_value = int(getattr(val, 'voltage', getattr(val, 'ecg_uv', val)))
            mv_value = round(uv_value / 1000.0, 3)
            sample_time = start_time + datetime.timedelta(milliseconds=idx * sample_spacing_ms)
            epoch_ms = int(sample_time.timestamp() * 1000)
            
            # Write all three values to the CSV
            ecg_writer.writerow([epoch_ms, mv_value, current_hr])
            ecg_counter += 1
        except (ValueError, TypeError):
            continue
        
    try:
        last_uv = int(getattr(samples[-1], 'voltage', getattr(samples[-1], 'ecg_uv', samples[-1])))
        last_mv = last_uv / 1000.0
        sys.stdout.write(
            f"\r[ ❤️ HR: {current_hr:3d} BPM ] [ Samples: {ecg_counter:6d} ] || ⚡ ECG: {last_mv:>6.3f} mV    "
        )
        sys.stdout.flush()
    except (ValueError, TypeError, IndexError):
        pass

async def main():
    logging.info("Engine Online. Scanning for Polar H10 via direct MAC lock...")
    
    while True:
        try:
            device = await BleakScanner.find_device_by_filter(
                lambda d, ad: d.address == "A0:9E:1A:E3:3F:94"
            )

            if not device:
                logging.info("Searching for strap... Ensure pads are wet.")
                await asyncio.sleep(3)
                continue

            logging.info(f"Target locked: {device.name} [{device.address}]. Connecting...")

            async with PolarDevice(device) as polar:
                logging.info("Connected successfully! Starting heart rate engine...")
                await polar.start_hr_stream(hr_callback)
                await asyncio.sleep(0.5)
                
                logging.info("Initializing 130Hz clean ECG stream hardware...")
                await polar.start_ecg_stream(ecg_callback, 130, 14)
                
                logging.info(f"!!! TELEMETRY ACTIVE: Logging live to {ecg_log_path} !!!\n")
                
                while True:
                    await asyncio.sleep(1)
                    
        except (asyncio.TimeoutError, TimeoutError):
            logging.warning("\n⚠️ OS DBus Timeout: Linux is holding a ghost connection.")
            logging.warning("Retrying in 5 seconds... (If it loops, run: `sudo systemctl restart bluetooth`)")
            await asyncio.sleep(5)
            
        except BleakError as e:
            logging.warning(f"\n⚠️ BLE Error: {e}. Retrying in 3 seconds...")
            await asyncio.sleep(3)
            
        except Exception as e:
            logging.error(f"\n❌ Unexpected Error: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("\nHalting streaming contexts. Closing open file descriptors...")
        ecg_file.close()
        logging.info("Logs closed cleanly. Safe to exit.")
