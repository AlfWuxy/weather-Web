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
    scrollIntoView() {}
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
    const exposed = script.replace('    Promise.all([', '    globalThis.hrw = {state, ui, applyDaily, currentHazard, setDay, setLayer, printSheet, renderVillages, fillFor, tooltipFor, refreshAfterMidnight, combineDailyLevel, selectPoi, runSearch, setTownFilter, priorityForDay, renderPoiLabels, safeSourceUrl, referenceName, selectCell};\n    Promise.all([');
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
        id: 'test:village', name: '测试村', township: '测试乡', lon_wgs84: 116.2, lat_wgs84: 29.3,
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
    const marker = (position, options) => ({position, options, bindTooltip(text) { this.tooltip = text; return this; }, on() { return this; }, setStyle(style) { Object.assign(this.options, style); return this; }});
    h.window.L = {circleMarker: marker, marker, divIcon: (options) => options, polygon: () => ({addTo() { return this; }, remove() { this.removed = true; }})};
    h.state.map = {getZoom: () => 10, getBounds: () => ({contains: () => true}), latLngToContainerPoint: (position) => ({x: position[1] * 10000, y: position[0] * 10000})};
    for (const kind of ['villages', 'medical', 'candidates', 'cooling', 'poiLabels']) h.state.layers[kind] = layer();
    h.state.poiRenderer = {sharedCanvas: true};
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
    assert.equal(h.state.layers.villages.items.length, 1);
    assert.equal(h.state.layers.cooling.items.length, 1);
    assert.match(h.state.layers.villages.items[0].tooltip(), /风险暂不可判定/);
    assert.doesNotMatch(h.state.layers.villages.items[0].tooltip(), /当日 0 级/);
    assert.ok(h.ui.searchOptions.children.some((option) => option.value.startsWith('测试村 ·') && option.dataset.poiId === 'test:village'));
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


function poiPayload() {
    const data = payload();
    const towns = workbench.townships.features.slice(0, 2).map((town) => town.properties.name_zh);
    const scored = workbench.cells.scored.findIndex(Boolean);
    const village = {...data.villages[0], name: '同名村', township: towns[0], cell_id: workbench.cells.cell_id[scored],
        population: null, elderly_ratio: null, source_url: 'https://www.geonames.org/123', source_label: 'GeoNames',
        coordinate_precision: 'approximate', settlement_level: 'unknown', verification_status: 'unverified',
        source_updated_at: '2021-06-01', potential_duplicate: false};
    data.villages = [village, {...village, id: 'test:second', township: towns[1], lon_wgs84: 116.25}];
    data.priority[0].villages = [{...village, daily_level: 0, reasons: []}];
    data.priority[0].village_ids = ['test:village', 'test:second'];
    data.medical_pois = [{...village, id: 'medical:one', name: '真实来源医疗机构', source_url: 'https://www.openstreetmap.org/node/123'}];
    data.cooling_candidates = [{...village, id: 'candidate:one', name: '候选场所', cooling_status: 'candidate', open_hours: '08:00–18:00', has_ac: null, is_accessible: null}];
    data.cooling_resources = [];
    data.unmapped_resources = [{kind: 'medical', name: '官方列名医院', township_name: towns[1], address: '来源列名地址', source_url: 'https://example.org/directory', source_date: '2024-01-01', status: 'unmapped'}];
    data.poi_metadata = {coverage_note: '收录数量，不是完整覆盖率；零记录不表示无机构。', retrieved_at: '2026-09-27', sources: [{name: 'GeoNames', url: 'https://www.geonames.org/', license: 'CC BY 4.0'}]};
    return data;
}

test('24乡镇只比较收录数，待定位机构不上图、不混入医疗计数', async () => {
    const h = await page();
    h.applyDaily(poiPayload());
    assert.equal(h.ui.poiCoverageBody.children.length, 24);
    assert.match(h.ui.poiCoverageNote.textContent, /不是完整覆盖率/);
    assert.equal(h.state.poiById.size, 4);
    assert.match(h.ui.unmappedList.textContent, /官方列名医院.*坐标待核验，不上图、不计算距离/);
    assert.match(h.ui.poiCounts.textContent, /0坐标已核验避暑资源 · 暂未收录/);
    assert.match(h.ui.poiSources.textContent, /不等于现场核验日期/);
});

