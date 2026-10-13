// Hydro-dash: HidrateSpark bottle. Polls the worker status (1 s) and the summary computed
// from the sip log (5 s, or immediately when a new sip arrives).

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ML_PER_OZ = 29.5735;
let units = 'ml';
let settings = {};
const vol = (ml, d = 0) => ml == null ? '--' : units === 'oz' ? (ml / ML_PER_OZ).toFixed(1) : Math.round(ml).toFixed(d);
const unitLabel = () => units === 'oz' ? 'fl oz' : 'mL';
const ago = (ts) => {
    const s = Math.max(0, Date.now() / 1000 - ts);
    return s < 60 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};

// ---------- status badge ----------
const BADGE = 'px-3 py-1 rounded-full text-xs font-semibold border ';
const TONES = { ok: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30', wait: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
                info: 'bg-sky-500/20 text-sky-400 border-sky-500/30', bad: 'bg-red-500/20 text-red-400 border-red-500/30' };
const setStatus = (text, tone) => { $('status').textContent = text; $('status').className = BADGE + TONES[tone]; };

// ---------- charts ----------
const axis = { ticks: { color: '#94a3b8' }, grid: { color: '#1e293b' } };
const hourChart = new Chart($('hourChart'), {
    type: 'bar',
    data: { labels: [...Array(24).keys()].map(h => `${h}`), datasets: [{ data: Array(24).fill(0), backgroundColor: '#22d3ee', borderRadius: 3 }] },
    options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false } },
               scales: { x: axis, y: { ...axis, beginAtZero: true } } },
});
const weekChart = new Chart($('weekChart'), {
    type: 'bar',
    data: { labels: [], datasets: [
        { type: 'line', label: 'Goal', data: [], borderColor: '#f59e0b', borderDash: [6, 4], borderWidth: 2, pointRadius: 0 },
        { label: 'Intake', data: [], backgroundColor: '#38bdf8', borderRadius: 4 } ] },
    options: { responsive: true, maintainAspectRatio: false, animation: false,
               plugins: { legend: { display: true, labels: { color: '#94a3b8', boxWidth: 12 } } },
               scales: { x: axis, y: { ...axis, beginAtZero: true } } },
});

// ---------- summary (from the sip log) ----------
let lastSipSeen = null;
async function loadSummary() {
    let s;
    try { s = await (await fetch('/api/summary?days=7')).json(); } catch (e) { return; }
    const goal = s.goal_ml || 2500;
    $('today-val').textContent = vol(s.today_ml);
    $('today-goal').textContent = `${Math.round(100 * s.today_ml / goal)}% of ${vol(goal)} ${unitLabel()} goal`;
    $('goal-ring').style.strokeDashoffset = 314.16 * (1 - Math.min(1, s.today_ml / goal));
    lastToday = s.today_ml;
    paintPace();
    $('sips-val').textContent = s.sips_today;
    $('refills-val').textContent = s.refills_today;
    if (s.last_sip) { $('last-val').textContent = vol(s.last_sip.ml); $('last-ago').textContent = ago(s.last_sip.ts); }
    const k = units === 'oz' ? 1 / ML_PER_OZ : 1;
    hourChart.data.datasets[0].data = s.hourly.map(v => +(v * k).toFixed(1));
    hourChart.update('none');
    weekChart.data.labels = s.daily.map(d => luxon.DateTime.fromISO(d.date).toFormat('ccc d'));
    weekChart.data.datasets[1].data = s.daily.map(d => +(d.ml * k).toFixed(1));
    weekChart.data.datasets[0].data = s.daily.map(() => +(goal * k).toFixed(1));
    weekChart.update('none');
    $('sip-list').innerHTML = s.recent.length ? s.recent.map(r =>
        `<div class="grid grid-cols-3 items-center py-1.5"><span class="text-slate-300">${new Date(r.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
         <span class="font-mono text-cyan-300 text-right pr-6">${vol(r.ml)} ${unitLabel()}</span>
         <span class="text-xs text-right ${r.source === 'replayed' ? 'text-slate-500' : 'text-emerald-400'}">${r.source === 'replayed' ? 'synced from bottle' : 'live'}</span></div>`).join('')
        : '<div class="text-slate-500 py-2">No sips yet today.</div>';
    // trend: cumulative intake today
    for (const p of s.cumulative) trends.record('intake', p.x, p.y);
}

