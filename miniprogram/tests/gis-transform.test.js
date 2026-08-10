const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {
  colorForValue,
  enrichDerivedLayers,
  formatLayerValue,
  hitTest,
  legendEntries,
  makeCanvasModel,
  project,
  resolveLayer,
} = require('../utils/gis-transform');

function sampleCollection() {
  return {
    type: 'FeatureCollection',
    metadata: {
      layers: {
        age65_share_pct: {
          label: '老年人口', short_label: '65+', unit: '%', digits: 1,
          breaks: [0, 10, 20], palette: ['#111111', '#222222'], source: 'test',
        },
      },
    },
    features: [
      {
        type: 'Feature',
        properties: { feature_type: 'study_boundary' },
        geometry: { type: 'Polygon', coordinates: [[[115, 29], [117, 29], [117, 30], [115, 30], [115, 29]]] },
      },
      {
        type: 'Feature', id: 'cell-1',
        properties: { feature_type: 'modis_cell', cell_id: 'cell-1', age65_share_pct: 15 },
        geometry: { type: 'Polygon', coordinates: [[[116, 29.2], [116.1, 29.2], [116.1, 29.3], [116, 29.3], [116, 29.2]]] },
      },
    ],
  };
}

test('GeoJSON 转换为 Canvas 模型并可命中网格', () => {
  const collection = sampleCollection();
  const model = makeCanvasModel(collection, 'age65_share_pct', 300, 240, 10);
  assert.equal(model.cells.length, 1);
  assert.equal(model.cells[0].color, '#222222');
  const centerX = (model.cells[0].minX + model.cells[0].maxX) / 2;
  const centerY = (model.cells[0].minY + model.cells[0].maxY) / 2;
  assert.equal(hitTest(model.cells, centerX, centerY).id, 'cell-1');
});

test('图层断点映射稳定', () => {
  const spec = resolveLayer(sampleCollection(), 'age65_share_pct');
  assert.equal(colorForValue(0, spec), '#111111');
  assert.equal(colorForValue(15, spec), '#222222');
  assert.equal(colorForValue(null, spec), '#ddd8d3');
});

test('现有网格可在内存中派生 65+ 与地表温度比较图层且不改写原数据', () => {
  const collection = {
    type: 'FeatureCollection',
    metadata: { layers: {} },
    features: [
      {
        type: 'Feature',
        properties: { feature_type: 'study_boundary' },
        geometry: { type: 'Polygon', coordinates: [] },
      },
      ...[
        { id: 'a', age: 10, support: true, lst: 20 },
        { id: 'b', age: null, support: false, lst: 30 },
        { id: 'c', age: 30, support: true, lst: 40 },
      ].map((item) => ({
        type: 'Feature',
        id: item.id,
        properties: {
          feature_type: 'modis_cell',
          cell_id: item.id,
          age65_share_pct: item.age,
          positive_population_support: item.support,
          q3_lst_c_mean: item.lst,
        },
        geometry: { type: 'Polygon', coordinates: [] },
      })),
    ],
  };

  const enriched = enrichDerivedLayers(collection);
  const cells = enriched.features.filter((feature) => feature.properties.feature_type === 'modis_cell');

  assert.equal(Object.hasOwn(collection.features[1].properties, 'age65_percentile'), false);
  assert.deepEqual(cells.map((feature) => feature.properties.age65_percentile), [50, null, 100]);
  assert.deepEqual(cells.map((feature) => feature.properties.age65_population_support), [1, 0, 1]);
  assert.deepEqual(cells.map((feature) => feature.properties.q3_lst_delta_median_c), [-10, 0, 10]);
  assert.deepEqual(cells.map((feature) => feature.properties.q3_lst_percentile), [33, 67, 100]);
  const deltaLayer = enriched.metadata.layers.q3_lst_delta_median_c;
  assert.deepEqual(
    deltaLayer.palette,
    ['#2c7bb6', '#abd9e9', '#f7f7f7', '#fdae61', '#d7191c']
  );
  assert.deepEqual(deltaLayer.breaks, [-10, -5, -0.1, 0.1, 5, 10]);
  assert.deepEqual(deltaLayer.neutral_range, [-0.1, 0.1]);
  assert.equal(deltaLayer.neutral_color_index, 2);
  assert.equal(colorForValue(0, deltaLayer), '#f7f7f7');
  assert.equal(colorForValue(-0.05, deltaLayer), '#f7f7f7');
  assert.equal(colorForValue(0.05, deltaLayer), '#f7f7f7');
  assert.equal(colorForValue(-0.1, deltaLayer), '#f7f7f7');
  assert.equal(colorForValue(0.1, deltaLayer), '#f7f7f7');
  assert.equal(colorForValue(-0.1001, deltaLayer), '#abd9e9');
  assert.equal(colorForValue(0.1001, deltaLayer), '#fdae61');
  assert.equal(enriched.metadata.layers.age65_percentile.valid_cells, 2);
  assert.equal(enriched.metadata.layers.age65_percentile.missing_cells, 1);
  assert.deepEqual(
    [
      enriched.metadata.layers.age65_percentile.min,
      enriched.metadata.layers.age65_percentile.median,
      enriched.metadata.layers.age65_percentile.max,
    ],
    [50, 75, 100]
  );
  assert.deepEqual(
    [
      enriched.metadata.layers.q3_lst_percentile.min,
      enriched.metadata.layers.q3_lst_percentile.median,
      enriched.metadata.layers.q3_lst_percentile.max,
    ],
    [33, 67, 100]
  );
  assert.equal(
    formatLayerValue(0, enriched.metadata.layers.age65_population_support),
    '无正人口支持'
  );
  assert.deepEqual(
    legendEntries(enriched.metadata.layers.age65_population_support).map((item) => item.label),
    ['无正人口支持', '有正人口支持']
  );
});

