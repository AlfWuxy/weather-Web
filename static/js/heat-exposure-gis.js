(function () {
    'use strict';

    const app = document.getElementById('heatExposureGisApp');
    if (!app) return;

    const layerOrder = [
        'age65_share_pct',
        'q3_lst_c_mean',
        'q3_coverage_pct',
        'tree_cover_pct',
        'built_up_pct',
        'permanent_water_pct',
        'mean_elevation_m'
    ];

    const layerMetricKeys = {
        age65_share_pct: 'gis_age65_share',
        q3_lst_c_mean: 'gis_lst_mean',
        q3_coverage_pct: 'gis_q3_coverage',
        tree_cover_pct: 'gis_tree_cover',
        built_up_pct: 'gis_built_up',
        permanent_water_pct: 'gis_permanent_water',
        mean_elevation_m: 'gis_mean_elevation'
    };

    const compactLayerSources = {
        age65_share_pct: 'ASPECT 2020',
        q3_lst_c_mean: 'MYD11A1.061',
        q3_coverage_pct: '复核程序 v3',
        tree_cover_pct: 'WorldCover v100',
        built_up_pct: 'WorldCover v100',
        permanent_water_pct: 'WorldCover v100',
        mean_elevation_m: 'Copernicus GLO-30'
    };

    const ui = {
        map: document.getElementById('gisMap'),
        mapLoading: document.getElementById('gisMapLoading'),
        mapFallback: document.getElementById('gisMapFallback'),
        mapLayerTitle: document.getElementById('gisMapLayerTitle'),
        coordinateReadout: document.getElementById('gisCoordinateReadout'),
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
        cellById: new Map(),
        sortedValues: new Map(),
        activeLayer: 'age65_share_pct',
        selectedIndex: 0,
        map: null,
        cellLayer: null,
        boundaryLayer: null,
        selectedLayer: null,
        countyBounds: null,
        tableRows: [],
        tablePage: 1,
        tablePageSize: 50
    };

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
        const unit = spec.unit === '%' ? '%' : ` ${spec.unit}`;
        return `${formatNumber(value, spec.digits)}${unit}`;
    }

    function formatWithUnit(value, digits, unit) {
        if (!isFiniteNumber(value)) return '无数据';
        return `${formatNumber(value, digits)}${unit}`;
    }

    function colorForValue(value, spec) {
        if (!isFiniteNumber(value)) return '#dfe4e5';
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

    function percentileText(field, value) {
        if (!isFiniteNumber(value)) {
            return field === 'age65_share_pct' ? '该网格无正人口支持，比例不显示' : '该图层在本格无有效值';
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
        minimum.textContent = formatNumber(spec.min, spec.digits);
        median.textContent = `中位 ${formatNumber(spec.median, spec.digits)}`;
        maximum.textContent = formatNumber(spec.max, spec.digits);
        labels.append(minimum, median, maximum);
        missingText.textContent = `无值 ${spec.missing_cells} 格`;
        missing.append(missingSwatch, missingText);
        classification.textContent = visibleBins.length < spec.palette.length
            ? `六分位色阶 · 并列断点已合并为 ${visibleBins.length} 类`
            : '全县有效网格六分位色阶';
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

    function drawSelection() {
        if (!state.map || !window.L) return;
        if (state.selectedLayer) state.selectedLayer.remove();
        const selected = state.cells[state.selectedIndex];
        state.selectedLayer = window.L.geoJSON(selected, {
            pane: 'selectionPane',
            interactive: false,
            style: {
                color: '#092c48',
                weight: 3,
                opacity: 1,
                fill: false
            }
        }).addTo(state.map);
    }

    function selectCell(index, options) {
        if (index < 0 || index >= state.cells.length) return;
        state.selectedIndex = index;
        updateInspector();
        drawSelection();
        updateUrl();
        if (options && options.zoom && state.map && state.selectedLayer) {
            state.map.fitBounds(state.selectedLayer.getBounds(), {padding: [80, 80], maxZoom: 12});
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
        if (state.cellLayer) state.cellLayer.setStyle(baseCellStyle);
        renderLegend();
        updateInspector();
        updateUrl();
    }

    function initializeMap() {
        if (!window.L) {
            ui.mapLoading.hidden = true;
            ui.mapFallback.hidden = false;
            ui.map.setAttribute('aria-label', '地图组件未载入，请使用本页网格数据表。');
            return;
        }

        const L = window.L;
        state.map = L.map(ui.map, {
            preferCanvas: true,
            zoomControl: true,
            attributionControl: false,
            minZoom: 8,
            maxZoom: 15,
            zoomSnap: .25
        });
        state.map.createPane('cellPane');
        state.map.getPane('cellPane').style.zIndex = 310;
        state.map.createPane('boundaryPane');
        state.map.getPane('boundaryPane').style.zIndex = 420;
        state.map.createPane('selectionPane');
        state.map.getPane('selectionPane').style.zIndex = 440;

        state.boundaryLayer = L.geoJSON(state.boundary, {
            pane: 'boundaryPane',
            interactive: false,
            style: {
                color: '#102b49',
                weight: 2.2,
                opacity: .95,
                fillColor: '#dce7e8',
                fillOpacity: .13,
                dashArray: '5 4'
            }
        }).addTo(state.map);

        const renderer = L.canvas({padding: .5, tolerance: 6, pane: 'cellPane'});
        state.cellLayer = L.geoJSON({type: 'FeatureCollection', features: state.cells}, {
            pane: 'cellPane',
            renderer: renderer,
            style: baseCellStyle,
            onEachFeature: function (feature, layer) {
                layer.on({
                    click: function () {
                        const index = state.cellById.get(feature.properties.cell_id);
                        selectCell(index);
                    },
                    mouseover: function () {
                        layer.setStyle({color: '#102b49', weight: 1.4, opacity: 1});
                    },
                    mouseout: function () {
                        layer.setStyle(baseCellStyle(feature));
                    }
                });
                layer.bindTooltip(function () {
                    return createTooltip(feature.properties);
                }, {sticky: true, className: 'gis-cell-tooltip', direction: 'top'});
            }
        }).addTo(state.map);

        state.countyBounds = state.boundaryLayer.getBounds();
        state.map.fitBounds(state.countyBounds, {padding: [28, 28]});
        state.map.setMaxBounds(state.countyBounds.pad(.35));
        L.control.scale({position: 'bottomleft', imperial: false, maxWidth: 110}).addTo(state.map);
        const attribution = L.control.attribution({position: 'bottomright', prefix: false}).addTo(state.map);
        attribution.addAttribution('NASA · ASPECT · ESA · Copernicus · geoBoundaries');
        state.map.on('mousemove', function (event) {
            ui.coordinateReadout.textContent = `${event.latlng.lng.toFixed(5)}°E · ${event.latlng.lat.toFixed(5)}°N · WGS84`;
        });
        state.map.on('mouseout', function () {
            ui.coordinateReadout.textContent = 'WGS84 · EPSG:4326';
        });
        ui.mapLoading.hidden = true;
        drawSelection();

        window.setTimeout(function () {
            state.map.invalidateSize();
        }, 80);
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
        ui.resetView.addEventListener('click', function () {
            if (state.map && state.countyBounds) state.map.fitBounds(state.countyBounds, {padding: [28, 28]});
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
        state.metadata = collection.metadata;
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
        if (requestedLayer && state.metadata.layers[requestedLayer]) state.activeLayer = requestedLayer;
        if (state.cellById.has(requestedCell)) state.selectedIndex = state.cellById.get(requestedCell);

        buildLayerControls();
        renderLegend();
        renderFingerprints();
        bindInterfaceEvents();
        updateInspector();
        ui.mapLayerTitle.textContent = state.metadata.layers[state.activeLayer].label;
        ui.statCellCount.textContent = state.metadata.spatial_definition.county_center_cells.toLocaleString('zh-CN');
        ui.statPopulationCells.textContent = state.metadata.spatial_definition.positive_population_support_cells.toLocaleString('zh-CN');
        ui.statQ3Days.textContent = state.metadata.quality_summary.q3_valid_cell_days.toLocaleString('zh-CN');
        ui.statPeriod.textContent = `${state.metadata.study_period.start.slice(0, 4)}–${state.metadata.study_period.end.slice(0, 4)}`;
        ui.statScenes.textContent = state.metadata.study_period.local_frozen_scenes.toLocaleString('zh-CN');
        ui.buildTimestamp.textContent = `GeoJSON 构建：${state.metadata.generated_at_utc} · schema ${state.metadata.schema_version}`;
        initializeMap();
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
            ui.resetView.disabled = true;
            ui.previousCell.disabled = true;
            ui.nextCell.disabled = true;
            ui.zoomCell.disabled = true;
            ui.fingerprintToggle.disabled = true;
            ui.tableToggle.disabled = true;
            ui.tableToggle.textContent = '数据表不可用';
            console.error('热暴露 GIS 初始化失败', error);
        });

    window.addEventListener('resize', () => closeMetricPopovers(false));
})();