// ---------- pace: today's intake against the goal, spread over the reminder hours ----------
let lastToday = null;
function paintPace() {
    if (lastToday == null) return;
    const r = settings.reminders || {};
    const goal = settings.goal_ml || 2500;
    const p = WaterCalc.pace(lastToday, goal, new Date(), r.from, r.to);
    const el = $('today-pace');
    if (!p) { el.textContent = ''; return; }
    if (p.done) { el.textContent = 'Goal reached 🎉'; el.className = 'text-xs text-emerald-400 mt-0.5'; return; }
    const cap = settings.capacity_ml;
    const bottles = cap ? ` (≈ ${(p.left / cap).toFixed(1)} bottles)` : '';
    let text = `${vol(p.left)} ${unitLabel()} to go${bottles}`, tone = 'text-slate-400';
    if (p.before) text += ` · day starts ${r.from || '08:00'}`;
    else if (p.after) tone = 'text-amber-400';          // the drinking window is over: "to go" says it all
    else if (p.behind) {
        text += ` · ${vol(p.behind)} behind`; tone = 'text-amber-400';
    } else if (!p.after) text += ' · on pace';
    if (p.perHour && !p.before) text += ` · ~${vol(p.perHour)} ${unitLabel()}/h until ${r.to || '22:00'}`;
    el.textContent = text;
    el.className = `text-xs ${tone} mt-0.5`;
}

