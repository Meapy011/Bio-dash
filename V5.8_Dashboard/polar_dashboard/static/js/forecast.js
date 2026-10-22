// Live forecasts in Polar-dash: the 🔮 Forecast pills, the overlays and a live error readout.
//
// forecast_service.py (next to app.py) tails the same session files, forecasts the next second every
// 0.5 s and posts it; app.py broadcasts it here as ecg_forecast / acc_forecast. A forecast carries, per
// lane, the band (lo / hi over the samples), the median and one representative sample (`rep`) on the
// model's 8 ms grid from t0, plus the last real value. The ECG overlay comes from the ECG-only model, the
// accelerometer overlay from the ECG + accelerometer model. The dashed line is the representative sample:
// an average of the samples would smear a heartbeat whose timing is uncertain; the band shows the spread.
//
//   const forecast = new ForecastOverlay({ monitor, ecgView, accChart });
//   forecast.onMessage(packet)                            // ecg_forecast | acc_forecast | forecast_settings
//   forecast.onEcgRows(rows); forecast.onAccRows(rows)    // the live data, for the error readout

// Running forecast error: every forecast against the samples that then arrived (|actual - median| over
// its horizon, and the same for "hold the last value"), averaged over the last 30 s of forecasts.
class ForecastErrors {
    constructor(lanes) {
        this.lanes = lanes;
        this.open = [];
        this.done = [];
    }

    add(fc) {
        const idx = this.lanes.map(n => fc.lanes.indexOf(n));
        if (idx.some(i => i < 0)) return;
        this.open.push({
            t0: fc.t0, dt: fc.dt_ms, n: fc.median[idx[0]].length,
            med: idx.map(i => fc.median[i]), last: idx.map(i => fc.last[i]), sum: 0, hold: 0, cnt: 0,
        });
        if (this.open.length > 8) this.open.shift();
    }

    observe(ts, values) {
        for (const o of this.open) {
            const pos = (ts - o.t0) / o.dt;
            if (pos < 0 || pos > o.n - 1) continue;
            const k = Math.floor(pos), w = pos - k;
            for (let j = 0; j < values.length; j++) {
                const m = o.med[j];
                const pred = k + 1 < o.n ? m[k] * (1 - w) + m[k + 1] * w : m[k];
                o.sum += Math.abs(values[j] - pred);
                o.hold += Math.abs(values[j] - o.last[j]);
                o.cnt++;
            }
        }
        const now = Date.now();
        this.open = this.open.filter(o => {
            if (ts <= o.t0 + (o.n - 1) * o.dt) return true;
            if (o.cnt) this.done.push({ mae: o.sum / o.cnt, hold: o.hold / o.cnt, at: now });
            return false;
        });
        this.done = this.done.filter(d => d.at >= now - 30000);
    }

    summary() {
        if (!this.done.length) return null;
        const avg = (key) => this.done.reduce((s, d) => s + d[key], 0) / this.done.length;
        return { mae: avg('mae'), hold: avg('hold'), n: this.done.length };
    }

    reset() { this.open = []; this.done = []; }
}

class ForecastOverlay {
    constructor({ monitor, ecgView, accChart }) {
        this.monitor = monitor;
        this.ecgView = ecgView;
        this.accChart = accChart;
        const $ = (id) => document.getElementById(id);
        this.btn = { ecg: $('fc-ecg-btn'), acc: $('fc-acc-btn') };
        this.statusEl = { ecg: $('fc-ecg-status'), acc: $('fc-acc-status') };
        this.settings = { ecg: false, acc: false, horizon_s: 1.0, samples: 8 };
        this.service = null;
        this.latest = { ecg: null, acc: null };
        this.receivedAt = { ecg: 0, acc: 0 };
        this.errors = { ecg: new ForecastErrors(['ecg_uv']), acc: new ForecastErrors(['acc_x_mg', 'acc_y_mg', 'acc_z_mg']) };
        this.AHEAD_DELAY = -500;          // chart delay while forecasting: the coming second stays in view
        this.chartDelay = {
            ecg: ecgView.chart.options.scales.x.realtime.delay,
            acc: accChart.options.scales.x.realtime.delay,
        };

        this._addDatasets();
        for (const kind of ['ecg', 'acc']) {
            if (this.btn[kind]) this.btn[kind].addEventListener('click', () => this.toggle(kind));
        }
        this.refresh();
        setInterval(() => this.refresh(), 2000);
        setInterval(() => this._tick(), 1000);
    }

