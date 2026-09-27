'use strict';

// 在最小 DOM 中执行真实工作台脚本，检验用户能看到的风险状态而非源码片段。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const script = fs.readFileSync(path.join(root, 'static/js/heat-risk-workbench.js'), 'utf8');
const geojson = JSON.parse(fs.readFileSync(path.join(root, 'static/data/gis/duchang_heat_exposure_cells.geojson'), 'utf8'));
const workbench = JSON.parse(fs.readFileSync(path.join(root, 'static/data/gis/duchang_heat_risk_workbench.json'), 'utf8'));
const unknown = '风险暂不可判定';

class Element {
    constructor(tag = 'div') {
        this.tagName = tag;
        this.children = [];
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this.listeners = {};
        this.className = '';
        this.value = '';
        this._text = '';
        this.classList = {
            contains: (name) => this.className.split(' ').includes(name),
            toggle: (name, force) => {
                const enabled = force === undefined ? !this.classList.contains(name) : force;
                const classes = new Set(this.className.split(' ').filter(Boolean));
                enabled ? classes.add(name) : classes.delete(name);
                this.className = [...classes].join(' ');
                return enabled;
            },
            add: (name) => this.classList.toggle(name, true),
            remove: (name) => this.classList.toggle(name, false)
        };
    }
    get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
    set textContent(value) { this._text = String(value); this.children = []; }
    append(...nodes) { this.children.push(...nodes); }
    appendChild(node) { this.append(node); return node; }
    replaceChildren(...nodes) { this._text = ''; this.children = [...nodes]; }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    querySelectorAll(selector) {
        const result = [];
        this.children.forEach((child) => {
            if (selector.startsWith('.') && child.classList.contains(selector.slice(1))) result.push(child);
            result.push(...child.querySelectorAll(selector));
        });
        return result;
    }
    cloneNode(deep) {
        const clone = new Element(this.tagName);
        clone._text = this._text;
        clone.className = this.className;
        if (deep) clone.children = this.children.map((child) => child.cloneNode(true));
        return clone;
    }
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

async function page(search = '') {
    const elements = new Map();
    const getElement = (id) => {
        if (!elements.has(id)) elements.set(id, new Element());
        return elements.get(id);
    };
    const buttons = ['daily', 'score', 'bivariate', 'q3_lst_c_mean', 'age65_share_pct'].map((layer) => {
        const button = new Element('button');
        button.dataset.layer = layer;
        return button;
    });
    getElement('heatRiskWorkbench').dataset = {geojsonUrl: 'geojson', workbenchUrl: 'workbench', dailyUrl: 'daily'};
    const document = {
        getElementById: getElement,
        createElement: (tag) => new Element(tag),
        createTextNode: (text) => { const node = new Element('#text'); node.textContent = text; return node; },
        querySelectorAll: (selector) => selector.includes('#hrwLayerTabs') ? buttons : [],
        addEventListener() {}
    };
    let now = Date.parse('2026-09-27T04:00:00Z');
    class TestDate extends Date {
        constructor(...args) { super(...(args.length ? args : [now])); }
        static now() { return now; }
    }
    const requests = [];
    const errors = [];
    const window = {
        location: {search, href: `https://weather.example/heat-exposure-gis${search}`},
        history: {replaceState() {}},
        matchMedia: () => ({matches: false}),
        addEventListener() {}, setInterval() {}, setTimeout() {}, print() {}
    };
    const context = vm.createContext({
        document, window, URL, URLSearchParams, Intl, Date: TestDate,
        console: {error: (...args) => errors.push(args)},
        fetch: (url) => {
            if (url === 'geojson') return Promise.resolve({ok: true, json: async () => geojson});
            if (url === 'workbench') return Promise.resolve({ok: true, json: async () => workbench});
            return new Promise((resolve, reject) => requests.push({resolve, reject}));
        }
    });
    // 仅测试运行时暴露闭包；业务文件不添加测试开关，也不重写风险算法。
    const exposed = script.replace('    Promise.all([', '    globalThis.hrw = {state, ui, applyDaily, currentHazard, setDay, setLayer, printSheet, renderVillages, fillFor, tooltipFor, refreshAfterMidnight, combineDailyLevel};\n    Promise.all([');
    vm.runInContext(exposed, context, {filename: 'heat-risk-workbench.js'});
    await flush();
    assert.deepEqual(errors, [], '脚本初始化不应异常');
    assert.equal(requests.length, 1, '应完成静态数据初始化并请求逐日预报');
    const api = context.hrw;
    api.state.selected = api.state.cells.findIndex((cell) => cell.scored);
    return {...api, requests, window, advance: (value) => { now = Date.parse(value); }};
}

function payload(options = {}) {
    const day = {
        date: '2026-09-27', level: 0, label: '较低', temperature_max: 30, temperature_min: 22,
        escalated: false, hot_night: false, reasons: [], ...options.day
    };
    const village = {
        name: '测试村', township: '测试乡', lon_wgs84: 116.2, lat_wgs84: 29.3,
        static_level: 3, static_score: 65, daily_level: day.level === 0 ? 0 : Math.min(day.level + 1, 4), reasons: []
    };
    return {
        forecast_status: 'ok', forecast_source: '和风天气 7 天预报', forecast_notice: '', hot_night_tmin_c: 26,
        days: [day], priority: [{date: day.date, villages: [village]}], villages: [village],
        cooling_resources: [{name: '测试避暑点', lon_wgs84: 116.2, lat_wgs84: 29.3}],
        action_cards: workbench.metadata.action_cards, ...options.payload
    };
}

function assertUnknown(h) {
    assert.equal(h.currentHazard(), null);
    assert.equal(h.ui.title.textContent, unknown);
    assert.equal(h.ui.dailyChip.textContent, unknown);
    assert.match(h.ui.actionCard.textContent, /风险暂不可判定/);
    assert.doesNotMatch(h.ui.actionCard.textContent, /常规随访|0 级/);
    assert.doesNotMatch(h.ui.lede.textContent, /无高温|常规随访/);
    assert.equal(h.state.layer, 'score');
    assert.equal(h.ui.layerTabs[0].disabled, true);
    const cell = h.state.cells[h.state.selected];
    assert.equal(h.fillFor(cell, 'daily').nodata, true);
    assert.match(h.tooltipFor(cell, 'daily').textContent, /风险暂不可判定/);
    h.printSheet();
    assert.match(h.ui.printSheet.textContent, /风险暂不可判定/);
    assert.doesNotMatch(h.ui.printSheet.textContent, /常规随访|热危险 0 级|无高温/);
}

function addMarkerLayer(h) {
    const layer = () => ({items: [], clearLayers() { this.items = []; }, addLayer(item) { this.items.push(item); }});
    const marker = (position, options) => ({position, options, bindTooltip(text) { this.tooltip = text; return this; }, on() { return this; }});
    h.window.L = {circleMarker: marker, marker, divIcon: (options) => options};
    h.state.map = {};
    h.state.layers.villages = layer();
    h.state.layers.cooling = layer();
}

test('首屏等待预报时不会闪出 0 级或常规随访行动卡', async () => {
    const h = await page('?layer=daily');
    assert.equal(h.currentHazard(), null);
    assert.equal(h.state.layer, 'score');
    assert.match(h.ui.actionCard.textContent, /风险暂不可判定/);
    assert.doesNotMatch(h.ui.actionCard.textContent, /0 级|常规随访/);
});

test('空预报保留村点与避暑点，同时清理全部天气判断和打印', async () => {
    const h = await page();
    addMarkerLayer(h);
    const data = payload({payload: {forecast_status: 'unavailable', forecast_notice: '天气源全部不可用', days: [], priority: []}});
    h.applyDaily(data);
    assertUnknown(h);
    assert.match(h.ui.forecastSource.textContent, /天气源全部不可用/);
    assert.equal(h.state.layers.villages.items.length, 2);
    assert.equal(h.state.layers.cooling.items.length, 1);
    assert.match(h.state.layers.villages.items[0].tooltip, /风险暂不可判定/);
    assert.doesNotMatch(h.state.layers.villages.items[0].tooltip, /当日 0 级/);
    assert.ok(h.ui.searchOptions.children.some((option) => option.value === '测试村'));
});

for (const [name, day] of [
    ['最高温缺失', {temperature_max: null}], ['最低温缺失', {temperature_min: undefined}],
    ['等级缺失', {level: undefined}], ['等级为文本', {level: '0'}], ['等级越界', {level: 5}],
    ['温度范围倒置', {temperature_min: 40}], ['日期过期', {date: '2026-09-26'}]
]) {
    test(`${name}不显示低风险`, async () => {
        const h = await page();
        h.applyDaily(payload({day}));
        assertUnknown(h);
        assert.match(h.ui.days.textContent, /风险暂不可判定/);
    });
}

test('请求失败后撤下旧结论，并保留已加载静态资源', async () => {
    const h = await page();
    h.applyDaily(payload());
    h.requests[0].reject(new Error('offline'));
    await flush();
    assertUnknown(h);
    assert.equal(h.state.daily.villages.length, 1);
    assert.equal(h.state.daily.cooling_resources.length, 1);
});

test('真实 0 级保持无高温与常规随访，旧版有效 payload 仍兼容', async () => {
    const h = await page();
    const data = payload();
    delete data.forecast_status;
    h.applyDaily(data);
    assert.equal(h.currentHazard(), 0);
    assert.match(h.ui.dailyChip.textContent, /0 级/);
    assert.match(h.ui.actionCard.textContent, /常规随访/);
    assert.match(h.ui.lede.textContent, /无高温/);
    assert.equal(h.ui.layerTabs[0].disabled, false);
    h.setLayer('daily');
    assert.equal(h.state.layer, 'daily');
});

test('备用预报和演示数据在页面与打印中明确标记', async () => {
    const h = await page();
    h.applyDaily(payload({payload: {forecast_status: 'fallback', forecast_source: 'Open-Meteo（备用预报）', forecast_notice: '主预报暂不可用，已启用真实备用预报。'}}));
    assert.match(h.ui.forecastSource.textContent, /备用预报.*Open-Meteo.*主预报暂不可用/);
    h.printSheet();
    assert.match(h.ui.printSheet.textContent, /Open-Meteo.*主预报暂不可用/);
    h.applyDaily(payload({payload: {forecast_status: 'demo', forecast_notice: '此处为演示预报'}}));
    assert.match(h.ui.forecastSource.textContent, /演示数据，仅供演示/);
    for (const node of [h.ui.title, h.ui.lede, h.ui.dailyChip, h.ui.days, h.ui.actionCard]) assert.match(node.textContent, /演示/);
    h.printSheet();
    assert.match(h.ui.printSheet.textContent, /演示数据，仅供演示/);
});

test('首项是未来日期时不能称今天，切换到缺损日期撤下结论', async () => {
    const h = await page();
    const data = payload({day: {date: '2026-09-28'}});
    data.days.push({...data.days[0], date: '2026-09-29', level: null});
    h.applyDaily(data);
    assert.doesNotMatch(h.ui.days.children[0].textContent, /今天/);
    assert.match(h.ui.metaDay.textContent, /9\/28/);
    assert.doesNotMatch(h.ui.title.textContent, /今天/);
    h.setLayer('daily');
    h.setDay(1);
    assertUnknown(h);
});

test('跨午夜先撤下昨天判断再刷新，刷新前打印也不会带出旧行动卡', async () => {
    const h = await page();
    h.applyDaily(payload());
    h.advance('2026-09-27T16:01:00Z');
    h.printSheet();
    assert.doesNotMatch(h.ui.printSheet.textContent, /常规随访|热危险 0 级/);
    h.refreshAfterMidnight();
    assert.equal(h.requests.length, 2);
    assertUnknown(h);
    assert.doesNotMatch(h.ui.days.textContent, /今天/);
    h.requests[1].resolve({ok: true, json: async () => payload({day: {date: '2026-09-28'}})});
    await flush();
    assert.equal(h.currentHazard(), 0);
    assert.match(h.ui.metaDay.textContent, /今天 9\/28/);
    assert.match(h.ui.actionCard.textContent, /常规随访/);
});

test('null 日期项和错误来源状态按不可判定处理', async () => {
    const h = await page();
    h.applyDaily(payload({payload: {days: [null]}}));
    assertUnknown(h);
    h.applyDaily(payload({payload: {forecast_status: 'unavailable'}}));
    assertUnknown(h);
});

test('正常 0–4 级组合矩阵保持不变', async () => {
    const h = await page();
    const expected = [[0, 0, 0, 0, 0], [1, 1, 1, 2, 2], [1, 1, 2, 3, 3], [2, 2, 3, 4, 4], [3, 3, 4, 4, 4]];
    expected.forEach((row, hazard) => row.forEach((level, staticLevel) => assert.equal(h.combineDailyLevel(hazard, staticLevel), level)));
});
