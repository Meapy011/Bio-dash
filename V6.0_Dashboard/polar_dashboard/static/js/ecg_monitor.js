// Bedside-monitor style ECG: sweeping pen with an erase gap, ECG-paper grid at
// 25 mm/s, auto or fixed gain, R-wave flash + optional beep.
// Colours are options, so it can follow the dashboard theme.
//
//   const mon = new EcgMonitor({ grid, trace, overlay, strip, heart, noSignal, onBeat, theme });
//   mon.push(rows)      // rows = [[ts_ms, mV, hr], ...] straight from the websocket
//   mon.cycleGain()     // AUTO -> 5 -> 10 -> 20 mm/mV -> AUTO
//   mon.setForecast(f)  // optional overlay canvas: {t0, dt, rep, lo, hi} (mV on a dt grid from t0) or null
//                       // strip: optional {grid, trace} canvases for the inference strip
//
// Sample timestamps come from the Polar's own clock (evenly spaced). They are
// replayed at real speed a little behind "now", so the pen moves smoothly even
// though BLE delivers ECG in ~0.5 s bursts, and so the viewer's clock doesn't
// have to match the Orin's.
//
// A forecast is drawn ahead of the pen (dashed representative sample + band), through the same 0.5 Hz
// high-pass as the trace, so it shows where the pen is about to go; the pen draws the real ECG over it.
// The overlay canvas sits under the trace, and what was forecast stays there after the pen has passed:
// when a new forecast arrives, the previous one's points up to the new one's start are kept as the
// "inference" (the same rule forecast_service.py logs to Forecasts/<day>/<time>_forecast_ecg.csv). The
// inference strip shows it alone, under the live ECG, on the same time axis.

class EcgMonitor {
    constructor({ grid, trace, overlay = null, strip = null, heart, noSignal, onBeat, theme = {} }) {
        this.grid = grid;
        this.trace = trace;
        this.overlay = overlay;
        this.strip = strip && strip.grid && strip.trace ? strip : null;
        this.heart = heart;
        this.noSignal = noSignal;
        this.onBeat = onBeat || (() => {});

        this.MM_PER_SEC = 25;       // standard paper speed
        this.PLAY_DELAY_MS = 900;   // must exceed the ~560 ms PMD frame spacing
        this.GAP_FRAC = 0.025;      // width of the erase bar ahead of the pen
        // Defaults match the dashboard: slate-800 screen, rose-500 trace
        this.baseTheme = {
            trace: '#f43f5e',
            glow: 'rgba(244, 63, 94, 0.55)',
            background: '#1e293b',
            gridMinor: 'rgba(148, 163, 184, 0.07)',
            gridMajor: 'rgba(148, 163, 184, 0.16)',
            forecast: '#a78bfa',
            forecastBand: 'rgba(167, 139, 250, 0.20)',
            ...theme,
        };
        // follow the page theme (themes.js): ECG paper in Light, true black in OLED / Warm night
        this.theme = this._pageTheme();
        window.addEventListener('biodash-theme', () => { this.theme = this._pageTheme(); if (this.W) this._drawGrid(); });

        this.gainModes = ['AUTO', 5, 10, 20];   // mm per mV
        this.gainIdx = 0;
        this.autoMmPerMv = 10;

        this.queue = [];
        this.qHead = 0;
        this.clockOffset = null;    // local ms - sensor ms (lag + clock skew)
        this.lastPlotted = null;    // {ts, x, y}
        this.lastSampleAt = 0;
        this.recent = [];           // the last few seconds of [ts, mV], to seed the forecast's filter
        this.fc = null;             // forecast, already high-passed
        this.inf = [];              // the inference: [{ts, v}] kept from each forecast (high-passed mV)
        this.infLast = -Infinity;

        // Display filter state: 0.5 Hz high-pass removes baseline wander
        this.hpPrevIn = 0;
        this.hpPrevOut = 0;
        this.hpLastTs = null;
        this.envelope = 0.5;        // running |mV| peak, for auto gain + beat detection

        this.lastBeatTs = 0;
        this.aboveThr = false;

        this._resize = this._resize.bind(this);
        new ResizeObserver(this._resize).observe(this.trace.parentElement);
        if (this.strip) new ResizeObserver(this._resize).observe(this.strip.trace.parentElement);
        this._resize();
        requestAnimationFrame(this._frame.bind(this));
    }