test('同名跨乡点位以稳定ID选择；未知人口不显示成0', async () => {
    const h = await page();
    const data = poiPayload();
    data.villages.forEach((point) => { point.potential_duplicate = true; });
    h.applyDaily(data);
    h.ui.search.value = '同名村';
    h.runSearch();
    assert.equal(h.state.selectedPoiId, null);
    assert.match(h.ui.searchNote.textContent, /2 个同名或近似点/);
    const option = h.ui.searchOptions.children.find((item) => item.dataset.poiId === 'test:second');
    h.ui.search.value = option.value;
    h.runSearch();
    assert.equal(h.state.selectedPoiId, 'test:second');
    assert.match(h.ui.poiDetails.textContent, /人口待补充（未知）/);
    assert.match(h.ui.poiDetails.textContent, /行政村 \/ 自然村级别待核验/);
    assert.match(h.ui.poiDetails.textContent, /疑似同名点，暂不参与巡访排序，待核验/);
    assert.match(h.ui.poiDetails.textContent, /来源更新时间2021-06-01/);
    assert.doesNotMatch(h.ui.poiDetails.textContent, /人口0 人/);
});

test('按乡镇筛选全量排序ID，能选出县级top5之外的村点', async () => {
    const h = await page();
    const data = poiPayload();
    h.applyDaily(data);
    h.setTownFilter(data.villages[1].township, false);
    const ranked = h.priorityForDay();
    assert.equal(ranked.length, 1);
    assert.equal(ranked[0].id, 'test:second');
    assert.equal(h.ui.poiList.children.length, 1);
    assert.match(h.ui.priorityList.textContent, /同名村/);
});

test('候选场所即使有来源营业时间，也不可列为已开放避暑点', async () => {
    const h = await page();
    h.applyDaily(poiPayload());
    h.selectPoi('candidate:one', {pan: false});
    assert.match(h.ui.poiDetails.textContent, /开放情况待核实/);
    assert.match(h.ui.poiDetails.textContent, /尚不能作为已开放避暑点/);
    assert.match(h.ui.poiDetails.textContent, /空调待核实/);
    assert.doesNotMatch(h.ui.poiDetails.textContent, /来源记录无空调/);
    assert.equal(h.state.daily.cooling_resources.length, 0);
});

test('村点无网格数据时不伪造0级，实际机构无网格时清除旧分数', async () => {
    const h = await page();
    const data = poiPayload();
    data.villages[1] = {...data.villages[1], cell_id: null, static_level: null, static_score: null, risk_data_status: 'no_grid_data'};
    data.medical_pois[0].cell_id = null;
    h.applyDaily(data);
    h.selectPoi('test:second', {pan: false});
    assert.match(h.ui.poiDetails.textContent, /网格数据缺失，仅供县级天气参考/);
    assert.equal(h.ui.dailyChip.textContent, unknown);
    assert.equal(h.ui.score.textContent, '—');
    h.selectPoi('medical:one', {pan: false});
    assert.equal(h.ui.score.textContent, '—');
});

test('点位来源只接受无凭据HTTP(S)链接', async () => {
    const h = await page();
    assert.equal(h.safeSourceUrl('javascript:alert(1)'), null);
    assert.equal(h.safeSourceUrl('https://user:pass@example.org/'), null);
    assert.equal(h.safeSourceUrl('https://www.geonames.org/123'), 'https://www.geonames.org/123');
    const data = poiPayload();
    data.villages[0].source_url = 'javascript:alert(1)';
    h.applyDaily(data);
    h.selectPoi('test:village', {pan: false});
    assert.match(h.ui.poiDetails.textContent, /链接待补充/);
});

