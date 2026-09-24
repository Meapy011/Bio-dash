import asyncio
import csv
import datetime
import os
import sys
import json
import time
import logging
from bleak import BleakScanner, BleakClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs_air")
os.makedirs(LOGS_DIR, exist_ok=True)

DEVICES_FILE = os.path.join(LOGS_DIR, "devices.json")
COMMAND_FILE = os.path.join(LOGS_DIR, "command.json")

# Nordic UART Service UUIDs
UART_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
UART_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

date_str = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
csv_path = os.path.join(LOGS_DIR, f"sen69c_telemetry_{date_str}.csv")

csv_file = open(csv_path, mode="a", newline="")
csv_writer = csv.writer(csv_file)
csv_writer.writerow([
    "Timestamp_Epoch_ms", "PM1_0", "PM2_5", "PM4_0", "PM10_0", 
    "Humidity_RH", "Temp_C", "VOC_Index", "NOx_Index", "HCHO_ppb", "CO2_ppm"
])

data_buffer = ""
samples_written = 0
selected_target_mac = None
last_message_time = 0

def handle_rx(sender, data):
    """Buffers UART chunks and writes complete lines to the CSV."""
    global data_buffer, samples_written, last_message_time
    last_message_time = time.time()
    
    data_buffer += data.decode('utf-8')
    
    if '\n' in data_buffer:
        lines = data_buffer.split('\n')
        complete_line = lines[0].strip()
        data_buffer = '\n'.join(lines[1:])
        
        if complete_line:
            metrics = complete_line.split(',')
            if len(metrics) >= 10:
                epoch_ms = int(datetime.datetime.now().timestamp() * 1000)
                clean_metrics = [m.strip() for m in metrics[:10]]
                
                csv_writer.writerow([epoch_ms] + clean_metrics)
                csv_file.flush() 
                
                samples_written += 1
                sys.stdout.write(f"\r[ 🌍 Air Monitor Connected ] | Samples Logged: {samples_written:5d} | CO2: {clean_metrics[9]:>4s} ppm   ")
                sys.stdout.flush()

async def scan_and_list_devices():
    """Scans for active environmental monitors and updates the UI state list."""
    global selected_target_mac
    
    if os.path.exists(COMMAND_FILE):
        try: os.remove(COMMAND_FILE)
        except Exception: pass

    logging.info("Radar active. Sweeping for SuperMini units...")
    found_devices = {}

    def detection_callback(device, adv_data):
        name = device.name or adv_data.local_name or ""
        # You can adjust this filter if you rename your ESP32/nRF52 board
        if "SuperMini" in name or "SEN69" in name or "Air" in name:
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
    global selected_target_mac, last_message_time
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

            async with BleakClient(device) as client:
                logging.info("Connected! Subscribing to Nordic UART TX characteristic...")
                await client.start_notify(UART_TX_CHAR_UUID, handle_rx)
                
                logging.info(f"!!! TELEMETRY ACTIVE: Logging live to {LOGS_DIR} !!!\n")
                
                # Watchdog Timer: Reset if no data arrives for 10 seconds
                last_message_time = time.time()
                while time.time() - last_message_time < 10.0:
                    await asyncio.sleep(1)
                    
                logging.warning("\nConnection Dropped (Data flow stopped). Returning to Scanner...")
                selected_target_mac = None
                
        except (asyncio.TimeoutError, TimeoutError):
            logging.warning("\n⚠️ BLE Timeout. Resetting radar...")
            selected_target_mac = None
            await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"\n❌ Unexpected Error: {e}. Resetting radar...")
            selected_target_mac = None
            await asyncio.sleep(2)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("\nHalting streaming contexts. Closing open file descriptors...")
        csv_file.close()
        logging.info("Logs closed cleanly. Safe to exit.")