// ---------- goal calculator (the server works it out: water.py) ----------
const postJSON = async (url, body) => (await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
const tempUnit = () => units === 'oz' ? '°F' : '°C';
const temp = (c) => c == null ? '--' : units === 'oz' ? Math.round(c * 9 / 5 + 32) : Math.round(c);
let calcPlace = null;            // {name, label, lat, lon}
function calcInputs() {
    const w = $('calc-weight').value;
    return { weight: w === '' ? null : +w, weight_unit: $('calc-unit').value, reference: $('calc-ref').value,
             exercise_min: +$('calc-exercise').value || 0, weather: $('calc-weather').value };
}
let calcResult = null, calcSeq = 0;
async function paintCalc() {
    const seq = ++calcSeq;
    let c;
    try {
        const res = await fetch('/api/goal/suggest', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ goal_calc: calcInputs() }) });
        const text = await res.text();
        try { c = JSON.parse(text); } catch (e) { c = { detail: text.slice(0, 200) }; }
        if (!res.ok || c.goal == null) throw new Error(res.status === 404
            ? 'the server is older than this page: restart Hydro (systemctl --user restart biodash-hydro)'
            : `server said ${res.status}: ${c.detail || text.slice(0, 200)}`);
    } catch (e) {
        if (seq !== calcSeq) return;
        $('calc-goal').textContent = '--';
        $('calc-working').innerHTML = '';
        $('calc-weather-now').textContent = `Couldn't work out the goal: ${e.message}`;
        $('calc-weather-now').className = 'mt-3 text-xs text-amber-400';
        return;
    }
    if (seq !== calcSeq) return;                    // a newer edit is on its way
    $('calc-weather-now').className = 'mt-3 text-xs text-slate-400';
    calcResult = c;
    $('calc-goal').textContent = vol(c.goal);
    $('calc-goal-unit').textContent = unitLabel();
    const cap = settings.capacity_ml, u = unitLabel(), lines = [];
    $('calc-bottles').textContent = cap ? `≈ ${(c.goal / cap).toFixed(1)} bottles of ${vol(cap)} ${unitLabel()}` : '';
    lines.push([`reference intake  ${vol(c.reference)} ${u}`, `${$('calc-ref').value === 'women' ? '2.7' : '3.7'} L total × 80 % from drinks`]);
    if (c.by_weight) {
        lines.push([`by body weight    ${vol(c.by_weight)} ${u}`, `${c.kg} kg × 33 mL/kg ("half your weight in oz")`]);
        lines.push([`halfway between   ${vol(c.base)} ${u}`, '']);
    } else lines.push(['', 'add your weight to scale it to you']);
    if (c.exercise) lines.push([`+ exercise        ${vol(c.exercise)} ${u}`, '≈ 0.7 L per hour']);
    const wx = c.weather;
    if (c.weather_ml) lines.push([`+ weather         ${vol(c.weather_ml)} ${u}`,
        wx ? `${c.weather_word}: feels like ${temp(wx.feels_max_c)} ${tempUnit()} at the warmest` : c.weather_word]);
    else if (c.weather_word === 'cold') lines.push(['+ weather         0', "cold: no less water needed (cold air is dry, and thirst runs low)"]);
    else if (c.weather_word === 'mild') lines.push(['+ weather         0', 'mild day']);
    lines.push([`= ${vol(c.goal)} ${u}`, c.clamped ? 'rounded, kept within 1.5–5 L' : 'rounded']);
    $('calc-working').innerHTML = lines.map(([fig, why]) =>
        `<div><span style="white-space:pre">${esc(fig)}</span>${why ? `  <span class="text-slate-500" style="white-space:normal">${esc(why)}</span>` : ''}</div>`).join('');
    // today's weather, in words
    const auto = $('calc-weather').value === 'auto';
    $('calc-place-wrap').style.display = auto ? '' : 'none';
    $('calc-weather-now').textContent = !auto ? '' : c.weather_note ? c.weather_note
        : wx ? `Today in ${calcPlace ? calcPlace.name : 'your town'}: high ${temp(wx.max_c)} ${tempUnit()} (feels like ${temp(wx.feels_max_c)}), low ${temp(wx.min_c)}`
               + (wx.now_c != null ? `, now ${temp(wx.now_c)} ${tempUnit()}` : '') + ' · Open-Meteo' : '';
    const same = c.goal === settings.goal_ml;
    $('calc-use').disabled = same;
    $('calc-use').style.opacity = same ? .5 : 1;
    $('calc-use').textContent = same ? 'This is your goal' : 'Use as my daily goal';
}
function paintPlace() {
    $('calc-place').textContent = calcPlace ? calcPlace.label || calcPlace.name : '';
    const have = !!calcPlace;
    $('calc-town').classList.toggle('hidden', have);
    $('calc-find').classList.toggle('hidden', have);
    $('calc-change').classList.toggle('hidden', !have);
}
function fillCalcForm() {
    const g = settings.goal_calc || {};
    $('calc-weight').value = g.weight ?? '';
    $('calc-unit').value = g.weight_unit || 'lb';
    $('calc-ref').value = g.reference || 'men';
    $('calc-exercise').value = g.exercise_min ?? 0;
    $('calc-weather').value = g.weather || (g.hot ? 'hot' : 'off');
    $('calc-follow').checked = !!g.follow;
    calcPlace = g.place || null;
    paintPlace(); paintCalc();
}
let calcSaveTimer = null, calcPaintTimer = null;
function saveCalc(extra = {}) {
    clearTimeout(calcSaveTimer);
    return postJSON('/api/settings', { goal_calc: Object.assign(calcInputs(), extra) }).then(s => { settings = s; }).catch(() => {});
}
$('calc-form').addEventListener('input', (e) => {
    if (e.target.id === 'calc-town') return;
    clearTimeout(calcPaintTimer); calcPaintTimer = setTimeout(paintCalc, 250);
    clearTimeout(calcSaveTimer); calcSaveTimer = setTimeout(saveCalc, 800);
});
$('calc-form').addEventListener('submit', (e) => { e.preventDefault(); if (document.activeElement === $('calc-town')) findTown(); });
async function findTown() {
    const q = $('calc-town').value.trim();
    const box = $('calc-places');
    if (q.length < 2) return;
    box.classList.remove('hidden');
    box.innerHTML = '<span class="text-slate-500">Searching…</span>';
    let r;
    try {
        const res = await fetch(`/api/goal/places?q=${encodeURIComponent(q)}`);
        r = res.ok ? await res.json() : { places: [], error: res.status === 404 ? 'restart Hydro to use town search' : `town search failed (${res.status})` };
    } catch (e) { r = { places: [], error: 'town search unavailable' }; }
    box.innerHTML = r.places.length ? r.places.map((p, i) =>
        `<button type="button" data-i="${i}" class="px-3 py-1 rounded-full border bg-slate-700/50 text-slate-200 border-slate-600 hover:text-white">${esc(p.label)}</button>`).join('')
        : `<span class="text-slate-500">${esc(r.error || 'No town by that name.')}</span>`;
    box._places = r.places;
}
$('calc-find').addEventListener('click', findTown);
$('calc-places').addEventListener('click', async (e) => {
    const i = e.target.closest('button[data-i]')?.dataset.i;
    if (i == null) return;
    calcPlace = $('calc-places')._places[+i];
    $('calc-places').classList.add('hidden');
    paintPlace();
    await saveCalc({ place: calcPlace });
    paintCalc();
});
$('calc-change').addEventListener('click', () => {
    calcPlace = null; paintPlace(); $('calc-town').value = ''; $('calc-town').focus();
});
$('calc-follow').addEventListener('change', async () => {
    await saveCalc({ follow: $('calc-follow').checked });
    flash('calc-msg', $('calc-follow').checked
        ? "Each morning (from 5:00) your goal is set from this, with that day's weather, and sent to the bottle."
        : 'Your goal stays as it is until you change it.', 8000);
    // turning it on applies today's suggestion now
    if ($('calc-follow').checked && calcResult) $('calc-use').click();
});
$('calc-use').addEventListener('click', async () => {
    if (!calcResult) return;
    await saveCalc();
    try { settings = await postJSON('/api/settings', { goal_ml: calcResult.goal }); } catch (err) { return; }
    fillSettingsForm(); paintCalc(); loadSummary();
    flash('calc-msg', bottleConnected ? `Goal set to ${vol(calcResult.goal)} ${unitLabel()} and sent to the bottle.` : `Goal set to ${vol(calcResult.goal)} ${unitLabel()}; the bottle gets it when it next connects.`, 6000);
});
setInterval(() => { if ($('calc-weather').value === 'auto') paintCalc(); }, 30 * 60 * 1000);   // the forecast moves on