test('大量聚落复用canvas，低缩放无常驻标签，高缩放标签不超过40', async () => {
    const h = await page();
    const data = poiPayload();
    data.villages = Array.from({length: 700}, (_, i) => ({...data.villages[0], id: `many:${i}`, name: `聚落${i}`, lon_wgs84: 116 + i / 1000, lat_wgs84: 29 + i / 2000}));
    data.priority = [];
    addMarkerLayer(h);
    h.applyDaily(data);
    assert.equal(h.state.layers.villages.items.length, 700);
    assert.equal(h.state.layers.poiLabels.items.length, 0);
    assert.ok(h.state.layers.villages.items.every((marker) => marker.options.renderer === h.state.poiRenderer));
    h.state.map.getZoom = () => 14;
    h.renderPoiLabels();
    assert.ok(h.state.layers.poiLabels.items.length > 0);
    assert.ok(h.state.layers.poiLabels.items.length <= 40);
    assert.ok(h.state.layers.candidates.items[0].options.dashArray);
});

test('旧乡镇驻地改名为参考点，不能继续冒充卫生院', async () => {
    const h = await page();
    const old = workbench.facilities.find((point) => point.precision !== 'exact');
    const name = h.referenceName(old);
    assert.match(name, /驻地参考点/);
    assert.doesNotMatch(name, /医院|卫生院|医疗机构/);
});


test('恶意点名只渲染为文字，地图tooltip与局部标签转义HTML', async () => {
    const h = await page();
    const data = poiPayload();
    const name = '<img src=x onerror="globalThis.__unsafe=1">恶意名称';
    data.villages[0].name = name;
    data.priority = [];
    addMarkerLayer(h);
    h.applyDaily(data);
    const tooltip = h.state.poiMarkers.get('test:village').tooltip();
    assert.match(tooltip, /&lt;img/);
    assert.doesNotMatch(tooltip, /<img/);
    h.state.map.getZoom = () => 14;
    h.renderPoiLabels();
    const html = h.state.layers.poiLabels.items.map((marker) => marker.options.icon.html).join('');
    assert.match(html, /&lt;img/);
    assert.doesNotMatch(html, /<img/);
    h.state.map = null;
    h.selectPoi('test:village', {pan: false});
    assert.ok(h.ui.poiDetails.textContent.includes(name));
    assert.equal(h.ui.poiDetails.children[1].tagName, 'h2');
    assert.equal(h.ui.poiDetails.children[1].children.length, 0);
});

test('同ID刷新后重取网格，无网格、删除或移出筛选都清除旧评分和选择框', async () => {
    const h = await page();
    const data = poiPayload();
    data.priority = [];
    addMarkerLayer(h);
    h.applyDaily(data);
    h.selectPoi('test:village', {pan: false});
    assert.notEqual(h.ui.score.textContent, '—');
    const originalSelection = h.state.layers.selection;
    const otherCell = h.state.cells.find((cell) => cell.scored && cell.id !== data.villages[0].cell_id);
    data.villages[0] = {...data.villages[0], cell_id: otherCell.id};
    h.applyDaily(data);
    assert.equal(h.state.selected, h.state.cellById.get(otherCell.id));
    assert.equal(originalSelection.removed, true);
    data.villages[0] = {...data.villages[0], cell_id: null, static_level: null, risk_data_status: 'no_grid_data'};
    h.applyDaily(data);
    assert.equal(h.state.selected, -1);
    assert.equal(h.ui.score.textContent, '—');
    assert.equal(h.state.layers.selection.removed, true);
    data.villages = data.villages.filter((point) => point.id !== 'test:village');
    h.applyDaily(data);
    assert.equal(h.state.selectedPoiId, null);
    assert.equal(h.state.selected, -1);
    h.selectPoi('test:second', {pan: false});
    h.setTownFilter('不存在此乡', false);
    assert.equal(h.state.selectedPoiId, null);
    assert.equal(h.state.selected, -1);
    assert.equal(h.ui.score.textContent, '—');
});

test('从点位改选网格后清除旧点的橙色高亮', async () => {
    const h = await page();
    const data = poiPayload();
    data.priority = [];
    addMarkerLayer(h);
    h.applyDaily(data);
    h.selectPoi('test:village', {pan: false});
    const marker = h.state.poiMarkers.get('test:village');
    assert.equal(marker.options.radius, 8);
    h.selectCell(h.state.selected);
    assert.equal(h.state.selectedPoiId, null);
    assert.equal(marker.options.radius, 4.5);
});

