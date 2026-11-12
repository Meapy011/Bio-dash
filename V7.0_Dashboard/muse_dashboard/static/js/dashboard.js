// Muse-dash: the page. Rows arrive on the websocket as {type, rows} (eeg, optics, imu, bands, heart, fnirs,
// battery), in the columns of the session's CSVs (muse_worker.py).
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const MAIN = ['TP9', 'AF7', 'AF8', 'TP10'];
const BANDS = [['delta', 'δ Delta', '1-4 Hz', '#818cf8'], ['theta', 'θ Theta', '4-8 Hz', '#38bdf8'], ['alpha', 'α Alpha', '8-13 Hz', '#34d399'],
               ['beta', 'β Beta', '13-30 Hz', '#fbbf24'], ['gamma', 'γ Gamma', '30-44 Hz', '#f472b6']];
const CONTACT = { 1: ['good', 'var(--ok)'], 2: ['fair', 'var(--warn)'], 4: ['bad', 'var(--bad)'], 0: ['off', 'var(--line-2)'] };

// Charts in the V6 look: axis and grid colours come from the theme's tokens (re-read when the theme changes)
const tok = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
Chart.defaults.font.size = 11;
const charts = [];
function themeCharts() {
    const axis = tok('--axis') || '#7b88a1', grid = tok('--grid') || 'rgba(148,163,184,.12)';
    for (const c of charts) {
        for (const sc of Object.values(c.options.scales || {})) {
            sc.ticks = { ...(sc.ticks || {}), color: axis };
            if (sc.grid && sc.grid.display !== false) sc.grid.color = grid;
            if (sc.grid) sc.grid.drawBorder = false;          // Chart.js 3
        }
        c.update('none');
    }
    eeg.theme();
}
new MutationObserver(themeCharts).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
const keep = (chart) => { charts.push(chart); return chart; };

const realtime = (duration, delay) => ({
    responsive: true, maintainAspectRatio: false, animation: false,
    scales: { x: { type: 'realtime', realtime: { duration, refresh: 100, delay }, grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 24 } },
              y: { grid: { color: '#33415520' }, ticks: { maxTicksLimit: 5 } } },
    plugins: { legend: { display: false } },
});
const line = (label, color, extra = {}) => ({ label, borderColor: color, backgroundColor: color, borderWidth: 1.5, pointRadius: 0, data: [], ...extra });

// ---------------------------------------------------------------- EEG
const eeg = new EegView({ canvas: $('eegCanvas'), noSignal: $('eeg-nosignal'), channels: ['TP9', 'AF7', 'AF8', 'TP10', 'AUX1', 'AUX2', 'AUX3', 'AUX4'] });
const pillOn = (el, on) => el.setAttribute('aria-pressed', on ? 'true' : 'false');
$('eeg-scale').textContent = `±${eeg.scale} µV`;
$('eeg-notch').textContent = eeg.notch ? `${eeg.notch} Hz` : 'No notch';
$('eeg-filter').textContent = eeg.filtered ? 'Filtered' : 'Raw';
$('eeg-specs').textContent = eeg.specs;
pillOn($('eeg-aux'), eeg.aux);
$('eeg-scale').addEventListener('click', e => { e.currentTarget.textContent = eeg.cycleScale(); });
$('eeg-notch').addEventListener('click', e => { e.currentTarget.textContent = eeg.cycleNotch(); $('eeg-specs').textContent = eeg.specs; });
$('eeg-filter').addEventListener('click', e => { e.currentTarget.textContent = eeg.toggleFilter(); $('eeg-specs').textContent = eeg.specs; });
$('band-legend').innerHTML = BANDS.map(b => `<span><i style="background:${b[3]}"></i>${b[1]}</span>`).join('');
$('eeg-aux').addEventListener('click', e => pillOn(e.currentTarget, eeg.toggleAux()));

// ---------------------------------------------------------------- band powers
const bandBars = keep(new Chart($('bandBars').getContext('2d'), {
    type: 'bar',
    data: { labels: BANDS.map(b => [b[1], b[2]]), datasets: [{ data: BANDS.map(() => 0), backgroundColor: BANDS.map(b => b[3]), borderRadius: 7, maxBarThickness: 64 }] },
    options: { responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
               scales: { y: { min: 0, suggestedMax: 50, grid: { color: '#33415520' }, ticks: { maxTicksLimit: 6, callback: v => v + '%' } },
                         x: { grid: { display: false } } },
               plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => `${c.parsed.y.toFixed(1)} %` } } } },
}));
const bandTrend = keep(new Chart($('bandTrend').getContext('2d'), {
    type: 'line', data: { datasets: BANDS.map(b => line(b[1], b[3])) },
    options: realtime(120000, 2000),
}));
bandTrend.options.scales.y.ticks.callback = v => v + '%';

