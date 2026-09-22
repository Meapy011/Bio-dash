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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs_advanced")
os.makedirs(LOGS_DIR, exist_ok=True)

date_str = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

ecg_log_path = os.path.join(LOGS_DIR, f"polar_ecg_{date_str}.csv")
acc_log_path = os.path.join(LOGS_DIR, f"polar_acc_{date_str}.csv")
ppi_log_path = os.path.join(LOGS_DIR, f"polar_ppi_{date_str}.csv")

# Standard opens (we will manually flush the buffers)
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

def hr_callback(data):
    """Parses Heart Rate and extracts the hidden R-R Intervals for HRV."""
    global current_hr
    
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
            
    # CRITICAL: Force Linux to write to disk instantly
    ppi_file.flush()

def acc_callback(data):
    """Extracts 200Hz 3-Axis Accelerometer vectors securely as Floats."""
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
            # Bulletproof XYZ Extraction
            if isinstance(val, (list, tuple)) and len(val) >= 3:
                x = float(val[0])
                y = float(val[1])
                z = float(val[2])
            elif isinstance(val, dict):
                x = float(val.get('x', val.get('X', 0)))
                y = float(val.get('y', val.get('Y', 0)))
                z = float(val.get('z', val.get('Z', 0)))
            else:
                x = float(getattr(val, 'x', 0))
                y = float(getattr(val, 'y', 0))
                z = float(getattr(val, 'z', 0))
            
            sample_time = start_time + datetime.timedelta(milliseconds=idx * sample_spacing_ms)
            epoch_ms = int(sample_time.timestamp() * 1000)
            
            acc_writer.writerow([epoch_ms, x, y, z])
        except (ValueError, TypeError, IndexError): continue
        
    # CRITICAL: Force Linux to write to disk instantly
    acc_file.flush()

def ecg_callback(data):
    """Extracts sample arrays, converts to mV, and writes Epoch ms + HR."""
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
            epoch_ms = int(sample_time.timestamp() * 1000)
            ecg_writer.writerow([epoch_ms, mv_value, current_hr])
            ecg_counter += 1
        except (ValueError, TypeError): continue
        
    # CRITICAL: Force Linux to write to disk instantly
    ecg_file.flush()
        
    try:
        last_uv = int(getattr(samples[-1], 'voltage', getattr(samples[-1], 'ecg_uv', samples[-1])))
        last_mv = last_uv / 1000.0
        sys.stdout.write(
            f"\r[ ❤️ HR: {current_hr:3d} BPM ] [ ECG: {ecg_counter:6d} ] || ⚡ ECG: {last_mv:>6.3f} mV    "
        )
        sys.stdout.flush()
    except (ValueError, TypeError, IndexError): pass

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
                logging.info("Connected! Starting standard Heart Rate + HRV engine...")
                await polar.start_hr_stream(hr_callback)
                await asyncio.sleep(0.5)
                
                logging.info("Initializing 200Hz Accelerometer stream...")
                await polar.start_acc_stream(acc_callback, 200, 16, 8)
                await asyncio.sleep(0.5)

                logging.info("Initializing 130Hz clean ECG stream...")
                await polar.start_ecg_stream(ecg_callback, 130, 14)
                
                logging.info(f"!!! TELEMETRY ACTIVE: Logging live to {LOGS_DIR} !!!\n")
                
                while True:
                    await asyncio.sleep(1)
                    
        except (asyncio.TimeoutError, TimeoutError):
            logging.warning("\n⚠️ OS DBus Timeout. Retrying in 5 seconds...")
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
        acc_file.close()
        ppi_file.close()
        logging.info("Logs closed cleanly. Safe to exit.")