test('历史地址日期和来源独立于近期列名资料，明确现址待核验', async () => {
    const h = await page();
    const data = poiPayload();
    Object.assign(data.unmapped_resources[0], {source_date: '2026-06-18', address_source_date: '2022-12-16',
        address_source_url: 'https://example.org/old-address', address_status: 'historical_address_unverified_current'});
    h.applyDaily(data);
    assert.match(h.ui.unmappedList.textContent, /历史地址：来源列名地址；现址待核验/);
    assert.match(h.ui.unmappedList.textContent, /地址资料日期：2022-12-16/);
    assert.match(h.ui.unmappedList.textContent, /列名资料日期：2026-06-18/);
    assert.ok(h.ui.unmappedList.children[0].children.some((node) => node.href === 'https://example.org/old-address'));
});

test('仅待定位目录存在无乡记录时也提供乡镇待核验筛选', async () => {
    const h = await page();
    const data = poiPayload();
    data.unmapped_resources[0].township_name = null;
    h.applyDaily(data);
    assert.ok(h.ui.townFilter.children.some((option) => option.value === '__unknown__'));
    h.setTownFilter('__unknown__', false);
    assert.match(h.ui.unmappedSummary.textContent, /（1）/);
    assert.match(h.ui.unmappedList.textContent, /官方列名医院/);
    assert.match(h.ui.poiListTitle.textContent, /（0）/);
});

test('坐标核验不代表开放，跨午夜断网后过期资源降为候选', async () => {
    const h = await page();
    const data = poiPayload();
    data.cooling_resources = [{...data.cooling_candidates[0], id: 'cooling:verified', name: '坐标核验资源',
        cooling_status: 'verified', verification_status: 'verified', valid_until: '2026-09-27'}];
    h.applyDaily(data);
    h.selectPoi('cooling:verified', {pan: false});
    assert.match(h.ui.poiDetails.textContent, /坐标已核验避暑资源/);
    assert.match(h.ui.poiDetails.textContent, /来源记录时段：08:00–18:00；当前开放情况待核实/);
    h.advance('2026-09-27T16:05:00Z');
    h.refreshAfterMidnight();
    assert.equal(h.state.poiById.get('cooling:verified').kind, 'candidates');
    h.applyDaily(null);
    assert.equal(h.state.poiById.get('cooling:verified').kind, 'candidates');
    assert.match(h.ui.poiDetails.textContent, /核验已过期，已降为候选/);
    assert.match(h.ui.poiCounts.textContent, /0坐标已核验避暑资源 · 暂未收录/);
});


test('疑似同名或明确不具备条件的点保留地图，但不能通过排序fallback重入', async () => {
    const h = await page();
    const data = poiPayload();
    data.villages[0].potential_duplicate = true;
    data.villages[1].priority_eligible = false;
    data.villages[1].priority_exclusion_reason = '网格数据缺失';
    h.applyDaily(data);
    assert.equal(h.state.poiById.has('test:village'), true);
    assert.equal(h.priorityForDay().length, 0);
    delete data.priority[0].village_ids;
    h.applyDaily(data);
    assert.equal(h.priorityForDay().length, 0);
    h.selectPoi('test:village', {pan: false});
    assert.match(h.ui.poiDetails.textContent, /疑似同名点，暂不参与巡访排序，待核验/);
    h.selectPoi('test:second', {pan: false});
    assert.match(h.ui.poiDetails.textContent, /巡访排序网格数据缺失/);
});

function descendantNodes(node) {
    return [node, ...node.children.flatMap(descendantNodes)];
}

function historicalCoolingEvidence() {
    return {
        audience_hint: '户外劳动者，服务范围待确认',
        facilities_hint: '历史报道列有风扇、饮水机；未提供空调证据',
        source_date: '2024-08-14', official_source_date: '2025-07-18',
        official_source_url: 'https://example.org/official-cooling',
        coordinate_source_url: 'https://ditu.amap.com/place/TEST',
        coordinate_checked_at: '2026-09-27', source_label: 'amap-public',
        source_url: 'https://example.org/official-cooling',
        verification_note: '劳动者服务点，公众进入条件待核实',
        notes: ['历史设施不代表当前运行', '劳动者服务点，公众进入条件待核实'],
        current_opening_status: 'unknown', has_ac: null, is_accessible: null
    };
}

