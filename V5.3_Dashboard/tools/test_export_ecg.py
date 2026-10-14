#!/usr/bin/env python3
"""Checks for export_ecg.py with made-up ECG (a test tone and QRS-like spikes). Run: python3 tools/test_export_ecg.py"""
import csv
import datetime as dt
import math
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.argv = [sys.argv[0]]
import export_ecg as e  # noqa: E402

ok = 0


def check(name, got, want):
    global ok
    if got != want:
        raise AssertionError(f"{name}: got {got!r}, want {want!r}")
    ok += 1


# resampling: a QRS-like spike every second, 10 ms wide
spike = lambda ms: math.exp(-((ms % 1000) - 500) ** 2 / (2 * 10 ** 2))
n = 130 * 20
vals = [round(spike(i * 1000 / 130), 3) for i in range(n)]
lin = e.resample(vals, 0, n - 1, 0, (n - 1) * 1000 / 130, 200, "linear")
cub = e.resample(vals, 0, n - 1, 0, (n - 1) * 1000 / 130, 200, "cubic")
check("200 Hz over the stretch", len(lin), int((n - 1) / 130 * 200) + 1)
err = lambda out: max(abs(v - spike(k * 5)) for k, v in enumerate(out))
check("linear within 0.06 mV of a 10 ms spike", err(lin) < 0.06, True)
check("cubic closer than linear", err(cub) < err(lin) / 2, True)
check("stretches split on a gap", e.stretches([0, 8, 15, 23, 100, 108], 1000 / 130), [(0, 3), (4, 5)])

# end to end: two recordings of one strap (other phase) merged, a 2 s gap, a 3 s scrap left out
T = tempfile.mkdtemp()
data = os.path.join(T, "data")
t0 = int(dt.datetime(2026, 10, 7, 9, 0, 0).timestamp() * 1000)
tone = lambda ms: math.sin(2 * math.pi * 1.2 * ms / 1000)


def write(path, segs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("Timestamp_Epoch_ms,ECG_mV,HR_BPM\n")
        for s0, cnt in segs:
            for i in range(cnt):
                ms = s0 + i * 1000 / 130
                f.write(f"{int(t0 + ms)},{tone(ms):.3f},72\n")


write(os.path.join(data, "Polar H10", "Polar H10 0A1B2C3D", "2026-10-07", "09-00-00_ecg.csv"),
      [(0, 130 * 60), (62000, 130 * 60), (200000, 130 * 3)])
write(os.path.join(data, "Omni", "Polar H10 0A1B2C3D", "2026-10-07", "09-00-10_ecg.csv"), [(10003, 130 * 20)])
for extra in ([], ["--uv"]):
    out = os.path.join(T, "out" + "".join(extra))
    p = subprocess.run([sys.executable, os.path.join(HERE, "export_ecg.py"), "2026-10-07", "--data-dir", data, "--out", out, *extra],
                       capture_output=True, text=True)
    check("runs " + " ".join(extra), p.returncode, 0)
    raw = list(csv.reader(open(os.path.join(out, "bio-dash_2026-10-07_ecg_raw.csv"))))
    hz = open(os.path.join(out, "bio-dash_2026-10-07_ecg_200hz.csv")).read().split()
    st = list(csv.DictReader(open(os.path.join(out, "bio-dash_2026-10-07_ecg_200hz_stretches.csv"))))
    unit = "ECG_uV" if extra else "ECG_mV"
    check("raw: timestamp + ECG", raw[0], ["Timestamp_Epoch_ms", unit])
    check("raw: every sample once (the second recording's copies dropped)", len(raw) - 1, 130 * 123)
    check("200 Hz: one column, no timestamp", hz[0], unit)
    check("two stretches, the 3 s scrap left out", [s["rows"] for s in st], ["11999", "11999"])
    check("gap before the second", st[1]["gap_before_s"], "2.008")
    if extra:
        check("whole microvolts", all(v.lstrip("-").isdigit() for v in hz[1:]), True)
    else:
        vals = [float(v) for v in hz[1:]]
        worst = max(abs(vals[k] - tone(k * 5)) for k in range(11999))
        check("200 Hz follows the tone", worst < 0.01, True)
shutil.rmtree(T)
print(f"{ok} checks passed")