    // ---------- chart datasets: band (lo, hi filled to it) + dashed representative sample ----------
    _addDatasets() {
        const band = (color, label) => [
            { label: `fc lo ${label}`, data: [], borderWidth: 0, pointRadius: 0, borderColor: 'transparent', fill: false },
            { label: `fc hi ${label}`, data: [], borderWidth: 0, pointRadius: 0, borderColor: 'transparent',
              backgroundColor: color + '33', fill: '-1' },
            { label: `fc ${label}`, data: [], borderColor: color, borderWidth: 1.5, borderDash: [5, 4], pointRadius: 0, fill: false },
        ];
        this.ecgSets = band('#a78bfa', 'ECG');
        this.ecgView.chart.data.datasets.push(...this.ecgSets);
        this.accSets = {};
        const colors = { acc_x_mg: '#38bdf8', acc_y_mg: '#a78bfa', acc_z_mg: '#fbbf24' };
        for (const [lane, color] of Object.entries(colors)) {
            this.accSets[lane] = band(color, lane);
            this.accChart.data.datasets.push(...this.accSets[lane]);
        }
        const legend = this.accChart.options.plugins.legend;
        legend.labels = { ...(legend.labels || {}), filter: (item) => !String(item.text || '').startsWith('fc ') };
        this.ecgView.chart.update('none');
        this.accChart.update('none');
    }

    // ---------- settings / service status ----------
    async refresh() {
        try {
            this._apply(await (await fetch('/api/forecast/settings')).json());
        } catch (e) {}
    }

