const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {
  beginPublicPage,
  hidePublicPage,
  nextRefreshAt,
  pageCanRender,
  schedulePublicRefresh,
  showPublicPage,
  staleRetryMeta,
  unloadPublicPage,
} = require('../utils/public-page-lifecycle');

const miniRoot = path.resolve(__dirname, '..');

test('公共页显示、隐藏和卸载状态阻止隐藏后的异步渲染', () => {
  const page = {};
  beginPublicPage(page);
  assert.equal(pageCanRender(page), true);
  hidePublicPage(page);
  assert.equal(pageCanRender(page), false);
  let reloads = 0;
  showPublicPage(page, () => { reloads += 1; });
  assert.equal(pageCanRender(page), true);
  assert.equal(reloads, 1);
  unloadPublicPage(page);
  assert.equal(pageCanRender(page), false);
});

test('freshness 下一次检查优先遵守失败退避时间', () => {
  const originalNow = Date.now;
  Date.now = () => 1000;
  try {
    assert.equal(nextRefreshAt({ effectiveExpiresAt: 2000 }), 2000);
    assert.equal(nextRefreshAt({ effectiveExpiresAt: 900, retryAfter: 3000 }), 3000);
    assert.equal(nextRefreshAt({ effectiveExpiresAt: 900, refreshStarted: true }), null);
    assert.equal(nextRefreshAt({}), null);
  } finally {
    Date.now = originalNow;
  }
});

test('公共失败元数据统一降级为较早状态并按指定间隔重试', () => {
  const originalNow = Date.now;
  Date.now = () => 1000;
  try {
    const meta = staleRetryMeta({ updatedText: '刚刚更新', effectiveExpiresAt: 900 }, 60 * 1000);
    assert.equal(meta.updatedText, '刚刚更新');
    assert.equal(meta.stale, true);
    assert.equal(meta.source, 'stale-cache');
    assert.equal(meta.refreshDeferred, false);
    assert.equal(meta.refreshStarted, false);
    assert.equal(meta.effectiveExpiresAt, null);
    assert.equal(meta.retryAfter, 61 * 1000);
    assert.equal(staleRetryMeta({}, 0).retryAfter, 61 * 1000);
  } finally {
    Date.now = originalNow;
  }
});

test('快照到期计时器只在页面可见时调用既有加载函数', () => {
  const originalNow = Date.now;
  const originalSetTimeout = global.setTimeout;
  const originalClearTimeout = global.clearTimeout;
  let scheduled = null;
  Date.now = () => 1000;
  global.setTimeout = (callback, delay) => {
    scheduled = { callback, delay };
    return 9;
  };
  global.clearTimeout = () => {};
  try {
    const page = {};
    beginPublicPage(page);
    let reloads = 0;
    schedulePublicRefresh(page, { effectiveExpiresAt: 1500 }, () => { reloads += 1; });
    assert.equal(scheduled.delay, 520);
    scheduled.callback();
    assert.equal(reloads, 1);

    hidePublicPage(page);
    schedulePublicRefresh(page, { effectiveExpiresAt: 1600 }, () => { reloads += 1; });
    assert.equal(reloads, 1);
  } finally {
    Date.now = originalNow;
    global.setTimeout = originalSetTimeout;
    global.clearTimeout = originalClearTimeout;
  }
});

test('八个动态公共页统一在 onShow 读取缓存并按过期时间调度', () => {
  const names = ['home', 'forecast', 'alerts', 'actions', 'community', 'cooling', 'gis', 'transparency'];
  names.forEach((name) => {
    const source = fs.readFileSync(path.join(miniRoot, 'pages', name, 'index.js'), 'utf8');
    assert.match(source, /onShow\(\)[\s\S]*showPublicPage\(this(?:,|\))/s, `${name} 缺少 onShow 显示态恢复`);
    assert.match(source, /onShow\(\)[\s\S]*this\.load(?:Data|Metadata|Sources)\(/s, `${name} 缺少 onShow 缓存重检`);
    assert.match(source, /onHide\(\)[\s\S]*hidePublicPage\(this\)/s, `${name} 缺少隐藏守卫`);
    assert.match(source, /schedulePublicRefresh\(this, result\.meta,/s, `${name} 缺少快照到期调度`);
    assert.match(source, /pageCanRender\(this\)/, `${name} 缺少异步渲染守卫`);
  });
});
