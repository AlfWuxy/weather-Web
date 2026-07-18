const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {
  buildGridDetails,
  buildGridPage,
  buildRepresentativeRows,
  gridCellCount,
} = require('../pages/gis/view-model');

const miniRoot = path.resolve(__dirname, '..');

function cell(id, age, temperature) {
  return {
    type: 'Feature',
    id,
    properties: {
      feature_type: 'modis_cell',
      cell_id: id,
      age65_share_pct: age,
      q3_lst_c_mean: temperature,
      q3_coverage_pct: 82,
      tree_cover_pct: 25,
      built_up_pct: 14,
      permanent_water_pct: 3,
      mean_elevation_m: 46,
    },
    geometry: { type: 'Polygon', coordinates: [] },
  };
}

function sampleCollection() {
  return {
    type: 'FeatureCollection',
    features: [
      { type: 'Feature', properties: { feature_type: 'study_boundary' }, geometry: null },
      cell('cell-z', 31, 36.2),
      cell('cell-low', 12, 34.1),
      cell('cell-a', 31, 37.4),
      cell('cell-missing', null, 35.0),
    ],
  };
}

function loadPageDefinition() {
  const pagePath = require.resolve('../pages/gis/index');
  const previousPage = global.Page;
  let definition;
  try {
    global.Page = (candidate) => { definition = candidate; };
    delete require.cache[pagePath];
    require(pagePath);
  } finally {
    global.Page = previousPage;
  }
  return definition;
}

function pageInstance(definition) {
  const instance = Object.assign({}, definition);
  instance.data = JSON.parse(JSON.stringify(definition.data));
  instance.setData = function setData(next, callback) {
    Object.assign(this.data, next);
    if (typeof callback === 'function') callback();
  };
  return instance;
}

test('可读列表按当前图层最高值排序并提供完整数值', () => {
  const collection = sampleCollection();
  const rows = buildRepresentativeRows(collection, 'age65_share_pct', 3);

  assert.equal(gridCellCount(collection), 4);
  assert.deepEqual(rows.map((row) => row.id), ['cell-a', 'cell-z', 'cell-low']);
  assert.equal(rows[0].rank, 1);
  assert.equal(rows[0].activeValue, '31.0%');
  assert.equal(rows[0].temperature, '37.4°C');
  assert.match(rows[0].ariaLabel, /网格 cell-a/);
  assert.match(rows[0].ariaLabel, /观测覆盖 82.0%/);
});

test('Canvas 网格详情沿用命中值并保留所有可读字段', () => {
  const collection = sampleCollection();
  const details = buildGridDetails(collection, {
    id: 'canvas-cell',
    value: 42,
    properties: cell('canvas-cell', 17, 38.3).properties,
  }, 'age65_share_pct');

  assert.equal(details.id, 'canvas-cell');
  assert.equal(details.activeValue, '42.0%');
  assert.equal(details.age, '17.0%');
  assert.equal(details.elevation, '46m');
});

test('全部网格支持编号搜索和稳定分页，缺值网格也可读', () => {
  const collection = sampleCollection();
  const firstPage = buildGridPage(collection, 'age65_share_pct', { page: 1, pageSize: 5 });
  assert.equal(firstPage.total, 4);
  assert.deepEqual(firstPage.rows.map((row) => row.id), ['cell-a', 'cell-low', 'cell-missing', 'cell-z']);
  assert.equal(firstPage.rows[2].activeValue, '无数据');

  const search = buildGridPage(collection, 'age65_share_pct', { query: 'MISSING', page: 1, pageSize: 5 });
  assert.equal(search.total, 1);
  assert.equal(search.rows[0].id, 'cell-missing');

  const fullCollection = {
    type: 'FeatureCollection',
    features: Array.from({ length: 2593 }, (_, index) => cell(`grid-${String(index + 1).padStart(4, '0')}`, index % 40, 30 + index % 9)),
  };
  const lastPage = buildGridPage(fullCollection, 'age65_share_pct', { page: 130, pageSize: 20 });
  assert.equal(lastPage.total, 2593);
  assert.equal(lastPage.pageCount, 130);
  assert.equal(lastPage.rows.length, 13);
  assert.equal(lastPage.rows[12].id, 'grid-2593');
});

