const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const miniRoot = path.resolve(__dirname, '..');

test('避暑筛选同时提供语义状态与非颜色选中标记', () => {
  const view = fs.readFileSync(path.join(miniRoot, 'pages/cooling/index.wxml'), 'utf8');

  assert.match(view, /role="tablist" aria-role="tablist" aria-label="避暑资源筛选条件"/);
  assert.equal((view.match(/aria-pressed=/g) || []).length, 3);
  assert.equal((view.match(/aria-selected=/g) || []).length, 3);
  assert.equal((view.match(/class="filter-icon"/g) || []).length, 3);
  assert.equal((view.match(/src="\/assets\/icons\/check-white\.png"/g) || []).length, 3);
  assert.equal((view.match(/class="filter-state"/g) || []).length, 3);
  assert.doesNotMatch(view, /[✓○]/);
});

test('避暑页局部交互控件达到 88rpx 且正文不小于 14px', () => {
  const style = fs.readFileSync(path.join(miniRoot, 'pages/cooling/index.wxss'), 'utf8');
  const filterRule = style.match(/\.filter-pill\s*\{[^}]+\}/s);
  const actionRule = style.match(/\.small-button\s*\{[^}]+\}/s);

  assert.ok(filterRule);
  assert.match(filterRule[0], /min-height:\s*88rpx/);
  assert.match(filterRule[0], /font-size:\s*(?:28rpx|14px)/);
  assert.ok(actionRule);
  assert.match(actionRule[0], /min-height:\s*88rpx/);
  assert.match(actionRule[0], /font-size:\s*(?:28rpx|14px)/);
  const localFontSizes = Array.from(style.matchAll(/font-size:\s*(\d+)rpx/g), (match) => Number(match[1]));
  assert.equal(localFontSizes.every((size) => size >= 28), true);
  const localPxSizes = Array.from(style.matchAll(/font-size:\s*(\d+(?:\.\d+)?)px/g), (match) => Number(match[1]));
  assert.equal(localPxSizes.every((size) => size >= 14), true);
});
