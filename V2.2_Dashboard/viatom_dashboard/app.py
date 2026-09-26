# app.py
import json
import os
import time
from flask import Flask, Response, jsonify, request, send_from_directory

app = Flask(__name__, root_path=os.path.dirname(os.path.abspath(__file__)))  # works from any cwd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")
DEVICES_FILE = os.path.join(BASE_DIR, "devices.json")
COMMAND_FILE = os.path.join(BASE_DIR, "command.json")


@app.route('/')
def home():
    return send_from_directory(os.path.join(app.root_path, "templates"), "index.html")

@app.route('/api/scan-results')
def scan_results():
    if os.path.exists(DEVICES_FILE):
        try:
            with open(DEVICES_FILE, 'r') as f: return jsonify(json.load(f))
        except Exception: pass
    return jsonify([])

@app.route('/api/connect', methods=['POST'])
def connect():
    target_mac = request.json.get('address')
    tmp = COMMAND_FILE + ".tmp"
    with open(tmp, 'w') as f:
        json.dump({"action": "connect", "address": target_mac}, f)
    os.replace(tmp, COMMAND_FILE)
    return jsonify({"status": "command_sent"})

@app.route('/api/vitals-stream')
def vitals_stream():
    def event_stream():
        last_mtime = 0
        while True:
            if os.path.exists(DATA_FILE):
                try:
                    current_mtime = os.path.getmtime(DATA_FILE)
                    if current_mtime != last_mtime:
                        last_mtime = current_mtime
                        with open(DATA_FILE, 'r') as f: yield f"data: {f.read()}\n\n"
                except Exception: pass
            time.sleep(0.05)
    return Response(event_stream(), mimetype="text/event-stream")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
