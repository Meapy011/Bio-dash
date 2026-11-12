# V6.3: a live R-R forecast on the Heart page
- New 🔮 Forecast pill on the Heart Rate Variability (R-R) card: the next beats' R-R from the ECG + R-R model, with the samples' range and a running error against repeating the last R-R
- The ECG + R-R model is included: polar_dashboard/forecast/models/ecg_rr (13.5 M parameters, the proof-of-concept run)
- polar_dashboard/rr_lane.py builds the model's R-R lane live from the strap's R-R messages, exactly as its training data was made
- forecast_service.py follows the R-R file and runs the R-R overlay; tools/orin_setup.sh installs and times the new model
- A proof of concept: on held-out data it was about as good as holding the last R-R
