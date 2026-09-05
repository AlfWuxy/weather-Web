const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const { allowsJsMotion, safeJsDuration } = require('../utils/motion');

test('JS 滚动和进度动画采用即时完成策略', () => {
  assert.equal(allowsJsMotion(), false);
  assert.equal(safeJsDuration(240), 0);
  assert.equal(safeJsDuration(0), 0);
});

test('动效策略不读取未公开的系统字段，CSS 保留系统媒体查询', () => {
  const utility = fs.readFileSync(path.join(__dirname, '..', 'utils/motion.js'), 'utf8');
  const styles = fs.readFileSync(path.join(__dirname, '..', 'app.wxss'), 'utf8');
  assert.doesNotMatch(utility, /getSystemSetting|getDeviceInfo|getSystemInfoSync|reduceMotion/);
  assert.match(styles, /@media\s*\(prefers-reduced-motion:\s*reduce\)/);
});
