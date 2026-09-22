# app_polar.py
import json
import os
import time
import glob
from flask import Flask, Response, jsonify, request, render_template_string

app = Flask(__name__)

# Dynamic Path Tracking
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_ADVANCED_DIR = os.path.join(BASE_DIR, "logs_advanced")

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Polar H10 Advanced Research Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@3.9.1/dist/chart.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-streaming@2.0.0/dist/chartjs-plugin-streaming.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/luxon@3.0.1/build/global/luxon.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-luxon@1.2.0/dist/chartjs-adapter-luxon.min.js"></script>
    <style>
        body { background: #0f172a; color: #f8fafc; font-family: system-ui, sans-serif; display: flex; flex-direction: column; align-items: center; padding: 20px; margin: 0; }
        h1 { margin-bottom: 5px; color: #f43f5e; }
        .grid { display: grid; grid-template-columns: 1fr; gap: 20px; width: 90%; max-width: 1200px; margin-top: 20px; }
        @media(min-width: 900px) { .grid { grid-template-columns: 2fr 1fr; } }
        .card { background: #1e293b; border: 1px solid #334155; padding: 20px; border-radius: 12px; height: 350px; }
        .metrics-panel { display: flex; flex-direction: column; justify-content: space-around; height: 100%; }
        .metric-box { background: #0f172a; padding: 15px; border-radius: 8px; border: 1px solid #475569; text-align: center; }
        .metric-val { font-size: 2.5rem; font-weight: bold; color: #38bdf8; }
        .metric-label { font-size: 0.85rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; }
    </style>
</head>
<body>
    <h1>Polar H10 Live Signal Lab</h1>
    <div style="color: #64748b; font-size: 0.9rem;">Real-time Sub-Surface Biometrics (ECG @ 130Hz | IMU @ 200Hz)</div>

    <div class="grid">
        <div class="card">
            <h3 style="margin:0 0 10px 0; color:#f43f5e;">⚡ Real-Time ECG Waveform (µV)</h3>
            <div style="position: relative; height: 290px;"><canvas id="ecgChart"></canvas></div>
        </div>

        <div class="card metrics-panel">
            <div class="metric-box">
                <div class="metric-val" id="ecg-val">0 <span style="font-size:1.2rem;color:#64748b;">µV</span></div>
                <div class="metric-label">Raw Potential Amplitude</div>
            </div>
            <div class="metric-box">
                <div class="metric-val" style="color:#4ade80;" id="acc-val">X: 0 | Y: 0 | Z: 0</div>
                <div class="metric-label">G-Force Vectors (mg)</div>
            </div>
        </div>
    </div>

    <script>
        const ecgCtx = document.getElementById('ecgChart').getContext('2d');
        
        const ecgChart = new Chart(ecgCtx, {
            type: 'line',
            data: { datasets: [{ label: 'ECG Signal', borderColor: '#f43f5e', borderWidth: 2, pointRadius: 0, data: [] }] },
            options: {
                maintainAspectRatio: false,
                animation: false,
                scales: {
                    x: { type: 'realtime', realtime: { duration: 4000, refresh: 40, delay: 50 } },
                    y: { grid: { color: '#334155' }, ticks: { color: '#94a3b8' } }
                },
                plugins: { legend: { display: false } }
            }
        });

        const eventSource = new EventSource("/stream_advanced");
        eventSource.onmessage = (e) => {
            const packet = JSON.parse(e.data);
            
            if (packet.ecg && packet.ecg.length > 0) {
                const now = Date.now();
                packet.ecg.forEach((val, index) => {
                    const interpolatedTime = now - (packet.ecg.length - index) * 7.7;
                    ecgChart.data.datasets[0].data.push({ x: interpolatedTime, y: val });
                });
                document.getElementById('ecg-val').innerText = packet.ecg[packet.ecg.length - 1] + " µV";
            }
            
            if (packet.acc) {
                document.getElementById('acc-val').innerText = `X: ${packet.acc.x} | Y: ${packet.acc.y} | Z: ${packet.acc.z}`;
            }
        };
    </script>
</body>
</html>
"""

def tail_file(filename, lines=20):
    """Fast, raw file tailing method that bypasses file locking locks."""
    try:
        with open(filename, 'rb') as f:
            try:
                f.seek(-4096, os.SEEK_END)
            except IOError:
                f.seek(0)
            last_lines = f.read().splitlines()
            return [line.decode('utf-8', errors='ignore') for line in last_lines[-lines:]]
    except Exception:
        return []

@app.route('/')
def home(): 
    return render_template_string(HTML_TEMPLATE)

@app.route('/stream_advanced')
def stream_advanced():
    def event_stream():
        while True:
            ecg_files = glob.glob(os.path.join(LOGS_ADVANCED_DIR, "polar_raw_ecg_*.csv"))
            acc_files = glob.glob(os.path.join(LOGS_ADVANCED_DIR, "polar_raw_acc_*.csv"))
            
            packet = {"ecg": [], "acc": {"x": 0, "y": 0, "z": 0}}

            if ecg_files and acc_files:
                latest_ecg = max(ecg_files, key=os.path.getmtime)
                latest_acc = max(acc_files, key=os.path.getmtime)
                
                # Read last 20 samples of ECG raw lines
                ecg_lines = tail_file(latest_ecg, 20)
                for line in ecg_lines:
                    parts = line.split(',')
                    if len(parts) == 2 and parts[1] != "ECG_uV":
                        try: packet["ecg"].append(int(parts[1]))
                        except ValueError: pass

                # Read last 1 sample of Accelerometer lines
                acc_lines = tail_file(latest_acc, 2)
                if acc_lines:
                    for line in reversed(acc_lines):
                        parts = line.split(',')
                        if len(parts) == 4 and parts[1] != "X_mg":
                            try:
                                packet["acc"] = {"x": int(parts[1]), "y": int(parts[2]), "z": int(parts[3])}
                                break
                            except ValueError: pass

            yield f"data: {json.dumps(packet)}\n\n"
            time.sleep(0.05) # 20Hz UI Stream

    return Response(event_stream(), mimetype="text/event-stream")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
