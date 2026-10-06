/* 小时雨量与降雨概率独立展示；未确认累计时段时不推断雨强。 */
(function (root) {
    'use strict';
    const isNumber = (value, max) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= max;
    const amountText = value => Number(value.toFixed(2)).toString();
    const grade = amount => amount === 0 ? '未预报到降雨' : amount <= 2.54 ? '弱' : amount <= 7.62 ? '中' : '强';
    const localTime = value => new Date(value + 8 * 3600000).toISOString().slice(0, 16).replace('T', ' ');
    const parseTime = value => {
        if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?$/.test(value)) return NaN;
        const parts = value.slice(0, 19).split(/[-T:]/).map(Number);
        if (parts[1] < 1 || parts[1] > 12 || parts[3] > 23 || parts[4] > 59
            || (parts.length > 5 && parts[5] > 59)
            || new Date(Date.UTC(parts[0], parts[1] - 1, parts[2])).toISOString().slice(0, 10) !== value.slice(0, 10)) return NaN;
        // 供应商未带偏移的字符串按契约中的北京时间解释。
        return Date.parse(/(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : value + '+08:00');
    };

    function summarize(payload, now = Date.now()) {
        const unknown = {window: '统计时段：未知', amount: '时段雨量预报：未知',
            intensity: '小时雨强：未知', probability: '最高降雨概率：未知', source: '数据来源：未知', rows: []};
        const data = payload && payload.data;
        if (!payload || payload.success !== true || !data || data.available !== true || data.is_mock
            || typeof data.source !== 'string' || !data.source.trim()) return unknown;
        unknown.source = '数据来源：' + data.source + ' · 小时级预报';
        if (data.timezone !== 'Asia/Shanghai' || data.precipitation_unit !== 'mm' || data.rainfall_interval_minutes !== 60
            || data.rainfall_time_reference !== 'interval_end'
            || !Array.isArray(data.timeline) || !data.timeline.length || data.timeline.length > 24) return unknown;
        const times = data.timeline.map(item => item && parseTime(item.time));
        if (times.some(time => !Number.isFinite(time))
            || times.some((time, index) => index > 0 && time - times[index - 1] !== 3600000)) return unknown;
        if (times[times.length - 1] <= now) return {...unknown, window: '统计时段：已过期，等待新预报'};
        const result = {...unknown, window: `${localTime(times[0] - 3600000)} — ${localTime(times[times.length - 1])}（北京时间，${times.length} 小时）`};
        const amounts = data.timeline.map(item => item.rainfall_mm);
        const probabilities = data.timeline.map(item => item.precipitation_probability);
        if (amounts.every(value => isNumber(value, 1000))) {
            const peak = Math.max(...amounts);
            result.amount = `时段雨量预报：${amountText(amounts.reduce((sum, value) => sum + value, 0))} mm`;
            result.intensity = `最强小时平均雨强：${grade(peak)}（${amountText(peak)} mm/h）`;
        }
        if (probabilities.every(value => isNumber(value, 100))) {
            result.probability = `最高降雨概率：${Math.round(Math.max(...probabilities))}%（不是雨量）`;
        }
        result.rows = data.timeline.map((item, index) => ({
            window: `${localTime(times[index] - 3600000)} — ${localTime(times[index])}`,
            amount: isNumber(item.rainfall_mm, 1000) ? `${amountText(item.rainfall_mm)} mm` : '未知',
            intensity: isNumber(item.rainfall_mm, 1000) ? grade(item.rainfall_mm) : '未知',
            probability: isNumber(item.precipitation_probability, 100) ? `${Math.round(item.precipitation_probability)}%` : '未知',
        }));
        return result;
    }

    function render(card, summary) {
        ['window', 'amount', 'intensity', 'probability', 'source'].forEach(key => {
            card.querySelector(`[data-rain-${key}]`).textContent = summary[key];
        });
        const rows = card.querySelector('[data-rain-rows]');
        rows.replaceChildren();
        summary.rows.forEach(row => {
            const tr = document.createElement('tr');
            [row.window, row.amount, row.intensity, row.probability].forEach(value => {
                const td = document.createElement('td');
                td.textContent = value;
                tr.appendChild(td);
            });
            rows.appendChild(tr);
        });
        card.querySelector('[data-rain-details]').hidden = !summary.rows.length;
    }

    if (typeof module !== 'undefined' && module.exports) module.exports = {summarize};
    if (typeof document === 'undefined') return;
    root.YilaoRainCard = {summarize};
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('[data-rain-card]').forEach(card => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 12000);
            fetch(card.dataset.nowcastUrl, {signal: controller.signal, credentials: 'same-origin'})
                .then(response => response.ok ? response.json() : Promise.reject())
                .then(payload => render(card, summarize(payload)))
                .catch(() => render(card, summarize(null)))
                .finally(() => clearTimeout(timer));
        });
    });
})(typeof window === 'undefined' ? globalThis : window);
