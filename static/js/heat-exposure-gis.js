(function () {
    'use strict';

    const app = document.getElementById('heatExposureGisApp');
    if (!app) return;

    const layerOrder = [
        'age65_share_pct',
        'age65_percentile',
        'age65_population_support',
        'q3_lst_c_mean',
        'q3_lst_delta_median_c',
        'q3_lst_percentile',
        'q3_coverage_pct',
        'tree_cover_pct',
        'built_up_pct',
        'permanent_water_pct',
        'mean_elevation_m'
    ];

    const layerMetricKeys = {
        age65_share_pct: 'gis_age65_share',
        age65_percentile: 'gis_age65_share',
        age65_population_support: 'gis_age65_share',
        q3_lst_c_mean: 'gis_lst_mean',
        q3_lst_delta_median_c: 'gis_lst_mean',
        q3_lst_percentile: 'gis_lst_mean',
        q3_coverage_pct: 'gis_q3_coverage',
        tree_cover_pct: 'gis_tree_cover',
        built_up_pct: 'gis_built_up',
        permanent_water_pct: 'gis_permanent_water',
        mean_elevation_m: 'gis_mean_elevation'
    };

    const compactLayerSources = {
        age65_share_pct: 'ASPECT 2020',
        age65_percentile: 'ASPECT 派生',
        age65_population_support: 'ASPECT 支持状态',
        q3_lst_c_mean: 'MYD11A1.061',
        q3_lst_delta_median_c: 'MYD11A1 派生',
        q3_lst_percentile: 'MYD11A1 派生',
        q3_coverage_pct: '复核程序 v3',
        tree_cover_pct: 'WorldCover v100',
        built_up_pct: 'WorldCover v100',
        permanent_water_pct: 'WorldCover v100',
        mean_elevation_m: 'Copernicus GLO-30'
    };

    const geometryModes = new Set(['rectified', 'native']);
    const geometryModeLabels = {
        rectified: '正交显示',
        native: '原生几何'
    };

    const ui = {
        map: document.getElementById('gisMap'),
        gridCanvas: document.getElementById('gisGridCanvas'),
        mapTooltip: document.getElementById('gisMapTooltip'),
        mapLoading: document.getElementById('gisMapLoading'),
        mapFallback: document.getElementById('gisMapFallback'),
        mapLayerTitle: document.getElementById('gisMapLayerTitle'),
        coordinateReadout: document.getElementById('gisCoordinateReadout'),
        geometryButtons: Array.from(document.querySelectorAll('[data-geometry-mode]')),
        geometryLive: document.getElementById('gisGeometryLive'),
        legend: document.getElementById('gisLegend'),
        layerButtons: document.getElementById('gisLayerButtons'),
        layerSelect: document.getElementById('gisLayerSelect'),
        mobileLayerInfo: document.getElementById('gisMobileLayerInfo'),
        resetView: document.getElementById('gisResetView'),
        cellId: document.getElementById('gisCellId'),
        cellPosition: document.getElementById('gisCellPosition'),
        primaryLabel: document.getElementById('gisPrimaryLabel'),
        primaryInfo: document.getElementById('gisPrimaryInfo'),
        primaryValue: document.getElementById('gisPrimaryValue'),
        primaryRank: document.getElementById('gisPrimaryRank'),
        activeDefinition: document.getElementById('gisActiveDefinition'),
        metricLst: document.getElementById('gisMetricLst'),
        metricCoverage: document.getElementById('gisMetricCoverage'),
        metricTree: document.getElementById('gisMetricTree'),
        metricBuilt: document.getElementById('gisMetricBuilt'),
        metricWater: document.getElementById('gisMetricWater'),
        metricElevation: document.getElementById('gisMetricElevation'),
        cellTile: document.getElementById('gisCellTile'),
        cellRowCol: document.getElementById('gisCellRowCol'),
        cellGeometryMode: document.getElementById('gisCellGeometryMode'),
        previousCell: document.getElementById('gisPreviousCell'),
        nextCell: document.getElementById('gisNextCell'),
        zoomCell: document.getElementById('gisZoomCell'),
        fingerprintToggle: document.getElementById('gisFingerprintToggle'),
        fingerprints: document.getElementById('gisFingerprints'),
        tableToggle: document.getElementById('gisTableToggle'),
        dataPanel: document.getElementById('gisDataPanel'),
        tableSearch: document.getElementById('gisTableSearch'),
        tableCount: document.getElementById('gisTableCount'),
        tableBody: document.getElementById('gisDataTableBody'),
        tablePrevious: document.getElementById('gisTablePrevious'),
        tableNext: document.getElementById('gisTableNext'),
        tablePage: document.getElementById('gisTablePage'),
        buildTimestamp: document.getElementById('gisBuildTimestamp'),
        statCellCount: document.getElementById('gisStatCellCount'),
        statPopulationCells: document.getElementById('gisStatPopulationCells'),
        statQ3Days: document.getElementById('gisStatQ3Days'),
        statPeriod: document.getElementById('gisStatPeriod'),
        statScenes: document.getElementById('gisStatScenes')
    };

    const state = {
        metadata: null,
        boundary: null,
        cells: [],
        rectifiedCells: [],
        cellById: new Map(),
        sortedValues: new Map(),
        activeLayer: 'age65_share_pct',
        selectedIndex: 0,
        map: null,
        mapReady: false,
        canvasContext: null,
        displayBoundary: null,
        displayCells: {
            rectified: [],
            native: []
        },
        drawCache: [],
        hitBuckets: new Map(),
        hoverIndex: null,
        countyBounds: null,
        tableRows: [],
        tablePage: 1,
        tablePageSize: 50,
        geometryMode: 'rectified',
        renderFrame: null,
        resizeFrame: null,
        mapMoving: false,
        mapZooming: false
    };

    const GCJ_PI = Math.PI;
    const GCJ_AXIS = 6378245.0;
    const GCJ_EE = 0.00669342162296594323;
    const HIT_BUCKET_SIZE = 40;

    function isFiniteNumber(value) {
        return typeof value === 'number' && Number.isFinite(value);
    }

    function formatNumber(value, digits) {
        if (!isFiniteNumber(value)) return '无数据';
        return new Intl.NumberFormat('zh-CN', {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        }).format(value);
    }

    function formatLayerValue(value, spec) {
        if (!isFiniteNumber(value)) return '无数据';
        const valueLabels = spec.value_labels || {};
        if (Object.prototype.hasOwnProperty.call(valueLabels, String(value))) {
            return valueLabels[String(value)];
        }
        const unit = spec.unit === '%' ? '%' : ` ${spec.unit}`;
        return `${formatNumber(value, spec.digits)}${unit}`;
    }

    function formatWithUnit(value, digits, unit) {
        if (!isFiniteNumber(value)) return '无数据';
        return `${formatNumber(value, digits)}${unit}`;
    }

    function rectifiedFeature(feature) {
        const spatial = state.metadata.spatial_definition;
        const properties = feature.properties;
        const radius = spatial.native_sphere_radius_m;
        const resolution = spatial.native_nominal_resolution_m;
        const centerLon = properties.center_lon_wgs84;
        const centerLat = properties.center_lat_wgs84;
        if (![radius, resolution, centerLon, centerLat].every(isFiniteNumber) || radius <= 0 || resolution <= 0) {
            throw new Error(`正交显示参数无效：${properties.cell_id || '未知网格'}`);
        }

        // 只替换浏览器制图几何，原生 GeoJSON、网格 ID、中心点和指标值保持不变。
        const halfLatitude = resolution / (2 * radius) * 180 / Math.PI;
        const halfLongitude = halfLatitude / Math.cos(centerLat * Math.PI / 180);
        const west = Number((centerLon - halfLongitude).toFixed(9));
        const east = Number((centerLon + halfLongitude).toFixed(9));
        const north = Number((centerLat + halfLatitude).toFixed(9));
        const south = Number((centerLat - halfLatitude).toFixed(9));
        return {
            ...feature,
            geometry: {
                type: 'Polygon',
                coordinates: [[
                    [west, north],
                    [east, north],
                    [east, south],
                    [west, south],
                    [west, north]
                ]]
            }
        };
    }

    function geometryReadoutLabel() {
        return state.geometryMode === 'native' ? '原生几何' : '正交显示近似';
    }

    function syncGeometryControls() {
        const label = geometryModeLabels[state.geometryMode];
        ui.geometryButtons.forEach((button) => {
            button.setAttribute('aria-pressed', String(button.dataset.geometryMode === state.geometryMode));
        });
        app.dataset.activeGeometry = state.geometryMode;
        ui.geometryLive.textContent = `当前使用${label}`;
        ui.cellGeometryMode.textContent = label;
        ui.coordinateReadout.textContent = `高德底图 GCJ-02 · 数据 WGS84 · ${geometryReadoutLabel()}`;
    }

    function transformGcjLatitude(longitude, latitude) {
        let delta = -100 + 2 * longitude + 3 * latitude + .2 * latitude * latitude;
        delta += .1 * longitude * latitude + .2 * Math.sqrt(Math.abs(longitude));
        delta += (20 * Math.sin(6 * longitude * GCJ_PI) + 20 * Math.sin(2 * longitude * GCJ_PI)) * 2 / 3;
        delta += (20 * Math.sin(latitude * GCJ_PI) + 40 * Math.sin(latitude / 3 * GCJ_PI)) * 2 / 3;
        delta += (160 * Math.sin(latitude / 12 * GCJ_PI) + 320 * Math.sin(latitude * GCJ_PI / 30)) * 2 / 3;
        return delta;
    }

    function transformGcjLongitude(longitude, latitude) {
        let delta = 300 + longitude + 2 * latitude + .1 * longitude * longitude;
        delta += .1 * longitude * latitude + .1 * Math.sqrt(Math.abs(longitude));
        delta += (20 * Math.sin(6 * longitude * GCJ_PI) + 20 * Math.sin(2 * longitude * GCJ_PI)) * 2 / 3;
        delta += (20 * Math.sin(longitude * GCJ_PI) + 40 * Math.sin(longitude / 3 * GCJ_PI)) * 2 / 3;
        delta += (150 * Math.sin(longitude / 12 * GCJ_PI) + 300 * Math.sin(longitude / 30 * GCJ_PI)) * 2 / 3;
        return delta;
    }

    function isOutsideGcjCoverage(longitude, latitude) {
        return longitude < 72.004 || longitude > 137.8347 || latitude < .8293 || latitude > 55.8271;
    }

    function wgs84ToGcj02(longitude, latitude) {
        if (isOutsideGcjCoverage(longitude, latitude)) return [longitude, latitude];
        let latitudeDelta = transformGcjLatitude(longitude - 105, latitude - 35);
        let longitudeDelta = transformGcjLongitude(longitude - 105, latitude - 35);
        const latitudeRadians = latitude / 180 * GCJ_PI;
        let magic = Math.sin(latitudeRadians);
        magic = 1 - GCJ_EE * magic * magic;
        const sqrtMagic = Math.sqrt(magic);
        latitudeDelta = latitudeDelta * 180 / ((GCJ_AXIS * (1 - GCJ_EE)) / (magic * sqrtMagic) * GCJ_PI);
        longitudeDelta = longitudeDelta * 180 / (GCJ_AXIS / sqrtMagic * Math.cos(latitudeRadians) * GCJ_PI);
        return [longitude + longitudeDelta, latitude + latitudeDelta];
    }

    function geometryPolygons(geometry) {
        if (geometry?.type === 'Polygon') return [geometry.coordinates];
        if (geometry?.type === 'MultiPolygon') return geometry.coordinates;
        throw new Error(`不支持的地图几何：${geometry?.type || '未知'}`);
    }

    function displayShape(feature) {
        let minLongitude = Infinity;
        let minLatitude = Infinity;
        let maxLongitude = -Infinity;
        let maxLatitude = -Infinity;
        const polygons = geometryPolygons(feature.geometry).map((rings) => rings.map((ring) => ring.map((coordinate) => {
            const converted = wgs84ToGcj02(Number(coordinate[0]), Number(coordinate[1]));
            minLongitude = Math.min(minLongitude, converted[0]);
            minLatitude = Math.min(minLatitude, converted[1]);
            maxLongitude = Math.max(maxLongitude, converted[0]);
            maxLatitude = Math.max(maxLatitude, converted[1]);
            return converted;
        })));
        return {
            polygons,
            bounds: {
                minLongitude,
                minLatitude,
                maxLongitude,
                maxLatitude
            }
        };
    }

    function amapBounds(bounds) {
        const southWest = new window.AMap.LngLat(bounds.minLongitude, bounds.minLatitude);
        const northEast = new window.AMap.LngLat(bounds.maxLongitude, bounds.maxLatitude);
        return new window.AMap.Bounds(southWest, northEast);
    }

    function colorForValue(value, spec) {
        if (!isFiniteNumber(value)) return '#dfe4e5';
        const neutralRange = spec.neutral_range;
        const neutralColorIndex = Number(spec.neutral_color_index);
        if (
            Array.isArray(neutralRange)
            && neutralRange.length === 2
            && neutralRange.every(isFiniteNumber)
            && Number.isInteger(neutralColorIndex)
            && neutralColorIndex >= 0
            && neutralColorIndex < spec.palette.length
            && value >= neutralRange[0]
            && value <= neutralRange[1]
        ) {
            return spec.palette[neutralColorIndex];
        }
        for (let index = 1; index < spec.breaks.length; index += 1) {
            if (value <= spec.breaks[index]) return spec.palette[index - 1];
        }
        return spec.palette[spec.palette.length - 1];
    }

    function upperBound(sortedValues, target) {
        let low = 0;
        let high = sortedValues.length;
        while (low < high) {
            const middle = Math.floor((low + high) / 2);
            if (sortedValues[middle] <= target) low = middle + 1;
            else high = middle;
        }
        return low;
    }

    function percentileRank(sortedValues, value) {
        if (!isFiniteNumber(value) || !sortedValues.length) return null;
        return Math.max(
            1,
            Math.min(100, Math.round(upperBound(sortedValues, value) / sortedValues.length * 100))
        );
    }

    function median(sortedValues) {
        if (!sortedValues.length) return null;
        const middle = Math.floor(sortedValues.length / 2);
        if (sortedValues.length % 2) return sortedValues[middle];
        return (sortedValues[middle - 1] + sortedValues[middle]) / 2;
    }

    function summarize(values) {
        const sorted = values.filter(isFiniteNumber).sort((left, right) => left - right);
        return {
            sorted,
            min: sorted.length ? sorted[0] : null,
            median: median(sorted),
            max: sorted.length ? sorted[sorted.length - 1] : null
        };
    }

    function enrichDerivedLayers(collection) {
        const cells = collection.features.filter((feature) => feature.properties?.feature_type === 'modis_cell');
        const ageSummary = summarize(cells.map((feature) => feature.properties.age65_share_pct));
        const lstSummary = summarize(cells.map((feature) => feature.properties.q3_lst_c_mean));
        cells.forEach((feature) => {
            const properties = feature.properties;
            const ageValue = properties.age65_share_pct;
            const lstValue = properties.q3_lst_c_mean;
            let supportValue = null;
            if (properties.positive_population_support === true) supportValue = 1;
            if (properties.positive_population_support === false) supportValue = 0;
            properties.age65_percentile = percentileRank(ageSummary.sorted, ageValue);
            properties.age65_population_support = supportValue;
            properties.q3_lst_delta_median_c = isFiniteNumber(lstValue) && isFiniteNumber(lstSummary.median)
                ? Number((lstValue - lstSummary.median).toFixed(4))
                : null;
            properties.q3_lst_percentile = percentileRank(lstSummary.sorted, lstValue);
        });
        const deltaSummary = summarize(cells.map((feature) => feature.properties.q3_lst_delta_median_c));
        const agePercentileSummary = summarize(cells.map((feature) => feature.properties.age65_percentile));
        const supportSummary = summarize(cells.map((feature) => feature.properties.age65_population_support));
        const lstPercentileSummary = summarize(cells.map((feature) => feature.properties.q3_lst_percentile));
        const maxDelta = Math.max(
            Math.abs(deltaSummary.min || 0),
            Math.abs(deltaSummary.max || 0),
            1
        );
        // 给 0 附近保留独立中性色带，避免将无偏差误画成冷色。
        const zeroBand = Math.min(
            maxDelta / 4,
            Math.max(.05, maxDelta / 100)
        );
        const deltaBreaks = [
            -maxDelta,
            -maxDelta / 2,
            -zeroBand,
            zeroBand,
            maxDelta / 2,
            maxDelta
        ].map((value) => Number(value.toFixed(4)));
        const sharedAge = {
            metric_key: 'gis_age65_share',
            details_anchor: 'gis-age65-share'
        };
        const sharedLst = {
            metric_key: 'gis_lst_mean',
            details_anchor: 'gis-lst-mean'
        };
        collection.metadata.layers = {
            ...collection.metadata.layers,
            age65_percentile: {
                label: '65+ 人口比例全县相对分位',
                short_label: '65+ 相对分位',
                unit: '%',
                digits: 0,
                palette: ['#f1eef6', '#d7b5d8', '#df65b0', '#ce1256', '#7a0177'],
                breaks: [0, 20, 40, 60, 80, 100],
                source: '由 ASPECT 2020 有效网格计算',
                min: agePercentileSummary.min,
                median: agePercentileSummary.median,
                max: agePercentileSummary.max,
                valid_cells: agePercentileSummary.sorted.length,
                missing_cells: cells.length - agePercentileSummary.sorted.length,
                definition: '把有正人口支持网格的模型化 65+ 人口比例放入全县有效网格分布，使用并列值上界计算相对百分位。',
                ...sharedAge
            },
            age65_population_support: {
                label: '65+ 比例人口支持状态',
                short_label: '人口支持状态',
                unit: '',
                digits: 0,
                palette: ['#d9dfe2', '#237a57'],
                breaks: [0, 0, 1],
                value_labels: {'0': '无正人口支持', '1': '有正人口支持'},
                classification_label: '模型化人口支持状态',
                source: 'ASPECT 2020 支持状态',
                min: supportSummary.min,
                median: supportSummary.median,
                max: supportSummary.max,
                valid_cells: supportSummary.sorted.length,
                missing_cells: cells.length - supportSummary.sorted.length,
                definition: '仅区分该网格是否具有正的模型化人口支持。无正人口支持不等于 65+ 人口比例为 0。',
                ...sharedAge
            },
            q3_lst_delta_median_c: {
                label: '地表温度相对全县中位数偏差',
                short_label: '地表温度偏差',
                unit: '°C',
                digits: 1,
                palette: ['#2c7bb6', '#abd9e9', '#f7f7f7', '#fdae61', '#d7191c'],
                breaks: deltaBreaks,
                neutral_range: [-zeroBand, zeroBand],
                neutral_color_index: 2,
                classification_label: '相对全县有效网格中位数的分级，中性色表示接近 0°C',
                source: '由 MYD11A1.061 有效网格计算',
                min: deltaSummary.min,
                median: deltaSummary.median,
                max: deltaSummary.max,
                valid_cells: deltaSummary.sorted.length,
                missing_cells: cells.length - deltaSummary.sorted.length,
                definition: '每个网格的 2020 至 2024 年夏季晴空地表温度均值减去全县有效网格中位数。',
                ...sharedLst
            },
            q3_lst_percentile: {
                label: '地表温度全县相对分位',
                short_label: '地表温度分位',
                unit: '%',
                digits: 0,
                palette: ['#ffffcc', '#fed976', '#fd8d3c', '#e31a1c', '#800026'],
                breaks: [0, 20, 40, 60, 80, 100],
                source: '由 MYD11A1.061 有效网格计算',
                min: lstPercentileSummary.min,
                median: lstPercentileSummary.median,
                max: lstPercentileSummary.max,
                valid_cells: lstPercentileSummary.sorted.length,
                missing_cells: cells.length - lstPercentileSummary.sorted.length,
                definition: '把卫星晴空地表温度均值放入全县有效网格分布，使用并列值上界计算相对百分位。',
                ...sharedLst
            }
        };
        return collection;
    }

    function percentileText(field, value) {
        if (!isFiniteNumber(value)) {
            return field === 'age65_share_pct' ? '该网格无正人口支持，比例不显示' : '该图层在本格无有效值';
        }
        if (field === 'age65_population_support') {
            return '人口支持状态不计算百分位';
        }
        const values = state.sortedValues.get(field) || [];
        if (!values.length) return '暂无全县比较值';
        const percentile = Math.max(1, Math.min(100, Math.round(upperBound(values, value) / values.length * 100)));
        return `位于全县有效网格第 ${percentile} 百分位`;
    }

    function setExpanded(button, panel, expanded, openText, closeText) {
        button.setAttribute('aria-expanded', String(expanded));
        panel.hidden = !expanded;
        button.textContent = expanded ? closeText : openText;
    }

    function updateUrl() {
        const selected = state.cells[state.selectedIndex];
        if (!selected || !window.history || !window.history.replaceState) return;
        const url = new URL(window.location.href);
        url.searchParams.set('layer', state.activeLayer);
        url.searchParams.set('cell', selected.properties.cell_id);
        url.searchParams.set('geometry', state.geometryMode);
        window.history.replaceState({}, '', url);
    }

    function createTooltip(properties) {
        const spec = state.metadata.layers[state.activeLayer];
        const wrapper = document.createElement('div');
        const id = document.createElement('strong');
        const reading = document.createElement('span');
        id.textContent = properties.cell_id;
        reading.textContent = `${spec.short_label}：${formatLayerValue(properties[state.activeLayer], spec)}`;
        wrapper.append(id, reading);
        return wrapper;
    }

    function createMetricInfoButton(field, spec) {
        const metricKey = spec.metric_key || layerMetricKeys[field];
        if (!metricKey) return null;

        const button = document.createElement('button');
        const icon = document.createElement('i');
        const anchor = spec.details_anchor || metricKey.replaceAll('_', '-');
        button.type = 'button';
        button.className = 'yl-metric-info gis-layer-info';
        button.dataset.metricInfo = metricKey;
        button.dataset.detailsUrl = `${app.dataset.transparencyUrl}#${anchor}`;
        button.setAttribute('aria-label', `查看“${spec.label}”的计算说明`);
        button.setAttribute('aria-expanded', 'false');
        icon.className = 'bi bi-info-circle';
        icon.setAttribute('aria-hidden', 'true');
        button.appendChild(icon);
        return button;
    }

    function replaceMetricInfo(container, field) {
        if (!container) return;
        container.querySelectorAll('[data-metric-info]').forEach((button) => {
            if (window.bootstrap?.Popover) {
                window.bootstrap.Popover.getInstance(button)?.dispose();
            }
        });
        const spec = state.metadata.layers[field];
        const button = createMetricInfoButton(field, spec);
        container.replaceChildren(...(button ? [button] : []));
        if (button && typeof window.initMetricInfo === 'function') {
            window.initMetricInfo(button);
        }
    }

    function closeMetricPopovers(disposeDynamic = false) {
        if (!window.bootstrap?.Popover) return;
        document.querySelectorAll('[data-metric-info][aria-expanded="true"]').forEach((button) => {
            const instance = window.bootstrap.Popover.getInstance(button);
            if (!instance) return;
            button.blur();
            const isDynamic = ui.primaryInfo?.contains(button) || ui.mobileLayerInfo?.contains(button);
            if (disposeDynamic && isDynamic) {
                // 动态按钮即将被替换，直接销毁实例和节点，避免隐藏过渡访问已释放状态。
                instance.dispose();
                button.setAttribute('aria-expanded', 'false');
                return;
            }
            if (instance._activeTrigger) {
                Object.keys(instance._activeTrigger).forEach((triggerName) => {
                    instance._activeTrigger[triggerName] = false;
                });
            }
            instance.hide();
        });
    }

    function baseCellStyle(feature) {
        const spec = state.metadata.layers[state.activeLayer];
        return {
            color: '#ffffff',
            weight: 0.45,
            opacity: 0.58,
            fillColor: colorForValue(feature.properties[state.activeLayer], spec),
            fillOpacity: isFiniteNumber(feature.properties[state.activeLayer]) ? 0.86 : 0.58
        };
    }

    function buildLayerControls() {
        ui.layerButtons.replaceChildren();
        ui.layerSelect.replaceChildren();
        layerOrder.forEach((field) => {
            const spec = state.metadata.layers[field];
            if (!spec) return;

            const item = document.createElement('div');
            const button = document.createElement('button');
            const label = document.createElement('strong');
            const source = document.createElement('small');
            const infoButton = createMetricInfoButton(field, spec);
            item.className = 'gis-layer-item';
            item.classList.toggle('is-active', field === state.activeLayer);
            item.setAttribute('role', 'listitem');
            button.type = 'button';
            button.className = 'gis-layer-button';
            button.dataset.layer = field;
            button.setAttribute('aria-pressed', String(field === state.activeLayer));
            label.textContent = spec.short_label;
            source.textContent = spec.source;
            button.append(label, source);
            button.addEventListener('click', () => setActiveLayer(field));
            item.appendChild(button);
            if (infoButton) item.appendChild(infoButton);
            ui.layerButtons.appendChild(item);

            const option = document.createElement('option');
            option.value = field;
            option.textContent = `${spec.label} · ${compactLayerSources[field] || spec.source}`;
            option.selected = field === state.activeLayer;
            ui.layerSelect.appendChild(option);
        });
    }

    function renderLegend() {
        const spec = state.metadata.layers[state.activeLayer];
        const title = document.createElement('div');
        const titleText = document.createElement('span');
        const unit = document.createElement('span');
        const ramp = document.createElement('div');
        const labels = document.createElement('div');
        const minimum = document.createElement('span');
        const median = document.createElement('span');
        const maximum = document.createElement('span');
        const missing = document.createElement('div');
        const missingSwatch = document.createElement('i');
        const missingText = document.createElement('span');
        const classification = document.createElement('div');
        const visibleBins = [];

        spec.palette.forEach((color, index) => {
            const lower = spec.breaks[index];
            const upper = spec.breaks[index + 1];
            const duplicatePointAlreadyShown = lower === upper && visibleBins.some((bin) => bin.lower === lower && bin.upper === upper);
            if (!duplicatePointAlreadyShown) visibleBins.push({color, lower, upper});
        });

        title.className = 'gis-legend-title';
        ramp.className = 'gis-legend-ramp';
        labels.className = 'gis-legend-labels';
        missing.className = 'gis-legend-missing';
        classification.className = 'gis-legend-classification';
        titleText.textContent = spec.short_label;
        unit.textContent = spec.unit;
        title.append(titleText, unit);
        ramp.style.gridTemplateColumns = `repeat(${visibleBins.length}, 1fr)`;
        visibleBins.forEach((bin) => {
            const swatch = document.createElement('i');
            swatch.style.backgroundColor = bin.color;
            ramp.appendChild(swatch);
        });
        minimum.textContent = spec.value_labels
            ? formatLayerValue(spec.min, spec)
            : formatNumber(spec.min, spec.digits);
        median.textContent = spec.value_labels
            ? `中位 ${formatLayerValue(spec.median, spec)}`
            : `中位 ${formatNumber(spec.median, spec.digits)}`;
        maximum.textContent = spec.value_labels
            ? formatLayerValue(spec.max, spec)
            : formatNumber(spec.max, spec.digits);
        labels.append(minimum, median, maximum);
        missingText.textContent = `无值 ${spec.missing_cells} 格`;
        missing.append(missingSwatch, missingText);
        classification.textContent = spec.classification_label || (visibleBins.length < spec.palette.length
            ? `六分位色阶 · 并列断点已合并为 ${visibleBins.length} 类`
            : '全县有效网格六分位色阶');
        ui.legend.replaceChildren(title, ramp, labels, classification, missing);
    }

    function updateInspector() {
        const selected = state.cells[state.selectedIndex];
        if (!selected) return;
        const properties = selected.properties;
        const spec = state.metadata.layers[state.activeLayer];
        const value = properties[state.activeLayer];

        ui.cellId.textContent = properties.cell_id;
        ui.cellPosition.textContent = `${properties.center_lon_wgs84.toFixed(6)}°E · ${properties.center_lat_wgs84.toFixed(6)}°N`;
        ui.primaryLabel.textContent = spec.label;
        ui.primaryValue.textContent = formatLayerValue(value, spec);
        ui.primaryRank.textContent = percentileText(state.activeLayer, value);
        ui.activeDefinition.textContent = spec.definition;
        ui.metricLst.textContent = formatWithUnit(properties.q3_lst_c_mean, 1, ' °C');
        ui.metricCoverage.textContent = `${properties.q3_dates} / ${properties.local_available_dates} 天`;
        ui.metricTree.textContent = formatWithUnit(properties.tree_cover_pct, 1, '%');
        ui.metricBuilt.textContent = formatWithUnit(properties.built_up_pct, 1, '%');
        ui.metricWater.textContent = formatWithUnit(properties.permanent_water_pct, 1, '%');
        ui.metricElevation.textContent = formatWithUnit(properties.mean_elevation_m, 1, ' m');
        ui.cellTile.textContent = properties.modis_tile;
        ui.cellRowCol.textContent = `${properties.modis_row_0based} / ${properties.modis_col_0based}`;
        ui.previousCell.disabled = state.selectedIndex === 0;
        ui.nextCell.disabled = state.selectedIndex === state.cells.length - 1;
        replaceMetricInfo(ui.primaryInfo, state.activeLayer);
        replaceMetricInfo(ui.mobileLayerInfo, state.activeLayer);
    }

    function numericPixel(pixel) {
        if (!pixel) return null;
        const x = typeof pixel.getX === 'function' ? pixel.getX() : pixel.x;
        const y = typeof pixel.getY === 'function' ? pixel.getY() : pixel.y;
        return isFiniteNumber(Number(x)) && isFiniteNumber(Number(y))
            ? {x: Number(x), y: Number(y)}
            : null;
    }

    function lngLatPixel(coordinate) {
        if (!state.map) return null;
        const lngLat = Array.isArray(coordinate)
            ? new window.AMap.LngLat(coordinate[0], coordinate[1])
            : coordinate;
        return numericPixel(state.map.lngLatToContainer(lngLat));
    }

    function shapePixels(shape) {
        const polygons = shape.amapPolygons || shape.polygons;
        return polygons.map((rings) => rings.map((ring) => (
            ring.map(lngLatPixel).filter(Boolean)
        )));
    }

    function cacheAmapCoordinates(shape) {
        shape.amapPolygons = shape.polygons.map((rings) => rings.map((ring) => ring.map((coordinate) => (
            new window.AMap.LngLat(coordinate[0], coordinate[1])
        ))));
    }

    function pixelBounds(polygons) {
        const bounds = {
            minX: Infinity,
            minY: Infinity,
            maxX: -Infinity,
            maxY: -Infinity
        };
        polygons.forEach((rings) => rings.forEach((ring) => ring.forEach((point) => {
            bounds.minX = Math.min(bounds.minX, point.x);
            bounds.minY = Math.min(bounds.minY, point.y);
            bounds.maxX = Math.max(bounds.maxX, point.x);
            bounds.maxY = Math.max(bounds.maxY, point.y);
        })));
        return bounds;
    }

    function tracePolygons(context, polygons) {
        context.beginPath();
        polygons.forEach((rings) => rings.forEach((ring) => {
            if (!ring.length) return;
            context.moveTo(ring[0].x, ring[0].y);
            ring.slice(1).forEach((point) => context.lineTo(point.x, point.y));
            context.closePath();
        }));
    }

    function paintPolygons(context, polygons, style) {
        tracePolygons(context, polygons);
        if (style.fillColor && style.fillOpacity > 0) {
            context.save();
            context.globalAlpha = style.fillOpacity;
            context.fillStyle = style.fillColor;
            context.fill('evenodd');
            context.restore();
        }
        if (style.color && style.weight > 0) {
            context.save();
            context.globalAlpha = style.opacity;
            context.lineWidth = style.weight;
            context.strokeStyle = style.color;
            if (Array.isArray(style.dash)) context.setLineDash(style.dash);
            context.stroke();
            context.restore();
        }
    }

    function resizeGridCanvas() {
        const width = Math.max(0, Math.round(ui.map.clientWidth));
        const height = Math.max(0, Math.round(ui.map.clientHeight));
        if (!width || !height) return false;
        const ratio = Math.min(2, Math.max(1, window.devicePixelRatio || 1));
        const targetWidth = Math.round(width * ratio);
        const targetHeight = Math.round(height * ratio);
        if (ui.gridCanvas.width !== targetWidth || ui.gridCanvas.height !== targetHeight) {
            ui.gridCanvas.width = targetWidth;
            ui.gridCanvas.height = targetHeight;
            ui.gridCanvas.style.width = `${width}px`;
            ui.gridCanvas.style.height = `${height}px`;
        }
        state.canvasContext = ui.gridCanvas.getContext('2d');
        state.canvasContext.setTransform(ratio, 0, 0, ratio, 0, 0);
        state.canvasContext.clearRect(0, 0, width, height);
        return true;
    }

    function paintCachedCell(index, style) {
        const item = state.drawCache[index];
        if (!item || !state.canvasContext) return;
        paintPolygons(state.canvasContext, item.polygons, style);
    }

    function hitBucketKey(x, y) {
        return `${x}:${y}`;
    }

    function buildHitIndex(items) {
        const buckets = new Map();
        items.forEach((item, index) => {
            const minX = Math.floor(item.bounds.minX / HIT_BUCKET_SIZE);
            const maxX = Math.floor(item.bounds.maxX / HIT_BUCKET_SIZE);
            const minY = Math.floor(item.bounds.minY / HIT_BUCKET_SIZE);
            const maxY = Math.floor(item.bounds.maxY / HIT_BUCKET_SIZE);
            for (let x = minX; x <= maxX; x += 1) {
                for (let y = minY; y <= maxY; y += 1) {
                    const key = hitBucketKey(x, y);
                    if (!buckets.has(key)) buckets.set(key, []);
                    buckets.get(key).push(index);
                }
            }
        });
        return buckets;
    }

    function renderMapCanvas() {
        state.renderFrame = null;
        if (
            !state.map
            || state.mapMoving
            || state.mapZooming
            || !ui.gridCanvas
            || !resizeGridCanvas()
        ) return;
        const context = state.canvasContext;
        const cells = state.displayCells[state.geometryMode];
        state.drawCache = cells.map((shape, index) => {
            const polygons = shapePixels(shape);
            paintPolygons(context, polygons, baseCellStyle(state.cells[index]));
            return {polygons, bounds: pixelBounds(polygons)};
        });
        state.hitBuckets = buildHitIndex(state.drawCache);

        const boundaryPixels = shapePixels(state.displayBoundary);
        paintPolygons(context, boundaryPixels, {
            color: '#102b49',
            weight: 2.2,
            opacity: .95,
            fillColor: '#dce7e8',
            fillOpacity: .08,
            dash: [5, 4]
        });
        paintCachedCell(state.selectedIndex, {
            color: '#092c48',
            weight: 3,
            opacity: 1,
            fillOpacity: 0
        });
        ui.gridCanvas.hidden = false;
    }

    function scheduleMapRender() {
        if (!state.map || state.mapMoving || state.mapZooming || state.renderFrame) return;
        state.renderFrame = window.requestAnimationFrame(renderMapCanvas);
    }

    function pointInRing(point, ring) {
        let inside = false;
        for (let index = 0, previous = ring.length - 1; index < ring.length; previous = index, index += 1) {
            const currentPoint = ring[index];
            const previousPoint = ring[previous];
            const crosses = (currentPoint.y > point.y) !== (previousPoint.y > point.y)
                && point.x < (previousPoint.x - currentPoint.x) * (point.y - currentPoint.y)
                / ((previousPoint.y - currentPoint.y) || Number.EPSILON) + currentPoint.x;
            if (crosses) inside = !inside;
        }
        return inside;
    }

    function pointInPolygons(point, polygons) {
        return polygons.some((rings) => (
            rings.length
            && pointInRing(point, rings[0])
            && !rings.slice(1).some((ring) => pointInRing(point, ring))
        ));
    }

    function cellAtPixel(point) {
        if (
            state.mapMoving
            || state.mapZooming
            || ui.gridCanvas.hidden
        ) return null;
        const key = hitBucketKey(
            Math.floor(point.x / HIT_BUCKET_SIZE),
            Math.floor(point.y / HIT_BUCKET_SIZE)
        );
        const candidates = state.hitBuckets.get(key) || [];
        for (let position = candidates.length - 1; position >= 0; position -= 1) {
            const index = candidates[position];
            const item = state.drawCache[index];
            if (
                point.x < item.bounds.minX
                || point.x > item.bounds.maxX
                || point.y < item.bounds.minY
                || point.y > item.bounds.maxY
            ) {
                continue;
            }
            if (pointInPolygons(point, item.polygons)) return index;
        }
        return null;
    }

    function hideMapTooltip() {
        ui.mapTooltip.hidden = true;
        ui.mapTooltip.replaceChildren();
    }

    function positionMapTooltip(point) {
        ui.mapTooltip.style.left = `${point.x}px`;
        ui.mapTooltip.style.top = `${point.y}px`;
    }

    function showMapTooltip(index, point) {
        const feature = state.cells[index];
        if (!feature) {
            hideMapTooltip();
            return;
        }
        ui.mapTooltip.replaceChildren(createTooltip(feature.properties));
        positionMapTooltip(point);
        ui.mapTooltip.hidden = false;
    }

    function eventPoint(event) {
        return numericPixel(event?.pixel) || (
            event?.lnglat ? numericPixel(state.map.lngLatToContainer(event.lnglat)) : null
        );
    }

    function updateMapHover(event) {
        const point = eventPoint(event);
        if (!point) return;
        const index = cellAtPixel(point);
        if (index !== state.hoverIndex) {
            state.hoverIndex = index;
            state.map.setDefaultCursor(index === null ? 'grab' : 'pointer');
            if (index === null) hideMapTooltip();
            else showMapTooltip(index, point);
            return;
        }
        if (index !== null) positionMapTooltip(point);
    }

    function fitDisplayBounds(bounds, maxZoom) {
        if (!state.map || !bounds) return;
        state.map.setBounds(amapBounds(bounds), true);
        if (state.map.getZoom() > maxZoom) state.map.setZoom(maxZoom);
    }

    function renderCellGeometry() {
        state.hoverIndex = null;
        hideMapTooltip();
        scheduleMapRender();
    }

    function selectCell(index, options) {
        if (index < 0 || index >= state.cells.length) return;
        state.selectedIndex = index;
        updateInspector();
        scheduleMapRender();
        updateUrl();
        if (options && options.zoom && state.map) {
            fitDisplayBounds(state.displayCells[state.geometryMode][index].bounds, 12);
        }
    }

    function setActiveLayer(field) {
        if (!state.metadata.layers[field]) return;
        closeMetricPopovers(true);
        state.activeLayer = field;
        ui.mapLayerTitle.textContent = state.metadata.layers[field].label;
        ui.layerSelect.value = field;
        ui.layerButtons.querySelectorAll('.gis-layer-button[data-layer]').forEach((button) => {
            const active = button.dataset.layer === field;
            button.setAttribute('aria-pressed', String(active));
            button.closest('.gis-layer-item')?.classList.toggle('is-active', active);
        });
        scheduleMapRender();
        renderLegend();
        updateInspector();
        updateUrl();
    }

    function setGeometryMode(mode) {
        if (!geometryModes.has(mode)) return;
        closeMetricPopovers(false);
        const changed = state.geometryMode !== mode;
        state.geometryMode = mode;
        syncGeometryControls();
        if (changed && state.map) renderCellGeometry();
        updateUrl();
    }

    function showMapFallback(title, message, error) {
        if (state.map && typeof state.map.destroy === 'function') {
            try {
                state.map.destroy();
            } catch (destroyError) {
                console.error('高德地图实例清理失败', destroyError);
            }
        }
        state.map = null;
        state.mapReady = false;
        ui.gridCanvas.hidden = true;
        ui.mapLoading.hidden = true;
        ui.mapFallback.hidden = false;
        ui.mapFallback.querySelector('strong').textContent = title;
        ui.mapFallback.querySelector('p').textContent = message;
        ui.map.setAttribute('aria-label', '地图组件未载入，请使用本页网格数据表。');
        if (error) console.error('高德热暴露地图初始化失败', error);
    }

    function suspendMapCanvas(motionType) {
        state[motionType] = true;
        state.hoverIndex = null;
        // 手势结束后的下一帧之前禁用旧像素索引，避免点击命中移动前的网格。
        state.drawCache = [];
        state.hitBuckets = new Map();
        hideMapTooltip();
        ui.gridCanvas.hidden = true;
    }

    function resumeMapCanvas(motionType) {
        state[motionType] = false;
        if (state.mapMoving || state.mapZooming) return;
        scheduleMapRender();
    }

    function initializeMap() {
        if (!window.AMap || app.dataset.hasMapKey !== '1') {
            showMapFallback(
                '地图组件暂时无法载入',
                '下面的数据表和方法信息仍可正常使用。'
            );
            return;
        }
        const boundary = state.displayBoundary.bounds;
        cacheAmapCoordinates(state.displayBoundary);
        state.displayCells.native.forEach(cacheAmapCoordinates);
        state.displayCells.rectified.forEach(cacheAmapCoordinates);
        const center = [
            (boundary.minLongitude + boundary.maxLongitude) / 2,
            (boundary.minLatitude + boundary.maxLatitude) / 2
        ];
        state.map = new window.AMap.Map(ui.map, {
            viewMode: '2D',
            zoom: 9,
            center,
            minZoom: 8,
            maxZoom: 15,
            resizeEnable: true,
            rotateEnable: false,
            pitchEnable: false,
            mapStyle: 'amap://styles/normal'
        });
        state.countyBounds = boundary;
        state.map.addControl(new window.AMap.Scale({
            position: 'RB',
            offset: new window.AMap.Pixel(14, 48)
        }));
        state.map.addControl(new window.AMap.ToolBar({
            position: 'LT',
            offset: new window.AMap.Pixel(10, 70)
        }));
        state.map.on('complete', function () {
            state.mapReady = true;
            fitDisplayBounds(state.countyBounds, 11);
            ui.mapLoading.hidden = true;
            ui.mapFallback.hidden = true;
            scheduleMapRender();
        });
        // 手势期间只移动高德底图，结束后一次性重投影网格，避免低端手机逐帧计算全部顶点。
        state.map.on('movestart', function () {
            suspendMapCanvas('mapMoving');
        });
        state.map.on('moveend', function () {
            resumeMapCanvas('mapMoving');
        });
        state.map.on('zoomstart', function () {
            suspendMapCanvas('mapZooming');
        });
        state.map.on('zoomend', function () {
            resumeMapCanvas('mapZooming');
        });
        state.map.on('mousemove', function (event) {
            const longitude = Number(event.lnglat?.getLng?.());
            const latitude = Number(event.lnglat?.getLat?.());
            if (isFiniteNumber(longitude) && isFiniteNumber(latitude)) {
                ui.coordinateReadout.textContent = `${longitude.toFixed(5)}°E · ${latitude.toFixed(5)}°N · GCJ-02 · ${geometryReadoutLabel()}`;
            }
            updateMapHover(event);
        });
        state.map.on('click', function (event) {
            const point = eventPoint(event);
            const index = point ? cellAtPixel(point) : null;
            if (index !== null) selectCell(index);
        });
        ui.map.addEventListener('mouseleave', function () {
            state.hoverIndex = null;
            state.map.setDefaultCursor('grab');
            hideMapTooltip();
            syncGeometryControls();
        });
        renderCellGeometry();
        window.setTimeout(function () {
            if (state.mapReady) return;
            ui.mapLoading.hidden = true;
            ui.mapFallback.hidden = false;
            ui.mapFallback.querySelector('strong').textContent = '高德底图载入超时';
            ui.mapFallback.querySelector('p').textContent = '网格数据表和方法信息仍可正常使用，请稍后重试地图。';
        }, 8000);
    }

    function renderFingerprints() {
        const fragment = document.createDocumentFragment();
        state.metadata.input_fingerprints.forEach((item) => {
            const row = document.createElement('div');
            const name = document.createElement('strong');
            const hash = document.createElement('code');
            row.className = 'gis-fingerprint-row';
            name.textContent = item.logical_name;
            hash.textContent = item.sha256;
            row.append(name, hash);
            fragment.appendChild(row);
        });
        ui.fingerprints.replaceChildren(fragment);
    }

    function tableCell(row, text) {
        const cell = document.createElement('td');
        cell.textContent = text;
        row.appendChild(cell);
    }

    function renderTable() {
        const totalPages = Math.max(1, Math.ceil(state.tableRows.length / state.tablePageSize));
        state.tablePage = Math.max(1, Math.min(state.tablePage, totalPages));
        const start = (state.tablePage - 1) * state.tablePageSize;
        const visibleRows = state.tableRows.slice(start, start + state.tablePageSize);
        const fragment = document.createDocumentFragment();

        visibleRows.forEach((feature) => {
            const properties = feature.properties;
            const row = document.createElement('tr');
            const idCell = document.createElement('td');
            const selectButton = document.createElement('button');
            selectButton.type = 'button';
            selectButton.textContent = properties.cell_id;
            selectButton.addEventListener('click', function () {
                selectCell(state.cellById.get(properties.cell_id), {zoom: true});
            });
            idCell.appendChild(selectButton);
            row.appendChild(idCell);
            tableCell(row, `${properties.center_lon_wgs84.toFixed(5)}, ${properties.center_lat_wgs84.toFixed(5)}`);
            tableCell(row, isFiniteNumber(properties.age65_share_pct) ? `${formatNumber(properties.age65_share_pct, 1)}%` : '无正人口支持');
            tableCell(row, formatWithUnit(properties.q3_lst_c_mean, 1, ' °C'));
            tableCell(row, `${properties.q3_dates} / ${properties.local_available_dates}`);
            tableCell(row, formatWithUnit(properties.tree_cover_pct, 1, '%'));
            tableCell(row, formatWithUnit(properties.built_up_pct, 1, '%'));
            tableCell(row, formatWithUnit(properties.permanent_water_pct, 1, '%'));
            tableCell(row, formatWithUnit(properties.mean_elevation_m, 1, ' m'));
            fragment.appendChild(row);
        });

        ui.tableBody.replaceChildren(fragment);
        ui.tableCount.textContent = `匹配 ${state.tableRows.length.toLocaleString('zh-CN')} 格`;
        ui.tablePage.textContent = `第 ${state.tablePage} / ${totalPages} 页`;
        ui.tablePrevious.disabled = state.tablePage <= 1;
        ui.tableNext.disabled = state.tablePage >= totalPages;
    }

    function filterTable() {
        const query = ui.tableSearch.value.trim().toLowerCase();
        state.tableRows = state.cells.filter((feature) => {
            if (!query) return true;
            const properties = feature.properties;
            return [
                properties.cell_id,
                properties.center_lon_wgs84,
                properties.center_lat_wgs84,
                properties.age65_share_pct,
                properties.q3_lst_c_mean,
                properties.q3_dates
            ].some((value) => String(value ?? '').toLowerCase().includes(query));
        });
        state.tablePage = 1;
        renderTable();
    }

    function bindInterfaceEvents() {
        ui.layerSelect.addEventListener('change', function () {
            setActiveLayer(ui.layerSelect.value);
        });
        ui.geometryButtons.forEach((button) => {
            button.addEventListener('click', function (event) {
                event.stopPropagation();
                setGeometryMode(button.dataset.geometryMode);
            });
        });
        ui.resetView.addEventListener('click', function () {
            if (state.map && state.countyBounds) fitDisplayBounds(state.countyBounds, 11);
        });
        ui.previousCell.addEventListener('click', function () {
            selectCell(state.selectedIndex - 1);
        });
        ui.nextCell.addEventListener('click', function () {
            selectCell(state.selectedIndex + 1);
        });
        ui.zoomCell.addEventListener('click', function () {
            selectCell(state.selectedIndex, {zoom: true});
        });
        ui.fingerprintToggle.addEventListener('click', function () {
            const expanded = ui.fingerprintToggle.getAttribute('aria-expanded') !== 'true';
            setExpanded(ui.fingerprintToggle, ui.fingerprints, expanded, '查看 SHA-256 指纹', '收起 SHA-256 指纹');
        });
        ui.tableToggle.addEventListener('click', function () {
            const expanded = ui.tableToggle.getAttribute('aria-expanded') !== 'true';
            setExpanded(ui.tableToggle, ui.dataPanel, expanded, '打开数据表', '收起数据表');
            if (expanded) {
                renderTable();
                ui.tableSearch.focus();
            }
        });
        ui.tableSearch.addEventListener('input', filterTable);
        ui.tablePrevious.addEventListener('click', function () {
            state.tablePage -= 1;
            renderTable();
        });
        ui.tableNext.addEventListener('click', function () {
            state.tablePage += 1;
            renderTable();
        });
    }

    function initializeData(collection) {
        if (!collection || collection.type !== 'FeatureCollection' || !Array.isArray(collection.features)) {
            throw new Error('GeoJSON 不是有效的 FeatureCollection');
        }
        if (!collection.metadata?.layers || !collection.metadata?.spatial_definition) {
            throw new Error('GeoJSON 缺少 GIS 元数据');
        }
        // 派生分位与相对中位数只用于当前浏览器展示，不写回冻结科研 GeoJSON。
        enrichDerivedLayers(collection);
        state.metadata = collection.metadata;
        const spatial = state.metadata.spatial_definition;
        const displayGeometry = spatial.display_geometry;
        if (
            !isFiniteNumber(spatial.native_sphere_radius_m)
            || !isFiniteNumber(spatial.native_nominal_resolution_m)
            || spatial.native_sphere_radius_m <= 0
            || spatial.native_nominal_resolution_m <= 0
            || !displayGeometry
            || !geometryModes.has(displayGeometry.default_mode)
            || !Array.isArray(displayGeometry.available_modes)
            || [...geometryModes].some((mode) => !displayGeometry.available_modes.includes(mode))
            || displayGeometry.native_geometry_preserved !== true
            || displayGeometry.rectified_geometry_analysis_use !== false
        ) {
            throw new Error('GeoJSON 显示几何元数据不完整');
        }
        state.boundary = collection.features.find((feature) => feature.properties.feature_type === 'study_boundary');
        state.cells = collection.features.filter((feature) => feature.properties.feature_type === 'modis_cell');
        if (!state.boundary || !state.cells.length) {
            throw new Error('GeoJSON 缺少研究边界或网格要素');
        }
        if (state.cells.length !== state.metadata.spatial_definition.county_center_cells) {
            throw new Error('GeoJSON 网格数与元数据不一致');
        }
        if (layerOrder.some((field) => !state.metadata.layers[field])) {
            throw new Error('GeoJSON 图层定义不完整');
        }
        state.cells.forEach((feature, index) => state.cellById.set(feature.properties.cell_id, index));
        if (state.cellById.size !== state.cells.length) {
            throw new Error('GeoJSON cell ID 存在重复');
        }
        state.rectifiedCells = state.cells.map(rectifiedFeature);
        state.displayBoundary = displayShape(state.boundary);
        state.displayCells.native = state.cells.map(displayShape);
        state.displayCells.rectified = state.rectifiedCells.map(displayShape);
        state.tableRows = state.cells.slice();

        layerOrder.forEach((field) => {
            state.sortedValues.set(field, state.cells
                .map((feature) => feature.properties[field])
                .filter(isFiniteNumber)
                .sort((left, right) => left - right));
        });

        const parameters = new URLSearchParams(window.location.search);
        const requestedLayer = parameters.get('layer');
        const requestedCell = parameters.get('cell') || app.dataset.defaultCell;
        const requestedGeometry = parameters.get('geometry');
        if (requestedLayer && state.metadata.layers[requestedLayer]) state.activeLayer = requestedLayer;
        if (state.cellById.has(requestedCell)) state.selectedIndex = state.cellById.get(requestedCell);
        state.geometryMode = geometryModes.has(requestedGeometry)
            ? requestedGeometry
            : displayGeometry.default_mode;

        buildLayerControls();
        renderLegend();
        renderFingerprints();
        bindInterfaceEvents();
        updateInspector();
        syncGeometryControls();
        ui.mapLayerTitle.textContent = state.metadata.layers[state.activeLayer].label;
        ui.statCellCount.textContent = state.metadata.spatial_definition.county_center_cells.toLocaleString('zh-CN');
        ui.statPopulationCells.textContent = state.metadata.spatial_definition.positive_population_support_cells.toLocaleString('zh-CN');
        ui.statQ3Days.textContent = state.metadata.quality_summary.q3_valid_cell_days.toLocaleString('zh-CN');
        ui.statPeriod.textContent = `${state.metadata.study_period.start.slice(0, 4)}–${state.metadata.study_period.end.slice(0, 4)}`;
        ui.statScenes.textContent = state.metadata.study_period.local_frozen_scenes.toLocaleString('zh-CN');
        ui.buildTimestamp.textContent = `GeoJSON 构建：${state.metadata.generated_at_utc} · schema ${state.metadata.schema_version}`;
        try {
            initializeMap();
        } catch (error) {
            showMapFallback(
                '高德地图初始化失败',
                '网格数据表、下载和方法信息仍可正常使用，请稍后重试地图。',
                error
            );
        }
        updateUrl();
    }

    fetch(app.dataset.geojsonUrl, {headers: {'Accept': 'application/geo+json, application/json'}})
        .then(function (response) {
            if (!response.ok) throw new Error(`GeoJSON HTTP ${response.status}`);
            return response.json();
        })
        .then(initializeData)
        .catch(function (error) {
            ui.mapLoading.hidden = true;
            ui.mapFallback.hidden = false;
            ui.mapFallback.querySelector('strong').textContent = 'GIS 数据载入失败';
            ui.mapFallback.querySelector('p').textContent = '地图和网格数据表已停止使用，请稍后重试。';
            ui.layerSelect.disabled = true;
            ui.geometryButtons.forEach((button) => { button.disabled = true; });
            ui.resetView.disabled = true;
            ui.previousCell.disabled = true;
            ui.nextCell.disabled = true;
            ui.zoomCell.disabled = true;
            ui.fingerprintToggle.disabled = true;
            ui.tableToggle.disabled = true;
            ui.tableToggle.textContent = '数据表不可用';
            console.error('热暴露 GIS 初始化失败', error);
        });

    window.addEventListener('resize', function () {
        closeMetricPopovers(false);
        if (!state.map) return;
        if (state.resizeFrame) window.cancelAnimationFrame(state.resizeFrame);
        state.resizeFrame = window.requestAnimationFrame(function () {
            scheduleMapRender();
            state.resizeFrame = null;
        });
    });
})();
