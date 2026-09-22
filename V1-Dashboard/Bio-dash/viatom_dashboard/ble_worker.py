# ble_worker.py
import asyncio
import csv
import datetime
import json
import logging
import os
import sys
from bleak import BleakClient, BleakScanner
from bleak.backends.scanner import AdvertisementData
from bleak.backends.device import BLEDevice

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)]
)

# Known hardcoded fallback match
KNOWN_MAC = "F3:A0:A8:E3:F5:63"
HEALTH_SERVICE_UUID = "14839ac4-7d7e-415c-9a42-167340cf2339"
WRITE_CHAR_UUID = "8b00ace7-eb0a-49b0-b977-10a8d4d5e82f" 

BASE_DIR = os.path.expanduser("~/Forks/Bio-dash/viatom_dashboard")
DATA_FILE = os.path.join(BASE_DIR, "data.json")
DEVICES_FILE = os.path.join(BASE_DIR, "devices.json")
COMMAND_FILE = os.path.join(BASE_DIR, "command.json")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(LOGS_DIR, exist_ok=True)

current_metrics = {"spo2": 0, "hr": 0, "battery": 100, "status": "Scanning"}
selected_target_mac = None

def save_to_json_dashboard():
    data = {
        "spo2": current_metrics["spo2"] if current_metrics["status"] == "Connected" else 0,
        "hr": current_metrics["hr"] if current_metrics["status"] == "Connected" else 0,
        "battery": current_metrics["battery"],
        "status": current_metrics["status"]
    }
    temp_file = DATA_FILE + ".tmp"
    try:
        with open(temp_file, "w") as f: json.dump(data, f)
        os.replace(temp_file, DATA_FILE)
    except Exception: pass

def append_to_csv_log():
    if current_metrics["status"] != "Connected": return
    now = datetime.datetime.now()
    csv_file_path = os.path.join(LOGS_DIR, f"health_log_{now.strftime('%Y-%m-%d')}.csv")
    file_exists = os.path.exists(csv_file_path)
    try:
        with open(csv_file_path, mode="a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "SpO2 (%)", "Heart Rate (BPM)", "Battery (%)"])
            writer.writerow([now.strftime("%H:%M:%S"), current_metrics["spo2"], current_metrics["hr"], current_metrics["battery"]])
    except Exception: pass

def parse_checkme_notification(sender, data):
    if len(data) == 0: return
    raw_hex = data.hex().upper()
    if raw_hex == "A5":
        if current_metrics["status"] != "Calibrating":
            current_metrics["status"] = "Calibrating"
            save_to_json_dashboard()
        return
    try:
        data_dict = {}
        if len(data) >= 8: data_dict['spo2'] = int(data[7])
        if len(data) >= 9: data_dict['bpm'] = int(data[8])
        if len(data) >= 15: data_dict['battery'] = int(data[14])

        if data_dict and 40 <= data_dict.get('spo2', 0) <= 100:
            current_metrics["spo2"] = data_dict['spo2']
            current_metrics["hr"] = data_dict.get('bpm', current_metrics["hr"])
            current_metrics["battery"] = data_dict.get('battery', current_metrics["battery"])
            current_metrics["status"] = "Connected"
            print(f"🩸 LIVE BLE -> SpO2: {current_metrics['spo2']}% | HR: {current_metrics['hr']} BPM", flush=True)
            save_to_json_dashboard()
    except Exception: pass

async def log_timer_loop():
    while True:
        await asyncio.sleep(5)
        append_to_csv_log()

async def scan_and_list_devices():
    """Scans for target wrist monitors using aggressive MAC + Name detection rules."""
    global selected_target_mac
    if os.path.exists(COMMAND_FILE):
        try: os.remove(COMMAND_FILE)
        except Exception: pass

    current_metrics["status"] = "Scanning"
    save_to_json_dashboard()
    
    logging.info("Scanning for active Checkme wrist devices...")
    found_devices = {}

    def detection_callback(device: BLEDevice, adv_data: AdvertisementData):
        addr = device.address.upper()
        name = device.name or adv_data.local_name or ""
        uuids = [u.lower() for u in (adv_data.service_uuids or [])]
        
        # Absolute identification: MAC matches, UUID matches, or text identifiers match
        is_match = (
            addr == KNOWN_MAC.upper() or 
            HEALTH_SERVICE_UUID.lower() in uuids or 
            any(x in name.upper() for x in ["O2", "CHECKME", "VIATOM"])
        )
            
        if is_match:
            display_name = name if name else f"Checkme Wrist Unit ({addr[-5:]})"
            found_devices[device.address] = {
                "name": display_name,
                "address": device.address,
                "rssi": adv_data.rssi if adv_data.rssi else -100
            }

    async with BleakScanner(detection_callback, passive=False) as scanner:
        await asyncio.sleep(4.0)
        
    found_targets = list(found_devices.values())
    found_targets.sort(key=lambda x: x["rssi"], reverse=True)
    
    logging.info(f"Scan complete. Found {len(found_targets)} matching wrist monitor(s).")
    
    with open(DEVICES_FILE + ".tmp", "w") as f:
        json.dump(found_targets, f)
    os.replace(DEVICES_FILE + ".tmp", DEVICES_FILE)

    if os.path.exists(COMMAND_FILE):
        try:
            with open(COMMAND_FILE, 'r') as f:
                cmd = json.load(f)
                if cmd.get("action") == "connect":
                    selected_target_mac = cmd.get("address")
                    logging.info(f"UI Selection registered! Target locked: {selected_target_mac}")
        except Exception: pass

async def run():
    global selected_target_mac
    asyncio.create_task(log_timer_loop())

    while True:
        if not selected_target_mac:
            await scan_and_list_devices()
            await asyncio.sleep(1)
            continue
            
        logging.info(f"Connecting to user selected device: {selected_target_mac}")
        current_metrics["status"] = "Connecting"
        save_to_json_dashboard()
        
        try:
            async with BleakClient(selected_target_mac, timeout=10.0) as client:
                # Use a slightly softer service discovery check once connected
                services = client.services.get_service(HEALTH_SERVICE_UUID)
                
                # If the service cache lookup fails on this Linux build, extract characteristics directly
                if services:
                    target_notify_uuid = next((c.uuid for c in services.characteristics if "notify" in c.properties), services.characteristics[0].uuid)
                    target_write_uuid = next((c.uuid for c in services.characteristics if "write" in c.properties or "write-without-response" in c.properties), WRITE_CHAR_UUID)
                else:
                    logging.warning("Service UUID profile not cached. Attempting direct descriptor bind...")
                    target_notify_uuid = "14839ac4-7d7e-415c-9a42-167340cf2339" # Force raw mapping fallback
                    target_write_uuid = WRITE_CHAR_UUID

                await client.start_notify(target_notify_uuid, parse_checkme_notification)
                write_bytes = bytearray([0xAA, 0x17, 0xE8, 0x00, 0x00, 0x00, 0x00, 0x1B])
                
                while client.is_connected:
                    await client.write_gatt_char(target_write_uuid, write_bytes, response=True)
                    await asyncio.sleep(2)
                    
                logging.warning("Connection dropped. Returning to radar scan.")
                selected_target_mac = None
                
        except Exception as e:
            logging.error(f"Link failed: {e}. Resetting target alignment.")
            selected_target_mac = None
            await asyncio.sleep(2)

if __name__ == "__main__":
    try: asyncio.run(run())
    except KeyboardInterrupt: logging.info("Clean shutdown.")