test('单网格与全同值的派生图层使用真实百分位和支持状态统计', () => {
  const collection = sampleCollection();
  collection.features[1].properties.positive_population_support = true;
  collection.features[1].properties.q3_lst_c_mean = 30;

  const enriched = enrichDerivedLayers(collection);
  const cell = enriched.features[1];
  const layers = enriched.metadata.layers;

  assert.equal(cell.properties.age65_percentile, 100);
  assert.equal(cell.properties.q3_lst_percentile, 100);
  assert.deepEqual(
    [layers.age65_percentile.min, layers.age65_percentile.median, layers.age65_percentile.max],
    [100, 100, 100]
  );
  assert.deepEqual(
    [layers.q3_lst_percentile.min, layers.q3_lst_percentile.median, layers.q3_lst_percentile.max],
    [100, 100, 100]
  );
  assert.deepEqual(
    [layers.age65_population_support.min, layers.age65_population_support.median, layers.age65_population_support.max],
    [1, 1, 1]
  );
});

test('bbox 内但多边形外的点不会误命中', () => {
  const triangle = {
    id: 'triangle',
    path: [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 0, y: 10 }, { x: 0, y: 0 }],
    minX: 0,
    maxX: 10,
    minY: 0,
    maxY: 10,
  };
  assert.equal(hitTest([triangle], 9, 9), null);
  assert.equal(hitTest([triangle], 2, 2).id, 'triangle');
});

test('共享边界按绘制顺序确定归属', () => {
  const left = {
    id: 'left',
    path: [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }, { x: 0, y: 0 }],
    minX: 0, maxX: 10, minY: 0, maxY: 10,
  };
  const right = {
    id: 'right',
    path: [{ x: 10, y: 0 }, { x: 20, y: 0 }, { x: 20, y: 10 }, { x: 10, y: 10 }, { x: 10, y: 0 }],
    minX: 10, maxX: 20, minY: 0, maxY: 10,
  };
  assert.equal(hitTest([left, right], 10, 5).id, 'right');
  assert.equal(hitTest([right, left], 10, 5).id, 'left');
});

test('真实都昌 GIS 的全部网格质心命中自身多边形', () => {
  const fixturePath = path.resolve(__dirname, '../../data/gis/duchang_heat_exposure_cells.geojson');
  const collection = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
  const padding = 16;
  const model = makeCanvasModel(collection, 'q3_lst_c_mean', 750, 900, padding);
  const failures = [];
  model.cells.forEach((cell) => {
    const center = project(
      Number(cell.properties.center_lon_wgs84),
      Number(cell.properties.center_lat_wgs84),
      model.bounds,
      model.width,
      model.height,
      padding,
    );
    const matched = hitTest(model.cells, center.x, center.y);
    if (!matched || matched.id !== cell.id) failures.push([cell.id, matched && matched.id]);
  });
  assert.equal(model.cells.length, 2593);
  assert.deepEqual(failures, []);
});
