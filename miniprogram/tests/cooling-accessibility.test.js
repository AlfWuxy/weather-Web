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

test('定位、手选与地图入口有可读语义和诚实空态', () => {
  const view = fs.readFileSync(path.join(miniRoot, 'pages/cooling/index.wxml'), 'utf8');

  assert.match(view, /role="region" aria-role="region" aria-label="附近避暑资源定位控制"/);
  assert.match(view, /bindtap="startNearbyLocation"[^>]*aria-label="逐次确认后使用本次位置排列避暑资源"/);
  assert.match(view, /bindtap="showManualSelection"[^>]*aria-label="不使用设备定位，手动选择社区"/);
  assert.match(view, /role="status" aria-role="status" aria-live="polite"/);
  assert.match(view, /<picker[^>]*aria-label="手动选择所在社区"/);
  assert.match(view, /wx:if="\{\{item\.hasCoordinates\}\}"[^>]*bindtap="openResourceLocation"/);
  assert.match(view, /系统不会显示虚构地点/);
});

test('避暑页局部交互控件达到 88rpx 且正文不小于 14px', () => {
  const style = fs.readFileSync(path.join(miniRoot, 'pages/cooling/index.wxss'), 'utf8');
  const filterRule = style.match(/\.filter-pill\s*\{[^}]+\}/s);
  const actionRule = style.match(/\.small-button\s*\{[^}]+\}/s);
  const locationRule = style.match(/\.location-button\s*\{[^}]+\}/s);
  const pickerRule = style.match(/\.community-picker\s*\{[^}]+\}/s);

  assert.ok(filterRule);
  assert.match(filterRule[0], /min-height:\s*88rpx/);
  assert.match(filterRule[0], /font-size:\s*(?:28rpx|14px)/);
  assert.ok(actionRule);
  assert.match(actionRule[0], /min-height:\s*88rpx/);
  assert.match(actionRule[0], /font-size:\s*(?:28rpx|14px)/);
  assert.ok(locationRule);
  assert.match(locationRule[0], /min-height:\s*88rpx/);
  assert.match(locationRule[0], /font-size:\s*(?:28rpx|14px)/);
  assert.ok(pickerRule);
  assert.match(pickerRule[0], /min-height:\s*88rpx/);
  assert.match(pickerRule[0], /font-size:\s*(?:30rpx|15px)/);
  const localFontSizes = Array.from(style.matchAll(/font-size:\s*(\d+)rpx/g), (match) => Number(match[1]));
  assert.equal(localFontSizes.every((size) => size >= 28), true);
  const localPxSizes = Array.from(style.matchAll(/font-size:\s*(\d+(?:\.\d+)?)px/g), (match) => Number(match[1]));
  assert.equal(localPxSizes.every((size) => size >= 14), true);
});
