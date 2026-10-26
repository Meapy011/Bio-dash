# V6.0: Muse-dash, the Muse S Athena headband
- Muse-dash (port 5006): live EEG with display filters, brain-wave band powers, sensor contact, fNIRS (oxy- / deoxy-haemoglobin per side), heart rate and HRV from the forehead PPG, head motion and battery
- Presets in the debug panel (default p1041: everything); a new preset starts a new session
- Recordings in Documents/Bio-dash/Muse/<headband>/<date>/: eeg, optics, imu, bands, heart, fnirs, battery
- MUSE_FAKE=1 runs it with a stand-in headband (no Muse or Bluetooth needed)
- Not yet tried on a real headband: muse_dashboard/README.md has a checklist for the first session
- The sidebar, launch.sh, biodash.py and setup_env.sh know the new dashboard
