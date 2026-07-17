const test = require('node:test');
const assert = require('node:assert/strict');

const { motionDuration, prefersReducedMotion } = require('../utils/motion');

test('系统减少动态效果时关闭 JS 滚动和进度动画', () => {
  const reduced = { getSystemSetting: () => ({ reduceMotion: true }) };
  const ordinary = { getSystemSetting: () => ({ reduceMotion: false }) };
  assert.equal(prefersReducedMotion(reduced), true);
  assert.equal(motionDuration(240, reduced), 0);
  assert.equal(motionDuration(240, ordinary), 240);
  assert.equal(prefersReducedMotion({}), false);
});

test('新系统信息接口可用时不调用已弃用 getSystemInfoSync', () => {
  let legacyCalls = 0;
  const api = {
    getSystemSetting: () => ({}),
    getDeviceInfo: () => ({ reduceMotion: true }),
    getSystemInfoSync: () => { legacyCalls += 1; return {}; },
  };
  assert.equal(prefersReducedMotion(api), true);
  assert.equal(legacyCalls, 0);
});
