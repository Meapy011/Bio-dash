import asyncio
import csv
import datetime
import os
import sys
import logging
from bleak import BleakScanner, BleakClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs_air")
os.makedirs(LOGS_DIR, exist_ok=True)

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

def handle_rx(sender, data):
    """Buffers UART chunks and writes complete lines to the CSV."""
    global data_buffer, samples_written
    
    data_buffer += data.decode('utf-8')
    
    if '\n' in data_buffer:
        lines = data_buffer.split('\n')
        complete_line = lines[0].strip()
        # Keep the rest in the buffer
        data_buffer = '\n'.join(lines[1:])
        
        if complete_line:
            metrics = complete_line.split(',')
            if len(metrics) >= 10:
                epoch_ms = int(datetime.datetime.now().timestamp() * 1000)
                # Ensure we only grab the 10 core metrics even if the string has trailing commas
                clean_metrics = [m.strip() for m in metrics[:10]]
                
                csv_writer.writerow([epoch_ms] + clean_metrics)
                csv_file.flush() # Force write to disk
                
                samples_written += 1
                sys.stdout.write(f"\r[ 🌍 Air Monitor Connected ] | Samples Logged: {samples_written:5d} | CO2: {clean_metrics[9]:>4s} ppm   ")
                sys.stdout.flush()

async def main():
    logging.info("Starting Environmental Radar. Searching for SuperMini Air Monitor...")
    
    while True:
        try:
            device = await BleakScanner.find_device_by_filter(
                lambda d, ad: d.name and "SuperMini Air Monitor" in d.name
            )

            if not device:
                await asyncio.sleep(2)
                continue

            logging.info(f"Target Acquired: {device.name} [{device.address}]. Handshaking...")

            async with BleakClient(device) as client:
                logging.info("Connected! Subscribing to Nordic UART TX characteristic...")
                await client.start_notify(UART_TX_CHAR_UUID, handle_rx)
                
                logging.info(f"!!! TELEMETRY ACTIVE: Logging live to {LOGS_DIR} !!!\n")
                
                while client.is_connected:
                    await asyncio.sleep(1)
                    
                logging.warning("\nConnection Dropped. Returning to Scanner...")
                
        except (asyncio.TimeoutError, TimeoutError):
            logging.warning("\n⚠️ BLE Timeout. Resetting radar...")
            await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"\n❌ Unexpected Error: {e}. Resetting radar...")
            await asyncio.sleep(2)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("\nHalting streaming contexts. Closing open file descriptors...")
        csv_file.close()
        logging.info("Logs closed cleanly. Safe to exit.")