test('取消 GIS 下载会中止请求并清空所有未完成展示状态', () => {
  const definition = loadPageDefinition();
  const page = pageInstance(definition);
  let aborted = false;
  page.data.mapState = 'loading';
  page._mapLoadToken = 4;
  page._renderToken = 7;
  page._collection = sampleCollection();
  page._canvasModel = { cells: [] };
  page._mapRequest = { abort() { aborted = true; } };
  page.data.representativeRows = [{ id: 'partial' }];
  page.data.selected = { id: 'partial' };

  page.cancelMapLoad();

  assert.equal(aborted, true);
  assert.equal(page._mapLoadToken, 5);
  assert.equal(page._renderToken, 8);
  assert.equal(page._collection, null);
  assert.equal(page._canvasModel, null);
  assert.equal(page.data.mapState, 'idle');
  assert.deepEqual(page.data.representativeRows, []);
  assert.equal(page.data.selected, null);
  assert.match(page.data.mapNotice, /没有显示任何网格数据/);
});

test('GIS 元数据版本更新时立即中止旧下载并清空旧地图', () => {
  const definition = loadPageDefinition();
  const page = pageInstance(definition);
  let aborted = false;
  page._geojsonUrl = '/static/old.geojson';
  page._geojsonSignature = JSON.stringify(['/static/old.geojson', '2026-07-17T00:00:00Z', '', '', '1.2.0', 1800000]);
  page._mapLoadToken = 3;
  page._renderToken = 5;
  page._mapRequest = { abort() { aborted = true; } };
  page._collection = sampleCollection();
  page._canvasModel = { cells: [] };
  page.data.mapState = 'ready';
  page.data.cellCount = 2593;
  page.data.gridPageRows = [{ id: 'old-grid' }];

  page.renderMetadata({
    data: {
      gis: {
        available: true,
        geojson_url: '/static/new.geojson',
        generated_at: '2026-07-18T00:00:00Z',
        schema_version: '1.2.0',
        size_bytes: 1900000,
      },
    },
    meta: {},
  });

  assert.equal(aborted, true);
  assert.equal(page._mapLoadToken, 4);
  assert.equal(page._renderToken, 6);
  assert.equal(page._geojsonUrl, '/static/new.geojson');
  assert.equal(page._collection, null);
  assert.equal(page._canvasModel, null);
  assert.equal(page.data.mapState, 'idle');
  assert.equal(page.data.cellCount, 0);
  assert.deepEqual(page.data.gridPageRows, []);
  assert.match(page.data.mapNotice, /版本已更新/);
});

test('GIS 页面恢复时等元数据重验结束后再续载网格', async () => {
  const definition = loadPageDefinition();
  const page = pageInstance(definition);
  let resolveMetadata;
  let mapLoads = 0;
  page._metadataShowToken = 0;
  page._unloaded = false;
  page._publicPageVisible = false;
  page._resumeMapLoad = true;
  page.loadMetadata = () => new Promise((resolve) => { resolveMetadata = resolve; });
  page.loadMap = () => { mapLoads += 1; };

  page.onShow();
  assert.equal(mapLoads, 0);
  resolveMetadata();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(mapLoads, 1);
  assert.match(page.data.mapNotice, /正在重新加载/);
});

test('GIS 恢复会等过期缓存的 revalidated 新元数据后才下载', async () => {
  const publicData = require('../utils/public-data');
  const originalGetCommunity = publicData.getCommunity;
  let resolveFresh;
  try {
    publicData.getCommunity = () => Promise.resolve({
      data: { marker: 'stale' },
      meta: { source: 'stale-cache', refreshStarted: true },
      revalidated: new Promise((resolve) => { resolveFresh = resolve; }),
    });
    const definition = loadPageDefinition();
    const page = pageInstance(definition);
    const rendered = [];
    let mapLoads = 0;
    page._metadataShowToken = 0;
    page._unloaded = false;
    page._publicPageVisible = false;
    page._resumeMapLoad = true;
    page.renderMetadata = (result) => rendered.push(result.data.marker);
    page.loadMap = () => { mapLoads += 1; };

    page.onShow();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(rendered, ['stale']);
    assert.equal(mapLoads, 0);

    resolveFresh({ data: { marker: 'fresh' }, meta: { source: 'network' } });
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(rendered, ['stale', 'fresh']);
    assert.equal(mapLoads, 1);
  } finally {
    publicData.getCommunity = originalGetCommunity;
  }
});

