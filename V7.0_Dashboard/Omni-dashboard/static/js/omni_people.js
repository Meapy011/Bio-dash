// Omni V7: the people, the devices nearby and the comparison.
//
//   /api/people (polled every 2 s)   who is here, their devices and radios, the devices nearby
//   /api/stream (server-sent events) everyone's live values, ECG, accelerometer and beats, 10 a second
//
// Each person has a colour AND a line style (solid, dashed, dotted) on the shared charts, so colour never
// carries who is who on its own; their name sits beside both in the legend and the table.
(() => {
    const $ = (id) => document.getElementById(id);
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const ROLE = { polar: 'Polar H10', viatom: 'O2 ring' };
    const DASH = [[], [9, 5], [2, 4]];
    const LS = ['', 'dash', 'dot'];
    const WINDOW_MS = 5 * 60 * 1000;
    const POST = (url, body, method = 'POST') => fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                                                          body: body ? JSON.stringify(body) : undefined })
        .then(r => r.json()).catch(() => ({}));
    const store = { get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
                    set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} } };

    let view = { people: [], devices: { polar: [], viatom: [] }, max: 3, scanner: {}, routing: {} };
    const cards = new Map();          // person id -> card
    const live = {};                  // person id -> latest stream packet
    const lastSample = {};            // person id -> ms of the last point on the shared charts
    const indexOf = (pid) => view.people.findIndex(p => p.id === pid);
    const isLive = (st) => st === 'Connected' || st === 'Calibrating';

    // ------------------------------------------------------------------ shared charts
    function sharedChart(id, y) {
        return new Chart($(id).getContext('2d'), {
            type: 'line', data: { datasets: [] },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false, parsing: false,
                scales: { x: { type: 'realtime', realtime: { duration: WINDOW_MS, refresh: 1000, delay: 1500, ttl: WINDOW_MS + 15000 },
                               ticks: { maxTicksLimit: 6 } },
                          y: { grace: '5%', ...y } },
                plugins: { legend: { display: false },
                           tooltip: { mode: 'nearest', intersect: false, callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.y}` } } },
            },
        });
    }
    const shared = {
        hr: sharedChart('cmp-hr', { suggestedMin: 50, suggestedMax: 100 }),
        spo2: sharedChart('cmp-spo2', { suggestedMin: 90, suggestedMax: 100 }),
        rmssd: sharedChart('cmp-rmssd', { suggestedMin: 0, suggestedMax: 60 }),
    };
    const HISTORY = { hr: 'polar_hr', spo2: 'spo2', rmssd: 'rmssd' };

    function sharedSet(chart, p, i) {
        let ds = chart.data.datasets.find(d => d.pid === p.id);
        if (!ds) {
            ds = { pid: p.id, data: [], borderWidth: 2, pointRadius: 0, fill: false, stepped: chart !== shared.rmssd };
            chart.data.datasets.push(ds);
        }
        Object.assign(ds, { label: p.name, borderColor: p.color, backgroundColor: p.color, borderDash: DASH[i % 3] });
        return ds;
    }
    async function prefill(p) {
        for (const [k, metric] of Object.entries(HISTORY)) {
            try {
                const d = await (await fetch(`/api/history?metric=${metric}&minutes=5&person=${encodeURIComponent(p.id)}`)).json();
                const ds = shared[k].data.datasets.find(x => x.pid === p.id);
                if (ds && d[metric] && !ds.data.length) ds.data.push(...d[metric]);
            } catch (e) { /* not recorded yet */ }
        }
    }

    // ------------------------------------------------------------------ a person's card
    const tileDefs = [
        ['ecg_hr', 'Heart rate · H10', 'bpm'], ['ring_hr', 'Heart rate · ring', 'bpm'], ['spo2', 'SpO₂', '%'],
        ['rr', 'R-R', 'ms'], ['rmssd', 'HRV · RMSSD', 'ms'], ['sdnn', 'SDNN', 'ms'],
    ];

    function makeCard(p) {
        const el = document.createElement('section');
        el.className = 'card person';
        el.dataset.pid = p.id;
        el.innerHTML = `
            <div class="p-head">
                <span class="who"><span class="ls"></span></span>
                <button class="p-name" title="Rename"></button>
                <span class="p-tools">
                    <button class="btn sm ghost" data-act="left" title="Move left">◀</button>
                    <button class="btn sm ghost" data-act="right" title="Move right">▶</button>
                    <button class="btn sm danger" data-act="remove" title="Remove this person (disconnects their devices)">Remove</button>
                </span>
            </div>
            <div class="devrow" data-role="polar"><span class="kind">Polar H10</span><span class="dname"></span><span class="pill"></span><span class="acts"></span></div>
            <div class="devrow" data-role="viatom"><span class="kind">O2 ring</span><span class="dname"></span><span class="pill"></span><span class="acts"></span></div>
            <div class="grid tiles">${tileDefs.map(([k, label, unit]) => `
                <div class="tile"><div class="label">${label}</div><div class="value"><span data-v="${k}">--</span><small>${unit}</small></div>
                ${k === 'rmssd' ? '<div class="note" data-v="stress"></div>' : ''}</div>`).join('')}</div>
            <div class="p-ecg"><canvas></canvas><canvas></canvas><div class="nosig">No ECG yet</div><div class="heart">♥</div></div>
            <div class="p-charts">
                <div><h3>R-R (ms)</h3><div class="chart"><canvas data-c="rr"></canvas></div></div>
                <div><h3>Motion (mg)</h3><div class="chart"><canvas data-c="acc"></canvas></div></div>
            </div>`;
        const [grid, trace] = el.querySelectorAll('.p-ecg canvas');
        const card = {
            el, pid: p.id,
            v: Object.fromEntries([...el.querySelectorAll('[data-v]')].map(n => [n.dataset.v, n])),
            mon: new EcgMonitor({ grid, trace, heart: el.querySelector('.heart'), noSignal: el.querySelector('.nosig'),
                                  theme: { trace: p.color, glow: p.color + '88' } }),
            charts: {},
        };
        const small = (canvas, datasets, delay) => new Chart(canvas.getContext('2d'), {
            type: 'line', data: { datasets },
            options: { responsive: true, maintainAspectRatio: false, animation: false, parsing: false,
                       scales: { x: { type: 'realtime', realtime: { duration: 10000, refresh: 100, delay, ttl: 15000 }, ticks: { display: false } } },
                       plugins: { legend: { display: false } } },
        });
        card.charts.rr = small(el.querySelector('[data-c=rr]'),
            [{ data: [], borderColor: p.color, backgroundColor: p.color, borderWidth: 0, pointRadius: 3, showLine: false }], 1500);
        card.charts.acc = small(el.querySelector('[data-c=acc]'), ['#38bdf8', '#a78bfa', '#fbbf24'].map((c, i) =>
            ({ label: 'XYZ'[i], data: [], borderColor: c, borderWidth: 1.2, pointRadius: 0 })), 1000);
        el.addEventListener('click', (e) => onCardClick(card, e));
        return card;
    }

    function destroyCard(card) {
        Object.values(card.charts).forEach(c => c.destroy());
        card.el.remove();
    }

    function deviceRow(card, p, role) {
        const row = card.el.querySelector(`.devrow[data-role=${role}]`);
        const d = p.devices[role], st = p.state[role].status;
        const name = row.querySelector('.dname'), pill = row.querySelector('.pill'), acts = row.querySelector('.acts');
        if (!d) {
            name.innerHTML = `<span class="muted">none yet</span><span class="tiny faint">pick one under Devices nearby</span>`;
            pill.className = 'pill'; pill.textContent = '—';
            acts.innerHTML = '';
            return;
        }
        const radio = p.routing[role] ? `on ${p.routing[role]}` : '';
        name.innerHTML = `${esc(d.name || d.address)}${window.Nick ? Nick.tag(d.name, d.address) : ''}` +
                         `<span class="tiny faint mono">${esc(d.address)}${radio ? ' · ' + esc(radio) : ''}</span>`;
        const label = isLive(st) ? (st === 'Calibrating' ? 'Settling' : 'Live') : st === 'Connecting' ? 'Connecting'
                    : d.auto ? 'Waiting' : 'Off';
        pill.className = 'pill ' + (isLive(st) ? 'ok' : st === 'Connecting' || d.auto ? 'wait' : '');
        pill.innerHTML = `<span class="dot ${isLive(st) ? 'ok' : st === 'Connecting' || d.auto ? 'wait' : 'off'}"></span>${label}`;
        pill.title = d.auto && !isLive(st) ? 'Connects as soon as it is in range (switch it on)' : '';
        const on = isLive(st) || st === 'Connecting' || d.auto;
        acts.innerHTML = `<button class="btn sm" data-act="${on ? 'disconnect' : 'connect'}" data-role="${role}">${on ? 'Disconnect' : 'Connect'}</button>` +
                         `<button class="btn sm ghost" data-act="forget" data-role="${role}" title="Unassign this device">Forget</button>`;
    }

    function updateCard(card, p, i) {
        card.el.style.setProperty('--pc', p.color);
        const ls = card.el.querySelector('.who .ls');
        ls.className = 'ls ' + LS[i % 3];
        const nameBtn = card.el.querySelector('.p-name');
        if (nameBtn.tagName === 'BUTTON') nameBtn.textContent = p.name;
        card.el.querySelector('[data-act=left]').disabled = i === 0;
        card.el.querySelector('[data-act=right]').disabled = i === view.people.length - 1;
        card.el.querySelector('[data-act=remove]').disabled = view.people.length === 1;
        deviceRow(card, p, 'polar');
        deviceRow(card, p, 'viatom');
    }

    async function onCardClick(card, e) {
        const b = e.target.closest('button');
        if (!b) return;
        const pid = card.pid;
        if (b.classList.contains('p-name')) return rename(card, b);
        const act = b.dataset.act;
        if (act === 'left' || act === 'right') render(await POST(`/api/people/${pid}`, { move: act === 'left' ? -1 : 1 }));
        else if (act === 'remove') {
            if (b.dataset.armed !== '1') {          // two clicks: no browser dialog
                b.dataset.armed = '1'; b.textContent = 'Remove? Click again';
                setTimeout(() => { b.dataset.armed = ''; b.textContent = 'Remove'; }, 3500);
                return;
            }
            render(await POST(`/api/people/${pid}`, null, 'DELETE'));
        } else if (['disconnect', 'connect', 'forget'].includes(act)) {
            b.disabled = true;
            render(await POST(`/api/people/${pid}/device`, { role: b.dataset.role, [act]: true }));
        }
    }

    function rename(card, btn) {
        const input = document.createElement('input');
        input.className = 'p-name';
        input.value = btn.textContent;
        input.maxLength = 40;
        btn.replaceWith(input);
        input.focus(); input.select();
        let done = false;
        const finish = async (save) => {
            if (done) return; done = true;
            const name = input.value.trim();
            input.replaceWith(btn);
            if (save && name && name !== btn.textContent) { btn.textContent = name; render(await POST(`/api/people/${card.pid}`, { name })); }
        };
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') finish(true); if (e.key === 'Escape') finish(false); });
        input.addEventListener('blur', () => finish(true));
    }

    // ------------------------------------------------------------------ devices nearby
    function renderFound() {
        const box = $('found');
        const items = [...(view.devices.polar || []), ...(view.devices.viatom || [])];
        // devices that are connected stop advertising: list them from the people too
        for (const p of view.people) for (const role of ['polar', 'viatom']) {
            const d = p.devices[role];
            if (d && !items.some(x => (x.address || '').toUpperCase() === (d.address || '').toUpperCase()))
                items.push({ name: d.name || d.address, address: d.address, role, person: p.id, rssi: null, away: true });
        }
        if (!items.length) {
            box.innerHTML = `<div class="found-empty">Nothing yet. Switch the H10s on (wet the strap) and the O2 rings on; they show up here.</div>`;
            return;
        }
        const room = view.people.length < view.max;
        const free = items.filter(d => !d.person), taken = items.filter(d => d.person);
        const showTaken = store.get('omni-show-assigned') === '1';
        const list = free.concat(showTaken ? taken : []);
        const toggle = taken.length ? `<div class="found-empty"><button class="btn sm ghost" data-show-assigned="1">${showTaken ? 'Hide' : 'Show'} the ${taken.length} assigned device${taken.length === 1 ? '' : 's'}</button>${free.length ? '' : ' · every device found is assigned'}</div>` : '';
        const title = (d) => (d.role === 'polar' && /polar h10/i.test(d.name)) || (d.role === 'viatom' && /o2/i.test(d.name))
            ? esc(d.name) : `${esc(ROLE[d.role])} · ${esc(d.name)}`;
        box.innerHTML = list.map(d => {
            const owner = view.people.find(p => p.id === d.person);
            const others = view.people.filter(p => p.id !== d.person);
            const st = owner ? owner.state[d.role].status : null;
            const badge = owner ? `<span class="pill ${isLive(st) ? 'ok' : ''}" style="--pc:${esc(owner.color)}">${esc(owner.name)}’s${isLive(st) ? ' · live' : ''}</span>` : '';
            const give = others.map(p => `<button class="btn sm" style="--pc:${esc(p.color)}" data-give="${esc(p.id)}" data-role="${d.role}"
                    data-address="${esc(d.address)}" data-name="${esc(d.name)}">${owner ? 'Move to' : 'Give to'} ${esc(p.name)}</button>`).join('') +
                (room ? `<button class="btn sm ghost" data-give="new" data-role="${d.role}" data-address="${esc(d.address)}" data-name="${esc(d.name)}">New person</button>` : '');
            return `<div class="item">
                <div class="grow"><div class="title">${title(d)}${window.Nick ? Nick.tag(d.name, d.address) : ''}</div>
                    <div class="desc mono">${esc(d.address)}${d.rssi != null ? ` · ${esc(d.rssi)} dBm` : d.away ? ' · not advertising (connected, or out of range)' : ''}</div></div>
                ${badge}<div class="give">${give}</div></div>`;
        }).join('') + toggle;
    }

    $('found').addEventListener('click', async (e) => {
        if (e.target.closest('button[data-show-assigned]')) {
            store.set('omni-show-assigned', store.get('omni-show-assigned') === '1' ? '0' : '1');
            return renderFound();
        }
        const b = e.target.closest('button[data-give]');
        if (!b) return;
        b.classList.add('busy');
        let pid = b.dataset.give;
        if (pid === 'new') {
            const v = await POST('/api/people', {});
            if (!v.added) { b.classList.remove('busy'); return; }
            pid = v.added;
        }
        render(await POST(`/api/people/${pid}/device`, { role: b.dataset.role, address: b.dataset.address, name: b.dataset.name }));
    });
    $('find-btn').addEventListener('click', async (e) => { e.currentTarget.classList.add('busy'); await POST('/api/scan', {});
                                                           setTimeout(() => $('find-btn').classList.remove('busy'), 1500); });
    function setScanOpen(open) {
        $('found').hidden = !open;
        $('scan-toggle').textContent = open ? 'Hide' : 'Show';
        $('scan-toggle').setAttribute('aria-expanded', String(open));
        store.set('omni-scan-open', open ? '1' : '0');
    }
    $('scan-toggle').addEventListener('click', () => setScanOpen($('found').hidden));
    setScanOpen(store.get('omni-scan-open') !== '0');
    $('add-person').addEventListener('click', async () => render(await POST('/api/people', {})));

    // ------------------------------------------------------------------ the whole page from /api/people
    function render(v) {
        if (!v || !v.people) return;
        view = v;
        // cards: create, update, order, remove
        const box = $('people');
        view.people.forEach((p, i) => {
            let card = cards.get(p.id);
            if (!card) { card = makeCard(p); cards.set(p.id, card); }
            updateCard(card, p, i);
            if (box.children[i] !== card.el) box.insertBefore(card.el, box.children[i] || null);
            Object.values(shared).forEach(c => sharedSet(c, p, i));
            if (!card.prefilled) { card.prefilled = true; prefill(p); }
        });
        for (const [pid, card] of cards) if (indexOf(pid) < 0) {
            destroyCard(card); cards.delete(pid);
            Object.values(shared).forEach(c => { c.data.datasets = c.data.datasets.filter(d => d.pid !== pid); });
        }
        Object.values(shared).forEach(c => c.update('none'));
        $('cmp-legend').innerHTML = view.people.map((p, i) =>
            `<span class="who" style="--pc:${esc(p.color)}"><span class="ls ${LS[i % 3]}"></span>${esc(p.name)}</span>`).join('');
        $('add-person').disabled = view.people.length >= view.max;
        $('add-person').title = view.people.length >= view.max ? `Up to ${view.max} people (one H10 per radio)` : 'Add a person';
        // header status
        const devs = view.people.flatMap(p => ['polar', 'viatom'].filter(r => p.devices[r]).map(r => p.state[r].status));
        const n = devs.filter(isLive).length;
        $('status').className = 'pill ' + (n && n === devs.length ? 'ok' : 'wait');
        $('status').innerHTML = `<span class="dot ${n && n === devs.length ? 'ok' : 'wait'}"></span>` +
            `${view.people.length} ${view.people.length === 1 ? 'person' : 'people'} · ${n} of ${devs.length} devices live`;
        // scanner line
        const s = view.scanner || {};
        $('scan-line').textContent = s.scanning ? `scanning on ${s.radio || 'the default radio'}` :
            s.paused_reason ? `paused: ${s.paused_reason}` : 'idle';
        renderFound();
        renderTable();
    }

    async function poll() {
        try { render(await (await fetch('/api/people')).json()); } catch (e) { /* server restarting */ }
    }
    poll();
    setInterval(poll, 2000);

    // ------------------------------------------------------------------ the table
    const fmt = (x, dp = 0) => (x ? (dp ? x.toFixed(dp) : Math.round(x)) : null);
    function renderTable() {
        const body = $('cmp-table').querySelector('tbody');
        body.innerHTML = view.people.map((p, i) => {
            const l = live[p.id] || { polar: p.state.polar, viatom: p.state.viatom };
            const pl = isLive(l.polar.status), vl = isLive(l.viatom.status);
            const cell = (on, v, unit) => (on && v != null) ? `<td>${v} <span class="muted small">${unit}</span></td>` : `<td class="off">—</td>`;
            return `<tr><td><span class="who" style="--pc:${esc(p.color)}"><span class="ls ${LS[i % 3]}"></span>${esc(p.name)}</span></td>` +
                cell(pl, fmt(l.polar.hr), 'bpm') + cell(vl, fmt(l.viatom.hr), 'bpm') + cell(vl, fmt(l.viatom.spo2), '%') +
                cell(pl, fmt(l.polar.rr), 'ms') + cell(pl, fmt(l.polar.rmssd, 1), 'ms') + cell(pl, fmt(l.polar.sdnn, 1), 'ms') + '</tr>';
        }).join('');
    }
    setInterval(renderTable, 1000);

    // ------------------------------------------------------------------ live data
    const stress = (r) => r < 20 ? 'lower variability' : r < 50 ? 'moderate variability' : 'higher variability';
    const src = new EventSource('/api/stream');
    src.onmessage = (e) => {
        let d; try { d = JSON.parse(e.data); } catch (err) { return; }
        if (typeof DebugPanel !== 'undefined') DebugPanel.onPacket();
        const now = Date.now();
        for (const [pid, pk] of Object.entries(d.people || {})) {
            live[pid] = pk;
            const card = cards.get(pid);
            if (!card) continue;
            const { polar, viatom } = pk, v = card.v;
            const pOn = polar.status === 'Connected', vOn = isLive(viatom.status);
            v.ecg_hr.textContent = pOn && polar.hr > 0 ? polar.hr : '--';
            v.rr.textContent = pOn && polar.rr > 0 ? polar.rr : '--';
            v.rmssd.textContent = pOn && polar.rmssd > 0 ? polar.rmssd.toFixed(1) : '--';
            v.sdnn.textContent = pOn && polar.sdnn > 0 ? polar.sdnn.toFixed(1) : '--';
            v.stress.textContent = pOn && polar.rmssd > 0 ? stress(polar.rmssd) : '';
            v.spo2.textContent = vOn && viatom.spo2 > 0 ? viatom.spo2 : '--';
            v.ring_hr.textContent = vOn && viatom.hr > 0 ? viatom.hr : '--';
            if (pOn) {
                if (polar.ecg_buffer.length) card.mon.push(polar.ecg_buffer);
                const rr = card.charts.rr.data.datasets[0].data;
                for (const [ts, val] of polar.rr_buffer) rr.push({ x: ts, y: val });
                const [ax, ay, az] = card.charts.acc.data.datasets.map(s => s.data);
                for (const [ts, x, y, z] of polar.acc_buffer) { ax.push({ x: ts, y: x }); ay.push({ x: ts, y: y }); az.push({ x: ts, y: z }); }
            }
            if (now - (lastSample[pid] || 0) >= 1000) {          // the shared charts: one point a second
                lastSample[pid] = now;
                const put = (chart, y) => { const ds = chart.data.datasets.find(s => s.pid === pid); if (ds) ds.data.push({ x: now, y }); };
                if (pOn && polar.hr > 0) put(shared.hr, polar.hr);
                if (pOn && polar.rmssd > 0) put(shared.rmssd, Math.round(polar.rmssd * 10) / 10);
                if (vOn && viatom.spo2 > 0) put(shared.spo2, viatom.spo2);
            }
        }
    };

    window.OmniPeople = { get view() { return view; }, cards, shared };     // for the debug panel and the tests
})();
