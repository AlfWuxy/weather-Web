(function () {
    'use strict';

    const app = document.getElementById('heatRiskWorkbench');
    if (!app) return;

    // ------------------------------------------------------------------
    // 常量
    // ------------------------------------------------------------------
    const RAW_LAYERS = {
        q3_lst_c_mean: {label: '晴空地表温度', unit: '°C', digits: 1},
        age65_share_pct: {label: '65+ 人口比例', unit: '%', digits: 1},
        tree_cover_pct: {label: '树木覆盖', unit: '%', digits: 1},
        built_up_pct: {label: '建成区覆盖', unit: '%', digits: 1},
        mean_elevation_m: {label: '表面高程', unit: 'm', digits: 0},
        q3_coverage_pct: {label: 'Q3 观测覆盖', unit: '%', digits: 1}
    };
    const FACILITY_RAMP = {
        breaks: [0, 1, 2, 3, 5, 8, 99],
        palette: ['#eef6f2', '#cfe6dc', '#9fcbbd', '#e9b774', '#d7773e', '#8f3b2a'],
        labels: ['<1', '1–2', '2–3', '3–5', '5–8', '≥8']
    };
    const LAYER_TITLES = {
        daily: '当日热风险等级',
        score: '综合热风险分（静态）',
        bivariate: '高温 × 高龄',
        facility_km: '距可达性参考点（直线）'
    };
    const POI_KINDS = {villages: '聚落点', medical: '医疗机构', candidates: '避暑候选', cooling: '坐标已核验避暑资源'};
    const WATER_FILL = '#a9cfe3';
    const NODATA_FILL = '#c9cdc6';
    const UNKNOWN_RISK = '风险暂不可判定';
    const LOCAL_DATE_FORMAT = new Intl.DateTimeFormat('en-US', {
        timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'
    });
    let dateMinute = null;
    let cachedLocalDate = null;
    const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

    const ui = {
        title: document.getElementById('hrwTitle'),
        metaDay: document.getElementById('hrwMetaDay'),
        lede: document.getElementById('hrwLede'),
        days: document.getElementById('hrwDays'),
        forecastSource: document.getElementById('hrwForecastSource'),
        priorityList: document.getElementById('hrwPriorityList'),
        priorityNote: document.getElementById('hrwPriorityNote'),
        actionCard: document.getElementById('hrwActionCard'),
        layerTabs: Array.from(document.querySelectorAll('#hrwLayerTabs [data-layer]')),
        moreLayers: document.getElementById('hrwMoreLayers'),
        search: document.getElementById('hrwSearch'),
        searchOptions: document.getElementById('hrwSearchOptions'),
        searchNote: document.getElementById('hrwSearchNote'),
        townFilter: document.getElementById('hrwTownFilter'),
        poiCounts: document.getElementById('hrwPoiCounts'),
        poiInventoryHint: document.getElementById('hrwPoiInventoryHint'),
        poiCoverageBody: document.getElementById('hrwPoiCoverageBody'),
        poiCoverageNote: document.getElementById('hrwPoiCoverageNote'),
        poiDetails: document.getElementById('hrwPoiDetails'),
        poiList: document.getElementById('hrwPoiList'),
        poiListTitle: document.getElementById('hrwPoiListTitle'),
        poiMore: document.getElementById('hrwPoiMore'),
        poiSources: document.getElementById('hrwPoiSources'),
        unmappedList: document.getElementById('hrwUnmappedList'),
        unmappedSummary: document.getElementById('hrwUnmappedSummary'),
        unmappedMore: document.getElementById('hrwUnmappedMore'),
        inventoryNote: document.getElementById('hrwInventoryNote'),
        swipe: document.getElementById('hrwSwipe'),
        swipeHandle: document.getElementById('hrwSwipeHandle'),
        measure: document.getElementById('hrwMeasure'),
        reset: document.getElementById('hrwReset'),
        controlsToggle: document.getElementById('hrwControlsToggle'),
        map: document.getElementById('hrwMap'),
        mapLoading: document.getElementById('hrwMapLoading'),
        mapFallback: document.getElementById('hrwMapFallback'),
        basemapButtons: Array.from(document.querySelectorAll('#hrwBasemap [data-basemap]')),
        opacity: document.getElementById('hrwOpacity'),
        opacityValue: document.getElementById('hrwOpacityValue'),
        overlayInputs: Array.from(document.querySelectorAll('[data-overlay]')),
        legend: document.getElementById('hrwLegend'),
        readout: document.getElementById('hrwReadout'),
        basemapNote: document.getElementById('hrwBasemapNote'),
        place: document.getElementById('hrwPlace'),
        coords: document.getElementById('hrwCoords'),
        score: document.getElementById('hrwScore'),
        levelChip: document.getElementById('hrwLevelChip'),
        scoreSub: document.getElementById('hrwScoreSub'),
        dailyChip: document.getElementById('hrwDailyChip'),
        breakdown: document.getElementById('hrwBreakdown'),
        facts: document.getElementById('hrwFacts'),
        provenance: document.getElementById('hrwProvenance'),
        townBody: document.getElementById('hrwTownBody'),
        matrixBody: document.getElementById('hrwMatrixBody'),
        sources: document.getElementById('hrwSources'),
        build: document.getElementById('hrwBuild'),
        print: document.getElementById('hrwPrint'),
        printSheet: document.getElementById('hrwPrintSheet')
    };

    const state = {
        meta: null,
        cellMeta: null,
        cells: [],
        cellById: new Map(),
        cellByRowCol: new Map(),
        townships: [],
        facilities: [],
        boundary: null,
        daily: null,
        dayIndex: 0,
        layer: 'score',
        overlays: {hotspot: true, townships: true, villages: true, references: false, medical: true, candidates: true, cooling: true},
        townFilter: '',
        selectedPoiId: null,
        poiById: new Map(),
        poiMarkers: new Map(),
        searchByLabel: new Map(),
        poiListLimit: 20,
        unmappedListLimit: 20,
        basemap: 'vector',
        provider: app.dataset.tiandituKey ? 'tianditu' : 'amap',
        gcj: !app.dataset.tiandituKey,
        opacity: 0.7,
        selected: -1,
        map: null,
        panes: {},
        layers: {},
        polygonsLeft: [],
        polygonsRight: [],
        swipe: false,
        swipeX: 0,
        measuring: false,
        measurePoints: [],
        tileStats: {errors: 0, loads: 0},
        dailyLoaded: false,
        forecastDate: null
    };

    // ------------------------------------------------------------------
    // 工具函数
    // ------------------------------------------------------------------
    function isNum(value) {
        return typeof value === 'number' && Number.isFinite(value);
    }

    function fmt(value, digits) {
        if (!isNum(value)) return '无数据';
        return new Intl.NumberFormat('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits}).format(value);
    }

    // 地图标签与提示使用 HTML 字符串，所有数据字段必须先转义。
    function esc(value) {
        return String(value === undefined || value === null ? '' : value).replace(/[&<>"']/g, (ch) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        })[ch]);
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    // WGS84 → GCJ-02（与服务端 heat_risk_workbench_service.wgs84_to_gcj02 同一公式）。
    const GCJ_A = 6378245.0;
    const GCJ_EE = 0.00669342162296594323;

    function gcjDelta(lon, lat) {
        const x = lon - 105.0;
        const y = lat - 35.0;
        const PI = Math.PI;
        let dLat = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
        dLat += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
        dLat += (20.0 * Math.sin(y * PI) + 40.0 * Math.sin(y / 3.0 * PI)) * 2.0 / 3.0;
        dLat += (160.0 * Math.sin(y / 12.0 * PI) + 320.0 * Math.sin(y * PI / 30.0)) * 2.0 / 3.0;
        let dLon = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
        dLon += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
        dLon += (20.0 * Math.sin(x * PI) + 40.0 * Math.sin(x / 3.0 * PI)) * 2.0 / 3.0;
        dLon += (150.0 * Math.sin(x / 12.0 * PI) + 300.0 * Math.sin(x / 30.0 * PI)) * 2.0 / 3.0;
        const radLat = lat / 180.0 * PI;
        const magic = 1 - GCJ_EE * Math.sin(radLat) * Math.sin(radLat);
        const sqrtMagic = Math.sqrt(magic);
        dLat = (dLat * 180.0) / ((GCJ_A * (1 - GCJ_EE)) / (magic * sqrtMagic) * PI);
        dLon = (dLon * 180.0) / (GCJ_A / sqrtMagic * Math.cos(radLat) * PI);
        return [dLon, dLat];
    }

    function wgsToGcj(lon, lat) {
        const d = gcjDelta(lon, lat);
        return [lon + d[0], lat + d[1]];
    }

    function gcjToWgs(lon, lat) {
        let wLon = lon;
        let wLat = lat;
        for (let i = 0; i < 12; i += 1) {
            const g = wgsToGcj(wLon, wLat);
            wLon -= g[0] - lon;
            wLat -= g[1] - lat;
        }
        return [wLon, wLat];
    }

    // 所有叠加要素统一经此函数投到当前底图坐标系；高德底图需 GCJ-02 纠偏。
    function toMap(lon, lat) {
        if (state.gcj) {
            const g = wgsToGcj(lon, lat);
            return [g[1], g[0]];
        }
        return [lat, lon];
    }

    function fromMap(latlng) {
        if (state.gcj) {
            const w = gcjToWgs(latlng.lng, latlng.lat);
            return {lon: w[0], lat: w[1]};
        }
        return {lon: latlng.lng, lat: latlng.lat};
    }

    function haversineKm(lonA, latA, lonB, latB) {
        const toRad = Math.PI / 180;
        const dLat = (latB - latA) * toRad;
        const dLon = (lonB - lonA) * toRad;
        const h = Math.sin(dLat / 2) ** 2 + Math.cos(latA * toRad) * Math.cos(latB * toRad) * Math.sin(dLon / 2) ** 2;
        return 2 * 6371.0088 * Math.asin(Math.min(1, Math.sqrt(h)));
    }

    // 逐日风险矩阵（与服务端 combine_daily_level 同一规则）。
    function combineDailyLevel(hazard, staticLevel) {
        if (!isLevel(hazard)) return null;
        if (hazard === 0) return 0;
        let adjust = 0;
        if (isNum(staticLevel)) {
            if (staticLevel <= 1) adjust = -1;
            else if (staticLevel >= 3) adjust = 1;
        }
        return Math.max(1, Math.min(4, hazard + adjust));
    }

    function levelInfo(level) {
        const levels = state.meta.score_method.levels;
        return levels[Math.max(0, Math.min(levels.length - 1, level))];
    }

    function isLevel(value) {
        return Number.isInteger(value) && value >= 0 && value <= 4;
    }

    // 预报日期属于都昌；跨时区查看时也按当地日历判断“今天”。
    function localDate() {
        const minute = Math.floor(Date.now() / 60000);
        if (minute !== dateMinute) {
            const parts = LOCAL_DATE_FORMAT.formatToParts(new Date());
            const part = (type) => parts.find((item) => item.type === type).value;
            cachedLocalDate = `${part('year')}-${part('month')}-${part('day')}`;
            dateMinute = minute;
        }
        return cachedLocalDate;
    }

    function currentDay() {
        return state.daily && state.daily.days[state.dayIndex] ? state.daily.days[state.dayIndex] : null;
    }

    function dayHazard(day) {
        const status = state.daily && state.daily.forecast_status;
        if (status && !['ok', 'fallback', 'demo'].includes(status)) return null;
        if (!day || !isLevel(day.level) || !isNum(day.temperature_max) || !isNum(day.temperature_min)) return null;
        if (day.temperature_max < day.temperature_min || !/^\d{4}-\d{2}-\d{2}$/.test(day.date) || day.date < localDate()) return null;
        return day.level;
    }

    function currentHazard() {
        return dayHazard(currentDay());
    }

    function demoPrefix() {
        return state.daily && state.daily.forecast_status === 'demo' ? '演示 · ' : '';
    }

    function forecastSourceText() {
        if (!state.daily) return '正在读取天气预报。';
        const status = state.daily.forecast_status;
        const prefix = status === 'demo' ? '演示数据，仅供演示 · ' : status === 'fallback' ? '备用预报 · ' : '';
        const source = state.daily.forecast_source ? `来源：${state.daily.forecast_source}` : '天气预报不可用';
        const threshold = isNum(state.daily.hot_night_tmin_c) ? ` · 热夜阈值 ${fmt(state.daily.hot_night_tmin_c, 1)} °C` : '';
        const notice = state.daily.forecast_notice ? ` · ${state.daily.forecast_notice}` : '';
        return `${prefix}${source}${threshold}${notice}`;
    }

    function dayReasons(day) {
        return day && Array.isArray(day.reasons) ? day.reasons : [];
    }

    function dayLabel(isoDate) {
        const parts = String(isoDate || '').split('-').map(Number);
        if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) return isoDate || '';
        const date = new Date(parts[0], parts[1] - 1, parts[2]);
        return `${isoDate === localDate() ? '今天' : WEEKDAYS[date.getDay()]} ${parts[1]}/${parts[2]}`;
    }

    function levelChip(node, level, prefix) {
        if (!isLevel(level)) {
            node.textContent = UNKNOWN_RISK;
            node.style.backgroundColor = NODATA_FILL;
            node.style.color = '#2A2620';
            return;
        }
        const info = levelInfo(level);
        node.textContent = `${prefix || ''}${level} 级 · ${info.label}`;
        node.style.backgroundColor = info.color;
        node.style.color = level >= 3 ? '#ffffff' : '#2A2620';
    }

    // ------------------------------------------------------------------
    // 数据
    // ------------------------------------------------------------------
    function prepareData(geojson, workbench) {
        if (!geojson || geojson.type !== 'FeatureCollection' || !Array.isArray(geojson.features)) {
            throw new Error('网格 GeoJSON 无效');
        }
        if (!workbench || !workbench.metadata || !workbench.cells || !Array.isArray(workbench.cells.cell_id)) {
            throw new Error('风险分数据无效');
        }
        state.meta = workbench.metadata;
        state.cellMeta = geojson.metadata;
        state.boundary = geojson.features.find((f) => f.properties.feature_type === 'study_boundary');
        const features = geojson.features.filter((f) => f.properties.feature_type === 'modis_cell');
        const fields = workbench.cells;
        const byId = new Map(features.map((f) => [f.properties.cell_id, f]));
        if (fields.cell_id.length !== features.length) throw new Error('风险分与网格数量不一致');

        const spatial = geojson.metadata.spatial_definition;
        const halfLat = spatial.native_nominal_resolution_m / (2 * spatial.native_sphere_radius_m) * 180 / Math.PI;

        fields.cell_id.forEach((cellId, i) => {
            const feature = byId.get(cellId);
            if (!feature) throw new Error(`网格缺失：${cellId}`);
            const p = feature.properties;
            const halfLon = halfLat / Math.cos(p.center_lat_wgs84 * Math.PI / 180);
            const cell = {
                i,
                id: cellId,
                p,
                lon: p.center_lon_wgs84,
                lat: p.center_lat_wgs84,
                row: p.modis_row_0based,
                col: p.modis_col_0based,
                west: p.center_lon_wgs84 - halfLon,
                east: p.center_lon_wgs84 + halfLon,
                south: p.center_lat_wgs84 - halfLat,
                north: p.center_lat_wgs84 + halfLat,
                township: fields.township[i],
                townshipMethod: fields.township_method[i],
                land: fields.land[i] === 1,
                scored: fields.scored[i] === 1,
                facility: fields.facility[i],
                facility_km: fields.facility_km[i],
                hazard_pct: fields.hazard_pct[i],
                exposure_pct: fields.exposure_pct[i],
                vulnerability_pct: fields.vulnerability_pct[i],
                shade_deficit_pct: fields.shade_deficit_pct[i],
                built_pct: fields.built_pct[i],
                access_pct: fields.access_pct[i],
                score: fields.score[i],
                level: fields.level[i],
                bivariate: fields.bivariate[i],
                gi_z: fields.gi_z[i],
                gi_bin: fields.gi_bin[i],
                top_pct: fields.top_pct[i],
                top_pct_p05: fields.top_pct_p05[i],
                top_pct_p95: fields.top_pct_p95[i]
            };
            state.cells.push(cell);
            state.cellById.set(cellId, i);
            state.cellByRowCol.set(`${cell.row}:${cell.col}`, i);
        });
        state.townships = workbench.townships.features;
        state.facilities = workbench.facilities;
    }

    function cellValue(cell, layer) {
        if (layer === 'facility_km') return cell.facility_km;
        return cell.p[layer];
    }

    function rampColor(value, breaks, palette) {
        if (!isNum(value)) return NODATA_FILL;
        for (let k = 1; k < breaks.length; k += 1) {
            if (value <= breaks[k]) return palette[k - 1];
        }
        return palette[palette.length - 1];
    }

    function fillFor(cell, layer) {
        if (!cell.land) return {color: WATER_FILL, water: true};
        if (layer === 'daily' || layer === 'score' || layer === 'bivariate') {
            if (!cell.scored) return {color: NODATA_FILL, nodata: true};
            if (layer === 'bivariate') return {color: state.meta.bivariate.palette[cell.bivariate]};
            const level = layer === 'daily' ? combineDailyLevel(currentHazard(), cell.level) : cell.level;
            return isLevel(level) ? {color: levelInfo(level).color} : {color: NODATA_FILL, nodata: true};
        }
        if (layer === 'facility_km') return {color: rampColor(cell.facility_km, FACILITY_RAMP.breaks, FACILITY_RAMP.palette)};
        const spec = state.cellMeta.layers[layer];
        const value = cell.p[layer];
        if (!isNum(value)) return {color: NODATA_FILL, nodata: true};
        return {color: rampColor(value, spec.breaks, spec.palette)};
    }

    function styleFor(cell, layer) {
        const fill = fillFor(cell, layer);
        const basemapOn = state.basemap !== 'none';
        let fillOpacity = state.opacity;
        if (fill.water) fillOpacity = basemapOn ? 0 : 0.55;
        else if (fill.nodata) fillOpacity = state.opacity * 0.45;
        return {
            stroke: !fill.water,
            color: '#ffffff',
            weight: 0.35,
            opacity: basemapOn ? 0.35 : 0.6,
            fillColor: fill.color,
            fillOpacity
        };
    }

    // ------------------------------------------------------------------
    // 地图
    // ------------------------------------------------------------------
    function basemapDefs() {
        const key = app.dataset.tiandituKey;
        if (state.provider === 'tianditu') {
            const url = (layer) => `https://t{s}.tianditu.gov.cn/DataServer?T=${layer}_w&x={x}&y={y}&l={z}&tk=${encodeURIComponent(key)}`;
            return {
                vector: {base: url('vec'), labels: url('cva'), subdomains: '01234567', name: '天地图'},
                imagery: {base: url('img'), labels: url('cia'), subdomains: '01234567', name: '天地图影像'}
            };
        }
        return {
            vector: {
                base: 'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
                labels: null,
                subdomains: '1234',
                name: '高德地图'
            },
            imagery: {
                base: 'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
                labels: 'https://webst0{s}.is.autonavi.com/appmaptile?style=8&x={x}&y={y}&z={z}',
                subdomains: '1234',
                name: '高德影像'
            }
        };
    }

    function attributionText() {
        const base = state.provider === 'tianditu' ? '底图 © 天地图' : '底图 © 高德地图';
        return `${base} · © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a>（ODbL）· <a href="https://www.geonames.org/" target="_blank" rel="noopener noreferrer">GeoNames</a>（CC BY 4.0）· NASA · ASPECT · ESA · Copernicus`;
    }

    function setBasemap(mode) {
        state.basemap = mode;
        ['basemapBase', 'basemapLabels'].forEach((name) => {
            if (state.layers[name]) {
                state.layers[name].remove();
                state.layers[name] = null;
            }
        });
        ui.basemapButtons.forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.basemap === mode)));
        app.dataset.basemap = mode;
        if (mode !== 'none') {
            const def = basemapDefs()[mode];
            const L = window.L;
            state.layers.basemapBase = L.tileLayer(def.base, {subdomains: def.subdomains, maxZoom: 18, pane: 'basemapPane'})
                .on('tileerror', onTileError)
                .on('tileload', onTileLoad)
                .addTo(state.map);
            if (def.labels) {
                state.layers.basemapLabels = L.tileLayer(def.labels, {subdomains: def.subdomains, maxZoom: 18, pane: 'labelPane'}).addTo(state.map);
            }
        }
        restyleCells();
        renderLegend();
    }

    function onTileLoad() {
        state.tileStats.loads += 1;
    }

    function onTileError() {
        state.tileStats.errors += 1;
        if (state.tileStats.errors >= 8 && state.tileStats.loads === 0 && state.basemap !== 'none') {
            ui.basemapNote.hidden = false;
            ui.basemapNote.textContent = '底图瓦片无法访问，已切换为无底图模式。';
            setBasemap('none');
        }
    }

    function cellLatLngs(cell) {
        return [
            toMap(cell.west, cell.north),
            toMap(cell.east, cell.north),
            toMap(cell.east, cell.south),
            toMap(cell.west, cell.south)
        ];
    }

    function buildCellPolygons(renderer, pane, layerGetter, store) {
        const L = window.L;
        const group = L.layerGroup();
        state.cells.forEach((cell) => {
            const polygon = L.polygon(cellLatLngs(cell), {...styleFor(cell, layerGetter()), renderer, pane});
            polygon.on('click', () => selectCell(cell.i, {pan: false}));
            polygon.bindTooltip(() => tooltipFor(cell, layerGetter()), {sticky: true, className: 'hrw-tooltip', direction: 'top'});
            store.push(polygon);
            group.addLayer(polygon);
        });
        return group;
    }

    function tooltipFor(cell, layer) {
        const wrapper = el('div');
        const town = state.townships[cell.township];
        wrapper.appendChild(el('strong', null, town ? town.properties.name_zh : '都昌县'));
        let text;
        if (!cell.land) text = '湖面（不评分）';
        else if (layer === 'daily') text = !isLevel(currentHazard()) ? UNKNOWN_RISK : cell.scored ? `${demoPrefix()}当日 ${combineDailyLevel(currentHazard(), cell.level)} 级 · 综合分 ${fmt(cell.score, 0)}` : '无常住人口';
        else if (layer === 'score') text = cell.scored ? `综合分 ${fmt(cell.score, 0)} · ${levelInfo(cell.level).label}` : '无常住人口';
        else if (layer === 'bivariate') text = cell.scored ? `${cell.bivariate.toUpperCase()} · 地表 ${fmt(cell.p.q3_lst_c_mean, 1)} °C · 65+ ${fmt(cell.p.age65_share_pct, 1)}%` : '无常住人口';
        else if (layer === 'facility_km') text = `距可达性参考点 ${fmt(cell.facility_km, 1)} km（直线）`;
        else {
            const spec = RAW_LAYERS[layer];
            text = `${spec.label} ${fmt(cell.p[layer], spec.digits)} ${spec.unit}`;
        }
        wrapper.appendChild(el('span', null, text));
        return wrapper;
    }

    function activeLeftLayer() {
        return state.swipe ? 'q3_lst_c_mean' : state.layer;
    }

    function restyleCells() {
        const left = activeLeftLayer();
        state.polygonsLeft.forEach((polygon, i) => polygon.setStyle(styleFor(state.cells[i], left)));
        if (state.swipe) {
            state.polygonsRight.forEach((polygon, i) => polygon.setStyle(styleFor(state.cells[i], 'age65_share_pct')));
        }
    }

    function hotspotSegments() {
        // 只画热点簇外轮廓：某格某条边外侧的邻格不是热点时才绘制。
        const segments = {1: [], 2: []};
        state.cells.forEach((cell) => {
            if (!(cell.gi_bin > 0)) return;
            const neighbor = (dr, dc) => {
                const index = state.cellByRowCol.get(`${cell.row + dr}:${cell.col + dc}`);
                return index !== undefined && state.cells[index].gi_bin > 0;
            };
            const bucket = segments[cell.gi_bin >= 2 ? 2 : 1];
            if (!neighbor(-1, 0)) bucket.push([toMap(cell.west, cell.north), toMap(cell.east, cell.north)]);
            if (!neighbor(1, 0)) bucket.push([toMap(cell.west, cell.south), toMap(cell.east, cell.south)]);
            if (!neighbor(0, -1)) bucket.push([toMap(cell.west, cell.north), toMap(cell.west, cell.south)]);
            if (!neighbor(0, 1)) bucket.push([toMap(cell.east, cell.north), toMap(cell.east, cell.south)]);
        });
        return segments;
    }

    function buildOverlays() {
        const L = window.L;
        const segments = hotspotSegments();
        state.layers.hotspot = L.layerGroup([
            L.polyline(segments[2], {pane: 'hotspotPane', color: '#2A1414', weight: 2, opacity: 0.9, interactive: false}),
            L.polyline(segments[1], {pane: 'hotspotPane', color: '#2A1414', weight: 1.4, opacity: 0.8, dashArray: '4 3', interactive: false})
        ]);

        const townshipShapes = L.geoJSON({type: 'FeatureCollection', features: state.townships}, {
            pane: 'boundaryPane',
            interactive: false,
            coordsToLatLng: (coords) => window.L.latLng(...toMap(coords[0], coords[1])),
            style: {color: '#6B5F52', weight: 1.1, opacity: 0.85, dashArray: '6 4', fill: false}
        });
        const townLabels = state.townships.map((feature) => {
            const p = feature.properties;
            return L.marker(toMap(p.seat_lon_wgs84, p.seat_lat_wgs84), {
                pane: 'labelTextPane',
                interactive: false,
                keyboard: false,
                icon: L.divIcon({className: 'hrw-town-label', html: `<span>${esc(p.name_zh)}</span>`, iconSize: null})
            });
        });
        state.layers.townships = L.layerGroup([townshipShapes, ...townLabels]);

        if (state.boundary) {
            state.layers.county = L.geoJSON(state.boundary, {
                pane: 'boundaryPane',
                interactive: false,
                coordsToLatLng: (coords) => window.L.latLng(...toMap(coords[0], coords[1])),
                style: {color: '#2A2620', weight: 2.4, opacity: 0.9, fill: false}
            }).addTo(state.map);
        }

        // 旧版乡镇驻地仅为冻结模型的可达性参考，不冒充医疗机构。
        const references = state.facilities.filter((point) => point.precision !== 'exact').map((point) => {
            const marker = L.marker(toMap(point.lon, point.lat), {
                pane: 'pointPane',
                icon: L.divIcon({className: 'hrw-reference-icon', html: '<span>◇</span>', iconSize: [18, 18]}),
                title: referenceName(point)
            });
            marker.bindTooltip(`${esc(referenceName(point))}<br><small>乡镇驻地近似位置 · 非医疗机构位置</small>`, {className: 'hrw-tooltip'});
            return marker;
        });
        state.layers.references = L.layerGroup(references);
        Object.keys(POI_KINDS).forEach((kind) => { state.layers[kind] = L.layerGroup(); });
        state.layers.poiLabels = L.layerGroup().addTo(state.map);
        state.poiRenderer = L.canvas({padding: 0.4, tolerance: 5, pane: 'pointPane'});
        applyOverlayVisibility();
    }

    function referenceName(point) {
        if (point.precision === 'exact') return `${point.name}（模型参考点）`;
        const township = state.townships[point.township];
        const name = typeof point.township === 'string' ? point.township : township ? township.properties.name_zh : '乡镇';
        return `${name}驻地参考点`;
    }

    function applyOverlayVisibility() {
        Object.keys(state.overlays).forEach((key) => {
            const layer = state.layers[key];
            if (!layer) return;
            if (state.overlays[key]) layer.addTo(state.map);
            else layer.remove();
        });
        renderPoiLabels();
        renderLegend();
    }

    function poiId(point, kind) {
        return String(point.id || `${kind}:${point.name}:${point.lon_wgs84}:${point.lat_wgs84}`);
    }

    function matchesTown(point) {
        return !state.townFilter || (state.townFilter === '__unknown__' ? !point.township : point.township === state.townFilter);
    }

    function villageDailyLevel(point) {
        if (point.risk_data_status === 'no_grid_data' || !isLevel(point.static_level)) return null;
        return combineDailyLevel(currentHazard(), point.static_level);
    }

    function buildPoiCatalog() {
        state.poiById.clear();
        if (!state.daily) return;
        const arrays = {villages: state.daily.villages, medical: state.daily.medical_pois,
            candidates: state.daily.cooling_candidates, cooling: state.daily.cooling_resources};
        Object.entries(arrays).forEach(([kind, points]) => {
            points.forEach((point) => {
                if (!point || !isNum(point.lon_wgs84) || !isNum(point.lat_wgs84)) return;
                const id = poiId(point, kind);
                // 核验过期的缓存仍可作位置参考，但不能留在坐标核验图层。
                const expired = kind === 'cooling' && point.valid_until && String(point.valid_until).slice(0, 10) < localDate();
                const entry = expired ? {id, kind: 'candidates', point: {...point, coordinate_verification_expired: true}} : {id, kind, point};
                if (!state.poiById.has(id)) state.poiById.set(id, entry);
            });
        });

    }

    function poiStyle(entry) {
        const selected = entry.id === state.selectedPoiId;
        let color = '#46734b';
        if (entry.kind === 'villages') {
            const level = villageDailyLevel(entry.point);
            color = isLevel(level) ? levelInfo(level).color : NODATA_FILL;
        } else if (entry.kind === 'candidates') color = '#258896';
        else if (entry.kind === 'cooling') color = '#2466b0';
        return {
            pane: 'pointPane', renderer: state.poiRenderer,
            radius: selected ? 8 : entry.kind === 'villages' ? 4.5 : 6,
            color: selected ? '#a74407' : entry.kind === 'candidates' ? color : '#fff',
            weight: selected ? 3 : 1.8, fillColor: color,
            fillOpacity: entry.kind === 'candidates' ? 0.15 : 0.95,
            dashArray: entry.kind === 'candidates' ? '3 2' : null
        };
    }

    function poiTooltip(entry) {
        const point = entry.point;
        let detail = POI_KINDS[entry.kind];
        if (entry.kind === 'villages') {
            const level = villageDailyLevel(point);
            detail += isLevel(level) ? ` · ${demoPrefix()}当日 ${level} 级` : ` · ${!isLevel(point.static_level) ? '网格数据缺失' : UNKNOWN_RISK}`;
        }
        if (entry.kind === 'candidates' || entry.kind === 'cooling') detail += ' · 开放情况待核实';
        return `${esc(point.name)}<br><small>${esc(point.township || '乡镇归属待核验')} · ${esc(detail)}</small>`;
    }

    function renderVillages() {
        if (!state.map || !state.daily) return;
        Object.keys(POI_KINDS).forEach((kind) => state.layers[kind].clearLayers());
        state.poiMarkers.clear();
        state.poiById.forEach((entry) => {
            if (!matchesTown(entry.point)) return;
            const marker = window.L.circleMarker(toMap(entry.point.lon_wgs84, entry.point.lat_wgs84), poiStyle(entry));
            marker.bindTooltip(() => poiTooltip(entry), {direction: 'top', className: 'hrw-tooltip'});
            marker.on('click', () => selectPoi(entry.id, {reveal: true}));
            state.layers[entry.kind].addLayer(marker);
            state.poiMarkers.set(entry.id, marker);
        });
        renderPoiLabels();
    }

    function renderPoiLabels() {
        if (!state.map || !state.layers.poiLabels) return;
        state.layers.poiLabels.clearLayers();
        if (!isNum(state.map.getZoom()) || state.map.getZoom() < 14) return;
        const bounds = state.map.getBounds();
        const occupied = new Set();
        let count = 0;
        // 只给当前视野内、已打开图层的少量点显示名称，避免数百个常驻 DOM 标签。
        state.poiById.forEach((entry) => {
            if (count >= 40 || !state.overlays[entry.kind] || !matchesTown(entry.point)) return;
            const position = toMap(entry.point.lon_wgs84, entry.point.lat_wgs84);
            if (!bounds.contains(position)) return;
            const pixel = state.map.latLngToContainerPoint(position);
            const slot = `${Math.floor(pixel.x / 110)}:${Math.floor(pixel.y / 28)}`;
            if (occupied.has(slot)) return;
            occupied.add(slot);
            state.layers.poiLabels.addLayer(window.L.marker(position, {
                pane: 'labelTextPane', interactive: false, keyboard: false,
                icon: window.L.divIcon({className: 'hrw-village-label', html: `<span>${esc(entry.point.name)}</span>`, iconSize: null})
            }));
            count += 1;
        });
    }

    function drawSelection() {
        if (!state.map) return;
        const L = window.L;
        if (state.layers.selection) state.layers.selection.remove();
        if (state.selected < 0) return;
        state.layers.selection = L.polygon(cellLatLngs(state.cells[state.selected]), {
            pane: 'selectionPane',
            interactive: false,
            color: '#A74407',
            weight: 3,
            fill: false
        }).addTo(state.map);
    }

    function initializeMap() {
        const L = window.L;
        state.map = L.map(ui.map, {
            zoomControl: true,
            attributionControl: false,
            minZoom: 8,
            maxZoom: 17,
            zoomSnap: 0.25,
            wheelPxPerZoomLevel: 90
        });
        const panes = [
            ['basemapPane', 200],
            ['cellPane', 350],
            ['cellPaneRight', 351],
            ['boundaryPane', 420],
            ['hotspotPane', 430],
            ['labelPane', 440],
            ['labelTextPane', 445],
            ['selectionPane', 460],
            ['pointPane', 620]
        ];
        panes.forEach(([name, z]) => {
            state.panes[name] = state.map.createPane(name);
            state.panes[name].style.zIndex = String(z);
        });
        state.panes.basemapPane.style.pointerEvents = 'none';
        state.panes.labelPane.style.pointerEvents = 'none';
        state.panes.labelTextPane.style.pointerEvents = 'none';

        state.rendererLeft = L.canvas({padding: 0.5, tolerance: 4, pane: 'cellPane'});
        state.layers.cells = buildCellPolygons(state.rendererLeft, 'cellPane', activeLeftLayer, state.polygonsLeft).addTo(state.map);
        buildOverlays();

        const bounds = L.latLngBounds(state.cells.map((cell) => toMap(cell.lon, cell.lat)));
        state.countyBounds = bounds;
        state.map.fitBounds(bounds, {padding: [16, 16]});
        state.map.setMaxBounds(bounds.pad(0.6));
        L.control.scale({position: 'bottomleft', imperial: false, maxWidth: 120}).addTo(state.map);
        L.control.attribution({position: 'bottomright', prefix: false}).addTo(state.map).addAttribution(attributionText());

        state.map.on('mousemove', (event) => {
            const w = fromMap(event.latlng);
            ui.readout.textContent = `${w.lon.toFixed(5)}°E · ${w.lat.toFixed(5)}°N · WGS84${state.gcj ? ' · 高德底图已纠偏' : ''}`;
        });
        state.map.on('zoomend', updateZoomClasses);
        state.map.on('zoomend moveend', renderPoiLabels);
        state.map.on('move zoom resize', updateSwipeClip);
        state.map.on('click', onMeasureClick);
        state.map.on('dblclick', finishMeasure);
        updateZoomClasses();
        setBasemap(state.basemap);
        ui.mapLoading.hidden = true;
        window.setTimeout(() => state.map.invalidateSize(), 80);
    }

    function updateZoomClasses() {
        const zoom = state.map.getZoom();
        app.classList.toggle('hrw-zoom-town', zoom >= 9);
        app.classList.toggle('hrw-zoom-village', zoom >= 12);
    }

    // ------------------------------------------------------------------
    // 卷帘对比
    // ------------------------------------------------------------------
    function setSwipe(enabled) {
        state.swipe = enabled;
        ui.swipe.setAttribute('aria-pressed', String(enabled));
        ui.swipeHandle.hidden = !enabled;
        app.classList.toggle('hrw-swiping', enabled);
        if (enabled) {
            if (!state.layers.cellsRight) {
                state.rendererRight = window.L.canvas({padding: 0.5, tolerance: 4, pane: 'cellPaneRight'});
                state.layers.cellsRight = buildCellPolygons(state.rendererRight, 'cellPaneRight', () => 'age65_share_pct', state.polygonsRight);
            }
            state.layers.cellsRight.addTo(state.map);
            state.swipeX = state.map.getSize().x / 2;
        } else if (state.layers.cellsRight) {
            state.layers.cellsRight.remove();
        }
        restyleCells();
        updateSwipeClip();
        renderLegend();
    }

    function updateSwipeClip() {
        const left = state.panes.cellPane;
        const right = state.panes.cellPaneRight;
        if (!left || !right) return;
        if (!state.swipe) {
            left.style.clip = '';
            right.style.clip = '';
            return;
        }
        const map = state.map;
        const size = map.getSize();
        state.swipeX = Math.max(0, Math.min(size.x, state.swipeX));
        const nw = map.containerPointToLayerPoint([0, 0]);
        const se = map.containerPointToLayerPoint(size);
        const x = map.containerPointToLayerPoint([state.swipeX, 0]).x;
        left.style.clip = `rect(${nw.y}px, ${x}px, ${se.y}px, ${nw.x}px)`;
        right.style.clip = `rect(${nw.y}px, ${se.x}px, ${se.y}px, ${x}px)`;
        ui.swipeHandle.style.left = `${state.swipeX}px`;
    }

    function bindSwipeHandle() {
        let dragging = false;
        ui.swipeHandle.addEventListener('pointerdown', (event) => {
            dragging = true;
            ui.swipeHandle.setPointerCapture(event.pointerId);
            state.map.dragging.disable();
            event.preventDefault();
        });
        ui.swipeHandle.addEventListener('pointermove', (event) => {
            if (!dragging) return;
            const rect = ui.map.getBoundingClientRect();
            state.swipeX = event.clientX - rect.left;
            updateSwipeClip();
        });
        const stop = () => {
            if (!dragging) return;
            dragging = false;
            state.map.dragging.enable();
        };
        ui.swipeHandle.addEventListener('pointerup', stop);
        ui.swipeHandle.addEventListener('pointercancel', stop);
    }

    // ------------------------------------------------------------------
    // 测距
    // ------------------------------------------------------------------
    function setMeasure(enabled) {
        state.measuring = enabled;
        ui.measure.setAttribute('aria-pressed', String(enabled));
        app.classList.toggle('hrw-measuring', enabled);
        if (enabled) state.map.doubleClickZoom.disable();
        else state.map.doubleClickZoom.enable();
        clearMeasure();
    }

    function clearMeasure() {
        state.measurePoints = [];
        if (state.layers.measure) state.layers.measure.remove();
        state.layers.measure = null;
    }

    function onMeasureClick(event) {
        if (!state.measuring) return;
        const L = window.L;
        state.measurePoints.push(event.latlng);
        if (state.layers.measure) state.layers.measure.remove();
        let total = 0;
        for (let k = 1; k < state.measurePoints.length; k += 1) {
            const a = fromMap(state.measurePoints[k - 1]);
            const b = fromMap(state.measurePoints[k]);
            total += haversineKm(a.lon, a.lat, b.lon, b.lat);
        }
        const line = L.polyline(state.measurePoints, {pane: 'selectionPane', color: '#A74407', weight: 3, dashArray: '6 5', interactive: false});
        const dots = state.measurePoints.map((point) => L.circleMarker(point, {pane: 'selectionPane', radius: 4, color: '#A74407', fillColor: '#fff', fillOpacity: 1, weight: 2, interactive: false}));
        const label = L.tooltip({permanent: true, direction: 'right', className: 'hrw-measure-label', offset: [8, 0]})
            .setLatLng(event.latlng)
            .setContent(state.measurePoints.length > 1 ? `${fmt(total, 2)} km（双击结束）` : '继续点击下一点');
        state.layers.measure = L.layerGroup([line, ...dots, label]).addTo(state.map);
    }

    function finishMeasure() {
        if (!state.measuring) return;
        state.measuring = false;
        ui.measure.setAttribute('aria-pressed', 'false');
        app.classList.remove('hrw-measuring');
        window.setTimeout(() => state.map.doubleClickZoom.enable(), 0);
    }

    // ------------------------------------------------------------------
    // 图例
    // ------------------------------------------------------------------
    function legendRow(color, text, extraClass) {
        const row = el('div', `hrw-legend-row${extraClass ? ` ${extraClass}` : ''}`);
        const swatch = el('i');
        swatch.style.backgroundColor = color;
        row.append(swatch, el('span', null, text));
        return row;
    }

    function renderLegend() {
        if (!state.meta) return;
        const nodes = [];
        if (state.swipe) {
            nodes.push(el('div', 'hrw-legend-title', '卷帘对比'));
            nodes.push(el('p', 'hrw-legend-note', '左：晴空地表温度；右：65+ 人口比例。拖动中线比较。'));
        } else if (state.layer === 'daily' || state.layer === 'score') {
            const day = currentDay();
            nodes.push(el('div', 'hrw-legend-title', state.layer === 'daily' ? `当日风险 · ${day ? dayLabel(day.date) : '预报不可用'}` : '综合热风险分'));
            state.meta.score_method.levels.slice().reverse().forEach((info) => {
                const range = state.layer === 'score'
                    ? ['< 20', '20–40', '40–60', '60–80', '≥ 80'][info.level]
                    : ['无高温', '', '', '', ''][info.level];
                nodes.push(legendRow(info.color, `${info.level} 级 ${info.label}${range ? ` · ${range}` : ''}`));
            });
        } else if (state.layer === 'bivariate') {
            nodes.push(el('div', 'hrw-legend-title', '高温 × 高龄'));
            const grid = el('div', 'hrw-bivariate');
            ['3', '2', '1'].forEach((y) => {
                ['a', 'b', 'c'].forEach((x) => {
                    const cell = el('i');
                    cell.style.backgroundColor = state.meta.bivariate.palette[`${x}${y}`];
                    cell.title = `${x}${y}`;
                    grid.appendChild(cell);
                });
            });
            const wrap = el('div', 'hrw-bivariate-wrap');
            wrap.append(el('span', 'hrw-bivariate-y', '↑ 65+ 比例'), grid, el('span', 'hrw-bivariate-x', '地表温度 →'));
            nodes.push(wrap);
            nodes.push(el('p', 'hrw-legend-note', '右上角深色 = 又热又老'));
        } else if (state.layer === 'facility_km') {
            nodes.push(el('div', 'hrw-legend-title', '距可达性参考点（km，直线）'));
            FACILITY_RAMP.palette.forEach((color, k) => nodes.push(legendRow(color, FACILITY_RAMP.labels[k])));
        } else {
            const spec = state.cellMeta.layers[state.layer];
            const raw = RAW_LAYERS[state.layer];
            nodes.push(el('div', 'hrw-legend-title', `${raw.label}（${raw.unit}）`));
            spec.palette.slice().reverse().forEach((color, k) => {
                const index = spec.palette.length - 1 - k;
                nodes.push(legendRow(color, `${fmt(spec.breaks[index], raw.digits)} – ${fmt(spec.breaks[index + 1], raw.digits)}`));
            });
            nodes.push(el('p', 'hrw-legend-note', '全县六分位，相对比较'));
        }
        nodes.push(legendRow(NODATA_FILL, '无常住人口 / 无数据', 'is-muted'));
        if (state.basemap === 'none') nodes.push(legendRow(WATER_FILL, '湖面（不评分）', 'is-muted'));
        if (state.overlays.hotspot) {
            const row = el('div', 'hrw-legend-row hrw-legend-line');
            row.append(el('i', 'is-hotspot'), el('span', null, '统计热点（实线强显著）'));
            nodes.push(row);
        }
        if (state.overlays.medical) nodes.push(legendRow('#46734b', '已收录医疗机构（开放待核实）'));
        if (state.overlays.candidates) nodes.push(legendRow('#cce5e7', '避暑候选（未核验开放）'));
        if (state.overlays.cooling) nodes.push(legendRow('#2466b0', '坐标已核验避暑资源'));
        if (state.overlays.references) nodes.push(el('p', 'hrw-legend-note', '◇ 可达性参考点，非医疗机构位置'));
        ui.legend.replaceChildren(...nodes);
    }

    // ------------------------------------------------------------------
    // 右侧详情
    // ------------------------------------------------------------------
    function nearestVillage(cell) {
        if (!state.daily) return null;
        let best = null;
        state.daily.villages.forEach((village) => {
            const km = haversineKm(cell.lon, cell.lat, village.lon_wgs84, village.lat_wgs84);
            if (km <= 1.5 && (!best || km < best.km)) best = {village, km};
        });
        return best;
    }

    function barRow(label, pct, hint, sub) {
        const row = el('div', `hrw-bar-row${sub ? ' is-sub' : ''}`);
        const head = el('div', 'hrw-bar-head');
        head.append(el('span', null, label), el('strong', null, isNum(pct) ? `P${fmt(pct, 0)}` : '—'));
        const track = el('div', 'hrw-bar-track');
        const fill = el('i');
        fill.style.width = `${isNum(pct) ? pct : 0}%`;
        track.appendChild(fill);
        row.append(head, track);
        if (hint) row.appendChild(el('small', null, hint));
        return row;
    }

    function fact(dl, term, value) {
        const wrap = el('div');
        wrap.append(el('dt', null, term), el('dd', null, value));
        dl.appendChild(wrap);
    }

    function updateInspector() {
        renderPoiDetails();
        const cell = state.cells[state.selected];
        if (!cell) {
            ui.place.textContent = '未匹配评分网格';
            ui.coords.textContent = '';
            ui.score.textContent = '—';
            ui.levelChip.textContent = '网格数据缺失';
            ui.levelChip.style.backgroundColor = NODATA_FILL;
            ui.levelChip.style.color = '#2A2620';
            ui.scoreSub.textContent = '仅供县级天气参考，不据此判定该点低风险。';
            levelChip(ui.dailyChip, null);
            ui.breakdown.replaceChildren();
            ui.facts.replaceChildren();
            ui.provenance.replaceChildren();
            return;
        }
        const town = state.townships[cell.township];
        const near = nearestVillage(cell);
        ui.place.textContent = `${town ? town.properties.name_zh : '都昌县'}${near ? ` · ${near.village.name} 附近` : ''}`;
        ui.coords.textContent = `${cell.lon.toFixed(5)}°E · ${cell.lat.toFixed(5)}°N（网格中心，WGS84）`;

        if (!cell.land) {
            ui.score.textContent = '湖面';
            ui.levelChip.textContent = '不评分';
            ui.levelChip.style.backgroundColor = WATER_FILL;
            ui.levelChip.style.color = '#10324a';
            ui.scoreSub.textContent = '近似永久水域 ≥ 50%，不参与评分与排名。';
            ui.dailyChip.textContent = '—';
            ui.dailyChip.style.backgroundColor = '';
        } else if (!cell.scored) {
            ui.score.textContent = '—';
            ui.levelChip.textContent = '无常住人口';
            ui.levelChip.style.backgroundColor = NODATA_FILL;
            ui.levelChip.style.color = '#2A2620';
            ui.scoreSub.textContent = 'ASPECT 模型显示该格无常住人口，不参与评分。';
            ui.dailyChip.textContent = '—';
            ui.dailyChip.style.backgroundColor = '';
        } else {
            ui.score.textContent = fmt(cell.score, 0);
            levelChip(ui.levelChip, cell.level);
            const span = cell.top_pct_p95 - cell.top_pct_p05;
            const stable = span <= state.meta.stability.stable_span_pct;
            ui.scoreSub.textContent = `全县前 ${fmt(cell.top_pct, 0)}%；权重扰动区间 前 ${fmt(cell.top_pct_p05, 0)}–${fmt(cell.top_pct_p95, 0)}%，${stable ? '排名稳定' : '排名对权重较敏感'}`;
            levelChip(ui.dailyChip, combineDailyLevel(currentHazard(), cell.level), demoPrefix());
        }

        const facility = state.facilities[cell.facility];
        ui.breakdown.replaceChildren(
            el('div', 'hrw-breakdown-title', '风险构成（全县百分位）'),
            barRow('危险性 · 地表温度', cell.hazard_pct, `${fmt(cell.p.q3_lst_c_mean, 1)} °C`),
            barRow('暴露 · 65+ 比例', cell.exposure_pct, `${fmt(cell.p.age65_share_pct, 1)}%`),
            barRow('脆弱性', cell.vulnerability_pct, null),
            barRow('树荫缺口', cell.shade_deficit_pct, `树木覆盖 ${fmt(cell.p.tree_cover_pct, 0)}%`, true),
            barRow('建成区', cell.built_pct, `${fmt(cell.p.built_up_pct, 0)}%`, true),
            barRow('可达性参考点距离', cell.access_pct, `${fmt(cell.facility_km, 1)} km`, true)
        );

        ui.facts.replaceChildren();
        const hotspotText = cell.gi_bin >= 2 ? '强显著热点（q<0.01）'
            : cell.gi_bin === 1 ? '显著热点（q<0.05）'
                : cell.gi_bin <= -1 ? '显著冷点' : '不显著';
        fact(ui.facts, '统计热点', cell.scored ? `${hotspotText} · z = ${fmt(cell.gi_z, 2)}` : '—');
        fact(ui.facts, '模型可达性参考点', facility ? `${referenceName(facility)} · ${fmt(cell.facility_km, 1)} km（直线）` : '—');
        fact(ui.facts, '近似永久水域', `${fmt(cell.p.permanent_water_pct, 1)}%`);
        fact(ui.facts, '表面高程', `${fmt(cell.p.mean_elevation_m, 0)} m`);
        fact(ui.facts, 'Q3 合格观测', `${cell.p.q3_dates} / ${cell.p.local_available_dates} 天`);

        ui.provenance.replaceChildren();
        fact(ui.provenance, 'cell ID', cell.id);
        fact(ui.provenance, 'MODIS row / col', `${cell.row} / ${cell.col}`);
        fact(ui.provenance, '乡镇归属', cell.townshipMethod === 'polygon' ? 'OSM 多边形' : '最近乡镇驻地（多边形缝隙）');
        fact(ui.provenance, '显示几何', '中心保持正交近似格');
    }

    function selectCell(index, options) {
        if (index < 0 || index >= state.cells.length) return;
        state.selected = index;
        if (!options || !options.keepPoi) {
            const previous = state.selectedPoiId;
            state.selectedPoiId = null;
            if (state.poiMarkers.has(previous)) state.poiMarkers.get(previous).setStyle(poiStyle(state.poiById.get(previous)));
        }
        updateInspector();
        drawSelection();
        updateUrl();
        if (options && options.zoom && state.map) {
            const cell = state.cells[index];
            state.map.flyTo(toMap(cell.lon, cell.lat), Math.max(state.map.getZoom(), 13), {duration: 0.6});
        }
    }

    function selectVillage(village) {
        selectPoi(poiId(village, 'villages'), {reveal: true});
    }

    function selectPoi(id, options = {}) {
        const entry = state.poiById.get(id);
        if (!entry) return;
        const previous = state.selectedPoiId;
        state.selectedPoiId = id;
        syncSelectedPoi();
        [previous, id].forEach((key) => {
            if (state.poiMarkers.has(key)) state.poiMarkers.get(key).setStyle(poiStyle(state.poiById.get(key)));
        });
        updateInspector();
        drawSelection();
        updateUrl();
        if (state.map && options.pan !== false) {
            state.map.flyTo(toMap(entry.point.lon_wgs84, entry.point.lat_wgs84), Math.max(state.map.getZoom(), 14), {duration: 0.6});
        }
        if (options.reveal && window.matchMedia && window.matchMedia('(max-width: 1280px)').matches) {
            ui.poiDetails.scrollIntoView({behavior: 'smooth', block: 'start'});
        }
    }

    function syncSelectedPoi() {
        if (!state.selectedPoiId) return;
        const entry = state.poiById.get(state.selectedPoiId);
        if (!entry || !matchesTown(entry.point)) {
            state.selectedPoiId = null;
            state.selected = -1;
            return;
        }
        const point = entry.point;
        const index = point.risk_data_status !== 'no_grid_data' && point.cell_id ? state.cellById.get(point.cell_id) : undefined;
        state.selected = index === undefined ? -1 : index;
    }

    function safeSourceUrl(value) {
        try {
            const url = new URL(value);
            return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
        } catch (_) { return null; }
    }

    function sourceLink(label, value) {
        const url = safeSourceUrl(value);
        if (!url) return el('span', null, `${label || '来源'}（链接待补充）`);
        const link = el('a', null, label || '查看来源');
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        return link;
    }

    function resourceEvidenceRows(point, includeDate = true) {
        // 历史报道只能说明当时的服务对象和设施，不能替代当前开放或空调核验。
        const rows = [];
        if (point.audience_hint) rows.push(['服务对象（历史资料）', `${point.audience_hint}；当前接待对象及进入条件待核实`]);
        if (point.facilities_hint) rows.push(['历史设施（不代表当前可用）', point.facilities_hint]);
        const date = point.official_source_date || point.source_date;
        if (includeDate && date) rows.push(['服务/设施资料日期', date]);
        const notes = (Array.isArray(point.notes) ? point.notes : [point.notes])
            .filter((note) => typeof note === 'string' && note.trim() && note !== point.verification_note);
        if (notes.length) rows.push(['资料补充说明', notes.join('；')]);
        return rows;
    }

    function appendResourceSources(target, point, fallbackLabel) {
        const append = (label, url) => {
            if (target.children.length) target.appendChild(el('span', null, ' · '));
            target.appendChild(sourceLink(label, url));
        };
        if (point.official_source_url) append('官方服务报道来源', point.official_source_url);
        const coordinateUrl = point.coordinate_source_url || (point.official_source_url && point.source_url !== point.official_source_url ? point.source_url : null);
        if (coordinateUrl) append(`坐标来源 · ${!fallbackLabel || fallbackLabel === '查看列名来源' ? '公开地图' : fallbackLabel}`, coordinateUrl);
        else if (!point.official_source_url) append(fallbackLabel || '查看列名来源', point.source_url);
    }

    function renderPoiDetails() {
        const entry = state.poiById.get(state.selectedPoiId);
        if (!entry) {
            ui.poiDetails.replaceChildren(el('h2', null, '点位详情'), el('p', 'hrw-empty', '点击地图点位，或搜索村、医疗机构和避暑候选，查看来源与待核验信息。'));
            return;
        }
        const p = entry.point;
        const details = el('dl', 'hrw-facts');
        fact(details, '点位类型', POI_KINDS[entry.kind]);
        fact(details, '乡镇归属', p.township || '待核验');
        if (!p.township && p.source_township) fact(details, '来源中的乡镇', `${p.source_township}（归属待核验）`);
        const precision = {mapped_point: '公开来源点位，待现场核验', building_centroid: '建筑轮廓中心近似位置', exact: '来源标注位置，仍需现场核验', approximate: '近似坐标', settlement_centroid: '聚落参考位置', unknown: '精度待核验', township_seat: '乡镇驻地近似位置'};
        fact(details, '坐标精度', precision[p.coordinate_precision] || p.coordinate_precision || '待核验');
        fact(details, '坐标', `${p.lon_wgs84.toFixed(5)}°E · ${p.lat_wgs84.toFixed(5)}°N（WGS84）`);
        const verification = {publicly_listed: '公开来源收录，待现场核验', needs_review: '资料待复核', unmapped: '坐标待核验', verified: '有核验记录', unverified: '待核验', pending: '待核验', source_only: '仅来源记录，待现场核验', source_verified: '来源已核对，待现场核验'};
        fact(details, '核验状态', verification[p.verification_status] || p.verification_status || '待核验');
        if (p.township_warning) fact(details, '乡镇核验提示', p.township_warning);
        if (p.verification_note) fact(details, '核验说明', p.verification_note);
        if (entry.kind === 'villages') {
            fact(details, '聚落级别', {administrative_village: '行政村', natural_village: '自然村', hamlet: '小聚落（行政级别待核验）', village: '村落（行政级别待核验）'}[p.settlement_level] || '行政村 / 自然村级别待核验');
            fact(details, '人口', isNum(p.population) ? `${fmt(p.population, 0)} 人` : '待补充（未知）');
            fact(details, '老人比例', isNum(p.elderly_ratio) ? `${fmt(p.elderly_ratio * 100, 1)}%` : '待补充（未知）');
            fact(details, '距已收录医疗机构', isNum(p.nearest_medical_km) ? `${fmt(p.nearest_medical_km, 1)} km（直线）` : '暂无可用记录，不代表附近无机构');
            if (!isLevel(p.static_level)) fact(details, '风险数据', '网格数据缺失，仅供县级天气参考');
        } else {
            fact(details, '开放情况', p.open_hours ? `来源记录时段：${p.open_hours}；当前开放情况待核实` : '开放情况待核实');
            if (p.coordinate_verification_expired) fact(details, '坐标核验有效性', '核验已过期，已降为候选；位置与当前开放情况需重新核验');
            if (entry.kind === 'candidates') fact(details, '使用边界', '候选场所，尚不能作为已开放避暑点安排前往');
            resourceEvidenceRows(p).forEach(([label, value]) => fact(details, label, value));
            fact(details, '空调', p.has_ac === true ? '来源记录有空调，使用前核实' : p.has_ac === false ? '来源记录无空调，待复核' : '待核实');
            fact(details, '无障碍', p.is_accessible === true ? '来源记录可达，使用前核实' : p.is_accessible === false ? '来源记录不具备，待复核' : '待核实');
        }
        if (p.potential_duplicate) fact(details, '同名核验', '疑似同名点，暂不参与巡访排序，待核验；当前分别保留来源记录，不视为两个已核验行政村');
        if (p.priority_eligible === false && p.priority_exclusion_reason) fact(details, '巡访排序', p.priority_exclusion_reason);
        if (p.source_updated_at) fact(details, '来源更新时间', p.source_updated_at);
        if (p.coordinate_checked_at) fact(details, '坐标来源查询日期', `${p.coordinate_checked_at}（资料查询，非现场核验）`);
        if (p.verified_at) fact(details, '核验记录时间', p.verified_at);
        if (p.valid_until) fact(details, '核验有效期至', p.valid_until);
        const source = el('p', 'hrw-poi-source');
        appendResourceSources(source, p, ({geonames: 'GeoNames', osm: 'OpenStreetMap', 'amap-public': '高德地图公开页面', 'baidu-public': '百度地图公开页面', 'tencent-public': '腾讯地图公开页面'})[p.source_label] || p.source_label || '点位来源');
        ui.poiDetails.replaceChildren(el('span', 'hrw-pill', POI_KINDS[entry.kind]), el('h2', null, p.name), source, details);
        const related = p.potential_duplicate_ids || p.related_ids || p.possible_duplicate_ids || p.duplicate_ids || [];
        if (p.potential_duplicate && Array.isArray(related)) related.forEach((id) => {
            const other = state.poiById.get(id);
            if (!other) return;
            const button = el('button', 'hrw-link-button', `查看同名记录：${other.point.name} · ${other.point.township || '乡镇待核验'}`);
            button.type = 'button';
            button.addEventListener('click', () => selectPoi(id, {reveal: true}));
            ui.poiDetails.appendChild(button);
        });
    }

    function coverageRows() {
        const available = new Map((state.daily ? state.daily.poi_coverage : []).map((row) => [row.township, row]));
        return state.townships.map((town) => {
            const name = town.properties.name_zh;

            const entries = Array.from(state.poiById.values()).filter((entry) => entry.point.township === name);
            const count = (kind) => entries.filter((entry) => entry.kind === kind).length;
            return {...(available.get(name) || {unmapped_medical_count: 0, unmapped_cooling_count: 0, completeness: 'unknown'}),
                township: name, settlement_count: count('villages'), medical_count: count('medical'),
                cooling_candidate_count: count('candidates'), cooling_verified_count: count('cooling')};
        });
    }

    function renderPoiCoverage() {
        const rows = coverageRows();
        ui.townFilter.replaceChildren(el('option', null, '全县 · 24 乡镇'));
        ui.townFilter.children[0].value = '';
        rows.forEach((row) => {
            const option = el('option', null, `${row.township} · ${row.settlement_count} 个聚落点`);
            option.value = row.township;
            ui.townFilter.appendChild(option);
        });
        const unknown = Array.from(state.poiById.values()).filter((entry) => !entry.point.township).length;
        const unknownDirectory = (state.daily ? state.daily.unmapped_resources : []).filter((point) => !point.township_name).length;
        if (unknown || unknownDirectory) {
            const option = el('option', null, `乡镇归属待核验 · ${unknown} 地图点 / ${unknownDirectory} 条待定位资料`);
            option.value = '__unknown__';
            ui.townFilter.appendChild(option);
        }
        ui.townFilter.value = state.townFilter;
        ui.poiCoverageBody.replaceChildren();
        rows.forEach((row) => {
            const tr = el('tr');
            const cell = el('td');
            const button = el('button', 'hrw-link-button', row.township);
            button.type = 'button';
            button.addEventListener('click', () => setTownFilter(row.township, true));
            cell.appendChild(button);
            tr.append(cell, ...['settlement_count', 'medical_count', 'cooling_candidate_count', 'cooling_verified_count', 'unmapped_medical_count', 'unmapped_cooling_count', 'needs_review_count'].map((key) => el('td', null, String(row[key] || 0))), el('td', null, '未知'));
            ui.poiCoverageBody.appendChild(tr);
        });
        const metadata = state.daily && state.daily.poi_metadata;
        ui.poiCoverageNote.textContent = `${metadata && metadata.coverage_note || '这里比较点位收录数量，不是行政村完整覆盖率。0 表示尚未收录，不代表当地没有村或机构。'}${unknown || unknownDirectory ? ` 另有 ${unknown} 个地图点、${unknownDirectory} 条待定位资料的乡镇归属待核验。` : ''}`;
        ui.poiSources.replaceChildren();
        (metadata && Array.isArray(metadata.sources) ? metadata.sources : []).forEach((source) => {
            const li = el('li');
            li.append(sourceLink(source.name || source.id, source.url), el('span', null, source.license ? ` · ${source.license}` : ''));
            ui.poiSources.appendChild(li);
        });
        if (metadata && metadata.retrieved_at) ui.poiSources.appendChild(el('li', null, `资料收录时间：${metadata.retrieved_at}；不等于现场核验日期。`));
        ui.inventoryNote.textContent = metadata && metadata.inventory_note || '待定位目录可能与地图点位重叠，不能相加作为机构总数；历史列名或拟注销记录需另行复核。';
        renderPoiCounts();
        renderUnmappedResources();
    }

    function renderUnmappedResources() {
        const records = (state.daily ? state.daily.unmapped_resources : []).filter((point) => matchesTown({township: point.township_name}));
        ui.unmappedSummary.textContent = `当前范围已收录但待定位的资源（${records.length}）`;
        ui.unmappedList.replaceChildren();
        if (!records.length) ui.unmappedList.appendChild(el('p', 'hrw-empty', '暂无待定位资源收录记录，不代表没有相关场所。'));
        records.slice(0, state.unmappedListLimit).forEach((point) => {
            const item = el('li', 'hrw-unmapped-item');
            item.append(el('strong', null, point.name), el('span', null, `${point.township_name || '乡镇待核验'} · 坐标待核验，不上图、不计算距离`));
            const status = {proposed_cancellation: '来源为拟注销记录，不能视为正常营业机构', listed_name_requires_review: '历史列名待复核，不能据此确认营业', historical_cooling_report: '历史避暑报道，当前开放情况待核实'}[point.official_status];
            if (status || point.verification_status === 'needs_review') item.appendChild(el('span', 'hrw-resource-review', status || '列名资料待复核，未计入医疗机构收录数'));
            resourceEvidenceRows(point, false).forEach(([label, value]) => item.appendChild(el('span', null, `${label}：${value}`)));
            if (point.verification_note) item.appendChild(el('span', null, `核验说明：${point.verification_note}`));
            if (point.address) {
                const historical = point.address_status === 'historical_address_unverified_current';
                item.appendChild(el('span', null, `${historical ? '历史地址' : '来源地址'}：${point.address}；现址待核验`));
                item.appendChild(el('small', null, `地址资料日期：${point.address_source_date || '待补充'}（与列名日期分开）`));
                if (point.address_source_url) item.appendChild(sourceLink('查看地址来源', point.address_source_url));
            }
            const source = el('span', 'hrw-poi-source');
            appendResourceSources(source, point, point.coordinate_source_url ? '公开地图' : '查看列名来源');
            item.appendChild(source);
            const evidenceDate = point.official_source_date || point.source_date;
            if (evidenceDate) item.appendChild(el('small', null, `${point.official_source_url ? '官方服务/设施资料日期' : '列名资料日期'}：${evidenceDate}；当前开放情况待核实`));
            else item.appendChild(el('small', null, '资料日期待补充；当前开放情况待核实'));
            if (point.coordinate_checked_at) item.appendChild(el('small', null, `坐标来源查询日期：${point.coordinate_checked_at}（资料查询，非现场核验）`));
            ui.unmappedList.appendChild(item);
        });
        ui.unmappedMore.hidden = records.length <= state.unmappedListLimit;
    }

    function renderPoiCounts() {
        const entries = Array.from(state.poiById.values()).filter((entry) => matchesTown(entry.point));
        ui.poiCounts.replaceChildren();
        Object.entries(POI_KINDS).forEach(([kind, label]) => {
            const count = entries.filter((entry) => entry.kind === kind).length;
            const card = el('div', 'hrw-poi-count');
            card.append(el('strong', null, String(count)), el('span', null, `${kind === 'medical' ? '地图医疗机构' : label}${count ? '' : ' · 暂未收录'}`));
            ui.poiCounts.appendChild(card);
        });
        const unmapped = (state.daily ? state.daily.unmapped_resources : []).filter((point) => matchesTown({township: point.township_name}));
        const reviewCount = unmapped.filter((point) => point.verification_status === 'needs_review').length;
        ui.poiInventoryHint.textContent = `地图数量仅统计有坐标点位。当前范围另有 ${unmapped.length} 条待定位资料${reviewCount ? `，其中 ${reviewCount} 条列名待复核` : ''}；可能与地图点位重叠，不能相加作为机构总数。`;
        ui.poiListTitle.textContent = `浏览当前范围点位（${entries.length}）`;
        ui.poiList.replaceChildren();
        const order = {medical: 0, candidates: 1, cooling: 2, villages: 3};
        entries.sort((a, b) => order[a.kind] - order[b.kind] || a.point.name.localeCompare(b.point.name, 'zh-CN') || a.id.localeCompare(b.id));
        entries.slice(0, state.poiListLimit).forEach((entry) => {
            const button = el('button', 'hrw-poi-list-item');
            button.type = 'button';
            button.dataset.poiId = entry.id;
            button.append(el('strong', null, entry.point.name), el('span', null, `${entry.point.township || '乡镇归属待核验'} · ${POI_KINDS[entry.kind]}${entry.kind === 'candidates' ? ' · 开放待核实' : ''}`));
            button.addEventListener('click', () => selectPoi(entry.id, {reveal: true}));
            ui.poiList.appendChild(button);
        });
        if (!entries.length) ui.poiList.appendChild(el('p', 'hrw-empty', '当前范围暂无收录点位，不代表没有村落或机构。'));
        ui.poiMore.hidden = entries.length <= state.poiListLimit;
    }

    function setTownFilter(name, zoom) {
        state.townFilter = name;
        state.poiListLimit = 20;
        state.unmappedListLimit = 20;
        ui.townFilter.value = name;
        syncSelectedPoi();
        renderVillages();
        renderPoiCounts();
        renderUnmappedResources();
        buildSearchOptions();
        renderPriority();
        updateHeader();
        updateInspector();
        drawSelection();
        updateUrl();
        if (zoom && name) {
            const town = state.townships.find((feature) => feature.properties.name_zh === name);
            if (town) zoomToTownship(town);
        } else if (zoom && state.map) state.map.flyToBounds(state.countyBounds, {padding: [16, 16], duration: 0.6});
    }

    // ------------------------------------------------------------------
    // 预报、清单、行动卡
    // ------------------------------------------------------------------
    function renderDays() {
        ui.days.replaceChildren();
        ui.forecastSource.textContent = forecastSourceText();
        if (!state.daily || !state.daily.days.length) {
            ui.days.appendChild(el('p', 'hrw-empty', `${UNKNOWN_RISK}：地图显示静态综合风险。`));
            return;
        }
        state.daily.days.forEach((day, index) => {
            const button = el('button', 'hrw-day');
            button.type = 'button';
            button.setAttribute('role', 'tab');
            button.setAttribute('aria-selected', String(index === state.dayIndex));
            const hazard = dayHazard(day);
            const info = isLevel(hazard) ? levelInfo(hazard) : {color: NODATA_FILL};
            const swatch = el('i');
            swatch.style.backgroundColor = info.color;
            const levelText = el('strong');
            levelText.append(swatch, document.createTextNode(isLevel(hazard) ? `${demoPrefix()}${hazard} 级 ${day.label || levelInfo(hazard).label}` : UNKNOWN_RISK));
            const temps = `${fmt(day.temperature_max, 0)}° / ${fmt(day.temperature_min, 0)}°`;
            button.append(
                el('span', 'hrw-day-date', dayLabel(day.date)),
                el('span', 'hrw-day-flag', isLevel(hazard) && day.escalated ? (day.hot_night ? '热夜 +1' : '热浪 +1') : ''),
                levelText,
                el('span', 'hrw-day-temp', temps)
            );
            button.title = isLevel(hazard) ? `${demoPrefix()}${dayReasons(day).join('；') || (hazard === 0 ? '无高温' : day.label || '热危险')}` : UNKNOWN_RISK;
            button.addEventListener('click', () => setDay(index));
            ui.days.appendChild(button);
        });
    }

    function setDay(index) {
        state.dayIndex = index;
        syncDailyLayer();
        Array.from(ui.days.querySelectorAll('.hrw-day')).forEach((button, k) => button.setAttribute('aria-selected', String(k === index)));
        restyleCells();
        renderVillages();
        renderPriority();
        renderLegend();
        updateInspector();
        updateHeader();
        updateUrl();
    }

    function priorityForDay() {
        if (!state.daily || !isLevel(currentHazard())) return [];
        const entry = state.daily.priority[state.dayIndex];
        if (!entry || !Array.isArray(entry.villages) || entry.date !== currentDay().date) return [];
        const ranked = new Map(entry.villages.map((point) => [poiId(point, 'villages'), point]));
        const ids = Array.isArray(entry.village_ids) ? entry.village_ids : Array.from(ranked.keys());
        return ids.map((id) => {
            const record = state.poiById.get(id);
            const point = record ? {...ranked.get(id), ...record.point} : ranked.get(id);
            if (!point || !matchesTown(point) || !isLevel(point.static_level) || point.potential_duplicate || point.priority_eligible === false) return null;
            const reasons = [`静态风险分 ${fmt(point.static_score, 0)}`];
            if (isNum(point.nearest_medical_km)) reasons.push(`距已收录医疗机构 ${fmt(point.nearest_medical_km, 1)} km（直线）`);
            const displayReasons = Array.isArray(point.reasons) ? point.reasons.slice() : reasons;
            return {...point, daily_level: villageDailyLevel(point), reasons: displayReasons};
        }).filter((point) => point && isLevel(point.daily_level)).slice(0, 5);
    }

    function renderPriority() {
        ui.priorityList.replaceChildren();
        const villages = priorityForDay();
        const known = isLevel(currentHazard());
        ui.priorityNote.textContent = known ? `${demoPrefix()}${state.townFilter && state.townFilter !== '__unknown__' ? state.townFilter + ' · ' : ''}按当日风险、静态风险分、已知老人数排序；人口未知不记为 0，疑似同名点待核验后再参与排序。` : '仅保留静态风险参考，天气恢复后生成当日巡访顺序。';
        if (!villages.length) {
            ui.priorityList.appendChild(el('li', 'hrw-empty', !known ? UNKNOWN_RISK : '当前范围暂无可参与风险排序的村点；可在点位列表查看已收录资料。'));
        }
        villages.forEach((village, index) => {
            const item = el('li');
            const button = el('button', 'hrw-priority-item');
            button.type = 'button';
            const chip = el('span', 'hrw-level-chip hrw-level-chip--small');
            levelChip(chip, village.daily_level, demoPrefix());
            const head = el('div', 'hrw-priority-head');
            head.append(el('span', 'hrw-rank', String(index + 1)), el('strong', null, village.name), chip);
            button.append(head, el('span', 'hrw-priority-town', village.township || ''), el('span', 'hrw-priority-reasons', (village.reasons || []).join(' · ')));
            button.addEventListener('click', () => selectVillage(village));
            item.appendChild(button);
            ui.priorityList.appendChild(item);
        });
        renderActionCard(villages.length ? villages[0].daily_level : currentHazard());
    }

    function renderActionCard(level) {
        const cards = (state.daily && state.daily.action_cards) || state.meta.action_cards || {};
        const card = isLevel(level) && isLevel(currentHazard()) ? cards[String(level)] : null;
        if (!card) {
            ui.actionCard.replaceChildren(
                el('div', 'hrw-action-title', UNKNOWN_RISK),
                el('p', 'hrw-empty', '有效天气预报恢复前，暂不生成当日分级行动卡。静态风险、村点和避暑点仍可查阅。')
            );
            return;
        }
        const title = el('div', 'hrw-action-title');
        const chip = el('span', 'hrw-level-chip hrw-level-chip--small');
        levelChip(chip, level, demoPrefix());
        title.append(el('strong', null, `行动卡 · ${card.title}`), chip);
        const doctor = el('ul');
        card.doctor.forEach((line) => doctor.appendChild(el('li', null, line)));
        const caregiver = el('ul');
        card.caregiver.forEach((line) => caregiver.appendChild(el('li', null, line)));
        ui.actionCard.replaceChildren(title, el('h3', null, '医生'), doctor, el('h3', null, '家属与照护者'), caregiver);
    }

    function updateHeader() {
        const day = currentDay();
        if (ui.metaDay) ui.metaDay.textContent = day ? dayLabel(day.date) : '预报不可用';
        if (!isLevel(currentHazard())) {
            ui.title.textContent = UNKNOWN_RISK;
            ui.lede.textContent = '有效天气预报暂不可用，地图显示静态综合热风险分；暂不生成当日等级和巡访顺序。';
            return;
        }
        const top = priorityForDay()[0];
        const when = day.date === localDate() ? '今天' : dayLabel(day.date);
        ui.title.textContent = `${demoPrefix()}${when}先去哪几个村`;
        const reasons = dayReasons(day).length ? `（${dayReasons(day).join('，')}）` : '';
        if (!day.level) {
            ui.lede.textContent = `${demoPrefix()}${when}都昌无高温（最高 ${fmt(day.temperature_max, 0)} °C），按常规随访。${top ? `静态风险最高的已收录聚落点：${top.name}（综合分 ${fmt(top.static_score, 0)}）。` : ''}`;
            return;
        }
        ui.lede.textContent = `${demoPrefix()}${when}都昌热危险 ${day.level} 级 · ${day.label}${reasons}。${top ? `优先：${top.name}（当日 ${top.daily_level} 级）。` : ''}`;
    }

    // ------------------------------------------------------------------
    // 乡镇表、方法、搜索、打印
    // ------------------------------------------------------------------
    function renderTownTable() {
        const rows = state.townships.slice().sort((a, b) => (b.properties.p90_score || 0) - (a.properties.p90_score || 0));
        ui.townBody.replaceChildren();
        rows.forEach((feature) => {
            const p = feature.properties;
            const tr = el('tr');
            const nameCell = el('td');
            const button = el('button', 'hrw-link-button', p.name_zh);
            button.type = 'button';
            button.addEventListener('click', () => zoomToTownship(feature));
            nameCell.appendChild(button);
            tr.append(
                nameCell,
                el('td', null, fmt(p.p90_score, 0)),
                el('td', null, fmt(p.mean_score, 0)),
                el('td', null, String(p.high_cells)),
                el('td', null, String(p.hotspot_cells)),
                el('td', null, `${p.scored_cells} / ${p.cells}`)
            );
            ui.townBody.appendChild(tr);
        });
    }

    function zoomToTownship(feature) {
        if (!state.map) return;
        const layer = window.L.geoJSON(feature, {coordsToLatLng: (c) => window.L.latLng(...toMap(c[0], c[1]))});
        state.map.flyToBounds(layer.getBounds(), {padding: [24, 24], duration: 0.6});
        ui.map.scrollIntoView({behavior: 'smooth', block: 'center'});
    }

    function renderMethod() {
        ui.matrixBody.replaceChildren();
        state.meta.daily.hazard_levels.forEach((hazard) => {
            const tr = el('tr');
            tr.appendChild(el('th', null, `${hazard.level} ${hazard.label}`));
            [1, 2, 3].forEach((staticLevel) => {
                const level = combineDailyLevel(hazard.level, staticLevel);
                const td = el('td', null, `${level} 级`);
                td.style.backgroundColor = levelInfo(level).color;
                td.style.color = level >= 3 ? '#fff' : '#2A2620';
                tr.appendChild(td);
            });
            ui.matrixBody.appendChild(tr);
        });
        ui.sources.replaceChildren();
        state.meta.sources.forEach((source) => {
            const li = el('li');
            const link = el('a', null, source.citation);
            link.href = source.url;
            link.target = '_blank';
            link.rel = 'noreferrer';
            li.append(el('span', null, `${source.topic}：`), link);
            ui.sources.appendChild(li);
        });
        ui.build.textContent = `风险分数据构建：${state.meta.generated_at_utc} · schema ${state.meta.schema_version} · 评分网格 ${state.meta.counts.scored_cells} · 湖面屏蔽 ${state.meta.counts.water_masked_cells}`;
    }

    function buildSearchOptions() {
        ui.searchOptions.replaceChildren();
        state.searchByLabel.clear();
        const entries = Array.from(state.poiById.values()).filter((entry) => matchesTown(entry.point)).sort((a, b) => a.id.localeCompare(b.id));
        entries.forEach((entry) => {
            let label = `${entry.point.name} · ${entry.point.township || '乡镇待核验'} · ${POI_KINDS[entry.kind]}`;
            const base = label;
            let suffix = 2;
            while (state.searchByLabel.has(label)) { label = `${base}（${suffix}）`; suffix += 1; }
            state.searchByLabel.set(label, entry.id);
            const option = el('option');
            option.value = label;
            option.dataset.poiId = entry.id;
            ui.searchOptions.appendChild(option);
        });
        state.townships.forEach((town) => {
            const option = el('option');
            option.value = town.properties.name_zh;
            ui.searchOptions.appendChild(option);
        });
        ui.searchNote.textContent = '同名点按乡镇区分；放大地图后显示局部名称，也可从点位列表选择。';
    }

    function runSearch() {
        const query = ui.search.value.trim();
        if (!query) return;
        const exact = state.searchByLabel.get(query);
        if (exact) { selectPoi(exact, {reveal: true}); return; }
        const town = state.townships.find((feature) => feature.properties.name_zh === query);
        if (town) { setTownFilter(query, true); return; }
        const matches = Array.from(state.poiById.values()).filter((entry) => matchesTown(entry.point) && entry.point.name.includes(query));
        if (matches.length === 1) { selectPoi(matches[0].id, {reveal: true}); return; }
        ui.searchNote.textContent = matches.length ? `找到 ${matches.length} 个同名或近似点，请从建议列表选择包含乡镇的完整名称。` : '当前范围未找到；可切换全县或查看乡镇收录表。';
    }

    function printSheet() {
        syncDailyLayer();
        renderPriority();
        const day = currentDay();
        const villages = priorityForDay();
        const sheet = ui.printSheet;
        sheet.replaceChildren();
        sheet.appendChild(el('h1', null, `都昌县热风险巡访单 · ${day ? dayLabel(day.date) : '静态风险'}`));
        if (isLevel(currentHazard())) {
            sheet.appendChild(el('p', null, `${demoPrefix()}热危险 ${day.level} 级 ${day.label}；最高 ${fmt(day.temperature_max, 0)} °C，最低 ${fmt(day.temperature_min, 0)} °C。${dayReasons(day).join('；')}`));
        } else {
            sheet.appendChild(el('p', null, `${UNKNOWN_RISK}，暂不生成当日等级和巡访顺序。`));
        }
        sheet.appendChild(el('p', null, forecastSourceText()));
        const table = el('table');
        const head = el('tr');
        ['序号', '村', '乡镇', '当日等级', '理由', '已巡访 / 备注'].forEach((h) => head.appendChild(el('th', null, h)));
        table.appendChild(head);
        villages.forEach((village, index) => {
            const tr = el('tr');
            [String(index + 1), village.name, village.township || '', `${village.daily_level} 级`, village.reasons.join('；'), ''].forEach((text) => tr.appendChild(el('td', null, text)));
            table.appendChild(tr);
        });
        sheet.appendChild(table);
        sheet.appendChild(ui.actionCard.cloneNode(true));
        sheet.appendChild(el('p', 'hrw-print-foot', `生成于 ${new Date().toLocaleString('zh-CN')} · 宜老天气通热风险工作台 · 排序为辅助判断，需结合实地情况`));
        window.print();
    }

    function updateUrl() {
        if (!window.history || !window.history.replaceState) return;
        const url = new URL(window.location.href);
        url.searchParams.set('layer', state.layer);
        url.searchParams.set('day', String(state.dayIndex));
        if (state.selected >= 0) url.searchParams.set('cell', state.cells[state.selected].id);
        else url.searchParams.delete('cell');
        if (state.selectedPoiId) url.searchParams.set('poi', state.selectedPoiId);
        else url.searchParams.delete('poi');
        if (state.townFilter) url.searchParams.set('town', state.townFilter);
        else url.searchParams.delete('town');
        window.history.replaceState({}, '', url);
    }

    function setLayer(layer) {
        if (layer === 'daily' && !isLevel(currentHazard())) layer = 'score';
        state.layer = layer;
        ui.layerTabs.forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.layer === layer)));
        ui.moreLayers.value = ui.layerTabs.some((b) => b.dataset.layer === layer) ? '' : layer;
        if (state.swipe) setSwipe(false);
        restyleCells();
        renderLegend();
        updateUrl();
    }

    function bindEvents() {
        ui.townFilter.addEventListener('change', () => setTownFilter(ui.townFilter.value, true));
        ui.poiMore.addEventListener('click', () => { state.poiListLimit += 20; renderPoiCounts(); });
        ui.unmappedMore.addEventListener('click', () => { state.unmappedListLimit += 20; renderUnmappedResources(); });
        ui.layerTabs.forEach((button) => button.addEventListener('click', () => setLayer(button.dataset.layer)));
        ui.moreLayers.addEventListener('change', () => {
            if (ui.moreLayers.value) setLayer(ui.moreLayers.value);
        });
        ui.basemapButtons.forEach((button) => button.addEventListener('click', () => setBasemap(button.dataset.basemap)));
        ui.opacity.addEventListener('input', () => {
            state.opacity = Number(ui.opacity.value) / 100;
            ui.opacityValue.textContent = `${ui.opacity.value}%`;
            restyleCells();
        });
        ui.overlayInputs.forEach((input) => input.addEventListener('change', () => {
            state.overlays[input.dataset.overlay] = input.checked;
            applyOverlayVisibility();
        }));
        ui.swipe.addEventListener('click', () => setSwipe(!state.swipe));
        ui.measure.addEventListener('click', () => setMeasure(!state.measuring));
        ui.reset.addEventListener('click', () => state.map && state.map.flyToBounds(state.countyBounds, {padding: [16, 16], duration: 0.6}));
        ui.search.addEventListener('change', runSearch);
        ui.search.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') runSearch();
        });
        ui.print.addEventListener('click', printSheet);
        ui.controlsToggle.addEventListener('click', () => {
            const open = !app.classList.contains('hrw-controls-open');
            app.classList.toggle('hrw-controls-open', open);
            ui.controlsToggle.setAttribute('aria-expanded', String(open));
            ui.controlsToggle.setAttribute('aria-pressed', String(open));
        });
        const toggleLegend = () => {
            const collapsed = ui.legend.classList.toggle('is-collapsed');
            ui.legend.setAttribute('aria-expanded', String(!collapsed));
        };
        ui.legend.addEventListener('click', (event) => {
            if (event.target.closest('button, a')) return;
            toggleLegend();
        });
        ui.legend.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                toggleLegend();
            }
        });
        // 手机屏幕默认收起图例，避免遮挡地图。
        if (window.matchMedia && window.matchMedia('(max-width: 860px)').matches) {
            ui.legend.classList.add('is-collapsed');
            ui.legend.setAttribute('aria-expanded', 'false');
        }
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && state.map) {
                clearMeasure();
                if (state.measuring) setMeasure(false);
            }
        });
        bindSwipeHandle();
        window.addEventListener('resize', () => {
            if (state.map) state.map.invalidateSize({pan: false});
            updateSwipeClip();
        });
    }

    function syncDailyLayer() {
        const unavailable = !isLevel(currentHazard());
        ui.layerTabs.find((button) => button.dataset.layer === 'daily').disabled = unavailable;
        if (unavailable && state.layer === 'daily') setLayer('score');
    }

    function applyDaily(daily) {
        // 即使无预报也保留村点、避暑点；请求失败时保留已载入的静态资源。
        const previous = state.daily;
        const payload = daily && typeof daily === 'object' ? daily : {
            forecast_status: 'unavailable', forecast_notice: '天气预报请求失败，请稍后重试。'
        };
        state.daily = {
            ...payload,
            days: Array.isArray(payload.days) ? payload.days.map((day) => day && typeof day === 'object' ? day : {}) : [],
            priority: Array.isArray(payload.priority) ? payload.priority : [],
            villages: Array.isArray(payload.villages) ? payload.villages : (previous ? previous.villages : []),
            cooling_resources: Array.isArray(payload.cooling_resources) ? payload.cooling_resources : (previous ? previous.cooling_resources : []),
            medical_pois: Array.isArray(payload.medical_pois) ? payload.medical_pois : (previous ? previous.medical_pois : []),
            cooling_candidates: Array.isArray(payload.cooling_candidates) ? payload.cooling_candidates : (previous ? previous.cooling_candidates : []),
            poi_coverage: Array.isArray(payload.poi_coverage) ? payload.poi_coverage : (previous ? previous.poi_coverage : []),
            poi_metadata: payload.poi_metadata || (previous ? previous.poi_metadata : null),
            unmapped_resources: Array.isArray(payload.unmapped_resources) ? payload.unmapped_resources : (previous ? previous.unmapped_resources : [])
        };
        buildPoiCatalog();
        const firstLoad = !state.dailyLoaded;
        if (firstLoad) {
            const town = initialParams.get('town');
            if (town === '__unknown__' || state.townships.some((feature) => feature.properties.name_zh === town)) state.townFilter = town;
        }
        syncSelectedPoi();
        renderPoiCoverage();
        state.dailyLoaded = true;
        state.forecastDate = localDate();
        const requestedDay = firstLoad ? Number(initialParams.get('day')) : state.dayIndex;
        state.dayIndex = Number.isInteger(requestedDay) && requestedDay >= 0 && requestedDay < state.daily.days.length ? requestedDay : 0;
        syncDailyLayer();
        if (firstLoad && isLevel(currentHazard()) && (!initialParams.get('layer') || initialParams.get('layer') === 'daily')) {
            setLayer(initialParams.get('layer') === 'daily' || currentHazard() > 0 ? 'daily' : 'score');
        }
        renderDays();
        renderVillages();
        renderPriority();
        updateHeader();
        buildSearchOptions();
        restyleCells();
        renderLegend();
        if (firstLoad && !initialParams.get('cell')) {
            const top = priorityForDay()[0];
            if (top && top.cell_id && state.cellById.has(top.cell_id)) selectCell(state.cellById.get(top.cell_id));
        }
        if (firstLoad && initialParams.get('poi')) selectPoi(initialParams.get('poi'), {pan: false});
        updateInspector();
        drawSelection();
        updateUrl();
    }

    function loadDaily() {
        return fetch(app.dataset.dailyUrl, {headers: {Accept: 'application/json'}, credentials: 'same-origin'})
            .then((response) => (response.ok ? response.json() : null))
            .catch(() => null)
            .then(applyDaily);
    }

    function refreshAfterMidnight() {
        if (!state.dailyLoaded || state.forecastDate === localDate()) return;
        state.forecastDate = localDate();
        // 长期开启的页面先撤下昨天结论、降级过期资源，再读取新日期预报。
        buildPoiCatalog();
        renderPoiCoverage();
        syncDailyLayer();
        renderDays();
        renderVillages();
        renderPriority();
        updateInspector();
        updateHeader();
        loadDaily();
    }

    function fail(error) {
        ui.mapLoading.hidden = true;
        ui.mapFallback.hidden = false;
        ui.lede.textContent = '工作台数据载入失败，请稍后重试或切换到科研版视图。';
        console.error('热风险工作台初始化失败', error);
    }

    // ------------------------------------------------------------------
    // 启动
    // ------------------------------------------------------------------
    const initialParams = new URLSearchParams(window.location.search);
    const params = initialParams;
    const requestedLayer = params.get('layer');
    if (requestedLayer && (requestedLayer in RAW_LAYERS || requestedLayer in LAYER_TITLES)) state.layer = requestedLayer;

    Promise.all([
        fetch(app.dataset.geojsonUrl, {headers: {Accept: 'application/geo+json, application/json'}}).then((r) => {
            if (!r.ok) throw new Error(`GeoJSON HTTP ${r.status}`);
            return r.json();
        }),
        fetch(app.dataset.workbenchUrl, {headers: {Accept: 'application/json'}}).then((r) => {
            if (!r.ok) throw new Error(`Workbench HTTP ${r.status}`);
            return r.json();
        })
    ]).then(([geojson, workbench]) => {
        prepareData(geojson, workbench);
        const requestedCell = params.get('cell') || app.dataset.defaultCell;
        state.selected = state.cellById.has(requestedCell) ? state.cellById.get(requestedCell) : 0;
        bindEvents();
        renderTownTable();
        renderMethod();
        setLayer(state.layer);
        if (window.L) initializeMap();
        else {
            ui.mapLoading.hidden = true;
            ui.mapFallback.hidden = false;
        }
        updateInspector();
        drawSelection();
        renderPriority();
        syncDailyLayer();
        window.setInterval(refreshAfterMidnight, 60000);
        window.addEventListener('focus', refreshAfterMidnight);
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) refreshAfterMidnight();
        });
        return loadDaily();
    }).catch(fail);
})();
