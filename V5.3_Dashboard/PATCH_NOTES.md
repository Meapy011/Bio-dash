# V5.3: an integer CSV export for ReaLLM-Forge, and Restart everything
- Analysis page: "For ReaLLM-Forge" downloads the selected signals as an integer CSV that data/csv_mc_int takes as it is
- Whole numbers everywhere: decimals kept by scaling (temp_c_x10), time as minute_of_day and second, -1 = not recording
- Fixed range per signal, so exports from different days share vocabularies and train together
- Each export brings columns.json (scales, ranges) and prepare.sh (get_dataset.sh with every --range)
- tools/export_mcint.py does the same from the command line; --forge ~/ReaLLM-Forge builds the datasets too
- tools/mcint_decode.py turns sampled CSVs back into real units
- Sidebar -> Restart -> Restart everything (or ./restart_all.py): stops every dashboard and Bluetooth worker from any release, resets Bluetooth, starts again what was running