test('网格选择会播报结果并把详情滚动到当前视野', () => {
  const definition = loadPageDefinition();
  const page = pageInstance(definition);
  const collection = sampleCollection();
  let scrollOptions;
  const previousWx = global.wx;
  global.wx = {
    pageScrollTo(options) { scrollOptions = options; },
  };
  page._collection = collection;
  page._canvasModel = { spec: require('../utils/gis-transform').resolveLayer(collection, 'age65_share_pct') };

  try {
    page.selectGrid({
      id: 'cell-a',
      value: 31,
      properties: collection.features.find((feature) => feature.id === 'cell-a').properties,
    });
  } finally {
    global.wx = previousWx;
  }

  assert.equal(page.data.selected.id, 'cell-a');
  assert.match(page.data.announcement, /已选择网格 cell-a/);
  assert.deepEqual(scrollOptions, { selector: '#selectedDetail', duration: 0 });
});

test('Canvas 上滑不会误选网格，轻点仍可选择', () => {
  const definition = loadPageDefinition();
  const page = pageInstance(definition);
  let selected = 0;
  page.data.mapState = 'ready';
  page._canvasModel = {
    cells: [{
      id: 'grid-1',
      minX: 0,
      maxX: 20,
      minY: 0,
      maxY: 20,
      path: [{ x: 0, y: 0 }, { x: 20, y: 0 }, { x: 20, y: 20 }, { x: 0, y: 20 }, { x: 0, y: 0 }],
    }],
  };
  page.selectGrid = () => { selected += 1; };

  page.onCanvasTouchStart({ touches: [{ clientX: 10, clientY: 10 }] });
  page.onCanvasTap({ changedTouches: [{ clientX: 10, clientY: 60, x: 10, y: 10 }] });
  assert.equal(selected, 0);

  page.onCanvasTouchStart({ touches: [{ clientX: 10, clientY: 10 }] });
  page.onCanvasTap({ changedTouches: [{ clientX: 12, clientY: 12, x: 12, y: 12 }] });
  assert.equal(selected, 1);
});

test('GIS 页面暴露模式、图层、Canvas 与选择结果的无障碍语义', () => {
  const view = fs.readFileSync(path.join(miniRoot, 'pages/gis/index.wxml'), 'utf8');
  const style = fs.readFileSync(path.join(miniRoot, 'pages/gis/index.wxss'), 'utf8');
  const script = fs.readFileSync(path.join(miniRoot, 'pages/gis/index.js'), 'utf8');

  assert.match(view, /class="cancel-download-button"[^>]*bindtap="cancelMapLoad"/);
  assert.match(view, /aria-label="取消 GIS 网格数据下载"/);
  assert.match(view, /完整下载并校验前不会显示地图或列表/);
  assert.match(script, /约 1\.8 MB/);
  assert.match(script, /Math\.min\(Number\(system\.pixelRatio\) \|\| 1, 2\)/);
  assert.match(view, /aria-live="polite"/);
  assert.match(view, /aria-pressed="\{\{viewMode === 'map'\}\}"/);
  assert.match(view, /aria-selected="\{\{activeLayer === item\.key\}\}"/);
  assert.match(view, /class="control-icon"/);
  assert.match(view, /src="\/assets\/icons\/check-white\.png"/);
  assert.match(view, /class="control-label"[^>]*aria-hidden="true">未选/);
  assert.doesNotMatch(view, /[✓○]/);
  assert.match(view, /aria-label="都昌县 1 km 网格\{\{activeLayerLabel\}\}地图/);
  assert.match(view, /bindtouchstart="onCanvasTouchStart"/);
  assert.doesNotMatch(view, /catchtouchmove=/);
  assert.match(view, /最高值代表网格/);
  assert.match(view, /全部网格逐页阅读/);
  assert.match(view, /bindconfirm="searchGrid"/);
  assert.match(view, /bindtap="previousGridPage"/);
  assert.match(view, /bindtap="nextGridPage"/);
  assert.match(view, /id="selectedDetail"/);
  assert.match(view, /focusable="true"/);
  assert.match(script, /wx\.pageScrollTo\(\{/);
  assert.match(style, /\.canvas-wrap[^}]*height: 720rpx/);
  assert.match(style, /\.cancel-download-button[^}]*min-height: 88rpx/);
  assert.match(view, /hidden="\{\{viewMode !== 'map'\}\}"/);
});
