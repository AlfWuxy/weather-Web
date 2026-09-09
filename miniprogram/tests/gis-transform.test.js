const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {
  colorForValue,
  hitTest,
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
  const fixturePath = path.resolve(__dirname, '../../static/data/gis/duchang_heat_exposure_cells.geojson');
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