    _pageTheme() {
        const by = {
            light:    { background: '#fff5f5', gridMinor: 'rgba(225, 29, 72, 0.10)', gridMajor: 'rgba(225, 29, 72, 0.24)',
                        trace: '#be123c', glow: 'rgba(190, 18, 60, 0.25)',
                        forecast: '#7c3aed', forecastBand: 'rgba(124, 58, 237, 0.14)' },
            oled:     { background: '#000000' },
            warm:     { background: '#000000' },
            contrast: { background: '#000000', gridMinor: 'rgba(255, 255, 255, 0.10)', gridMajor: 'rgba(255, 255, 255, 0.26)',
                        trace: '#fb7185', glow: 'rgba(251, 113, 133, 0.6)' },
        }[document.documentElement.dataset.theme] || {};
        return { ...this.baseTheme, ...by };
    }

    get gainLabel() {
        const m = this.gainModes[this.gainIdx];
        return m === 'AUTO' ? `AUTO (${this.autoMmPerMv.toFixed(0)} mm/mV)` : `${m} mm/mV`;
    }

    cycleGain() {
        this.gainIdx = (this.gainIdx + 1) % this.gainModes.length;
        return this.gainLabel;
    }

    // ---------- input ----------
    push(rows) {
        if (!rows.length) return;
        const cand = Date.now() - rows[rows.length - 1][0];
        // Track the smallest arrival lag seen (best case); drift upward only slowly
        if (this.clockOffset === null || cand < this.clockOffset) this.clockOffset = cand;
        else this.clockOffset += (cand - this.clockOffset) * 0.02;

        for (const r of rows) this.queue.push(r);
        this.lastSampleAt = Date.now();

        for (const r of rows) this.recent.push([r[0], r[1]]);
        const keepFrom = rows[rows.length - 1][0] - 4000;
        let k = 0;
        while (k < this.recent.length && this.recent[k][0] < keepFrom) k++;
        if (k > 256) this.recent = this.recent.slice(k);
    }

    // The forecast through the same high-pass as the trace: the filter starts 2.5 s before the forecast on the
    // real samples (it settles in ~1.5 s), then runs on through each forecast series.
    setForecast(f) {
        if (!f || !f.rep || f.rep.length < 2) { this.fc = null; this.inf = []; this.infLast = -Infinity; return; }
        const hp = (s, ts, mv) => {
            if (s.last === null || ts - s.last > 500) { s.prevIn = mv; s.prevOut = 0; }
            else {
                const rc = 1 / (2 * Math.PI * 0.5), a = rc / (rc + (ts - s.last) / 1000);
                s.prevOut = a * (s.prevOut + mv - s.prevIn);
                s.prevIn = mv;
            }
            s.last = ts;
            return s.prevOut;
        };
        const seed = { prevIn: 0, prevOut: 0, last: null };
        for (const [ts, mv] of this.recent) {
            if (ts >= f.t0 - 2500 && ts < f.t0) hp(seed, ts, mv);
        }
        const run = (vals) => { const s = { ...seed }; return vals.map((v, k) => hp(s, f.t0 + k * f.dt, v)); };
        const old = this.fc;
        this.fc = { t0: f.t0, dt: f.dt, n: f.rep.length, rep: run(f.rep), lo: run(f.lo), hi: run(f.hi) };
        // keep the previous forecast up to where this one starts (its index 0 is the last real row, so skipped)
        if (old && f.t0 - old.t0 <= (old.n - 1) * old.dt) {     // (after a gap the service starts afresh too)
            for (let k = 1; k < old.n; k++) {
                const ts = old.t0 + k * old.dt;
                if (ts > f.t0) break;
                if (ts > this.infLast) { this.inf.push({ ts, v: old.rep[k] }); this.infLast = ts; }
            }
        }
    }