test('候选详情分别呈现劳动者服务对象、历史风扇证据和当前设施未知', async () => {
    const h = await page();
    const data = poiPayload();
    Object.assign(data.cooling_candidates[0], historicalCoolingEvidence());
    h.applyDaily(data);
    h.selectPoi('candidate:one', {pan: false});
    const text = h.ui.poiDetails.textContent;
    assert.match(text, /服务对象（历史资料）户外劳动者.*当前接待对象及进入条件待核实/);
    assert.match(text, /历史设施（不代表当前可用）历史报道列有风扇、饮水机；未提供空调证据/);
    assert.match(text, /服务\/设施资料日期2025-07-18/);
    assert.match(text, /坐标来源查询日期2026-09-27（资料查询，非现场核验）/);
    assert.match(text, /空调待核实/);
    assert.match(text, /当前开放情况待核实/);
    assert.match(text, /尚不能作为已开放避暑点/);
    assert.match(text, /资料补充说明历史设施不代表当前运行/);
    assert.equal(text.split('劳动者服务点，公众进入条件待核实').length - 1, 1);
    const links = descendantNodes(h.ui.poiDetails).filter((node) => node.tagName === 'a');
    assert.ok(links.some((link) => link.textContent === '官方服务报道来源' && link.href === 'https://example.org/official-cooling'));
    assert.ok(links.some((link) => link.textContent === '坐标来源 · 高德地图公开页面' && link.href === 'https://ditu.amap.com/place/TEST'));
});

test('待定位避暑目录保留服务限制、历史无空调资料、说明和独立证据日期', async () => {
    const h = await page();
    const data = poiPayload();
    Object.assign(data.unmapped_resources[0], historicalCoolingEvidence(), {
        kind: 'cooling', name: '劳动者驿站待定位', facilities_hint: '旧报道明确未配空调，仅有电风扇',
        official_source_date: null, coordinate_source_url: null, coordinate_checked_at: null
    });
    h.applyDaily(data);
    const text = h.ui.unmappedList.textContent;
    assert.match(text, /户外劳动者.*当前接待对象及进入条件待核实/);
    assert.match(text, /历史设施（不代表当前可用）：旧报道明确未配空调，仅有电风扇/);
    assert.match(text, /官方服务\/设施资料日期：2024-08-14；当前开放情况待核实/);
    assert.match(text, /核验说明：劳动者服务点，公众进入条件待核实/);
    assert.match(text, /资料补充说明：历史设施不代表当前运行/);
    assert.match(text, /坐标待核验，不上图、不计算距离/);
    const link = descendantNodes(h.ui.unmappedList).find((node) => node.tagName === 'a' && node.textContent === '官方服务报道来源');
    assert.equal(link.href, 'https://example.org/official-cooling');
    assert.equal(link.rel, 'noopener noreferrer');
});

test('新增资源证据只渲染文字，官方和坐标来源分别拦截危险URL', async () => {
    const h = await page();
    const data = poiPayload();
    const html = '<img src=x onerror="globalThis.__unsafe=1">';
    const evidence = {...historicalCoolingEvidence(), audience_hint: html, facilities_hint: html,
        notes: [html, null, {html}], verification_note: html,
        official_source_date: html, official_source_url: 'javascript:alert(1)',
        coordinate_source_url: 'https://user:pass@example.org/'};
    Object.assign(data.cooling_candidates[0], evidence);
    Object.assign(data.unmapped_resources[0], evidence);
    h.applyDaily(data);
    h.selectPoi('candidate:one', {pan: false});
    for (const container of [h.ui.poiDetails, h.ui.unmappedList]) {
        assert.ok(container.textContent.includes(html));
        const nodes = descendantNodes(container);
        assert.equal(nodes.some((node) => node.tagName === 'img'), false);
        assert.equal(nodes.some((node) => node.href && /javascript:|user:pass/.test(node.href)), false);
        assert.match(container.textContent, /官方服务报道来源（链接待补充）/);
        assert.match(container.textContent, /坐标来源.*（链接待补充）/);
        assert.doesNotMatch(container.textContent, /\[object Object\]/);
    }
});