function contactDots(codes) {
    $('contact').innerHTML = MAIN.map((ch, i) => {
        const [word, color] = CONTACT[codes[i]] || CONTACT[0];
        return `<div title="${ch}: ${word}"><i style="--c:${color}"></i><span>${ch}</span></div>`;
    }).join('');
    const good = codes.filter(c => c === 1 || c === 2).length;
    $('contact-note').textContent = good === 4 ? 'all four in contact' : good ? `${good} of 4 in contact: wet / adjust the others` : 'not on the head yet';
}
contactDots([0, 0, 0, 0]);

let smooth = null;
function onBands(rows) {
    for (const r of rows) {
        const ts = r[0], codes = r.slice(21, 25);
        contactDots(codes);
        // shares of the power per channel in contact (the file holds log10 uV^2), then their mean
        const use = MAIN.map((_, c) => c).filter(c => codes[c] === 1 || codes[c] === 2);
        if (!use.length) { $('alpha-val').textContent = '--'; smooth = null; continue; }
        const share = BANDS.map(() => 0);
        for (const c of use) {
            const p = r.slice(1 + 5 * c, 6 + 5 * c).map(v => 10 ** v), tot = p.reduce((a, b) => a + b, 0);
            p.forEach((v, k) => { share[k] += 100 * v / tot / use.length; });
        }
        // shown smoothed over ~3 s (the files keep every second as it was): one blink or jaw clench
        // would otherwise swing the shares from one second to the next
        smooth = smooth ? smooth.map((v, k) => v + (share[k] - v) * 0.35) : share;
        bandBars.data.datasets[0].data = smooth;
        bandTrend.data.datasets.forEach((d, k) => d.data.push({ x: ts, y: +smooth[k].toFixed(1) }));
        $('alpha-val').textContent = smooth[2].toFixed(0);
        $('alpha-note').textContent = `8-13 Hz, ${use.length} of 4 channels`;
        trends.record('alpha', ts, +share[2].toFixed(1));
    }
    bandBars.update('none');
}

// ---------------------------------------------------------------- fNIRS
const fnirsChart = keep(new Chart($('fnirsChart').getContext('2d'), {
    type: 'line',
    data: { datasets: [line('Left HbO', '#f43f5e'), line('Left HbR', '#3b82f6'),
                       line('Right HbO', '#fb7185', { borderDash: [5, 4] }), line('Right HbR', '#60a5fa', { borderDash: [5, 4] })] },
    options: realtime(120000, 2000),
}));
fnirsChart.options.scales.y.ticks.callback = v => v + ' µM';
function onFnirs(rows) {
    for (const r of rows) fnirsChart.data.datasets.forEach((d, k) => { if (r[k + 1] !== null) d.data.push({ x: r[0], y: r[k + 1] }); });
}
$('fnirs-zero').addEventListener('click', async () => {
    try { await fetch('/api/fnirs/zero', { method: 'POST' }); fnirsChart.data.datasets.forEach(d => { d.data.length = 0; }); } catch (e) {}
});

// ---------------------------------------------------------------- pulse (PPG) and heart rate
const ppgChart = keep(new Chart($('ppgChart').getContext('2d'), { type: 'line', data: { datasets: [line('PPG', tok('--heart') || '#fb7185', { borderWidth: 2 })] }, options: realtime(10000, 1500) }));
ppgChart.options.scales.y.ticks.display = false;
let ppgChannel = null, opticsCols = null, ppgMean = null;
function onOptics(rows) {
    if (!opticsCols || ppgChannel === null) return;
    const i = opticsCols.indexOf(ppgChannel);
    if (i < 0) return;
    const d = ppgChart.data.datasets[0].data;
    for (const r of rows) {
        const x = r[i + 1];
        ppgMean = ppgMean === null ? x : ppgMean + (x - ppgMean) / 64;      // ~1 s high-pass
        d.push({ x: r[0], y: -(x - ppgMean) });                            // more blood = less light: flip it
    }
}
const ibis = [];
function onHeart(rows) {
    for (const [ts, ibi, hr] of rows) {
        $('hr-val').textContent = hr;
        trends.record('hr', ts, hr);
        ibis.push(ibi); if (ibis.length > 20) ibis.shift();
        if (ibis.length > 2) {
            let s = 0; for (let k = 1; k < ibis.length; k++) s += (ibis[k] - ibis[k - 1]) ** 2;
            $('rmssd-val').textContent = Math.sqrt(s / (ibis.length - 1)).toFixed(1);
        }
    }
}