    // ---------- layout ----------
    _resize() {
        const box = this.trace.parentElement.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        this.W = Math.max(50, box.width);
        this.H = Math.max(50, box.height);
        for (const c of [this.grid, this.trace, this.overlay]) {
            if (!c) continue;
            c.width = Math.round(this.W * dpr);
            c.height = Math.round(this.H * dpr);
            c.getContext('2d').setTransform(dpr, 0, 0, dpr, 0, 0);
        }
        if (this.strip) {
            const sb = this.strip.trace.parentElement.getBoundingClientRect();
            this.SH = Math.max(0, sb.height);
            for (const c of [this.strip.grid, this.strip.trace]) {
                c.width = Math.round(this.W * dpr);
                c.height = Math.round(Math.max(1, this.SH) * dpr);
                c.getContext('2d').setTransform(dpr, 0, 0, dpr, 0, 0);
            }
        }
        // Pick a sweep length that keeps ~4-5 px per mm, like a real screen
        this.sweepSec = Math.min(10, Math.max(4, Math.round(this.W / (this.MM_PER_SEC * 4.5))));
        this.sweepMs = this.sweepSec * 1000;
        this.pxPerMm = this.W / (this.MM_PER_SEC * this.sweepSec);
        this.baseline = this.H * 0.62;   // R-waves are mostly upward: leave more room above
        this._drawGrid();
        this.trace.getContext('2d').clearRect(0, 0, this.W, this.H);
        this.lastPlotted = null;
    }

    _drawGrid() {
        const g = this.grid.getContext('2d');
        const { W, H, pxPerMm } = this;
        g.fillStyle = this.theme.background;
        g.fillRect(0, 0, W, H);
        const lines = (step, color, width) => {
            g.strokeStyle = color;
            g.lineWidth = width;
            g.beginPath();
            for (let x = 0; x <= W; x += step) { g.moveTo(Math.round(x) + 0.5, 0); g.lineTo(Math.round(x) + 0.5, H); }
            for (let y = this.baseline % step; y <= H; y += step) { g.moveTo(0, Math.round(y) + 0.5); g.lineTo(W, Math.round(y) + 0.5); }
            g.stroke();
        };
        lines(pxPerMm, this.theme.gridMinor, 1);        // 1 mm
        lines(pxPerMm * 5, this.theme.gridMajor, 1);    // 5 mm (0.2 s)
        if (this.strip && this.SH > 1) {               // the strip: same paper, scaled to its height
            const sg = this.strip.grid.getContext('2d'), k = this.SH / H, base = this.SH * 0.62;
            sg.fillStyle = this.theme.background;
            sg.fillRect(0, 0, W, this.SH);
            for (const [step, color] of [[pxPerMm, this.theme.gridMinor], [pxPerMm * 5, this.theme.gridMajor]]) {
                sg.strokeStyle = color;
                sg.lineWidth = 1;
                sg.beginPath();
                for (let x = 0; x <= W; x += step) { sg.moveTo(Math.round(x) + 0.5, 0); sg.lineTo(Math.round(x) + 0.5, this.SH); }
                for (let y = base % (step * k); y <= this.SH; y += step * k) { sg.moveTo(0, Math.round(y) + 0.5); sg.lineTo(W, Math.round(y) + 0.5); }
                sg.stroke();
            }
        }
    }

    // ---------- signal processing ----------
    _filter(ts, mv) {
        // First-order high-pass at 0.5 Hz; reset across gaps so a reconnect doesn't ring
        if (this.hpLastTs === null || ts - this.hpLastTs > 500) {
            this.hpPrevIn = mv;
            this.hpPrevOut = 0;
        } else {
            const dt = (ts - this.hpLastTs) / 1000;
            const rc = 1 / (2 * Math.PI * 0.5);
            const a = rc / (rc + dt);
            this.hpPrevOut = a * (this.hpPrevOut + mv - this.hpPrevIn);
            this.hpPrevIn = mv;
        }
        this.hpLastTs = ts;
        return this.hpPrevOut;
    }

    _detectBeat(ts, y) {
        const a = Math.abs(y);
        this.envelope = Math.max(a, this.envelope * 0.9985);   // ~5 s decay at 130 Hz
        const thr = Math.max(0.15, this.envelope * 0.55);
        if (a > thr && !this.aboveThr && ts - this.lastBeatTs > 280) {
            this.lastBeatTs = ts;
            this._flash();
            this.onBeat();
        }
        this.aboveThr = a > thr * 0.8;
    }

