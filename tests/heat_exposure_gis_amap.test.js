const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const script = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'js', 'heat-exposure-gis.js'),
  'utf8',
);

function functionSource(name) {
  const start = script.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `缺少函数 ${name}`);
  const bodyStart = script.indexOf('{', start);
  let depth = 0;
  for (let index = bodyStart; index < script.length; index += 1) {
    if (script[index] === '{') depth += 1;
    if (script[index] === '}') depth -= 1;
    if (depth === 0) return script.slice(start, index + 1);
  }
  throw new Error(`函数 ${name} 未闭合`);
}

function loadDisplayTransform() {
  const source = [
    'const GCJ_PI = Math.PI;',
    'const GCJ_AXIS = 6378245.0;',
    'const GCJ_EE = 0.00669342162296594323;',
    functionSource('transformGcjLatitude'),
    functionSource('transformGcjLongitude'),
    functionSource('isOutsideGcjCoverage'),
    functionSource('wgs84ToGcj02'),
    functionSource('geometryPolygons'),
    functionSource('displayShape'),
    'return {wgs84ToGcj02, displayShape};',
  ].join('\n');
  return Function(source)();
}

function loadHitIndex() {
  const source = [
    'const HIT_BUCKET_SIZE = 40;',
    functionSource('hitBucketKey'),
    functionSource('buildHitIndex'),
    'return {buildHitIndex};',
  ].join('\n');
  return Function(source)();
}

function loadColorForValue() {
  const source = [
    functionSource('isFiniteNumber'),
    functionSource('colorForValue'),
    'return {colorForValue};',
  ].join('\n');
  return Function(source)();
}

function loadPercentileText() {
  const source = [
    functionSource('isFiniteNumber'),
    functionSource('upperBound'),
    "const state = {sortedValues: new Map([['age65_share_pct', [10, 20, 30]]])};",
    functionSource('percentileText'),
    'return {percentileText};',
  ].join('\n');
  return Function(source)();
}

function loadCameraTransactionHarness() {
  const frames = new Map();
  let nextFrameId = 1;
  const state = {
    map: {},
    renderFrame: null,
    cameraInMotion: false,
    cameraSettleFrame: null,
    hoverIndex: 8,
    drawCache: [{polygons: []}],
    hitBuckets: new Map([['0:0', [0]]]),
    renderCalls: 0,
  };
  const ui = {gridCanvas: {hidden: false}};
  const window = {
    requestAnimationFrame(callback) {
      const frameId = nextFrameId;
      nextFrameId += 1;
      frames.set(frameId, callback);
      return frameId;
    },
    cancelAnimationFrame(frameId) {
      frames.delete(frameId);
    },
  };
  let tooltipHides = 0;
  const hideMapTooltip = () => {
    tooltipHides += 1;
  };
  const factory = Function(
    'state',
    'ui',
    'window',
    'hideMapTooltip',
    [
      'function renderMapCanvas() {',
      '  state.renderFrame = null;',
      '  state.renderCalls += 1;',
      '  ui.gridCanvas.hidden = false;',
      '}',
      functionSource('scheduleMapRender'),
      functionSource('beginCameraTransaction'),
      functionSource('settleCameraTransaction'),
      'return {beginCameraTransaction, settleCameraTransaction};',
    ].join('\n'),
  );
  const camera = factory(state, ui, window, hideMapTooltip);

  function flushNextFrame() {
    const next = frames.entries().next();
    assert.equal(next.done, false, '预期存在待执行的动画帧');
    const [frameId, callback] = next.value;
    frames.delete(frameId);
    callback();
  }

  return {
    ...camera,
    state,
    ui,
    frames,
    flushNextFrame,
    tooltipHides: () => tooltipHides,
  };
}

test('高德显示转换只生成 GCJ-02 副本，不修改科研 WGS84 几何', () => {
  const { wgs84ToGcj02, displayShape } = loadDisplayTransform();
  const feature = {
    type: 'Feature',
    properties: { cell_id: 'sample-cell' },
    geometry: {
      type: 'Polygon',
      coordinates: [[
        [116.20, 29.27],
        [116.21, 29.27],
        [116.21, 29.28],
        [116.20, 29.28],
        [116.20, 29.27],
      ]],
    },
  };
  const frozenCopy = JSON.parse(JSON.stringify(feature));
  const shape = displayShape(feature);
  const converted = wgs84ToGcj02(116.20, 29.27);

  assert.deepEqual(feature, frozenCopy);
  assert.notDeepEqual(converted, [116.20, 29.27]);
  assert.ok(Math.abs(converted[0] - 116.20) < 0.02);
  assert.ok(Math.abs(converted[1] - 29.27) < 0.02);
  assert.deepEqual(shape.polygons[0][0][0], converted);
  assert.ok(shape.bounds.maxLongitude > shape.bounds.minLongitude);
  assert.ok(shape.bounds.maxLatitude > shape.bounds.minLatitude);
});

