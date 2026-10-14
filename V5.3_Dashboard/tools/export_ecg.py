#!/usr/bin/env python3
"""Just the ECG: the Polar H10's raw ECG (mV) as recorded, and the same resampled to an even 200 Hz.

    ./tools/export_ecg.py                         the most recent day with ECG
    ./tools/export_ecg.py 2026-10-07
    ./tools/export_ecg.py --from "2026-10-07 22:00" --to "2026-10-08 07:00"
    ./tools/export_ecg.py 2026-10-07 --uv         whole microvolts (for ReaLLM-Forge csv_mc_int)

Writes a folder (default <Documents>/Bio-dash/Exports/bio-dash_<day>_ecg/) with:

  <name>_raw.csv            Timestamp_Epoch_ms,ECG_mV   every sample as recorded (130 Hz), nothing
                                                        changed: recordings from Polar-dash and Omni
                                                        merged in time order, duplicates dropped
  <name>_200hz.csv          ECG_mV                      one column, no timestamp: an even 200 Hz,
                                                        interpolated, so row k is k / 200 s into
                                                        its stretch
  <name>_200hz_stretches.csv                            where each unbroken stretch starts in the
                                                        200 Hz file (row, rows, start time, gap before)

Stretches: the strap's stream has gaps (Bluetooth drop-outs, the strap taken off, a restart). A gap
of more than 3 samples (~23 ms) ends a stretch. Nothing is invented across a gap: the 200 Hz file
just carries on with the next stretch, and the stretches file says where that happens. --split
writes each stretch to its own file instead (<name>_200hz_001.csv ...); --min-stretch 10s (default)
leaves out stretches shorter than that.

How the 200 Hz is made: the H10 samples at a fixed rate on its own clock, but the recorded
timestamps are whole milliseconds (7 or 8 ms apart). So within a stretch each sample's time is
taken from its position (first + i x the stretch's measured spacing, ~130 Hz), then resampled to
200 Hz with --method linear (default) or cubic (Catmull-Rom: follows the QRS peaks more closely).

--uv writes whole microvolts (ECG_uV) instead of mV, clipped to +-10000 uV, and prints the
csv_mc_int --range for it (the H10 measures in uV, so nothing is lost).
Other options: --device <part of the strap's name>, --out DIR, --data-dir DIR. Standard library only.
"""
import argparse
import array
import csv
import datetime as dt
import heapq
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "analysis_dashboard"))
sys.path.insert(0, os.path.join(HERE, "tools"))
import biodata as b  # noqa: E402
from interpolate import parse_when, span_arg  # noqa: E402

NOMINAL_HZ = 130
GAP_SAMPLES = 3            # a gap of more than this many sample periods ends a stretch
UV_LIMIT = 10_000


def ecg_files(root, start, end, device=None):
    """Every *_ecg.csv that can overlap the range (Polar-dash and Omni both record it)."""
    recs = [r for r in b.recordings(root, start, end, include={"ecg"}) if r.stream == "ecg"]
    if device:
        recs = [r for r in recs if device.lower() in r.device.lower()]
    return recs


def read_ecg(path, start, end):
    """(t_ms, mV) from one recording, in range, in file order."""
    with open(path, newline="", errors="replace") as f:
        rd = csv.reader(b.clean_lines(f))
        head = next(rd, None)
        if not head:
            return
        try:
            col = [h.strip() for h in head].index("ECG_mV")
        except ValueError:
            return
        for row in rd:
            try:
                t = b.to_ms(row[0])
                v = float(row[col])
            except (ValueError, IndexError):
                continue
            if start <= t <= end:
                yield t, v


def merged(paths, start, end):
    """All samples in time order. The same strap recorded by two dashboards at once (Polar-dash and
    Omni) gives two interleaved copies: a sample less than half a period after the one kept is dropped."""
    last = None
    half = 0.5 * 1000.0 / NOMINAL_HZ
    for t, v in heapq.merge(*(read_ecg(p, start, end) for p in paths), key=lambda x: x[0]):
        if last is not None and t - last < half:
            continue
        last = t
        yield t, v