    _mmPerMv() {
        const m = this.gainModes[this.gainIdx];
        if (m !== 'AUTO') return m;
        // Aim for the biggest deflection to reach ~35% of the height; move smoothly
        const target = (this.H * 0.35) / this.pxPerMm / Math.max(0.3, this.envelope);
        this.autoMmPerMv += (Math.min(40, Math.max(2.5, target)) - this.autoMmPerMv) * 0.01;
        return this.autoMmPerMv;
    }

    _flash() {
        if (!this.heart) return;
        this.heart.style.opacity = '1';
        this.heart.style.transform = 'scale(1.25)';
        clearTimeout(this._flashT);
        this._flashT = setTimeout(() => {
            this.heart.style.opacity = '0.25';
            this.heart.style.transform = 'scale(1)';
        }, 140);
    }

    // ---------- drawing ----------
    _frame() {
        requestAnimationFrame(this._frame.bind(this));
        if (this.clockOffset === null) return;

        const playTs = Date.now() - this.clockOffset - this.PLAY_DELAY_MS;
        // If we fell far behind (tab was hidden), skip ahead instead of racing
        while (this.qHead < this.queue.length && this.queue[this.qHead][0] < playTs - 2000) this.qHead++;

        const ctx = this.trace.getContext('2d');
        ctx.lineWidth = 2;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.strokeStyle = this.theme.trace;
        ctx.shadowColor = this.theme.glow;
        ctx.shadowBlur = 4;
        const gap = this.W * this.GAP_FRAC;
        const mmPerMv = this._mmPerMv();

        let drew = false;
        ctx.beginPath();
        while (this.qHead < this.queue.length && this.queue[this.qHead][0] <= playTs) {
            const [ts, mv] = this.queue[this.qHead++];
            const y = this._filter(ts, mv);
            this._detectBeat(ts, y);

            const x = ((ts % this.sweepMs) / this.sweepMs) * this.W;
            const py = this.baseline - y * mmPerMv * this.pxPerMm;

            // Erase bar just ahead of the pen (wraps at the right edge)
            ctx.clearRect(x, 0, gap, this.H);
            if (x + gap > this.W) ctx.clearRect(0, 0, x + gap - this.W, this.H);

            const prev = this.lastPlotted;
            if (prev && x >= prev.x && ts - prev.ts < 200) {
                ctx.moveTo(prev.x, prev.y);
                ctx.lineTo(x, py);
                drew = true;
            }
            this.lastPlotted = { ts, x, y: py };
        }
        if (drew) ctx.stroke();
        this._drawForecast(playTs, mmPerMv);

        // Compact the queue now and then
        if (this.qHead > 4000) {
            this.queue = this.queue.slice(this.qHead);
            this.qHead = 0;
        }

        if (this.noSignal) {
            this.noSignal.style.display = Date.now() - this.lastSampleAt > 3000 ? 'flex' : 'none';
        }
    }

