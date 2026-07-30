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
  assert.match(functionSource('suspendMapCanvas'), /state\.drawCache = \[\]/);
  assert.match(functionSource('suspendMapCanvas'), /state\.hitBuckets = new Map\(\)/);
  assert.match(functionSource('cellAtPixel'), /ui\.gridCanvas\.hidden/);
  assert.doesNotMatch(functionSource('updateMapHover'), /scheduleMapRender/);
  assert.doesNotMatch(functionSource('renderMapCanvas'), /hoverIndex/);
  assert.doesNotMatch(script, /map\.on\('mapmove'/);
  assert.doesNotMatch(script, /map\.on\('zoomchange'/);
});