let lastGlowUpload = null;
// ---------- live status + scanner ----------
let showAll = false;
$('show-all').addEventListener('change', (e) => { showAll = e.target.checked; lastListKey = ''; });
let lastListKey = '';

async function poll() {
    let st;
    try { st = await (await fetch('/api/status')).json(); } catch (e) { setStatus('Server offline', 'bad'); return; }
    settings = st.settings || settings;
    // bottle size: "Automatic" follows what the bottle reports; otherwise one of the sizes sold
    bottleCapacity = (st.live || {}).capacity_bottle || null;
    if (document.activeElement !== $('set-capacity')) fillCapacity();
    $('cap-note').textContent = settings.capacity_auto !== false
        ? (bottleCapacity ? '' : 'the bottle reports its size when it connects')
        : (bottleCapacity && bottleCapacity !== settings.capacity_ml ? `the bottle itself reports ${bottleCapacity} mL` : '');
    if (settings.units && settings.units !== units) { units = settings.units; applyUnits(); }
    const live = st.live || {};
    const streaming = st.state === 'streaming';
    bottleConnected = streaming;
    canUploadGlow = live.can_upload_glow;
    const up = live.glow_upload;
    if (up && up.at !== lastGlowUpload && up.state !== 'sending') {
        lastGlowUpload = up.at;
        if (Date.now() / 1000 - up.at < 20) flash('glow-up', up.state === 'done' ? `On the bottle (${up.packets} packets).` : `Upload failed: ${up.error}`, 8000);
    }
    if (!$('glow-note')._t) {
        const n = live.reminders_set;
        $('glow-note').dataset.rest = $('glow-note').textContent = !streaming ? '' : live.can_glow === false ? "this bottle doesn't offer glow control"
            : n != null ? `${n} reminder${n === 1 ? '' : 's'} on the bottle` : '';
    }
    $('scanner-panel').classList.toggle('hidden', streaming);
    if (streaming) setStatus('Bottle connected 🟢', 'ok');
    else if (st.state === 'connecting') setStatus(`Connecting${st.attempt > 1 ? ` (try ${st.attempt}/${st.max_attempts})` : ''}...`, 'wait');
    else setStatus('Looking for the bottle', 'info');

    // fill + battery
    if (live.fill_ml != null) {
        $('fill-val').textContent = vol(live.fill_ml);
        $('fill-bar').style.width = `${live.fill_pct ?? 0}%`;
        $('fill-note').textContent = `${live.fill_pct}% of ${vol(settings.capacity_ml)} ${unitLabel()}` + (live.fill_from === 'last sip record' ? ' · as of the last sip' : '');
        trends.sample('fill', live.fill_ml);
    } else {
        $('fill-val').textContent = '--';
        $('fill-note').textContent = streaming ? 'shows after the first sip (the bottle sends its calibration with it)' : '';
    }
    if (live.battery != null) { $('batt-val').textContent = live.battery; trends.sample('battery', live.battery); }
    if (live.last_sip && live.last_sip.ts !== lastSipSeen) { lastSipSeen = live.last_sip.ts; loadSummary(); }
    if (live.serial) $('subtitle').textContent = `HidrateSpark ${live.serial}${live.firmware ? ' · firmware ' + live.firmware : ''}`;
    if (!streaming) renderDevices(st);
}