test('2,593 个网格使用单 Canvas 和 RAF 重绘，不创建高德覆盖物', () => {
  assert.match(script, /requestAnimationFrame\(renderMapCanvas\)/);
  assert.match(script, /Math\.min\(2, Math\.max\(1, window\.devicePixelRatio \|\| 1\)\)/);
  assert.match(script, /state\.drawCache = cells\.map/);
  assert.match(script, /state\.hitBuckets = buildHitIndex\(state\.drawCache\)/);
  assert.match(script, /pointInPolygons/);
  assert.match(script, /setBounds\(amapBounds\(bounds\), true\)/);
  assert.match(script, /function showMapFallback/);
  assert.match(script, /try \{\s+initializeMap\(\)/);
  assert.doesNotMatch(script, /new window\.AMap\.(Polygon|GeoJSON|Marker)/);
  assert.doesNotMatch(script, /AMap\.convertFrom/);
});

test('地图手势结束后才重绘，命中索引不会逐次扫描全部网格', () => {
  const { buildHitIndex } = loadHitIndex();
  const items = Array.from({length: 2593}, (_, index) => {
    const column = index % 51;
    const row = Math.floor(index / 51);
    return {
      bounds: {
        // zoom 9、北纬约 29° 时，1 km 网格在屏幕上约为 4 px。
        minX: column * 3.8,
        maxX: column * 3.8 + 4,
        minY: row * 3.8,
        maxY: row * 3.8 + 4,
      },
    };
  });
  const buckets = buildHitIndex(items);
  const largestBucket = Math.max(...Array.from(buckets.values(), (bucket) => bucket.length));

  assert.ok(largestBucket < 400, `单个命中桶包含 ${largestBucket} 个网格`);
  assert.match(script, /map\.on\('movestart'/);
  assert.match(script, /map\.on\('moveend'/);
  assert.match(script, /map\.on\('zoomstart'/);
  assert.match(script, /map\.on\('zoomend'/);
  assert.match(functionSource('beginCameraTransaction'), /state\.drawCache = \[\]/);
  assert.match(functionSource('beginCameraTransaction'), /state\.hitBuckets = new Map\(\)/);
  assert.match(functionSource('cellAtPixel'), /ui\.gridCanvas\.hidden/);
  assert.doesNotMatch(functionSource('updateMapHover'), /scheduleMapRender/);
  assert.doesNotMatch(functionSource('renderMapCanvas'), /hoverIndex/);
  assert.doesNotMatch(script, /map\.on\('mapmove'/);
  assert.doesNotMatch(script, /map\.on\('zoomchange'/);
});

test('zoomstart 后交错 movestart，即使缺少 moveend 也会恢复 Canvas', () => {
  const harness = loadCameraTransactionHarness();

  harness.beginCameraTransaction();
  harness.beginCameraTransaction();
  harness.settleCameraTransaction();

  assert.equal(harness.state.cameraInMotion, true);
  assert.equal(harness.ui.gridCanvas.hidden, true);
  assert.deepEqual(harness.state.drawCache, []);
  assert.equal(harness.state.hitBuckets.size, 0);
  assert.equal(harness.tooltipHides(), 2);
  harness.flushNextFrame();
  assert.equal(harness.state.cameraInMotion, false);
  assert.notEqual(harness.state.renderFrame, null, '结束事务后应安排重绘');
  harness.flushNextFrame();
  assert.equal(harness.ui.gridCanvas.hidden, false);
  assert.equal(harness.state.renderCalls, 1);
});

test('movestart 后交错 zoomstart，即使缺少 zoomend 也会恢复 Canvas', () => {
  const harness = loadCameraTransactionHarness();

  harness.beginCameraTransaction();
  harness.beginCameraTransaction();
  harness.settleCameraTransaction();
  harness.flushNextFrame();

  assert.equal(harness.state.cameraInMotion, false);
  assert.notEqual(harness.state.renderFrame, null, '结束事务后应安排重绘');
  harness.flushNextFrame();
  assert.equal(harness.ui.gridCanvas.hidden, false);
  assert.equal(harness.state.renderCalls, 1);
});

test('新的 start 会取消待结算帧，长拖拽期间不会提前重绘', () => {
  const harness = loadCameraTransactionHarness();

  harness.beginCameraTransaction();
  harness.settleCameraTransaction();
  assert.equal(harness.frames.size, 1);
  harness.beginCameraTransaction();

  assert.equal(harness.frames.size, 0);
  assert.equal(harness.state.cameraInMotion, true);
  assert.equal(harness.ui.gridCanvas.hidden, true);
  assert.equal(harness.state.renderCalls, 0);
  assert.doesNotMatch(functionSource('settleCameraTransaction'), /setTimeout/);
});

test('后续 end 会重新校准稳定帧并再次重绘', () => {
  const harness = loadCameraTransactionHarness();

  harness.beginCameraTransaction();
  harness.settleCameraTransaction();
  harness.settleCameraTransaction();
  assert.equal(harness.frames.size, 1);
  harness.flushNextFrame();
  harness.flushNextFrame();
  assert.equal(harness.state.renderCalls, 1);

  harness.settleCameraTransaction();
  harness.flushNextFrame();
  harness.flushNextFrame();
  assert.equal(harness.ui.gridCanvas.hidden, false);
  assert.equal(harness.state.renderCalls, 2);
});

test('温差图层的对称中性色带包含正负边界值', () => {
  const { colorForValue } = loadColorForValue();
  const spec = {
    breaks: [-10, -5, -0.1, 0.1, 5, 10],
    palette: ['#2c7bb6', '#abd9e9', '#f7f7f7', '#fdae61', '#d7191c'],
    neutral_range: [-0.1, 0.1],
    neutral_color_index: 2,
  };

  for (const value of [-0.1, 0, 0.1]) {
    assert.equal(colorForValue(value, spec), '#f7f7f7');
  }
  assert.equal(colorForValue(-0.1001, spec), '#abd9e9');
  assert.equal(colorForValue(0.1001, spec), '#fdae61');
});

test('人口支持状态不显示虚假的全县百分位', () => {
  const { percentileText } = loadPercentileText();

  assert.equal(
    percentileText('age65_population_support', 1),
    '人口支持状态不计算百分位'
  );
  assert.equal(
    percentileText('age65_population_support', null),
    '该图层在本格无有效值'
  );
  assert.equal(
    percentileText('age65_share_pct', 20),
    '位于全县有效网格第 67 百分位'
  );
});
