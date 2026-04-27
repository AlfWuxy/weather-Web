/* ========================================================================
   yilao-data-fx-extra.js
   扩展数据动效运行时
   D thermo-bar / E trend-line / F shap-bar / G sparkline /
   H alert-list / I radar / J thermometer
   ======================================================================== */

(function (global) {
    'use strict';

    function ease(t) { return 1 - Math.pow(1 - t, 3); }
    function animateNumber(el, from, to, opts) {
        opts = opts || {};
        const duration = opts.duration || 900;
        const decimals = opts.decimals || 0;
        const start = performance.now();
        const factor = Math.pow(10, decimals);
        function step(now) {
            const t = Math.min((now - start) / duration, 1);
            const cur = from + (to - from) * ease(t);
            el.textContent = (Math.round(cur * factor) / factor).toFixed(decimals);
            if (t < 1) requestAnimationFrame(step);
        }
        requestAnimationFrame(step);
    }

    // ========== D · 横向温度计条 ==========
    // <div class="fx-thermo" data-fx="thermo-bar" data-temp="24" data-min="-5" data-max="40">
    function tempZone(t) {
        if (t < 10) return 'cold';
        if (t < 20) return 'cool';
        if (t < 28) return 'warm';
        if (t < 35) return 'hot';
        return 'danger';
    }
    function tempZoneName(z) {
        return {cold:'寒冷', cool:'凉爽', warm:'适宜', hot:'炎热', danger:'高温危险'}[z];
    }

    function initThermoBar(el) {
        const min = parseFloat(el.dataset.min || '-5');
        const max = parseFloat(el.dataset.max || '40');
        const t = parseFloat(el.dataset.temp || '20');
        const range = max - min;
        const pct = Math.max(0, Math.min(100, ((t - min) / range) * 100));
        const zone = tempZone(t);

        const marker = el.querySelector('.fx-thermo-marker');
        const valTip = el.querySelector('.fx-thermo-value');
        const tag = el.querySelector('.fx-thermo-zone-tag');

        if (marker) {
            marker.style.left = '0%';
            marker.dataset.zone = zone;
            requestAnimationFrame(() => { marker.style.left = pct + '%'; });
        }
        if (valTip) {
            valTip.style.left = '0%';
            valTip.textContent = t + '°C';
            requestAnimationFrame(() => { valTip.style.left = pct + '%'; });
        }
        if (tag) {
            tag.dataset.zone = zone;
            tag.textContent = tempZoneName(zone);
        }

        el._fxUpdate = function (newTemp) {
            const nt = parseFloat(newTemp);
            const np = Math.max(0, Math.min(100, ((nt - min) / range) * 100));
            const nz = tempZone(nt);
            el.dataset.temp = nt;
            if (marker) { marker.style.left = np + '%'; marker.dataset.zone = nz; }
            if (valTip) { valTip.style.left = np + '%'; valTip.textContent = nt + '°C'; }
            if (tag) { tag.dataset.zone = nz; tag.textContent = tempZoneName(nz); }
        };
    }

    // ========== E · 折线图绘制 ==========
    // <div class="fx-trend" data-fx="trend-line"
    //      data-values='[{"label":"今","score":55,"temp":32,"level":"mid"}, ...]'>
    function initTrend(el) {
        const data = JSON.parse(el.dataset.values || '[]');
        if (!data.length) return;

        const W = el.clientWidth || 600, H = 280;
        const padL = 36, padR = 20, padT = 30, padB = 36;
        const innerW = W - padL - padR, innerH = H - padT - padB;
        const max = 100, min = 0;

        // x positions
        const xs = data.map((_, i) => padL + (data.length === 1 ? innerW/2 : (i / (data.length - 1)) * innerW));
        const ys = data.map(d => padT + innerH - ((d.score - min) / (max - min)) * innerH);

        // path
        let path = `M ${xs[0]} ${ys[0]}`;
        for (let i = 1; i < data.length; i++) {
            const cx = (xs[i-1] + xs[i]) / 2;
            path += ` Q ${cx} ${ys[i-1]} ${xs[i]} ${ys[i]}`;
        }
        // area
        const area = path + ` L ${xs[xs.length-1]} ${padT + innerH} L ${xs[0]} ${padT + innerH} Z`;

        // gridlines
        let grid = '';
        for (let g = 0; g <= 4; g++) {
            const y = padT + (g / 4) * innerH;
            grid += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}"/>`;
        }

        let svg = `<svg viewBox="0 0 ${W} ${H}">`;
        svg += `<g class="fx-trend-grid">${grid}</g>`;
        svg += `<path class="fx-trend-area" d="${area}"/>`;
        svg += `<path class="fx-trend-line" d="${path}"/>`;

        // dots + labels
        data.forEach((d, i) => {
            svg += `<circle class="fx-trend-dot" data-level="${d.level||'mid'}" cx="${xs[i]}" cy="${ys[i]}" r="5"/>`;
            svg += `<text class="fx-trend-value" x="${xs[i]}" y="${ys[i]-12}">${d.score}</text>`;
            svg += `<text class="fx-trend-label" x="${xs[i]}" y="${H - padB + 18}">${d.label}</text>`;
        });
        svg += `</svg>`;

        el.innerHTML = svg;

        const line = el.querySelector('.fx-trend-line');
        const areaEl = el.querySelector('.fx-trend-area');
        const dots = el.querySelectorAll('.fx-trend-dot');
        const vals = el.querySelectorAll('.fx-trend-value');

        // total length for stroke-dashoffset
        if (line && line.getTotalLength) {
            const len = line.getTotalLength();
            line.style.strokeDasharray = len;
            line.style.strokeDashoffset = len;
            requestAnimationFrame(() => {
                line.classList.add('fx-shown');
                line.style.strokeDashoffset = '0';
            });
        }
        setTimeout(() => areaEl && areaEl.classList.add('fx-shown'), 700);
        dots.forEach((d, i) => {
            setTimeout(() => {
                d.classList.add('fx-shown');
                if (vals[i]) vals[i].classList.add('fx-shown');
            }, 1100 + i * 130);
        });
    }

    // ========== F · SHAP 双向贡献条 ==========
    // <div class="fx-shap-list" data-fx="shap-bar"
    //      data-features='[{"name":"最高气温","value":+0.42},{"name":"用药依从","value":-0.18}]'>
    function initShap(el) {
        const features = JSON.parse(el.dataset.features || '[]');
        if (!features.length) return;
        // sort by abs value desc
        features.sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
        const maxAbs = Math.max(...features.map(f => Math.abs(f.value)), 0.01);

        let html = '';
        features.forEach(f => {
            const pct = (Math.abs(f.value) / maxAbs) * 50;  // half width
            const cls = f.value >= 0 ? 'pos' : 'neg';
            const sign = f.value >= 0 ? '+' : '';
            html += `<div class="fx-shap-row">
                <span class="feat">${f.name}</span>
                <div class="fx-shap-bar-wrap">
                    <span class="fx-shap-axis"></span>
                    <span class="fx-shap-bar ${cls}" style="width:${pct}%;"></span>
                </div>
                <span class="val ${cls}">${sign}${f.value.toFixed(2)}</span>
            </div>`;
        });
        el.innerHTML = html;
        const bars = el.querySelectorAll('.fx-shap-bar');
        bars.forEach((b, i) => {
            setTimeout(() => b.classList.add('fx-shown'), 200 + i * 90);
        });
    }

    // ========== G · Sparkline ==========
    // <div class="fx-spark" data-fx="sparkline" data-values="[120,124,...]"
    //      data-band-min="90" data-band-max="140" data-anomaly-idx="[5,12]">
    function initSpark(el) {
        const values = JSON.parse(el.dataset.values || '[]');
        if (values.length < 2) return;
        const bandMin = parseFloat(el.dataset.bandMin || 'NaN');
        const bandMax = parseFloat(el.dataset.bandMax || 'NaN');
        const anomalies = JSON.parse(el.dataset.anomalyIdx || '[]');

        const W = el.clientWidth || 240, H = 70;
        const padX = 4, padY = 6;
        const iw = W - padX * 2, ih = H - padY * 2;
        const dmin = Math.min(...values, isNaN(bandMin) ? Infinity : bandMin);
        const dmax = Math.max(...values, isNaN(bandMax) ? -Infinity : bandMax);
        const range = (dmax - dmin) || 1;

        const xs = values.map((_, i) => padX + (i / (values.length - 1)) * iw);
        const ys = values.map(v => padY + ih - ((v - dmin) / range) * ih);

        let path = `M ${xs[0]} ${ys[0]}`;
        for (let i = 1; i < values.length; i++) {
            const cx = (xs[i-1] + xs[i]) / 2;
            path += ` Q ${cx} ${ys[i-1]} ${xs[i]} ${ys[i]}`;
        }
        const area = path + ` L ${xs[xs.length-1]} ${padY+ih} L ${xs[0]} ${padY+ih} Z`;

        let svg = `<svg viewBox="0 0 ${W} ${H}">`;
        // band
        if (!isNaN(bandMin) && !isNaN(bandMax)) {
            const yTop = padY + ih - ((bandMax - dmin) / range) * ih;
            const yBot = padY + ih - ((bandMin - dmin) / range) * ih;
            svg += `<rect class="fx-spark-band" x="${padX}" y="${yTop}" width="${iw}" height="${yBot-yTop}"/>`;
        }
        svg += `<path class="fx-spark-area" d="${area}"/>`;
        svg += `<path class="fx-spark-line" d="${path}"/>`;
        // anomaly dots
        anomalies.forEach(idx => {
            if (idx >= 0 && idx < values.length) {
                svg += `<circle class="fx-spark-anomaly" cx="${xs[idx]}" cy="${ys[idx]}" r="3.5"/>`;
            }
        });
        svg += `</svg>`;
        el.innerHTML = svg;

        const line = el.querySelector('.fx-spark-line');
        const areaEl = el.querySelector('.fx-spark-area');
        if (line && line.getTotalLength) {
            const len = line.getTotalLength();
            line.style.strokeDasharray = len;
            line.style.strokeDashoffset = len;
            requestAnimationFrame(() => {
                line.classList.add('fx-shown');
                line.style.strokeDashoffset = '0';
            });
        }
        setTimeout(() => areaEl && areaEl.classList.add('fx-shown'), 600);
    }

    // ========== H · Alert List Stagger ==========
    // <div data-fx="alert-list"> ... 子元素自动 stagger
    function initAlertList(el) {
        const items = el.querySelectorAll('.fx-alert-item, [data-alert-item]');
        items.forEach((item, i) => {
            item.style.animationDelay = (i * 60) + 'ms';
        });
    }

    // ========== I · 雷达图绘制 ==========
    // <div class="fx-radar" data-fx="radar"
    //      data-axes='["心血管","代谢","神经","呼吸","精神"]'
    //      data-values="[70,65,80,55,72]"
    //      data-prev="[60,68,72,50,65]">
    function initRadar(el) {
        const axes = JSON.parse(el.dataset.axes || '[]');
        const values = JSON.parse(el.dataset.values || '[]');
        const prev = JSON.parse(el.dataset.prev || '[]');
        if (!axes.length || axes.length !== values.length) return;

        const W = 360, H = 320, cx = W/2, cy = H/2 - 10, R = 110;
        const N = axes.length;
        function pt(i, r) {
            const a = -Math.PI/2 + (i/N) * Math.PI * 2;
            return [cx + Math.cos(a) * r, cy + Math.sin(a) * r];
        }

        let svg = `<svg viewBox="0 0 ${W} ${H}">`;

        // grid rings
        for (let g = 1; g <= 4; g++) {
            const r = (R/4) * g;
            let pts = '';
            for (let i = 0; i < N; i++) {
                const [x,y] = pt(i, r);
                pts += (i ? ' L ' : 'M ') + x + ' ' + y;
            }
            pts += ' Z';
            svg += `<path class="fx-radar-grid" d="${pts}"/>`;
        }
        // axis lines + labels
        for (let i = 0; i < N; i++) {
            const [x,y] = pt(i, R);
            const [lx,ly] = pt(i, R + 22);
            svg += `<line class="fx-radar-axis" x1="${cx}" y1="${cy}" x2="${x}" y2="${y}"/>`;
            svg += `<text class="fx-radar-label" x="${lx}" y="${ly+4}">${axes[i]}</text>`;
        }
        // prev shape
        if (prev.length === N) {
            let pts = '';
            for (let i = 0; i < N; i++) {
                const [x,y] = pt(i, (prev[i]/100) * R);
                pts += (i ? ' L ' : 'M ') + x + ' ' + y;
            }
            pts += ' Z';
            svg += `<path class="fx-radar-prev" d="${pts}"/>`;
        }
        // current shape
        let curPts = '';
        const vertexCoords = [];
        for (let i = 0; i < N; i++) {
            const [x,y] = pt(i, (values[i]/100) * R);
            vertexCoords.push([x,y]);
            curPts += (i ? ' L ' : 'M ') + x + ' ' + y;
        }
        curPts += ' Z';
        svg += `<path class="fx-radar-shape" d="${curPts}"/>`;
        vertexCoords.forEach(([x,y]) => {
            svg += `<circle class="fx-radar-vertex" cx="${x}" cy="${y}" r="3.5"/>`;
        });
        svg += `</svg>`;
        el.innerHTML = svg;

        const prevEl = el.querySelector('.fx-radar-prev');
        const shape = el.querySelector('.fx-radar-shape');
        const verts = el.querySelectorAll('.fx-radar-vertex');
        setTimeout(() => prevEl && prevEl.classList.add('fx-shown'), 200);
        setTimeout(() => shape && shape.classList.add('fx-shown'), 600);
        verts.forEach((v, i) => setTimeout(() => v.classList.add('fx-shown'), 1400 + i*80));
    }

    // ========== J · 温度计水银柱 ==========
    // <div class="fx-thermometer" data-fx="thermometer" data-temp="24" data-min="-5" data-max="45">
    function initThermometer(el) {
        const min = parseFloat(el.dataset.min || '-5');
        const max = parseFloat(el.dataset.max || '45');
        const t = parseFloat(el.dataset.temp || '20');
        const pct = Math.max(0, Math.min(100, ((t - min) / (max - min)) * 100));
        const zone = tempZone(t);

        const tube = el.querySelector('.fx-thermometer-tube');
        const merc = el.querySelector('.fx-thermometer-mercury');
        const bulb = el.querySelector('.fx-thermometer-bulb');
        const valEl = el.querySelector('.fx-thermometer-info .v .num');
        // 用像素高度避免 absolute 元素在部分浏览器里百分比高度失效。
        const tubeH = (tube && tube.offsetHeight) || 140;

        if (merc) {
            merc.style.height = '0px';
            merc.dataset.zone = zone;
            requestAnimationFrame(() => { merc.style.height = (pct / 100 * tubeH) + 'px'; });
        }
        if (bulb) bulb.dataset.zone = zone;
        if (valEl) animateNumber(valEl, 0, t, { duration: 1500, decimals: 1 });

        if (zone === 'danger') el.classList.add('danger-active');
        else el.classList.remove('danger-active');

        el._fxUpdate = function (newT) {
            const nt = parseFloat(newT);
            const np = Math.max(0, Math.min(100, ((nt - min) / (max - min)) * 100));
            const nz = tempZone(nt);
            const h = (tube && tube.offsetHeight) || tubeH;
            el.dataset.temp = nt;
            if (merc) { merc.style.height = (np / 100 * h) + 'px'; merc.dataset.zone = nz; }
            if (bulb) bulb.dataset.zone = nz;
            if (valEl) {
                const cur = parseFloat(valEl.textContent) || 0;
                animateNumber(valEl, cur, nt, { duration: 1100, decimals: 1 });
            }
            if (nz === 'danger') el.classList.add('danger-active');
            else el.classList.remove('danger-active');
        };
    }

    // ========== 自动初始化 ==========
    function bootstrap(el) {
        if (el._fxxInit) return;
        el._fxxInit = true;
        const kind = el.dataset.fx;
        try {
            if (kind === 'thermo-bar')   initThermoBar(el);
            else if (kind === 'trend-line') initTrend(el);
            else if (kind === 'shap-bar')   initShap(el);
            else if (kind === 'sparkline')  initSpark(el);
            else if (kind === 'alert-list') initAlertList(el);
            else if (kind === 'radar')      initRadar(el);
            else if (kind === 'thermometer') initThermometer(el);
        } catch (e) { console.warn('[fx-extra] init failed', el, e); }
    }

    function initAll(root) {
        root = root || document;
        const els = root.querySelectorAll(
            '[data-fx="thermo-bar"], [data-fx="trend-line"], [data-fx="shap-bar"], [data-fx="sparkline"], [data-fx="alert-list"], [data-fx="radar"], [data-fx="thermometer"]'
        );
        if ('IntersectionObserver' in window) {
            const io = new IntersectionObserver((es) => {
                es.forEach(en => {
                    if (en.isIntersecting) { bootstrap(en.target); io.unobserve(en.target); }
                });
            }, { threshold: 0.1 });
            els.forEach(el => io.observe(el));
        } else {
            els.forEach(bootstrap);
        }
    }

    // 公开 API:扩展 YilaoFx
    if (!global.YilaoFx) global.YilaoFx = {};
    const prevUpdate = global.YilaoFx.update;
    global.YilaoFx.update = function (sel, val, opts) {
        const el = typeof sel === 'string' ? document.querySelector(sel) : sel;
        if (el && el._fxUpdate) { el._fxUpdate(val, opts); return; }
        if (prevUpdate) prevUpdate(sel, val, opts);
    };
    global.YilaoFx.initExtra = initAll;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => initAll());
    } else {
        initAll();
    }
})(window);
