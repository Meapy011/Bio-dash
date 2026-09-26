const $ = (id) => document.getElementById(id);
const statusBadge = $('status-badge');
const scannerPanel = $('scanner-panel');
const BADGE = 'px-4 py-2 rounded-full text-sm font-semibold border ';
const TONES = {
    ok: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
    info: 'bg-sky-500/20 text-sky-400 border-sky-500/30',
    wait: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
    bad: 'bg-red-500/20 text-red-400 border-red-500/30',
};
const setStatus = (text, tone) => { statusBadge.textContent = text; statusBadge.className = BADGE + TONES[tone]; };
const esc = (s) => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// Sensor streams ~1 line/s; if nothing for 15 s the link is gone -> show the scanner again
const STALE_MS = 15000;
let uiIsConnected = false;
let lastPacketAt = 0;

function markLive() {
    lastPacketAt = Date.now();
    if (!uiIsConnected) {
        uiIsConnected = true;
        scannerPanel.style.display = 'none';
        setStatus('Telemetry Active 🟢', 'ok');
    }
}
function markIdle(text, tone) {
    uiIsConnected = false;
    scannerPanel.style.display = 'block';
    setStatus(text, tone);
    updateScannerList();
}
setInterval(() => {
    if (uiIsConnected && Date.now() - lastPacketAt > STALE_MS) markIdle('Signal Lost — Rescanning', 'wait');
}, 1000);

async function updateScannerList() {
    if (uiIsConnected) return;
    try {
        const devices = await (await fetch('/api/scan-results')).json();
        const container = $('device-list');
        if (devices.length === 0) {
            container.innerHTML = '<div class="text-slate-500 text-sm animate-pulse col-span-3">No active environmental monitors detected...</div>';
            return;
        }
        container.innerHTML = devices.map(d => `
            <div class="bg-slate-700/50 p-4 rounded-xl border border-slate-600 flex flex-col gap-3 justify-between">
                <div>
                    <div class="font-bold text-slate-200">${esc(d.name)}</div>
                    <div class="text-xs text-slate-400 font-mono">${esc(d.address)} · ${esc(d.rssi)} dBm</div>
                </div>
                <button data-mac="${esc(d.address)}" class="bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-bold py-2 px-4 rounded w-full transition-colors">
                    Connect Signal
                </button>
            </div>`).join('');
    } catch (err) {}
}

$('device-list').addEventListener('click', async (e) => {
    const mac = e.target.closest('button[data-mac]')?.dataset.mac;
    if (!mac) return;
    $('device-list').innerHTML = `<div class="text-emerald-400 font-bold py-4 col-span-3 animate-pulse">Handshaking with ${esc(mac)}...</div>`;
    await fetch('/api/connect', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({address: mac})
    });
});

setInterval(updateScannerList, 3000);
updateScannerList();

// CSV column -> element id (column 0 is the timestamp)
const FIELDS = ['pm1', 'pm25', 'pm4', 'pm10', 'humidity', 'temp', 'voc', 'nox', 'hcho', 'co2'];
const els = FIELDS.map($);
const co2Card = $('co2').parentElement;

function render(line) {
    const parts = line.split(',');
    if (parts.length < 11) return;
    markLive();
    els.forEach((el, i) => { el.textContent = parts[i + 1]; });
    co2Card.style.borderColor = parseInt(parts[10]) > 1000 ? '#ef4444' : '#f59e0b';
}

function connectWs() {
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
    ws.onopen = () => { if (!uiIsConnected) setStatus('System Online', 'info'); };
    ws.onclose = () => {
        markIdle('Disconnected 🔴 — retrying', 'bad');
        setTimeout(connectWs, 2000);
    };
    // Server may batch several lines into one message; only the newest matters for the tiles
    ws.onmessage = (event) => {
        const lines = event.data.split('\n').filter(Boolean);
        if (lines.length) render(lines[lines.length - 1]);
    };
}
connectWs();
