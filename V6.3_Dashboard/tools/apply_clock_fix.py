#!/usr/bin/env python3
"""Put the Polar H10 timestamp fix (stream_clock.py) into another Bio-dash V5.x release folder, e.g.
the V5.5 forecasting copy: python3 apply_clock_fix.py ~/Bio-dash/V5.5_Dashboard

Copies stream_clock.py and its test next to the Polar-dash and Omni workers and changes the workers
to use it (one shared offset for ECG and accelerometer). Safe to run twice; touches nothing else.
Run its checks afterwards: python3 polar_dashboard/test_stream_clock.py"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# the fixed files: next to this script (the standalone zip), or in this release's polar_dashboard
SRC = HERE if os.path.isfile(os.path.join(HERE, "stream_clock.py")) else \
    os.path.join(os.path.dirname(HERE), "polar_dashboard")
WORKERS = {"polar_dashboard": "advanced_worker.py", "Omni-dashboard": "omni.py"}


def fix(path):
    s = open(path).read()
    if "from stream_clock import" in s:
        return "already fixed"
    a = s.find("class StreamClock")
    if a < 0:
        return "no StreamClock here, left alone"
    end = s.find("ecg_clock = StreamClock(ECG_HZ)")
    # the class ends at the first top-level line after it
    lines, i = s[a:].split("\n"), 1
    while i < len(lines) and (not lines[i] or lines[i][0] in " \t#"):
        i += 1
    b = a + len("\n".join(lines[:i]))
    s = s[:a] + s[b:].lstrip("\n")
    if "import bt_debug\n" not in s or end < 0:
        raise SystemExit(f"{path}: not the layout this expects; nothing written")
    s = s.replace("import bt_debug\n", "import bt_debug\nfrom stream_clock import ClockAnchor, StreamClock\n", 1)
    s = s.replace("ecg_clock = StreamClock(ECG_HZ)\nacc_clock = StreamClock(ACC_HZ)",
                  "h10_clock = ClockAnchor()   # ECG and accelerometer share the strap's clock\n"
                  "ecg_clock = StreamClock(ECG_HZ, h10_clock)\nacc_clock = StreamClock(ACC_HZ, h10_clock)", 1)
    if "h10_clock = ClockAnchor()" not in s:
        raise SystemExit(f"{path}: couldn't find where the clocks are made; nothing written")
    s = s.replace('        "acc_dropped": acc_clock.dropped,\n',
                  '        "acc_dropped": acc_clock.dropped,\n'
                  '        "acc_hz_measured": round(acc_clock.measured_hz, 2),\n'
                  '        "clock_reanchors": h10_clock.reanchors,\n', 1)
    s = s.replace('"ecg_dropped": ecg_clock.dropped, "acc_dropped": acc_clock.dropped}},',
                  '"ecg_dropped": ecg_clock.dropped, "acc_dropped": acc_clock.dropped,\n'
                  '                            "acc_hz_measured": round(acc_clock.measured_hz, 2),\n'
                  '                            "clock_reanchors": h10_clock.reanchors}},', 1)
    compile(s, path, "exec")
    with open(path, "w") as f:
        f.write(s)
    return "fixed"


def main():
    if len(sys.argv) != 2 or not os.path.isdir(sys.argv[1]):
        sys.exit(__doc__)
    root = os.path.abspath(sys.argv[1])
    for folder, worker in WORKERS.items():
        d = os.path.join(root, folder)
        if not os.path.isfile(os.path.join(d, worker)):
            continue
        print(f"{folder}/{worker}: {fix(os.path.join(d, worker))}")
        names = ("stream_clock.py", "test_stream_clock.py") if folder == "polar_dashboard" else ("stream_clock.py",)
        for n in names:
            if os.path.abspath(os.path.join(SRC, n)) != os.path.abspath(os.path.join(d, n)):
                shutil.copyfile(os.path.join(SRC, n), os.path.join(d, n))
    print("Now: python3 polar_dashboard/test_stream_clock.py (and restart Polar-dash / Omni)")


if __name__ == "__main__":
    main()
