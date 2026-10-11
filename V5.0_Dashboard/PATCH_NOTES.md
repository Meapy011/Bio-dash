# Standalone Bluetooth scanner
- tools/ble_scan.py lists every Bluetooth LE device nearby with signal, address type and what it advertises
- Recognises Apple Find My (AirTags, Find My accessories like the HidrateSpark PRO 2, iPhones), other Apple messages, Fast Pair, iBeacon, Eddystone and heart-rate straps
- --find-my shows only Find My traffic; --services NAME connects and lists a device's services (0xFD44 is Find My)
- --log saves every advertisement to CSV
