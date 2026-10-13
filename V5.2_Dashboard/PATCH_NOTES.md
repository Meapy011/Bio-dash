# V5.2: adapters tested on the Orin, the boot catch, and docs for every radio
- Every Bluetooth adapter tried and why it did or didn't work: a table and two new diagrams in docs/ARCHITECTURE.md section 9
- UB500 Plus proven with the bottle: 10/10 cold connects, heard 8x in 25 s at -60 dBm, chosen by Hydro-dash after a reboot
- Found: the Orin won't power on with a UB500 Plus and an ASUS BT400 both attached; plug the ASUS dongles in after boot (TROUBLESHOOTING 14)
- BCM20702 firmware patch for the ASUS BT400 (build 0000 -> 1467), with checksum and steps (TROUBLESHOOTING 15)
- hydro_test.py suggests the right bottle name when the one given is almost right
- How to vet any adapter with the bottle (TROUBLESHOOTING 13), and why a 12 V USB-C trigger is unreliable for the Orin
