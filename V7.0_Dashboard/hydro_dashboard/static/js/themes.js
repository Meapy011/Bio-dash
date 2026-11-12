// Bio-dash themes: Auto · Dark · Light · OLED black · High contrast · Warm night.
// Same file in every dashboard; loaded in <head> (after Chart.js) so the theme is set before the
// page draws. The choice is a cookie, not localStorage: cookies are shared by every port on a
// host, so picking a theme on one dashboard applies to all of them (open ones follow within a
// few seconds). The look itself is static/css/themes.css; this file picks the theme and
// recolours chart axes and grids, which Chart.js draws on a canvas where CSS can't reach.
(() => {
    const NAMES = { auto: 'Auto', dark: 'Dark', light: 'Light', oled: 'OLED black', contrast: 'High contrast', warm: 'Warm night' };
    const COOKIE = 'biodash_theme';
    const root = document.documentElement;
    const prefersLight = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;

    const readChoice = () => {
        const m = document.cookie.match(/(?:^|;\s*)biodash_theme=([a-z]+)/);
        return m && NAMES[m[1]] ? m[1] : 'auto';
    };
    let choice = readChoice();
    const resolve = (c) => c === 'auto' ? (prefersLight && prefersLight.matches ? 'light' : 'dark') : c;

    // Chart axis / grid / legend colours, by the dark colour each chart was written with.
    // Data colours are left alone: they mean something (heart, oxygen, ...).
    const CHART = {
        light:    { '#94a3b8': '#475569', '#64748b': '#64748b', '#475569': '#94a3b8', '#334155': '#cbd5e1',
                    '#243247': '#e2e8f0', '#1e293b': '#e2e8f0', '#cbd5e1': '#334155', '#e2e8f0': '#1e293b', '#f1f5f9': '#0f172a' },
        contrast: { '#94a3b8': '#f1f5f9', '#64748b': '#e2e8f0', '#475569': '#cbd5e1', '#334155': '#71717a',
                    '#243247': '#52525b', '#1e293b': '#52525b', '#cbd5e1': '#ffffff' },
        oled:     { '#334155': '#27272a', '#243247': '#1c1c1f', '#1e293b': '#1c1c1f' },
        warm:     { '#334155': '#27272a', '#243247': '#1c1c1f', '#1e293b': '#1c1c1f' },
    };
    // where those colours live in a Chart.js 3 options object
    function* colourSlots(options) {
        const scales = (options && options.scales) || {};
        for (const s of Object.values(scales)) {
            if (!s) continue;
            for (const part of ['ticks', 'grid', 'title', 'pointLabels', 'angleLines']) {
                const o = s[part];
                if (!o) continue;
                for (const key of ['color', 'borderColor', 'tickColor']) if (typeof o[key] === 'string') yield [o, key];
            }
        }
        const legend = options && options.plugins && options.plugins.legend && options.plugins.legend.labels;
        if (legend && typeof legend.color === 'string') yield [legend, 'color'];
    }
    function recolourCharts(force) {
        const C = window.Chart;
        if (!C || !C.instances) return;
        const theme = root.dataset.theme;
        for (const chart of Object.values(C.instances)) {
            if (!chart || (!force && chart.$bioTheme === theme)) continue;
            const opts = chart.config && chart.config.options;
            if (!chart.$bioOrig) {                        // remember what the chart was written with, once
                chart.$bioOrig = [];
                for (const [obj, key] of colourSlots(opts)) chart.$bioOrig.push([obj, key, obj[key].toLowerCase()]);
            }
            const map = CHART[theme] || {};
            for (const [obj, key, orig] of chart.$bioOrig) obj[key] = map[orig] || orig;
            chart.$bioTheme = theme;
            try { chart.update('none'); } catch (e) {}
        }
    }

    function apply() {
        const theme = resolve(choice);
        root.dataset.theme = theme;
        root.dataset.themeChoice = choice;
        root.style.colorScheme = theme === 'light' ? 'light' : 'dark';   // native controls (selects, date pickers)
        recolourCharts();
        window.dispatchEvent(new CustomEvent('biodash-theme', { detail: { choice, theme } }));
    }

    function set(c) {
        if (!NAMES[c]) return;
        choice = c;
        // a year; no domain, so it's this host only (every dashboard port shares it)
        document.cookie = `${COOKIE}=${c}; path=/; max-age=31536000; SameSite=Lax`;
        apply();
    }

    apply();
    if (prefersLight) {
        const onChange = () => { if (choice === 'auto') apply(); };
        if (prefersLight.addEventListener) prefersLight.addEventListener('change', onChange);
        else if (prefersLight.addListener) prefersLight.addListener(onChange);
    }
    // charts are made after this runs (and some later still, like tile trends): catch them as they appear
    document.addEventListener('DOMContentLoaded', () => recolourCharts());
    window.addEventListener('load', () => recolourCharts());
    setInterval(() => {
        const c = readChoice();
        if (c !== choice) { choice = c; apply(); }       // changed on another dashboard
        else recolourCharts();
    }, 2000);

    window.BioTheme = { NAMES, set, choice: () => choice, theme: () => root.dataset.theme, recolourCharts };
})();
