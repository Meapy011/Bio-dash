# app.py
import json
import os
import time
from flask import Flask, Response, render_template_string

app = Flask(__name__)

DATA_FILE = os.path.expanduser("~/Forks/Bio-dash/viatom_dashboard/data.json")

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Bio-Dash Health Monitor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: #121212;
            color: #e0e0e0;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
        }
        .dashboard {
            background-color: #1e1e1e;
            padding: 2.5rem;
            border-radius: 16px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.4);
            text-align: center;
            width: 320px;
        }
        h1 { margin-bottom: 2rem; font-size: 1.8rem; color: #ffffff; }
        .metric-card {
            background: #252525;
            padding: 1.2rem;
            margin: 1rem 0;
            border-radius: 12px;
            border-left: 5px solid #444;
            transition: all 0.1s ease;
        }
        .metric-card.spo2 { border-left-color: #38bdf8; }
        .metric-card.hr { border-left-color: #f43f5e; }
        .label { font-size: 0.85rem; text-transform: uppercase; color: #a3a3a3; letter-spacing: 1px; }
        .value { font-size: 2.2rem; font-weight: bold; margin-top: 0.3rem; }
        .status { font-size: 0.9rem; color: #10b981; margin-top: 1.5rem; }
    </style>
    <script>
        // High-Speed Persistent SSE Connection
        const eventSource = new EventSource("/api/vitals-stream");
        
        eventSource.onmessage = function(event) {
            const data = JSON.parse(event.data);
            document.getElementById('spo2-val').innerText = data.spo2 + '%';
            document.getElementById('hr-val').innerText = data.hr + ' BPM';
            document.getElementById('status-val').innerText = 'Device Status: ' + data.status;
        };

        eventSource.onerror = function(err) {
            console.error("Stream dropped, reconnecting...");
        };
    </script>
</head>
<body>
    <div class="dashboard">
        <h1>Bio-Dash</h1>
        
        <div class="metric-card spo2">
            <div class="label">Blood Oxygen (SpO₂)</div>
            <div id="spo2-val" class="value">--%</div>
        </div>
        
        <div class="metric-card hr">
            <div class="label">Heart Rate</div>
            <div id="hr-val" class="value">-- BPM</div>
        </div>
        
        <div id="status-val" class="status">Device Status: Initializing Stream...</div>
    </div>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/vitals-stream')
def vitals_stream():
    """Generates an ultra-low latency data stream pushing metrics to the client instantly."""
    def event_stream():
        last_mtime = 0
        while True:
            if os.path.exists(DATA_FILE):
                try:
                    current_mtime = os.path.getmtime(DATA_FILE)
                    # Only stream a frame update if the background file actually changed
                    if current_mtime != last_mtime:
                        last_mtime = current_mtime
                        with open(DATA_FILE, 'r') as f:
                            data = f.read()
                        yield f"data: {data}\n\n"
                except Exception:
                    pass
            # Super fast check frequency (50ms interval) to catch frames instantly
            time.sleep(0.05)

    return Response(event_stream(), mimetype="text/event-stream")

if __name__ == "__main__":
    # Using threaded=True allows Flask to process stream pipes concurrently
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
