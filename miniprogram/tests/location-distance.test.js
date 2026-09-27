const test = require('node:test');
const assert = require('node:assert/strict');

const {
  formatDistanceMeters,
  haversineDistanceMeters,
  normalizePoint,
  sortResourcesByDistance,
} = require('../utils/location-distance');

test('坐标规范化只接受成对有限且未越界的经纬度', () => {
  assert.deepEqual(normalizePoint({ latitude: '29.3', longitude: '116.2' }), {
    latitude: 29.3,
    longitude: 116.2,
  });
  assert.deepEqual(normalizePoint({ lat: 0, lng: 0 }), { latitude: 0, longitude: 0 });
  assert.equal(normalizePoint({ latitude: '', longitude: 116.2 }), null);
  assert.equal(normalizePoint({ latitude: 91, longitude: 116.2 }), null);
  assert.equal(normalizePoint({ latitude: 29.3, longitude: Infinity }), null);
});

test('Haversine 返回稳定米数并拒绝无效端点', () => {
  assert.equal(haversineDistanceMeters(
    { latitude: 29.3, longitude: 116.2 },
    { latitude: 29.3, longitude: 116.2 }
  ), 0);
  const oneLatitudeDegree = haversineDistanceMeters(
    { latitude: 0, longitude: 0 },
    { latitude: 1, longitude: 0 }
  );
  assert.ok(Math.abs(oneLatitudeDegree - 111194.93) < 0.1);
  assert.equal(haversineDistanceMeters({}, { latitude: 1, longitude: 0 }), null);
});

test('距离展示使用近似米数与公里数', () => {
  assert.equal(formatDistanceMeters(null), '');
  assert.equal(formatDistanceMeters(-1), '');
  assert.equal(formatDistanceMeters(0), '约 0 米');
  assert.equal(formatDistanceMeters(846), '约 850 米');
  assert.equal(formatDistanceMeters(1560), '约 1.6 公里');
  assert.equal(formatDistanceMeters(10200), '约 10 公里');
});

test('有坐标资源按距离稳定排序，缺坐标资源保持原顺序排后', () => {
  const resources = [
    { id: 'missing-a', name: '缺坐标甲' },
    { id: 'far', latitude: 29.3, longitude: 116.02 },
    { id: 'near-a', latitude: 29.3, longitude: 116.001 },
    { id: 'near-b', latitude: 29.3, longitude: 116.001 },
    { id: 'missing-b', latitude: null, longitude: 116.1 },
  ];
  const ranked = sortResourcesByDistance(resources, { latitude: 29.3, longitude: 116 });

  assert.deepEqual(ranked.map((item) => item.id), [
    'near-a',
    'near-b',
    'far',
    'missing-a',
    'missing-b',
  ]);
  assert.equal(ranked[0].hasCoordinates, true);
  assert.ok(ranked[0].distanceMeters < ranked[2].distanceMeters);
  assert.equal(ranked[3].hasCoordinates, false);
  assert.equal(ranked[3].distanceMeters, null);
  assert.equal(ranked[3].distanceText, '');
  assert.equal(Object.hasOwn(resources[1], 'distanceMeters'), false);
});

test('未提供本次位置时只标记公共资源坐标且保留服务器顺序', () => {
  const resources = [
    { id: 'second', latitude: 29.4, longitude: 116.3 },
    { id: 'first', latitude: 29.2, longitude: 116.1 },
  ];
  const result = sortResourcesByDistance(resources, null);
  assert.deepEqual(result.map((item) => item.id), ['second', 'first']);
  assert.equal(result.every((item) => item.hasCoordinates), true);
  assert.equal(result.every((item) => item.distanceMeters === null), true);
});
