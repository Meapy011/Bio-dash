// Names you give devices ("Mine", "Boss's H10"), shared by every dashboard (shared file: same copy in each).
// Saved by the server in ~/.config/bio-dash/device_names.json, keyed by the device's advertised name
// (or its address when it has none). Only what you see changes; recordings keep their folder names.
//   Nick.tag(name, address)  -> HTML: the name you gave it (or a "Name it" link), click to change
//   Nick.of(name, address)   -> the name you gave it, or ''
// Also adds a pill next to the page's status showing the remembered device by your name for it.
(() => {
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const css = document.createElement('style');
    css.textContent = `
    .nick{display:inline-flex;align-items:center;gap:4px;margin-left:6px;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;
      line-height:18px;cursor:pointer;vertical-align:middle;border:1px solid rgba(167,139,250,.45);background:rgba(167,139,250,.14);color:#c4b5fd;white-space:nowrap}
    .nick:hover{background:rgba(167,139,250,.26)}
    .nick.empty{border-style:dashed;background:transparent;color:#94a3b8;font-weight:500;border-color:rgba(148,163,184,.45)}
    .nick.empty:hover{color:#cbd5e1}
    .nick-bar{display:inline-flex;gap:6px;flex-wrap:wrap;align-items:center;margin-left:8px}
    :root[data-theme="light"] .nick,:root[data-theme="contrast"] .nick{color:#6d28d9;border-color:#a78bfa;background:#ede9fe}
    :root[data-theme="light"] .nick.empty{color:#64748b;background:transparent;border-color:#cbd5e1}
    :root[data-theme="warm"] .nick{color:#fbbf24;border-color:rgba(251,191,36,.45);background:rgba(251,191,36,.12)}
    @media (prefers-color-scheme: light){:root:not([data-theme]) .nick,:root[data-theme="auto"] .nick{color:#6d28d9;border-color:#a78bfa;background:#ede9fe}}`;
    document.head.appendChild(css);

    const Nick = window.Nick = {
        names: {},
        key(name, address) {
            name = String(name || '').trim();
            if (name && name !== '(no name)' && name !== 'unknown device' && name.toUpperCase() !== String(address || '').toUpperCase()) return name;
            return String(address || '').trim().toUpperCase();
        },
        of(name, address) { return Nick.names[Nick.key(name, address)] || ''; },
        tag(name, address, opts = {}) {
            const k = Nick.key(name, address);
            if (!k) return '';
            const n = Nick.names[k];
            return `<span class="nick${n ? '' : ' empty'}" data-nick-key="${esc(k)}" title="${n ? 'Your name for this device, click to change' : 'Give this device a name, e.g. Mine or Boss\'s'}">${n ? '🏷 ' + esc(n) : (opts.short ? '🏷' : '🏷 Name it')}</span>`;
        },
        async load() {
            try { Nick.names = (await (await fetch('/api/device-names', { cache: 'no-store' })).json()).names || {}; } catch (e) { /* older server */ }
            Nick.paint();
        },
        paint() {   // update every tag already on the page (lists that don't re-render still show the new name)
            document.querySelectorAll('[data-nick-key]').forEach(el => {
                const n = Nick.names[el.dataset.nickKey];
                el.classList.toggle('empty', !n);
                el.innerHTML = n ? '🏷 ' + esc(n) : '🏷 Name it';
            });
            paintBar();
        },
        async edit(key) {
            const cur = Nick.names[key] || '';
            const v = window.prompt(`Name for ${key}\n(e.g. Mine, Boss's, Spare. Leave empty to remove.)`, cur);
            if (v === null) return;
            try {
                const res = await fetch('/api/device-names', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key, name: v }) });
                const out = await res.json();
                if (!res.ok) { window.alert(out.error || 'Couldn\'t save the name.'); return; }
                Nick.names = out.names || {};
            } catch (e) { window.alert('No reply from the dashboard.'); return; }
            Nick.paint();
        },
    };

    // clicks on a tag edit the name, and never reach the card's Connect handler underneath
    document.addEventListener('click', (e) => {
        const t = e.target.closest('[data-nick-key]');
        if (!t) return;
        e.preventDefault();
        e.stopPropagation();
        Nick.edit(t.dataset.nickKey);
    }, true);

    // next to the status pill: the remembered device(s), by your name for them
    let bar = null, last = {};
    function paintBar() {
        const status = document.getElementById('status') || document.querySelector('[id^="stat-"]');
        if (!status) return;
        if (!bar) {
            bar = document.createElement('span');
            bar.className = 'nick-bar';
            status.insertAdjacentElement('afterend', bar);
        }
        const rows = Object.values(last).filter(d => d && (d.name || d.address));
        bar.innerHTML = rows.map(d => Nick.tag(d.name, d.address)).join('');
    }
    async function loadLast() {
        try { last = (await (await fetch('/api/last-device', { cache: 'no-store' })).json()).devices || {}; } catch (e) { last = {}; }
        paintBar();
    }

    const start = () => { Nick.load(); loadLast(); };
    document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', start) : start();
    setInterval(loadLast, 5000);
    setInterval(Nick.load, 30000);   // names given on another dashboard or computer
})();