// ---------------------------------------------------------------- motion
const accChart = keep(new Chart($('accChart').getContext('2d'), {
    type: 'line', data: { datasets: [line('X', '#38bdf8'), line('Y', '#a78bfa'), line('Z', '#fbbf24')] },
    options: realtime(10000, 1000),
}));
let gyroAvg = 0;
function onImu(rows) {
    const [dx, dy, dz] = accChart.data.datasets.map(d => d.data);
    for (const [ts, ax, ay, az, gx, gy, gz] of rows) {
        dx.push({ x: ts, y: ax }); dy.push({ x: ts, y: ay }); dz.push({ x: ts, y: az });
        gyroAvg += (Math.hypot(gx, gy, gz) - gyroAvg) / 26;               // ~0.5 s
    }
    const [, ax, ay, az, , , gz] = rows[rows.length - 1];
    const deg = r => `${Math.round(r * 180 / Math.PI) || 0}°`;          // never "-0°"
    $('pitch').textContent = deg(Math.atan2(-ax, Math.hypot(ay, az)));
    $('roll').textContent = deg(Math.atan2(ay, az));
    $('turn').textContent = `${Math.round(gz) || 0} °/s`;
    $('still').textContent = gyroAvg < 5 ? 'still' : gyroAvg < 30 ? 'some' : 'moving';
}

function onBattery(rows) {
    const [ts, pct] = rows[rows.length - 1];
    $('bat-val').textContent = Math.round(pct);
    trends.record('battery', ts, pct);
}

// ---------------------------------------------------------------- tile trends (pre-filled from the session's files)
const trends = createTileTrends({
    storageKey: 'muse-trend', insertAfter: '#tile-row', ring: ['ring-fuchsia-400/70'],
    metrics: {
        hr: { title: 'Heart Rate (PPG)', unit: 'BPM', tile: '#hr-val', color: '#f43f5e', stepped: true, span: 20 },
        alpha: { title: 'Alpha Share', unit: '%', tile: '#alpha-val', color: '#34d399', span: 20, dp: 1 },
        battery: { title: 'Battery', unit: '%', tile: '#bat-val', color: '#38bdf8', span: 10, dp: 1 },
    },
    prefill: async (metric, minutes) => (await fetch(`/api/history?metric=${metric}&minutes=${minutes}`)).json(),
});

// ---------------------------------------------------------------- connection state and the scanner
const statusEl = $('status'), scannerPanel = $('scanner-panel'), disconnectBtn = $('disconnect-btn');
function setStatus(text, tone) {           // the V6 status pill: ok (green) / wait (amber, pulsing) / bad (red)
    statusEl.className = `pill ${tone}`;
    statusEl.innerHTML = `<span class="dot ${tone}"></span>${esc(text)}`;
}
const STALE_MS = 5000;
let uiIsConnected = false, lastPacketAt = 0, ignoreDataUntil = 0;
function markLive() {
    if (Date.now() < ignoreDataUntil) return;
    lastPacketAt = Date.now();
    if (!uiIsConnected) {
        uiIsConnected = true;
        scannerPanel.style.display = 'none';
        disconnectBtn.classList.remove('hidden');
        setStatus('Recording', 'ok');
    }
}
function markIdle(text, tone) {
    uiIsConnected = false;
    disconnectBtn.classList.add('hidden');
    scannerPanel.style.display = 'block';
    setStatus(text, tone);
    updateScannerList();
}
setInterval(() => { if (uiIsConnected && Date.now() - lastPacketAt > STALE_MS) markIdle('Signal lost, looking again', 'wait'); }, 1000);

let disconnectArmed = 0;
disconnectBtn.addEventListener('click', async () => {
    if (Date.now() - disconnectArmed > 3000) {
        disconnectArmed = Date.now();
        disconnectBtn.textContent = 'Tap again to disconnect';
        setTimeout(() => { if (Date.now() - disconnectArmed >= 3000) disconnectBtn.textContent = '⏏ Disconnect'; }, 3100);
        return;
    }
    disconnectArmed = 0; disconnectBtn.textContent = '⏏ Disconnect';
    try { await fetch('/api/disconnect', { method: 'POST' }); ignoreDataUntil = Date.now() + 3000; markIdle('Disconnected: pick a headband', 'wait'); }
    catch (e) { setStatus('Couldn’t reach the dashboard', 'bad'); }
});

