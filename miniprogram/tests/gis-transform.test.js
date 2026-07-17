const test = require('node:test');
const assert = require('node:assert/strict');

const {
  colorForValue,
  hitTest,
  makeCanvasModel,
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
