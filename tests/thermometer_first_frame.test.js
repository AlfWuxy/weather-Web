const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const source = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'js', 'yilao-data-fx-extra.js'),
  'utf8',
);

test('温度计沿用服务端首屏数字且保留差值动画', () => {
  assert.match(source, /const renderedValue = parseFloat\(valEl\.textContent\)/);
  assert.match(source, /const startValue = Number\.isFinite\(renderedValue\)/);
  assert.match(source, /animateNumber\(valEl, startValue, t,/);
  assert.doesNotMatch(source, /animateNumber\(valEl, 0, t,/);
});