def stretches(ts, period_ms):
    """[(first index, last index)] of unbroken runs: a step over GAP_SAMPLES periods, or backwards, breaks."""
    out, s = [], 0
    limit = GAP_SAMPLES * period_ms
    for i in range(1, len(ts)):
        d = ts[i] - ts[i - 1]
        if d > limit or d <= 0:
            out.append((s, i - 1))
            s = i
    if ts:
        out.append((s, len(ts) - 1))
    return out


def resample(vals, i0, i1, t0, t1, hz, method):
    """The stretch vals[i0..i1] (evenly spaced from t0 to t1 ms) at `hz`. Returns a list of mV."""
    n = i1 - i0 + 1
    if n < 2 or t1 <= t0:
        return []
    step_in = (t1 - t0) / (n - 1)               # ms per recorded sample (the stretch's real spacing)
    pos_per_out = (1000.0 / hz) / step_in       # recorded samples per output sample
    count = int((n - 1) / pos_per_out) + 1
    out = []
    for k in range(count):
        x = k * pos_per_out
        j = int(x)
        f = x - j
        if j >= n - 1:
            out.append(vals[i1])
            continue
        a, c = vals[i0 + j], vals[i0 + j + 1]
        if method == "cubic":
            p0 = vals[i0 + j - 1] if j > 0 else a
            p3 = vals[i0 + j + 2] if j + 2 < n else c
            # Catmull-Rom
            out.append(0.5 * ((2 * a) + (-p0 + c) * f + (2 * p0 - 5 * a + 4 * c - p3) * f * f
                              + (-p0 + 3 * a - 3 * c + p3) * f * f * f))
        else:
            out.append(a + (c - a) * f)
    return out