const deviceList = $('device-list');
const note = (html, tone = 'muted') => { deviceList.innerHTML = `<div class="small ${tone}" style="grid-column:1/-1;padding:6px 0">${html}</div>`; };
let lastListKey = '';
async function updateScannerList() {
    if (uiIsConnected) return;
    try {
        const st = await (await fetch('/api/status')).json();
        if (st.state === 'connecting') {
            lastListKey = '';
            const tries = st.attempt > 1 ? ` (attempt ${st.attempt}/${st.max_attempts})` : '';
            return note(`Connecting to ${esc(st.address)}${tries}…`, 'ok-note');
        }
        if (st.state === 'streaming') { lastListKey = ''; return note('Connected, starting the headband’s streams…', 'ok-note'); }
        const devices = await (await fetch('/api/scan-results')).json();
        const key = JSON.stringify(devices.map(d => d.address));
        if (key === lastListKey) {
            devices.forEach(d => { const el = deviceList.querySelector(`[data-rssi="${CSS.escape(d.address)}"]`); if (el) el.textContent = `${d.rssi} dBm`; });
            return;
        }
        lastListKey = key;
        if (!devices.length) return note('None in range yet. Switch it on (hold the button until the lights move) and close the Muse phone app.');
        deviceList.innerHTML = devices.map(d => `
            <div class="tile" style="display:flex;flex-direction:column;gap:10px">
                <div>
                    <div style="font-weight:650">${esc(d.name)}${window.Nick ? Nick.tag(d.name, d.address) : ''}</div>
                    <div class="mono muted" style="margin-top:2px">${esc(d.address)} · <span data-rssi="${esc(d.address)}">${esc(d.rssi)} dBm</span></div>
                </div>
                <button data-mac="${esc(d.address)}" class="btn primary block">Connect Headband</button>
            </div>`).join('');
    } catch (err) {}
}
deviceList.addEventListener('click', async (e) => {
    const mac = e.target.closest('button[data-mac]')?.dataset.mac;
    if (!mac) return;
    lastListKey = '';
    note(`Connecting to ${esc(mac)}…`, 'ok-note');
    await fetch('/api/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ address: mac }) });
});
setInterval(updateScannerList, 1000);
updateScannerList();

// What the worker reports about the signals: which optics channel carries the pulse, the fNIRS baseline, the preset
const OPTICS_NAMES = { 16: ['LO_NIR', 'RO_NIR', 'LO_IR', 'RO_IR', 'LI_NIR', 'RI_NIR', 'LI_IR', 'RI_IR', 'LO_RED', 'RO_RED', 'LO_AMB', 'RO_AMB', 'LI_RED', 'RI_RED', 'LI_AMB', 'RI_AMB'],
                       8: ['LO_NIR', 'RO_NIR', 'LO_IR', 'RO_IR', 'LI_NIR', 'RI_NIR', 'LI_IR', 'RI_IR'], 4: ['LI_NIR', 'RI_NIR', 'LI_IR', 'RI_IR'], 0: [] };
async function pollSignals() {
    try {
        const st = await (await fetch('/api/status')).json(), d = st.debug || {}, sig = d.signals || {};
        opticsCols = OPTICS_NAMES[d.layout?.optics ?? 16] || null;
        if (sig.ppg && sig.ppg.channel !== ppgChannel) { ppgChannel = sig.ppg.channel; ppgMean = null; }
        $('ppg-note').textContent = sig.ppg ? (sig.ppg.channel ? `${sig.ppg.channel} · pulse clarity ${Math.round(100 * sig.ppg.quality)}%` : 'looking for a pulse…') : '';
        if (sig.ppg && !sig.ppg.hr && uiIsConnected) $('hr-note').textContent = 'no clear pulse yet: keep still';
        else $('hr-note').textContent = 'from the forehead PPG';
        const f = sig.fnirs;
        $('fnirs-note').textContent = !f ? (d.layout && !d.layout.optics ? 'this preset has no optics' : '')
            : f.baseline_s < f.of_s ? `baseline ${f.baseline_s}/${f.of_s} s — keep still` : `${Object.values(f.optodes)[0] || ''} optodes`;
        const dev = d.device || {};
        $('dev-name').textContent = uiIsConnected && dev.name ? ` · ${dev.name}` : '';      // its name tag sits by the status
        if (d.preset) $('preset-note').textContent = `preset ${d.preset}${d.info?.fw ? ' · fw ' + d.info.fw : ''}`;
        if (d.rates?.battery != null && $('bat-val').textContent === '--') $('bat-val').textContent = Math.round(d.rates.battery);
    } catch (e) {}
}
setInterval(pollSignals, 2000);
pollSignals();

// ---------------------------------------------------------------- the websocket
const handlers = { eeg: rows => eeg.push(rows), bands: onBands, fnirs: onFnirs, optics: onOptics, heart: onHeart, imu: onImu, battery: onBattery };
const LIVE = new Set(['eeg', 'optics', 'imu']);           // these mean the headband is streaming now
function connectWs() {
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
    ws.onopen = () => { if (!uiIsConnected) setStatus('Looking for the headband', 'wait'); };
    ws.onclose = () => { markIdle('Dashboard offline, retrying', 'bad'); setTimeout(connectWs, 2000); };
    ws.onmessage = (event) => {
        const packet = JSON.parse(event.data), h = handlers[packet.type];
        if (!h || !packet.rows || !packet.rows.length) return;
        DebugPanel.onPacket(packet.type, packet.rows.length);
        if (LIVE.has(packet.type)) markLive();
        h(packet.rows);
    };
}
connectWs();
themeCharts();
