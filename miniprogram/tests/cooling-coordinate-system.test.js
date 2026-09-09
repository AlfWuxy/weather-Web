const test = require('node:test');
const assert = require('node:assert/strict');

const { normalizeCommunity } = require('../utils/format');

test('避暑资源规范化保留集合和单项的坐标系声明', () => {
  const result = normalizeCommunity({
    coordinate_system: 'GCJ-02',
    cooling: [
      {
        id: 'gcj02',
        coordinate_system: 'GCJ-02',
        latitude: 29.3,
        longitude: 116.2,
      },
      {
        id: 'wgs84',
        coordinate_system: 'WGS84',
        latitude: 29.4,
        longitude: 116.3,
      },
      {
        id: 'missing',
        latitude: 29.5,
        longitude: 116.4,
      },
    ],
  });

  assert.equal(result.coordinateSystem, 'GCJ-02');
  assert.deepEqual(result.cooling.map((item) => item.coordinateSystem), [
    'GCJ-02',
    'WGS84',
    '',
  ]);
});