async function renderDevices(st) {
    const list = $('device-list');
    if (st.state === 'connecting') {
        lastListKey = '';
        const phase = (st.debug || {}).phase || 'connecting';
        const err = (st.debug || {}).last_error;
        list.innerHTML = `<div class="col-span-3 py-2"><div class="text-cyan-300 font-bold animate-pulse">Connecting to ${esc(st.address)} (try ${esc(st.attempt)}/${esc(st.max_attempts)})...</div>
            <div class="text-xs text-slate-400 mt-1">${esc(phase)}</div>
            ${err && Date.now() / 1000 - err.at < 60 ? `<div class="text-xs text-amber-300 mt-1">last try: ${esc(err.error)}</div>` : ''}</div>`;
        return;
    }
    let devices = [];
    try { devices = await (await fetch('/api/scan-results')).json(); } catch (e) { return; }
    const shown = devices.filter(d => showAll || d.bottle);
    const key = JSON.stringify(shown.map(d => d.address)) + showAll;
    if (key === lastListKey) return;
    lastListKey = key;
    list.innerHTML = shown.length ? shown.map(d => `
        <div class="bg-slate-700/50 p-4 rounded-xl border ${d.bottle ? 'border-cyan-500/40' : 'border-slate-600'} flex flex-col gap-3">
            <div><div class="font-bold text-slate-200">${esc(d.name)} ${d.bottle ? '<span class="text-xs text-cyan-300">· HidrateSpark</span>' : ''}</div>
                 <div class="text-xs text-slate-400 font-mono">${esc(d.address)} · ${esc(d.rssi)} dBm</div>
                 ${(d.services || []).length || (d.mfr || []).length ? `<div class="text-[11px] text-slate-500 font-mono mt-1">${(d.services || []).length ? 'svc ' + esc(d.services.join(' ')) : ''}${(d.mfr || []).length ? ' · mfr ' + esc(d.mfr.join(' ')) : ''}</div>` : ''}</div>
            <button data-mac="${esc(d.address)}" class="bg-cyan-600 hover:bg-cyan-500 text-white text-sm font-bold py-2 rounded transition-colors">Connect</button>
        </div>`).join('')
        : `<div class="text-slate-500 text-sm col-span-3">${devices.length && !showAll ? 'No HidrateSpark bottle seen yet (tick "Show all" if yours uses another name).' : 'Nothing in range yet...'}</div>`;
}
$('device-list').addEventListener('click', async (e) => {
    const mac = e.target.closest('button[data-mac]')?.dataset.mac;
    if (!mac) return;
    await fetch('/api/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ address: mac }) });
});

// ---------- settings + calibration ----------
function fillGlowForm() {
    const r = settings.reminders || {};
    $('rem-enabled').checked = r.enabled !== false;
    $('rem-from').value = r.from || '08:00';
    $('rem-to').value = r.to || '22:00';
    $('rem-every').value = r.every_min ?? 60;
    $('rem-always').value = r.always ? 'always' : 'behind';
    $('rem-sound').checked = !!r.sound;
    $('goal-glow').checked = settings.goal_glow !== false;
    $('sip-glow').value = settings.sip_glow == null ? 'leave' : settings.sip_glow ? 'on' : 'off';
}
function flash(id, text, ms = 6000) {
    const el = $(id), was = el.dataset.rest ?? (el.dataset.rest = el.textContent);
    el.textContent = text;
    clearTimeout(el._t); el._t = setTimeout(() => { el.textContent = el.dataset.rest; el._t = null; }, ms);
}
$('glow-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = { reminders: { enabled: $('rem-enabled').checked, from: $('rem-from').value, to: $('rem-to').value,
                                every_min: +$('rem-every').value || 60, always: $('rem-always').value === 'always', sound: $('rem-sound').checked },
                   goal_glow: $('goal-glow').checked, sip_glow: $('sip-glow').value };
    try { settings = await (await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json(); } catch (err) {}
    fillGlowForm();
    flash('glow-msg', bottleConnected ? 'Saved. Sending to the bottle…' : 'Saved. It will be sent when the bottle next connects.');
});
$('glow-now').addEventListener('click', async () => {
    if (!bottleConnected) return flash('glow-note', 'Connect the bottle first.', 4000);
    await fetch('/api/glow', { method: 'POST' });
    flash('glow-note', 'Sent.', 3000);
});
let bottleConnected = false;
// ---------- custom glow: style + two colours, previewed as the LED ring ----------
function ringColours(style, c1, c2, n = 10) {
    const stops = (style === 'rainbow' ? ['#ff0000', '#ffa200', '#eeff00', '#2bff00', '#1499ff', '#ff1ff8'] : [c1, c2])
        .map(h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)));
    return [...Array(n).keys()].map(i => {
        const pos = i * stops.length / n, k = Math.floor(pos), a = stops[k % stops.length], b = stops[(k + 1) % stops.length];
        return `rgb(${a.map((v, j) => Math.round(v + (b[j] - v) * (pos - k))).join(',')})`;
    });
}
function paintGlowPreview() {
    const cols = ringColours($('glow-style').value, $('glow-c1').value, $('glow-c2').value);
    $('glow-preview').innerHTML = cols.map((c, i) => {
        const a = 2 * Math.PI * i / cols.length - Math.PI / 2;
        return `<circle cx="${28 + 21 * Math.cos(a)}" cy="${28 + 21 * Math.sin(a)}" r="4.5" fill="${c}"/>`;
    }).join('');
    const rainbow = $('glow-style').value === 'rainbow';
    $('glow-c1').disabled = $('glow-c2').disabled = rainbow;
    $('glow-c1').style.opacity = $('glow-c2').style.opacity = rainbow ? .35 : 1;
}
['glow-style', 'glow-c1', 'glow-c2'].forEach(id => $(id).addEventListener('input', paintGlowPreview));
function fillGlowStyle() {
    const g = settings.glow || {};
    $('glow-style').value = g.style || 'pulse';
    $('glow-c1').value = g.color1 || '#2bff00';
    $('glow-c2').value = g.color2 || '#1499ff';
    paintGlowPreview();
}
$('glow-send').addEventListener('click', async () => {
    if (!bottleConnected) return flash('glow-up', 'Connect the bottle first.', 4000);
    if (canUploadGlow === false) return flash('glow-up', "This bottle doesn't take custom glows.", 5000);
    const body = { style: $('glow-style').value, color1: $('glow-c1').value, color2: $('glow-c2').value };
    const res = await fetch('/api/glow/pattern', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    flash('glow-up', res.ok ? 'Sending… the bottle plays it when it has arrived.' : 'Not sent.', 8000);
});
let canUploadGlow = null;
// The sizes HidrateSpark sells smart bottles in (mL, and the fl oz on the label).
const BOTTLE_SIZES = [[500, 17], [592, 20], [621, 21], [710, 24], [887, 30], [946, 32]];
let bottleCapacity = null;
function fillCapacity() {
    const box = $('set-capacity');
    const want = settings.capacity_auto !== false ? 'auto' : String(settings.capacity_ml);
    const opts = [['auto', `Automatic${bottleCapacity ? ` (${bottleCapacity} mL)` : ''}`]]
        .concat(BOTTLE_SIZES.map(([ml, oz]) => [String(ml), `${oz} oz · ${ml} mL`]));
    if (want !== 'auto' && !opts.some(o => o[0] === want)) opts.push([want, `${want} mL (custom)`]);   // a value saved before this was a list
    const sig = opts.map(o => o.join('=')).join('|');
    if (box.dataset.sig !== sig) {
        box.innerHTML = opts.map(([v, t]) => `<option value="${v}">${t}</option>`).join('');
        box.dataset.sig = sig;
    }
    box.value = want;
}
function fillSettingsForm() {
    fillCapacity();
    $('set-goal').value = settings.goal_ml ?? '';
    $('set-units').value = settings.units || 'ml';
}
function applyUnits() {
    for (const id of ['today-unit', 'fill-unit', 'last-unit']) $(id).textContent = unitLabel();
    if ($('calc-weight')) paintCalc();
    loadSummary();
}
$('settings-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const cap = $('set-capacity').value;
    const body = { capacity_auto: cap === 'auto' ? true : null, capacity_ml: cap === 'auto' ? null : +cap, goal_ml: +$('set-goal').value || null, units: $('set-units').value };
    try { settings = await (await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json(); } catch (err) {}
    units = settings.units || 'ml'; applyUnits(); fillSettingsForm();
    $('settings-msg').textContent = 'Saved.';
    setTimeout(() => { $('settings-msg').textContent = ''; }, 2500);
});
// Recalibrating changes what the bottle itself stores as "empty" / "full", so it takes a
// second click to confirm (the button arms for 5 s).
let calArmed = null, calTimer = null;
const CAL_LABEL = { full: "It's full now", empty: "It's empty now" };
function paintCal() {
    document.querySelectorAll('button[data-cal]').forEach(b => {
        const armed = calArmed === b.dataset.cal;
        b.textContent = armed ? 'Click again to confirm' : CAL_LABEL[b.dataset.cal];
        b.style.background = armed ? '#dc2626' : ''; b.style.color = armed ? '#fff' : ''; b.style.borderColor = armed ? '#ef4444' : '';
    });
}
document.querySelectorAll('button[data-cal]').forEach(b => b.addEventListener('click', async () => {
    const which = b.dataset.cal;
    clearTimeout(calTimer);
    if (calArmed !== which) {
        calArmed = which; paintCal();
        calTimer = setTimeout(() => { calArmed = null; paintCal(); }, 5000);
        return;
    }
    calArmed = null; paintCal();
    await fetch('/api/calibrate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ which }) });
    $('settings-msg').textContent = `Told the bottle it's ${which}: "In the bottle" now shows ${which === 'full' ? '100%' : '0%'}.`;
    setTimeout(() => { $('settings-msg').textContent = ''; }, 6000);
}));

// ---------- tap-a-tile trends (hydration is slow: hours and days) ----------
const trends = createTileTrends({
    storageKey: 'hydro-trend',
    insertAfter: '#tile-row-2',
    ring: ['ring-cyan-400/70'],
    historyMs: 8 * 24 * 3600 * 1000,
    sampleMs: 30000,
    windows: [[60, '1 hour'], [360, '6 hours'], [1440, '1 day'], [10080, '7 days']],
    metrics: {
        intake:  { title: 'Intake (cumulative, resets daily)', unit: 'mL', tile: '#today-val', color: '#22d3ee', stepped: true, span: 250 },
        fill:    { title: 'In the bottle', unit: 'mL', tile: '#fill-val', color: '#38bdf8', span: 200 },
        battery: { title: 'Bottle battery', unit: '%', tile: '#batt-val', color: '#34d399', stepped: true, fixed: [0, 100] },
    },
    prefill: async (metric, minutes) => (await fetch(`/api/history?metric=${metric}&minutes=${minutes}`)).json(),
});

(async () => {
    try { settings = await (await fetch('/api/settings')).json(); } catch (e) {}
    units = settings.units || 'ml';
    fillSettingsForm(); fillGlowForm(); fillGlowStyle(); fillCalcForm(); applyUnits();
    poll(); setInterval(poll, 1000);
    setInterval(loadSummary, 5000);
    setInterval(() => { if (lastSipSeen) $('last-ago').textContent = ago(lastSipSeen); }, 30000);
})();
