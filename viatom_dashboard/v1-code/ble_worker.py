# ble_worker.py
import asyncio
import csv
import datetime
import json
import logging
import os
import sys
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakDeviceNotFoundError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)

TARGET_MAC = "F3:A0:A8:E3:F5:63"  #
HEALTH_SERVICE_UUID = "14839ac4-7d7e-415c-9a42-167340cf2339"  #
WRITE_CHAR_UUID = "8b00ace7-eb0a-49b0-b977-10a8d4d5e82f" 

# Target Paths
BASE_DIR = os.path.expanduser("~/Forks/Bio-dash/viatom_dashboard")
DATA_FILE = os.path.join(BASE_DIR, "data.json")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

# Ensure log directory exists
os.makedirs(LOGS_DIR, exist_ok=True)

# Global metrics engine state
current_metrics = {"spo2": 0, "hr": 0, "battery": 100, "status": "Calibrating"}

def save_to_json_dashboard():
    """Commits local state frame atomically to data.json for the web UI."""
    data = {
        "spo2": current_metrics["spo2"],
        "hr": current_metrics["hr"],
        "battery": current_metrics["battery"],
        "status": current_metrics["status"]
    }
    temp_file = DATA_FILE + ".tmp"
    try:
        with open(temp_file, "w") as f:
            json.dump(data, f)
        os.replace(temp_file, DATA_FILE)
    except Exception as e:
        logging.error(f"Failed syncing local state file: {e}")

def append_to_csv_log():
    """Appends current active vitals to a daily CSV spreadsheet log file."""
    # Only write records if the device is connected
    if current_metrics["status"] != "Connected":
        return

    now = datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    
    csv_file_path = os.path.join(LOGS_DIR, f"health_log_{date_str}.csv")
    file_exists = os.path.exists(csv_file_path)

    try:
        with open(csv_file_path, mode="a", newline="") as csv_file:
            writer = csv.writer(csv_file)
            
            if not file_exists:
                writer.writerow(["Timestamp", "SpO2 (%)", "Heart Rate (BPM)", "Battery (%)"])
                
            writer.writerow([
                time_str, 
                current_metrics["spo2"], 
                current_metrics["hr"], 
                current_metrics["battery"]
            ])
        logging.info(f"💾 Saved Snapshot to CSV -> SpO2: {current_metrics['spo2']}% | HR: {current_metrics['hr']} BPM")
    except Exception as csv_err:
        logging.error(f"Failed structural logging write execution: {csv_err}")

def parse_checkme_notification(sender, data):
    """Callback function processing your explicit 18-byte stream payloads."""
    if len(data) == 0:
        return
        
    raw_hex = data.hex().upper()
    
    if raw_hex == "A5":
        if current_metrics["status"] != "Calibrating":
            current_metrics["status"] = "Calibrating"
            logging.info("⏳ Checkme Status: Probe Idle or Calibrating...")
            save_to_json_dashboard()
        return

    try:
        data_dict = {}
        if len(data) >= 8:
            data_dict['spo2'] = int(data[7])  #
        if len(data) >= 9:
            data_dict['bpm'] = int(data[8])  #
        if len(data) >= 15:
            data_dict['battery'] = int(data[14])  #

        if data_dict and 40 <= data_dict.get('spo2', 0) <= 100:
            # Update global state variable space IMMEDIATELY
            current_metrics["spo2"] = data_dict['spo2']
            current_metrics["hr"] = data_dict.get('bpm', current_metrics["hr"])
            current_metrics["battery"] = data_dict.get('battery', current_metrics["battery"])
            current_metrics["status"] = "Connected"
            
            # Print streaming notification lines as they stream from the device
            print(f"🩸 LIVE BLE STREAM -> SpO2: {current_metrics['spo2']}% | Heart Rate: {current_metrics['hr']} BPM", flush=True)
            
            # Instantly update data.json for the Web UI app
            save_to_json_dashboard()
            
    except Exception as parse_err:
        logging.error(f"Error decoding hardware byte stream buffer: {parse_err}")

async def log_timer_loop():
    """Independent async worker loop that logs data points strictly every 5 seconds."""
    while True:
        await asyncio.sleep(5)
        try:
            append_to_csv_log()
        except Exception as e:
            logging.error(f"Logger tracking execution exception: {e}")

async def run():
    # Spin up the 5-second logging background task
    asyncio.create_task(log_timer_loop())

    while True:
        logging.info(f"Scanning for Checkme O2 Ultra [{TARGET_MAC}]...")  #
        discovered_device = await BleakScanner.find_device_by_address(TARGET_MAC, timeout=8.0)  #
        
        if not discovered_device:
            logging.warning("O2 Ultra not spotted. Ensure its screen interface is awake...")
            await asyncio.sleep(2)
            continue
            
        logging.info("Device Spotted! Instantiating connection tunnel...")
        
        try:
            async with BleakClient(discovered_device, timeout=15.0) as client:
                logging.info("Connected! Mapping internal characteristic profiles...")
                
                services = client.services.get_service(HEALTH_SERVICE_UUID)  #
                if not services:
                    raise ValueError("Target custom health monitoring service profile missing.")
                
                target_notify_uuid = None
                target_write_uuid = None
                
                for char in services.characteristics:
                    if "notify" in char.properties:
                        target_notify_uuid = char.uuid
                    if "write" in char.properties or "write-without-response" in char.properties:
                        target_write_uuid = char.uuid

                if not target_notify_uuid:
                    target_notify_uuid = services.characteristics[0].uuid
                if not target_write_uuid:
                    target_write_uuid = WRITE_CHAR_UUID

                await client.start_notify(target_notify_uuid, parse_checkme_notification)
                await asyncio.sleep(0.5)
                
                # Registration token payload
                write_bytes = bytearray([0xAA, 0x17, 0xE8, 0x00, 0x00, 0x00, 0x00, 0x1B])  #
                
                logging.info("Pipes initialized cleanly! Starting active heartbeat polling...")
                
                while client.is_connected:
                    try:
                        # Re-send the wakeup poke frame every 2 seconds to keep the BLE stream alive
                        await client.write_gatt_char(target_write_uuid, write_bytes, response=True)
                    except Exception as write_err:
                        logging.debug(f"Heartbeat poke missed: {write_err}")
                    
                    await asyncio.sleep(2)
                    
                logging.warning("BLE communication link broken. Returning to scanner loop...")
                
        except (BleakDeviceNotFoundError, asyncio.TimeoutError):
            logging.warning("Handshake timed out. Retrying link strategy...")
            await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"Runtime processing error: {e}")
            await asyncio.sleep(2)

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.info("Daemon cleanly shut down.")