def fmt_mv(v):
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def main():
    ap = argparse.ArgumentParser(description="The ECG on its own: raw with timestamps, and an even 200 Hz.",
                                 epilog="Full notes: the top of this file.")
    ap.add_argument("day", nargs="?", help="YYYY-MM-DD (local time); default: the most recent day with ECG")
    ap.add_argument("--from", dest="start", type=parse_when)
    ap.add_argument("--to", dest="end", type=parse_when)
    ap.add_argument("--hz", type=float, default=200.0, help="rate of the resampled file (default 200)")
    ap.add_argument("--method", choices=["linear", "cubic"], default="linear")
    ap.add_argument("--uv", action="store_true", help="whole microvolts instead of mV (csv_mc_int)")
    ap.add_argument("--split", action="store_true", help="one 200 Hz file per unbroken stretch")
    ap.add_argument("--min-stretch", type=span_arg, default=10_000, help="leave out shorter stretches (default 10s)")
    ap.add_argument("--device", help="only the strap whose name contains this")
    ap.add_argument("--out")
    ap.add_argument("--data-dir")
    a = ap.parse_args()
    if not 1 <= a.hz <= 2000:
        sys.exit("--hz: 1 to 2000")
    root = b.data_root(a.data_dir)

    if a.start is not None or a.end is not None:
        if a.start is None or a.end is None or a.end <= a.start:
            sys.exit("Give both --from and --to, --to later than --from (or a day).")
        start, end = a.start, a.end
        tag = dt.datetime.fromtimestamp(start / 1000).strftime("%Y-%m-%d_%H%M")
    else:
        day = a.day
        if not day:
            days = sorted({r.day for r in ecg_files(root, None, None, a.device)})
            if not days:
                sys.exit(f"No ECG recordings under {root}. (Use --data-dir if they live elsewhere.)")
            day = days[-1]
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", day):
            sys.exit(f"'{day}' isn't a date (YYYY-MM-DD).")
        start, end, tag = b.local_ms(day), b.local_ms(day, end=True), day

    recs = ecg_files(root, start, end, a.device)
    if not recs:
        sys.exit("No ECG recordings in that range" + (f" for '{a.device}'" if a.device else "") + ".")
    devices = sorted({r.device for r in recs})
    if len(devices) > 1 and not a.device:
        print(f"Note: ECG from {len(devices)} straps ({', '.join(devices)}) is merged; --device picks one.")

    # read everything once, in time order
    ts, vals = array.array("q"), array.array("d")
    for t, v in merged([r.path for r in recs], start, end):
        ts.append(t)
        vals.append(v)
    if len(ts) < 2:
        sys.exit("No ECG samples in that range.")

    name = f"bio-dash_{b.clean_name(tag)}_ecg"
    out = a.out or os.path.join(root, "Exports", name)
    os.makedirs(out, exist_ok=True)
    unit = "uV" if a.uv else "mV"
    conv = (lambda v: str(max(-UV_LIMIT, min(UV_LIMIT, int(round(v * 1000)))))) if a.uv else fmt_mv

    # 1) raw, with timestamps
    raw_path = os.path.join(out, f"{name}_raw.csv")
    with open(raw_path, "w") as f:
        f.write(f"Timestamp_Epoch_ms,ECG_{unit}\n")
        for t, v in zip(ts, vals):
            f.write(f"{t},{conv(v)}\n")

    # 2) even 200 Hz, no timestamps, stretch by stretch
    period = 1000.0 / NOMINAL_HZ
    runs = stretches(ts, period)
    kept, skipped_ms, rows = [], 0, 0
    tag_hz = f"{a.hz:g}hz"
    single = None if a.split else open(os.path.join(out, f"{name}_{tag_hz}.csv"), "w")
    if single:
        single.write(f"ECG_{unit}\n")
    try:
        for i0, i1 in runs:
            dur = ts[i1] - ts[i0]
            if dur < a.min_stretch:
                skipped_ms += dur
                continue
            res = resample(vals, i0, i1, ts[i0], ts[i1], a.hz, a.method)
            if not res:
                continue
            gap = (ts[i0] - ts[kept[-1]["i1"]]) / 1000 if kept else None
            st = {"i1": i1, "row": rows, "rows": len(res), "start": ts[i0], "gap": gap,
                  "hz_in": (i1 - i0) / (dur / 1000) if dur else None}
            if single:
                single.write("\n".join(conv(v) for v in res) + "\n")
            else:
                st["file"] = f"{name}_{tag_hz}_{len(kept) + 1:03d}.csv"
                with open(os.path.join(out, st["file"]), "w") as f:
                    f.write(f"ECG_{unit}\n" + "\n".join(conv(v) for v in res) + "\n")
            rows += len(res)
            kept.append(st)
    finally:
        if single:
            single.close()

    with open(os.path.join(out, f"{name}_{tag_hz}_stretches.csv"), "w") as f:
        w = csv.writer(f)
        w.writerow(["stretch", "file" if a.split else "first_row", "rows", "seconds", "start_epoch_ms",
                    "start_local", "gap_before_s", "recorded_hz"])
        for k, st in enumerate(kept, 1):
            w.writerow([k, st["file"] if a.split else st["row"], st["rows"], round(st["rows"] / a.hz, 2), st["start"],
                        dt.datetime.fromtimestamp(st["start"] / 1000).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                        "" if st["gap"] is None else round(st["gap"], 3),
                        "" if st["hz_in"] is None else round(st["hz_in"], 3)])

    when = lambda ms: dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    span_s = (ts[-1] - ts[0]) / 1000
    kept_s = sum(st["rows"] for st in kept) / a.hz
    print(f"{len(ts):,} samples from {len(recs)} recording(s), {when(ts[0])} -> {when(ts[-1])}")
    print(f"  {raw_path}")
    print(f"  {len(kept):,} unbroken stretch(es) at {a.hz:g} Hz ({a.method}): {rows:,} rows, "
          f"{kept_s / 3600:.2f} h of the {span_s / 3600:.2f} h span"
          + (f"; {len(runs) - len(kept)} shorter than {a.min_stretch / 1000:g} s left out ({skipped_ms / 1000:.0f} s)"
             if len(runs) > len(kept) else ""))
    if kept:
        rates = sorted(st["hz_in"] for st in kept if st["hz_in"])
        if rates:
            print(f"  recorded rate within stretches: {rates[len(rates) // 2]:.2f} Hz (median)")
    print(f"  {os.path.join(out, name + '_' + tag_hz + ('_NNN.csv' if a.split else '.csv'))}"
          f"  +  {name}_{tag_hz}_stretches.csv")
    if a.uv:
        print(f"\ncsv_mc_int:  data/csv_mc_int/get_dataset.sh {name}_{tag_hz}.csv --range ECG_uV:-{UV_LIMIT}:{UV_LIMIT}")


if __name__ == "__main__":
    main()
