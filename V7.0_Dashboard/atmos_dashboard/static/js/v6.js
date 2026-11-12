// V6 chart colours (same file in every dashboard; load after themes.js and the page's scripts).
// Chart.js draws on a canvas, out of v6.css's reach: this gives every chart's axes, grid and legend the
// theme's tokens (--axis, --grid, --muted), for the charts the page has now, the ones it makes later
// (tap-a-tile trends) and again whenever the theme changes. Data colours are left alone.
(() => {
    if (!window.Chart) return;
    const root = document.documentElement;
    const tok = (n, d) => getComputedStyle(root).getPropertyValue(n).trim() || d;
    const done = new WeakMap();                       // chart -> the theme it was coloured for

    function paint(chart, theme, axis, grid, muted) {
        const o = chart.options || {};
        for (const sc of Object.values(o.scales || {})) {
            if (!sc) continue;
            if (sc.ticks) sc.ticks.color = axis; else sc.ticks = { color: axis };
            if (sc.grid && sc.grid.display !== false) { sc.grid.color = grid; sc.grid.borderColor = grid; sc.grid.tickColor = grid; }
            if (sc.title && sc.title.display) sc.title.color = muted;
        }
        const lg = o.plugins && o.plugins.legend;
        if (lg && lg.labels) lg.labels.color = muted;
        done.set(chart, theme);
        chart.update('none');
    }
    function all(force) {
        const theme = root.dataset.theme || 'dark';
        const axis = tok('--axis', '#7b88a1'), grid = tok('--grid', 'rgba(148,163,184,.12)'), muted = tok('--muted', '#8592ab');
        Chart.defaults.color = muted;
        Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
        for (const c of Object.values(Chart.instances || {})) {
            if (force || done.get(c) !== theme) {
                try { paint(c, theme, axis, grid, muted); } catch (e) { /* a chart being torn down */ }
            }
        }
    }
    new MutationObserver(() => setTimeout(() => all(true), 50))      // after themes.js's own recolouring
        .observe(root, { attributes: true, attributeFilter: ['data-theme'] });
    const start = () => { all(true); setInterval(() => all(false), 1500); };   // new charts, coloured once each
    document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', start) : start();
})();