    async toggle(kind) {
        const on = !this.settings[kind];
        this.settings[kind] = on;
        this._render();
        try {
            const r = await fetch('/api/forecast/settings', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ [kind]: on }),
            });
            this._apply(await r.json());
        } catch (e) {}
    }

    _apply(view) {
        if (!view || !view.settings) return;
        for (const kind of ['ecg', 'acc']) {
            const was = this.settings[kind];
            this.settings[kind] = !!view.settings[kind];
            if (was && !this.settings[kind]) this._clear(kind);
        }
        this.settings.horizon_s = view.settings.horizon_s;
        this.settings.samples = view.settings.samples;
        this.service = view.service || null;
        this._render();
    }

    onMessage(p) {
        if (p.type === 'forecast_settings') return this._apply(p);
        const kind = p.type === 'ecg_forecast' ? 'ecg' : p.type === 'acc_forecast' ? 'acc' : null;
        if (!kind || !this.settings[kind] || !p.lanes) return;
        this.latest[kind] = p;
        this.receivedAt[kind] = Date.now();
        this.errors[kind].add(p);
        if (kind === 'ecg') this._drawEcg(p); else this._drawAcc(p);
    }

    onEcgRows(rows) {
        if (!this.settings.ecg) return;
        for (const r of rows) this.errors.ecg.observe(r[0], [r[1]]);
    }

    onAccRows(rows) {
        if (!this.settings.acc) return;
        for (const r of rows) this.errors.acc.observe(r[0], [r[1], r[2], r[3]]);
    }

    // ---------- drawing ----------
    _series(p, lane) {
        const i = p.lanes.indexOf(lane);
        if (i < 0) return null;
        const t = (k) => p.t0 + (k - 1) * p.dt_ms;          // index 0: the last real row, then the forecast
        const pts = (arr) => [p.last[i], ...arr].map((v, k) => ({ x: t(k), y: v }));
        return { lo: pts(p.lo[i]), hi: pts(p.hi[i]), rep: pts(p.rep[i]),
                 raw: { t0: p.t0 - p.dt_ms, dt: p.dt_ms, last: p.last[i], lo: p.lo[i], hi: p.hi[i], rep: p.rep[i] } };
    }

    _drawEcg(p) {
        const s = this._series(p, 'ecg_uv');
        if (!s) return;
        [this.ecgSets[0].data, this.ecgSets[1].data, this.ecgSets[2].data] = [s.lo, s.hi, s.rep];
        const r = s.raw;
        this.monitor.setForecast({ t0: r.t0, dt: r.dt, rep: [r.last, ...r.rep], lo: [r.last, ...r.lo], hi: [r.last, ...r.hi] });
    }

    _drawAcc(p) {
        for (const lane of Object.keys(this.accSets)) {
            const s = this._series(p, lane);
            const sets = this.accSets[lane];
            [sets[0].data, sets[1].data, sets[2].data] = s ? [s.lo, s.hi, s.rep] : [[], [], []];
        }
    }

    _clear(kind) {
        this.latest[kind] = null;
        this.errors[kind].reset();
        if (kind === 'ecg') {
            for (const d of this.ecgSets) d.data = [];
            this.monitor.setForecast(null);
        } else {
            for (const sets of Object.values(this.accSets)) for (const d of sets) d.data = [];
        }
    }

    _setDelay(kind, ahead) {
        const chart = kind === 'ecg' ? this.ecgView.chart : this.accChart;
        const rt = chart.options.scales.x.realtime;
        const want = ahead ? this.AHEAD_DELAY : this.chartDelay[kind];
        if (rt.delay !== want) { rt.delay = want; chart.update('none'); }
    }

    _tick() {
        for (const kind of ['ecg', 'acc']) {          // a forecast older than 3 s is gone (service stopped)
            if (this.latest[kind] && Date.now() - this.receivedAt[kind] > 3000) this._clear(kind);
        }
        this._render();
    }

    _render() {
        const svc = this.service || {};
        const strip = document.getElementById('ecg-inference');     // the inference strip under the monitor
        if (strip) strip.classList.toggle('hidden', !this.settings.ecg);
        for (const kind of ['ecg', 'acc']) {
            const btn = this.btn[kind], el = this.statusEl[kind];
            const on = this.settings[kind];
            if (btn) setPillActive(btn, on);
            this._setDelay(kind, on && !!this.latest[kind]);
            if (!el) continue;
            let text = '', warn = false, title = '';
            if (on) {
                const ov = (svc.overlays || {})[kind] || {};
                if (!svc.online) { text = '⚠ forecast service offline'; warn = true; }
                else if (svc.state === 'unavailable') { text = '⚠ forecasts unavailable'; title = svc.detail || ''; warn = true; }
                else if (ov.error) { text = '⚠ ' + ov.error; title = ov.error; warn = true; }
                else if (this.latest[kind]) {
                    const p = this.latest[kind];
                    const unit = kind === 'ecg' ? 'mV' : 'mg', dp = kind === 'ecg' ? 3 : 1;
                    const horizon = (p.median[0].length * p.dt_ms / 1000).toFixed(1);
                    text = `${horizon} s ahead · ${p.samples} samples · ${Math.round(p.compute_ms)} ms`;
                    const e = this.errors[kind].summary();
                    if (e) text += ` · error ${e.mae.toFixed(dp)} ${unit} (hold ${e.hold.toFixed(dp)})`;
                    title = `${p.model}: ${p.context_rows} rows of context; band = range of the samples, dashed = ` +
                            `one representative sample; error = mean |actual - median| over the last 30 s ` +
                            `(hold = repeating the last value)`;
                } else if (svc.state === 'loading model') text = 'loading model…';
                else if (svc.state === 'waiting for data') text = 'waiting for data';
                else text = 'starting…';
            }
            el.textContent = text.length > 90 ? text.slice(0, 88) + '…' : text;
            el.title = title || text;
            el.classList.toggle('text-amber-400', warn);
            el.classList.toggle('text-slate-500', !warn);
        }
    }
}