    // The overlay (under the trace, redrawn every frame): the inference kept from earlier forecasts, solid
    // behind the pen and dashed ahead of it, then the newest forecast's band and dashed line beyond that.
    // The inference strip gets the inference alone, up to the pen.
    _drawForecast(playTs, mmPerMv) {
        const X = (ts) => ((ts % this.sweepMs) / this.sweepMs) * this.W;
        const Y = (v) => this.baseline - v * mmPerMv * this.pxPerMm;
        const gapMs = this.sweepMs * this.GAP_FRAC;
        const limit = playTs + this.sweepMs * 0.85;          // never wrap round onto the pen
        while (this.inf.length && this.inf[0].ts < playTs - this.sweepMs - 2000) this.inf.shift();
        const f = this.fc;
        const fEnd = f ? Math.min(f.t0 + (f.n - 1) * f.dt, limit) : -Infinity;
        const end = Math.max(playTs, fEnd, Math.min(this.infLast, limit));
        const from = end - this.sweepMs + gapMs;             // one sweep back from the furthest point drawn

        // runs of points that are contiguous on screen (break at the right-edge wrap and at gaps)
        const runsOf = (pts, maxGap) => {
            const runs = [];
            let cur = [];
            for (const p of pts) {
                const prev = cur[cur.length - 1];
                if (prev && (p.x < prev.x || p.ts - prev.ts > maxGap)) { runs.push(cur); cur = []; }
                cur.push(p);
            }
            if (cur.length) runs.push(cur);
            return runs.filter(r => r.length > 1);
        };
        const stroke = (ctx, runs, yOf, dash, width, color, alpha = 1) => {
            ctx.save();
            ctx.setLineDash(dash);
            ctx.lineWidth = width;
            ctx.strokeStyle = color;
            ctx.globalAlpha = alpha;
            ctx.lineJoin = 'round';
            for (const run of runs) {
                ctx.beginPath();
                run.forEach((p, i) => (i ? ctx.lineTo(p.x, yOf(p.v)) : ctx.moveTo(p.x, yOf(p.v))));
                ctx.stroke();
            }
            ctx.restore();
        };
        const dtMax = (f ? f.dt : 8) * 3;

        if (this.overlay) {
            const ctx = this.overlay.getContext('2d');
            ctx.clearRect(0, 0, this.W, this.H);
            if (f && fEnd > playTs) {
                // a wider erase bar while forecasting: the old sweep from the pen to the forecast's end goes
                // (the pen redraws that stretch within ~2 s), so the forecast isn't drawn over an old trace
                const tc = this.trace.getContext('2d');
                const xa = X(playTs) + 4, xb = X(fEnd);          // +4: the pen tip's glow
                if (xb >= xa) tc.clearRect(xa, 0, xb - xa, this.H);
                else { tc.clearRect(xa, 0, this.W - xa, this.H); tc.clearRect(0, 0, xb, this.H); }
            }
            const kept = this.inf.filter(p => p.ts >= from && p.ts <= limit).map(p => ({ ...p, x: X(p.ts) }));
            stroke(ctx, runsOf(kept.filter(p => p.ts <= playTs), dtMax), Y, [], 1.5, this.theme.forecast, 0.8);
            stroke(ctx, runsOf(kept.filter(p => p.ts >= playTs), dtMax), Y, [6, 5], 1.6, this.theme.forecast);
            if (f) {
                const ahead = [];
                for (let k = 0; k < f.n; k++) {
                    const ts = f.t0 + k * f.dt;
                    if (ts < Math.max(playTs, this.infLast) || ts > limit) continue;
                    ahead.push({ ts, k, x: X(ts), v: f.rep[k] });
                }
                for (const run of runsOf(ahead, dtMax)) {
                    ctx.beginPath();
                    run.forEach((p, i) => (i ? ctx.lineTo(p.x, Y(f.hi[p.k])) : ctx.moveTo(p.x, Y(f.hi[p.k]))));
                    for (let i = run.length - 1; i >= 0; i--) ctx.lineTo(run[i].x, Y(f.lo[run[i].k]));
                    ctx.closePath();
                    ctx.fillStyle = this.theme.forecastBand;
                    ctx.fill();
                }
                stroke(ctx, runsOf(ahead, dtMax), Y, [6, 5], 1.6, this.theme.forecast);
            }
        }

        if (this.strip && this.SH > 1) {
            const sc = this.strip.trace.getContext('2d'), k = this.SH / this.H, base = this.SH * 0.62;
            const SY = (v) => base - v * mmPerMv * this.pxPerMm * k;
            sc.clearRect(0, 0, this.W, this.SH);
            const lo = playTs - this.sweepMs + gapMs;
            const pts = this.inf.filter(p => p.ts >= lo && p.ts <= playTs).map(p => ({ ...p, x: X(p.ts) }));
            sc.save();
            sc.shadowColor = this.theme.forecast;
            sc.shadowBlur = 3;
            stroke(sc, runsOf(pts, dtMax), SY, [], 1.8, this.theme.forecast);
            sc.restore();
            const xp = X(playTs);                            // the pen's position, as on the ECG above
            sc.fillStyle = this.theme.forecast;
            sc.globalAlpha = 0.35;
            sc.fillRect(xp, 0, 1.5, this.SH);
            sc.globalAlpha = 1;
        }
    }
}
