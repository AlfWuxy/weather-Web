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
        facility_km: '到最近医疗点距离'
    };
    const WATER_FILL = '#a9cfe3';
    const NODATA_FILL = '#c9cdc6';
    const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

    const ui = {
        title: document.getElementById('hrwTitle'),
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
        swipe: document.getElementById('hrwSwipe'),
        swipeHandle: document.getElementById('hrwSwipeHandle'),
        measure: document.getElementById('hrwMeasure'),
        reset: document.getElementById('hrwReset'),
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
        layer: 'daily',
        overlays: {hotspot: true, townships: true, villages: true, facilities: true, cooling: true},
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
        dailyLoaded: false
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
        if (!hazard) return 0;
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

    function currentDay() {
        return state.daily && state.daily.days[state.dayIndex] ? state.daily.days[state.dayIndex] : null;
    }

    function currentHazard() {
        const day = currentDay();
        return day ? day.level : 0;
    }

    function dayLabel(isoDate) {
        const parts = String(isoDate || '').split('-').map(Number);
        if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) return isoDate || '';
        const date = new Date(parts[0], parts[1] - 1, parts[2]);
        return `${WEEKDAYS[date.getDay()]} ${parts[1]}/${parts[2]}`;
    }

    function levelChip(node, level, prefix) {
        const info = levelInfo(level);
        node.textContent = `${prefix || ''}${level} 级 · ${info.label}`;
        node.style.backgroundColor = info.color;
        node.style.color = level >= 3 ? '#ffffff' : '#1d2a1f';
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
            return {color: levelInfo(level).color};
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
        return `${base} · 乡镇边界 © OpenStreetMap · NASA · ASPECT · ESA · Copernicus`;
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
        else if (layer === 'daily') text = cell.scored ? `当日 ${combineDailyLevel(currentHazard(), cell.level)} 级 · 综合分 ${fmt(cell.score, 0)}` : '无常住人口';
        else if (layer === 'score') text = cell.scored ? `综合分 ${fmt(cell.score, 0)} · ${levelInfo(cell.level).label}` : '无常住人口';
        else if (layer === 'bivariate') text = cell.scored ? `${cell.bivariate.toUpperCase()} · 地表 ${fmt(cell.p.q3_lst_c_mean, 1)} °C · 65+ ${fmt(cell.p.age65_share_pct, 1)}%` : '无常住人口';
        else if (layer === 'facility_km') text = `距医疗点 ${fmt(cell.facility_km, 1)} km`;
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
            L.polyline(segments[2], {pane: 'hotspotPane', color: '#2a0620', weight: 2, opacity: 0.9, interactive: false}),
            L.polyline(segments[1], {pane: 'hotspotPane', color: '#2a0620', weight: 1.4, opacity: 0.8, dashArray: '4 3', interactive: false})
        ]);

        const townshipShapes = L.geoJSON({type: 'FeatureCollection', features: state.townships}, {
            pane: 'boundaryPane',
            interactive: false,
            coordsToLatLng: (coords) => window.L.latLng(...toMap(coords[0], coords[1])),
            style: {color: '#3b4a57', weight: 1.1, opacity: 0.8, dashArray: '6 4', fill: false}
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
                style: {color: '#102b49', weight: 2.4, opacity: 0.95, fill: false}
            }).addTo(state.map);
        }

        const facilityItems = [];
        state.facilities.forEach((facility) => {
            const position = toMap(facility.lon, facility.lat);
            facilityItems.push(L.circle(position, {
                radius: 3000,
                pane: 'boundaryPane',
                interactive: false,
                color: '#0f766e',
                weight: 1,
                opacity: 0.55,
                dashArray: '2 4',
                fill: false
            }));
            const marker = L.marker(position, {
                pane: 'pointPane',
                icon: L.divIcon({
                    className: `hrw-facility-icon${facility.precision === 'exact' ? ' is-exact' : ''}`,
                    html: '<span>+</span>',
                    iconSize: [18, 18]
                }),
                title: facility.name
            });
            marker.bindTooltip(`${esc(facility.name)}<br><small>${facility.precision === 'exact' ? 'OSM 精确位置' : '乡镇驻地近似位置'}</small>`, {direction: 'top', className: 'hrw-tooltip'});
            facilityItems.push(marker);
        });
        state.layers.facilities = L.layerGroup(facilityItems);
        state.layers.villages = L.layerGroup();
        state.layers.cooling = L.layerGroup();
        applyOverlayVisibility();
    }

    function applyOverlayVisibility() {
        Object.keys(state.overlays).forEach((key) => {
            const layer = state.layers[key];
            if (!layer) return;
            if (state.overlays[key]) layer.addTo(state.map);
            else layer.remove();
        });
        renderLegend();
    }

    function renderVillages() {
        if (!state.map || !state.daily) return;
        const L = window.L;
        const group = state.layers.villages;
        group.clearLayers();
        const hazard = currentHazard();
        state.daily.villages.forEach((village) => {
            const level = combineDailyLevel(hazard, village.static_level);
            const info = levelInfo(level);
            const marker = L.circleMarker(toMap(village.lon_wgs84, village.lat_wgs84), {
                pane: 'pointPane',
                radius: 6,
                color: '#1b2430',
                weight: 1.5,
                fillColor: info.color,
                fillOpacity: 1
            });
            marker.bindTooltip(`${esc(village.name)}<br><small>${esc(village.township)} · 当日 ${level} 级</small>`, {direction: 'top', className: 'hrw-tooltip'});
            marker.on('click', () => selectVillage(village));
            group.addLayer(marker);
            group.addLayer(L.marker(toMap(village.lon_wgs84, village.lat_wgs84), {
                pane: 'labelTextPane',
                interactive: false,
                keyboard: false,
                icon: L.divIcon({className: 'hrw-village-label', html: `<span>${esc(village.name)}</span>`, iconSize: null})
            }));
        });

        const cooling = state.layers.cooling;
        cooling.clearLayers();
        (state.daily.cooling_resources || []).forEach((point) => {
            const marker = L.marker(toMap(point.lon_wgs84, point.lat_wgs84), {
                pane: 'pointPane',
                icon: L.divIcon({className: 'hrw-cooling-icon', html: '<i class="bi bi-snow"></i>', iconSize: [20, 20]}),
                title: point.name
            });
            const detail = [point.open_hours, point.has_ac ? '有空调' : '', point.is_accessible ? '无障碍' : ''].filter(Boolean).join(' · ');
            marker.bindTooltip(`${esc(point.name)}<br><small>${esc(detail || '避暑点')}</small>`, {direction: 'top', className: 'hrw-tooltip'});
            cooling.addLayer(marker);
        });
    }

    function drawSelection() {
        if (!state.map || state.selected < 0) return;
        const L = window.L;
        if (state.layers.selection) state.layers.selection.remove();
        state.layers.selection = L.polygon(cellLatLngs(state.cells[state.selected]), {
            pane: 'selectionPane',
            interactive: false,
            color: '#0b1f33',
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
        const line = L.polyline(state.measurePoints, {pane: 'selectionPane', color: '#b45309', weight: 3, dashArray: '6 5', interactive: false});
        const dots = state.measurePoints.map((point) => L.circleMarker(point, {pane: 'selectionPane', radius: 4, color: '#b45309', fillColor: '#fff', fillOpacity: 1, weight: 2, interactive: false}));
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
            nodes.push(el('div', 'hrw-legend-title', '到最近医疗点（km）'));
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
        const cell = state.cells[state.selected];
        if (!cell) return;
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
            ui.levelChip.style.color = '#1d2a1f';
            ui.scoreSub.textContent = 'ASPECT 模型显示该格无常住人口，不参与评分。';
            ui.dailyChip.textContent = '—';
            ui.dailyChip.style.backgroundColor = '';
        } else {
            ui.score.textContent = fmt(cell.score, 0);
            levelChip(ui.levelChip, cell.level);
            const span = cell.top_pct_p95 - cell.top_pct_p05;
            const stable = span <= state.meta.stability.stable_span_pct;
            ui.scoreSub.textContent = `全县前 ${fmt(cell.top_pct, 0)}%；权重扰动区间 前 ${fmt(cell.top_pct_p05, 0)}–${fmt(cell.top_pct_p95, 0)}%，${stable ? '排名稳定' : '排名对权重较敏感'}`;
            levelChip(ui.dailyChip, combineDailyLevel(currentHazard(), cell.level));
        }

        const facility = state.facilities[cell.facility];
        ui.breakdown.replaceChildren(
            el('div', 'hrw-breakdown-title', '风险构成（全县百分位）'),
            barRow('危险性 · 地表温度', cell.hazard_pct, `${fmt(cell.p.q3_lst_c_mean, 1)} °C`),
            barRow('暴露 · 65+ 比例', cell.exposure_pct, `${fmt(cell.p.age65_share_pct, 1)}%`),
            barRow('脆弱性', cell.vulnerability_pct, null),
            barRow('树荫缺口', cell.shade_deficit_pct, `树木覆盖 ${fmt(cell.p.tree_cover_pct, 0)}%`, true),
            barRow('建成区', cell.built_pct, `${fmt(cell.p.built_up_pct, 0)}%`, true),
            barRow('医疗点距离', cell.access_pct, `${fmt(cell.facility_km, 1)} km`, true)
        );

        ui.facts.replaceChildren();
        const hotspotText = cell.gi_bin >= 2 ? '强显著热点（q<0.01）'
            : cell.gi_bin === 1 ? '显著热点（q<0.05）'
                : cell.gi_bin <= -1 ? '显著冷点' : '不显著';
        fact(ui.facts, '统计热点', cell.scored ? `${hotspotText} · z = ${fmt(cell.gi_z, 2)}` : '—');
        fact(ui.facts, '最近医疗点', facility ? `${facility.name} · ${fmt(cell.facility_km, 1)} km` : '—');
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
        updateInspector();
        drawSelection();
        updateUrl();
        if (options && options.zoom && state.map) {
            const cell = state.cells[index];
            state.map.flyTo(toMap(cell.lon, cell.lat), Math.max(state.map.getZoom(), 13), {duration: 0.6});
        }
    }

    function selectVillage(village) {
        const index = village.cell_id ? state.cellById.get(village.cell_id) : undefined;
        if (index !== undefined) selectCell(index, {zoom: false});
        if (state.map) state.map.flyTo(toMap(village.lon_wgs84, village.lat_wgs84), Math.max(state.map.getZoom(), 14), {duration: 0.6});
    }

    // ------------------------------------------------------------------
    // 预报、清单、行动卡
    // ------------------------------------------------------------------
    function renderDays() {
        ui.days.replaceChildren();
        if (!state.daily || !state.daily.days.length) {
            ui.days.appendChild(el('p', 'hrw-empty', '预报暂不可用：地图显示静态综合风险。'));
            return;
        }
        ui.forecastSource.textContent = `来源：${state.daily.forecast_source} · 热夜阈值 ${fmt(state.daily.hot_night_tmin_c, 1)} °C`;
        state.daily.days.forEach((day, index) => {
            const button = el('button', 'hrw-day');
            button.type = 'button';
            button.setAttribute('role', 'tab');
            button.setAttribute('aria-selected', String(index === state.dayIndex));
            const info = levelInfo(day.level);
            const swatch = el('i');
            swatch.style.backgroundColor = info.color;
            const levelText = el('strong');
            levelText.append(swatch, document.createTextNode(`${day.level} 级 ${day.label}`));
            const temps = `${fmt(day.temperature_max, 0)}° / ${fmt(day.temperature_min, 0)}°`;
            button.append(
                el('span', 'hrw-day-date', index === 0 ? `今天 ${dayLabel(day.date).split(' ')[1] || ''}` : dayLabel(day.date)),
                el('span', 'hrw-day-flag', day.escalated ? (day.hot_night ? '热夜 +1' : '热浪 +1') : ''),
                levelText,
                el('span', 'hrw-day-temp', temps)
            );
            button.title = day.reasons.join('；') || '无高温';
            button.addEventListener('click', () => setDay(index));
            ui.days.appendChild(button);
        });
    }

    function setDay(index) {
        state.dayIndex = index;
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
        if (!state.daily) return [];
        const entry = state.daily.priority[state.dayIndex];
        return entry ? entry.villages : [];
    }

    function renderPriority() {
        ui.priorityList.replaceChildren();
        const villages = priorityForDay();
        if (!villages.length) {
            ui.priorityList.appendChild(el('li', 'hrw-empty', state.daily ? '服务区村点暂未配置坐标。' : '预报载入后生成清单。'));
        }
        villages.forEach((village, index) => {
            const item = el('li');
            const button = el('button', 'hrw-priority-item');
            button.type = 'button';
            const chip = el('span', 'hrw-level-chip hrw-level-chip--small');
            levelChip(chip, village.daily_level);
            const head = el('div', 'hrw-priority-head');
            head.append(el('span', 'hrw-rank', String(index + 1)), el('strong', null, village.name), chip);
            button.append(head, el('span', 'hrw-priority-town', village.township || ''), el('span', 'hrw-priority-reasons', village.reasons.join(' · ')));
            button.addEventListener('click', () => selectVillage(village));
            item.appendChild(button);
            ui.priorityList.appendChild(item);
        });
        renderActionCard(villages.length ? villages[0].daily_level : currentHazard());
    }

    function renderActionCard(level) {
        const cards = state.daily ? state.daily.action_cards : state.meta.action_cards;
        const card = cards[String(level)] || cards[level];
        if (!card) return;
        const title = el('div', 'hrw-action-title');
        const chip = el('span', 'hrw-level-chip hrw-level-chip--small');
        levelChip(chip, level);
        title.append(el('strong', null, `行动卡 · ${card.title}`), chip);
        const doctor = el('ul');
        card.doctor.forEach((line) => doctor.appendChild(el('li', null, line)));
        const caregiver = el('ul');
        card.caregiver.forEach((line) => caregiver.appendChild(el('li', null, line)));
        ui.actionCard.replaceChildren(title, el('h3', null, '医生'), doctor, el('h3', null, '家属与照护者'), caregiver);
    }

    function updateHeader() {
        const day = currentDay();
        if (!day) {
            ui.lede.textContent = '预报暂不可用。地图显示静态综合热风险分，可按乡镇汇总表安排巡访。';
            return;
        }
        const top = priorityForDay()[0];
        const when = state.dayIndex === 0 ? '今天' : dayLabel(day.date);
        ui.title.textContent = state.dayIndex === 0 ? '今天先去哪几个村' : `${when}先去哪几个村`;
        const reasons = day.reasons.length ? `（${day.reasons.join('，')}）` : '';
        if (!day.level) {
            ui.lede.textContent = `${when}都昌无高温（最高 ${fmt(day.temperature_max, 0)} °C），按常规随访。${top ? `静态风险最高的服务区村：${top.name}（综合分 ${fmt(top.static_score, 0)}）。` : ''}`;
            return;
        }
        ui.lede.textContent = `${when}都昌热危险 ${day.level} 级 · ${day.label}${reasons}。${top ? `优先：${top.name}（当日 ${top.daily_level} 级）。` : ''}`;
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
                td.style.color = level >= 3 ? '#fff' : '#1d2a1f';
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
        const names = [];
        if (state.daily) state.daily.villages.forEach((v) => names.push(v.name));
        state.townships.forEach((t) => names.push(t.properties.name_zh));
        names.forEach((name) => {
            const option = el('option');
            option.value = name;
            ui.searchOptions.appendChild(option);
        });
    }

    function runSearch() {
        const query = ui.search.value.trim();
        if (!query) return;
        const village = state.daily && state.daily.villages.find((v) => v.name === query || v.name.includes(query));
        if (village) {
            selectVillage(village);
            return;
        }
        const town = state.townships.find((t) => t.properties.name_zh === query || t.properties.name_zh.includes(query));
        if (town) zoomToTownship(town);
    }

    function printSheet() {
        const day = currentDay();
        const villages = priorityForDay();
        const sheet = ui.printSheet;
        sheet.replaceChildren();
        sheet.appendChild(el('h1', null, `都昌县热风险巡访单 · ${day ? dayLabel(day.date) : '静态风险'}`));
        if (day) {
            sheet.appendChild(el('p', null, `热危险 ${day.level} 级 ${day.label}；最高 ${fmt(day.temperature_max, 0)} °C，最低 ${fmt(day.temperature_min, 0)} °C。${day.reasons.join('；')}`));
        }
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
        window.history.replaceState({}, '', url);
    }

    function setLayer(layer) {
        if (layer === 'daily' && !state.daily && state.dailyLoaded) layer = 'score';
        state.layer = layer;
        ui.layerTabs.forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.layer === layer)));
        ui.moreLayers.value = ui.layerTabs.some((b) => b.dataset.layer === layer) ? '' : layer;
        if (state.swipe) setSwipe(false);
        restyleCells();
        renderLegend();
        updateUrl();
    }

    function bindEvents() {
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

    function applyDaily(daily) {
        state.daily = daily && Array.isArray(daily.days) && daily.days.length ? daily : null;
        state.dailyLoaded = true;
        const requestedDay = Number(initialParams.get('day'));
        if (state.daily && Number.isInteger(requestedDay) && requestedDay >= 0 && requestedDay < state.daily.days.length) {
            state.dayIndex = requestedDay;
        }
        if (!state.daily && state.layer === 'daily') setLayer('score');
        ui.layerTabs.find((b) => b.dataset.layer === 'daily').disabled = !state.daily;
        renderDays();
        renderVillages();
        renderPriority();
        updateHeader();
        buildSearchOptions();
        restyleCells();
        renderLegend();
        // 用页面初次载入时的参数判断，避免被初始化阶段写回的 URL 覆盖。
        if (state.daily && !initialParams.get('layer') && state.layer === 'daily' && !currentHazard()) {
            setLayer('score');
        }
        if (!initialParams.get('cell')) {
            const top = priorityForDay()[0];
            if (top && top.cell_id && state.cellById.has(top.cell_id)) selectCell(state.cellById.get(top.cell_id));
        }
        updateInspector();
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
        return fetch(app.dataset.dailyUrl, {headers: {Accept: 'application/json'}, credentials: 'same-origin'})
            .then((r) => (r.ok ? r.json() : null))
            .catch(() => null)
            .then(applyDaily);
    }).catch(fail);
})();
