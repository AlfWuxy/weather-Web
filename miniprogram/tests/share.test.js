const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const {
  ACQUISITION_STORAGE_KEY,
  ACQUISITION_TTL_MS,
  FAMILY_ENTRY_STORAGE_KEY,
  FAMILY_ENTRY_TTL_MS,
  PUBLIC_SHARE_ROUTES,
  SHARE_COVER_PATH,
  clearAcquisitionContext,
  createPageShare,
  createTimelineShare,
  normalizeSource,
  readAcquisitionSource,
  readFamilyShareEntry,
  readFamilyShareEntryRecord,
  rememberAcquisitionSource,
  showPublicShareMenu,
  sourceFromShareEvent,
} = require('../utils/share');

function fakeStorage() {
  const values = new Map();
  return {
    getStorageSync(key) { return values.get(key); },
    setStorageSync(key, value) { values.set(key, value); },
    removeStorageSync(key) { values.delete(key); },
    values,
  };
}

function loadHomePageDefinition() {
  const pagePath = require.resolve('../pages/home/index');
  const previousPage = global.Page;
  let definition;
  try {
    global.Page = (candidate) => { definition = candidate; };
    delete require.cache[pagePath];
    require(pagePath);
  } finally {
    global.Page = previousPage;
  }
  return definition;
}

function loadActionsPageDefinition() {
  const pagePath = require.resolve('../pages/actions/index');
  const previousPage = global.Page;
  let definition;
  try {
    global.Page = (candidate) => { definition = candidate; };
    delete require.cache[pagePath];
    require(pagePath);
  } finally {
    global.Page = previousPage;
  }
  return definition;
}

function loadPublicPageDefinition(name) {
  const pagePath = require.resolve(`../pages/${name}/index`);
  const previousPage = global.Page;
  let definition;
  try {
    global.Page = (candidate) => { definition = candidate; };
    delete require.cache[pagePath];
    require(pagePath);
  } finally {
    global.Page = previousPage;
  }
  return definition;
}

function pageInstance(definition) {
  const instance = Object.assign({}, definition);
  instance.data = JSON.parse(JSON.stringify(definition.data));
  instance.setData = function setData(next, callback) {
    Object.assign(this.data, next);
    if (typeof callback === 'function') callback();
  };
  return instance;
}

test('所有公开白名单页面默认不冒充家庭分享', () => {
  const expectedRoutes = [
    '/pages/home/index',
    '/pages/forecast/index',
    '/pages/community/index',
    '/pages/alerts/index',
    '/pages/actions/index',
    '/pages/cooling/index',
    '/pages/gis/index',
    '/pages/transparency/index',
    '/pages/privacy/index',
    '/pages/about/index',
  ];
  assert.deepEqual(PUBLIC_SHARE_ROUTES, expectedRoutes);
  expectedRoutes.forEach((route) => {
    assert.deepEqual(createPageShare({ route, source: 'member-42' }), {
      title: '宜老天气通：把天气预警变成今天能做的事',
      path: route,
      imageUrl: SHARE_COVER_PATH,
    });
  });
  assert.equal(normalizeSource('user_123'), '');
});

test('只有明确家庭按钮来源会写入固定 family_share', () => {
  assert.deepEqual(createPageShare({
    route: '/pages/home/index',
    source: 'family_share',
  }), {
    title: '宜老天气通：把天气预警变成今天能做的事',
    path: '/pages/home/index?from=family_share',
    imageUrl: SHARE_COVER_PATH,
  });
  assert.equal(sourceFromShareEvent({ from: 'menu' }), '');
  assert.equal(sourceFromShareEvent({
    from: 'button',
    target: { dataset: { shareSource: 'family_share' } },
  }), 'family_share');
  assert.equal(sourceFromShareEvent({
    from: 'button',
    target: { dataset: { shareSource: 'member-42', elderId: '42' } },
  }), '');
});

test('未知或个人页面回退到公开首页且不透传参数', () => {
  assert.deepEqual(createPageShare({
    route: '/pages/elders/index?elder_id=42',
    source: 'member-42',
    elderId: '42',
    deviceId: 'device-8',
  }), {
    title: '宜老天气通：把天气预警变成今天能做的事',
    path: '/pages/home/index',
    imageUrl: SHARE_COVER_PATH,
  });
});

test('朋友圈分享不携带家庭归因或隐私参数', () => {
  const result = createTimelineShare({ title: '都昌天气', source: 'family_share', deviceId: 'device-8' });
  assert.deepEqual(result, { title: '都昌天气', imageUrl: SHARE_COVER_PATH });
});

test('分享菜单同时开启好友与朋友圈入口', () => {
  let options = null;
  const api = { showShareMenu(value) { options = value; } };
  assert.equal(showPublicShareMenu(api), true);
  assert.deepEqual(options, { menus: ['shareAppMessage', 'shareTimeline'] });
  assert.equal(showPublicShareMenu({}), false);
  assert.equal(showPublicShareMenu({ showShareMenu() { throw new Error('unsupported'); } }), false);
});

test('每个公开内容页统一启用好友与朋友圈分享', () => {
  const pageNames = ['home', 'forecast', 'alerts', 'actions', 'cooling', 'community', 'gis', 'about', 'transparency', 'privacy'];
  pageNames.forEach((name) => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'pages', name, 'index.js'), 'utf8');
    assert.match(source, /showPublicShareMenu\(\)/, `${name} 应开启分享菜单`);
    assert.match(source, /onShareAppMessage\([^)]*\)[\s\S]*createPageShare\(/, `${name} 应使用安全页面分享`);
    assert.match(source, /onShareTimeline\(\)[\s\S]*createTimelineShare\(/, `${name} 应开启朋友圈分享`);
  });
});

test('家庭来源只出现在文案明确的按钮上', () => {
  const pages = ['home', 'actions'];
  pages.forEach((name) => {
    const view = fs.readFileSync(path.join(__dirname, '..', 'pages', name, 'index.wxml'), 'utf8');
    assert.match(view, /data-share-source="family_share"[^>]*(?:aria-label="[^"]*家人"|>[^<]*家人)/);
  });
});

test('统一分享封面存在且使用轻量 JPEG', () => {
  const cover = path.join(__dirname, '..', SHARE_COVER_PATH.replace(/^\//, ''));
  assert.equal(fs.existsSync(cover), true);
  const header = fs.readFileSync(cover).subarray(0, 2).toString('hex');
  assert.equal(header, 'ffd8');
  assert.ok(fs.statSync(cover).size < 50 * 1024);
  const buffer = fs.readFileSync(cover);
  let offset = 2;
  let dimensions = null;
  while (offset + 9 < buffer.length) {
    if (buffer[offset] !== 0xff) { offset += 1; continue; }
    const marker = buffer[offset + 1];
    const length = buffer.readUInt16BE(offset + 2);
    if ([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf].includes(marker)) {
      dimensions = {
        height: buffer.readUInt16BE(offset + 5),
        width: buffer.readUInt16BE(offset + 7),
      };
      break;
    }
    if (!Number.isFinite(length) || length < 2) break;
    offset += 2 + length;
  }
  assert.deepEqual(dimensions, { width: 500, height: 400 });
  assert.equal(dimensions.width / dimensions.height, 5 / 4);
});

test('家庭分享落地页跳过营销首屏并提供老人和家属双入口', () => {
  const script = fs.readFileSync(path.join(__dirname, '..', 'pages/home/index.js'), 'utf8');
  const view = fs.readFileSync(path.join(__dirname, '..', 'pages/home/index.wxml'), 'utf8');

  assert.match(script, /readFamilyShareEntryRecord\(\)/);
  assert.match(script, /entryRecord\.expiresAt - Date\.now\(\)/);
  assert.match(script, /openTodayActions\(\)[\s\S]*\/pages\/actions\/index/);
  assert.match(script, /startFamilyCare\(\)[\s\S]*\/pages\/bind-token\/index/);
  assert.match(view, /wx:if="\{\{entryContextReady && !familyShareEntry\}\}" class="hero"/);
  assert.match(view, /wx:if="\{\{entryContextReady && familyShareEntry\}\}"/);
  assert.match(view, /家人把这份天气提醒分享给你/);
  assert.match(view, /先看今天行动/);
  assert.match(view, /我是家属，开启照护/);
});

test('首页在天气指标和快捷入口前提供数据最小化的今日行动入口', () => {
  const view = fs.readFileSync(path.join(__dirname, '..', 'pages/home/index.wxml'), 'utf8');
  const style = fs.readFileSync(path.join(__dirname, '..', 'pages/home/index.wxss'), 'utf8');
  const calloutStart = view.indexOf('class="today-action-callout"');
  const weatherDetailsStart = view.indexOf('class="weather-details"');
  const quickGridStart = view.indexOf('class="quick-grid"');
  const fullActionsStart = view.indexOf('<view class="section-title">今天先做什么</view>');
  const calloutEnd = view.indexOf('</view>\n      <view class="weather-details"', calloutStart);
  const callout = view.slice(calloutStart, calloutEnd);

  assert.ok(calloutStart >= 0, '首页应包含紧凑的今天先做入口');
  assert.ok(calloutStart < weatherDetailsStart, '今天先做入口应在天气指标之前');
  assert.ok(weatherDetailsStart < quickGridStart, '天气指标应继续位于快捷入口之前');
  assert.ok(quickGridStart < fullActionsStart, '后面的三条完整行动区应继续保留');
  assert.match(callout, /role="region"[^>]*aria-role="region"[^>]*aria-label="今天先做"/);
  assert.match(callout, /wx:if="\{\{topActions\.length\}\}"[^>]*>\{\{topActions\[0\]\.title\}\}/);
  assert.match(callout, /先查看通用防护清单/);
  assert.match(callout, /aria-label="\{\{topActions\.length \? '打开完整今日行动清单' : '打开通用防护清单'\}\}"/);
  assert.doesNotMatch(callout, /item\.detail|pair_id|member_id|elder_id|姓名|健康资料/);
  assert.match(style, /\.today-action-button\s*\{[^}]*min-height:\s*88rpx[^}]*font-size:\s*16px/);
  assert.match(view, /wx:for="\{\{topActions\}\}"/);
});

test('较早公共天气隐藏旧风险分数并退回通用行动', () => {
  const result = {
    data: {
      available: true,
      location: { name: '都昌县' },
      current: { temperature: 38, temperature_max: 40, temperature_min: 30 },
      risk: { available: true, score: 88, label: '高风险', summary: '旧风险结论' },
      actions: [{ id: 'stale-heat-action', title: '旧高温行动', detail: '旧数据生成' }],
    },
    meta: { source: 'stale-cache', stale: true },
  };

  const homePage = pageInstance(loadHomePageDefinition());
  homePage.renderSnapshot.call(homePage, result);
  assert.equal(homePage.data.freshness.stale, true);
  assert.equal(homePage.data.snapshot.risk.label, '风险待刷新');
  assert.equal(homePage.data.snapshot.risk.scoreText, '待刷新');
  assert.equal(homePage.data.snapshot.risk.summary, '');
  assert.deepEqual(homePage.data.snapshot.warnings, []);
  assert.equal(homePage.data.snapshot.warningsStatusText, '官方预警待刷新');
  assert.deepEqual(homePage.data.topActions, []);

  const actionsPage = pageInstance(loadActionsPageDefinition());
  actionsPage.renderActions.call(actionsPage, result);
  assert.equal(actionsPage.data.generalMode, true);
  assert.equal(actionsPage.data.freshness.stale, true);
  assert.equal(actionsPage.data.actions.some((item) => item.id === 'stale-heat-action'), false);
  assert.equal(actionsPage.data.actions[0].id, 'general-water');
  assert.match(actionsPage.data.error, /通用安全清单/);

  const homeView = fs.readFileSync(path.join(__dirname, '..', 'pages/home/index.wxml'), 'utf8');
  const actionsView = fs.readFileSync(path.join(__dirname, '..', 'pages/actions/index.wxml'), 'utf8');
  assert.match(homeView, /较早观测/);
  assert.match(homeView, /较早天气不生成风险行动/);
  assert.match(actionsView, /较早天气已切换为通用清单/);
});

test('首页只向视图层传递当前天气和前三项行动', () => {
  const result = {
    data: {
      available: true,
      location: { name: '都昌县' },
      current: { temperature: 35, temperature_max: 38, temperature_min: 28 },
      risk: { score: 72, level: '高风险' },
      warnings: [],
      source_status: { warnings: { available: true }, weather: { available: true } },
      forecast: Array.from({ length: 7 }, (_, index) => ({
        date: `2026-07-${String(index + 18).padStart(2, '0')}`,
        temperature_max: 36 + index,
      })),
      actions: Array.from({ length: 6 }, (_, index) => ({
        id: `action-${index + 1}`,
        title: `行动 ${index + 1}`,
      })),
    },
    meta: { source: 'network', stale: false },
  };
  const page = pageInstance(loadHomePageDefinition());

  page.renderSnapshot.call(page, result);

  assert.equal(page.data.snapshot.current.temperature, 35);
  assert.equal(page.data.snapshot.warningsStatusText, '当前暂无预警');
  assert.equal(Object.hasOwn(page.data.snapshot, 'forecast'), false);
  assert.equal(Object.hasOwn(page.data.snapshot, 'actions'), false);
  assert.equal(Object.hasOwn(page.data.snapshot, 'sources'), false);
  assert.deepEqual(page.data.topActions.map((item) => item.id), ['action-1', 'action-2', 'action-3']);
});

test('较早预警和预报不继续展示有效风险结论', () => {
  const result = {
    data: {
      available: true,
      location: { name: '都昌县' },
      current: { temperature: 38, temperature_max: 40, temperature_min: 30 },
      warnings: [{ id: 'old-warning', title: '旧高温预警', level: '橙色' }],
      forecast: [{
        date: '2026-07-18',
        temperature_max: 40,
        temperature_min: 30,
        risk_score: 90,
        risk_level: '高风险',
      }],
      source_status: { warning: { available: true } },
    },
    meta: { source: 'stale-cache', stale: true },
  };

  const alertsPage = pageInstance(loadPublicPageDefinition('alerts'));
  alertsPage.renderWarnings.call(alertsPage, result);
  assert.equal(alertsPage.data.freshness.stale, true);
  assert.deepEqual(alertsPage.data.warnings, []);
  assert.equal(alertsPage.data.warningsSourceAvailable, false);

  const forecastPage = pageInstance(loadPublicPageDefinition('forecast'));
  forecastPage.renderForecast.call(forecastPage, result);
  assert.equal(forecastPage.data.freshness.stale, true);
  assert.equal(forecastPage.data.highRiskDays, 0);
  assert.equal(forecastPage.data.forecast[0].riskLabel, '风险待刷新');
  assert.equal(forecastPage.data.forecast[0].scoreText, '待刷新');
  assert.equal(forecastPage.data.forecast[0].tone, 'unknown');

  const alertsView = fs.readFileSync(path.join(__dirname, '..', 'pages/alerts/index.wxml'), 'utf8');
  const forecastView = fs.readFileSync(path.join(__dirname, '..', 'pages/forecast/index.wxml'), 'utf8');
  assert.match(alertsView, /官方预警有效性待核对/);
  assert.match(alertsView, /较早观测/);
  assert.match(forecastView, /高风险日统计待刷新/);
  assert.match(forecastView, /较早 7 天预报/);
});

test('完成公共行动后的回执由用户主动分享且不包含个人细节', () => {
  const page = pageInstance(loadActionsPageDefinition());
  page.data.completedCount = 1;
  page.data.locationName = '都昌县';
  const receipt = page.onShareAppMessage({
    from: 'button',
    target: {
      dataset: {
        shareKind: 'completion_receipt',
        shareSource: 'family_share',
      },
    },
  });
  assert.deepEqual(receipt, {
    title: '我已看到，并完成一项防护准备',
    path: '/pages/actions/index?from=family_share',
    imageUrl: SHARE_COVER_PATH,
  });

  const view = fs.readFileSync(path.join(__dirname, '..', 'pages/actions/index.wxml'), 'utf8');
  assert.match(view, /wx:if="\{\{completedCount > 0\}\}"/);
  assert.match(view, /data-share-kind="completion_receipt"/);
  assert.match(view, /回发给家人/);
  assert.match(view, /不包含姓名、健康资料或具体行动记录/);
});

test('未完成行动时不能伪造完成回执标题', () => {
  const page = pageInstance(loadActionsPageDefinition());
  page.data.completedCount = 0;
  page.data.locationName = '都昌县';
  const result = page.onShareAppMessage({
    from: 'button',
    target: {
      dataset: {
        shareKind: 'completion_receipt',
        shareSource: 'family_share',
      },
    },
  });
  assert.equal(result.title, '都昌县今日防护清单');
});

test('分享来源只在本机保留三十天并能自动过期', () => {
  const storage = fakeStorage();
  const now = 1_000_000;
  assert.equal(rememberAcquisitionSource({ from: 'family_share' }, storage, now), 'family_share');
  assert.deepEqual(storage.values.get(ACQUISITION_STORAGE_KEY), {
    source: 'family_share',
    expires_at: now + ACQUISITION_TTL_MS,
  });
  assert.equal(readAcquisitionSource(storage, now + ACQUISITION_TTL_MS - 1), 'family_share');
  assert.equal(readAcquisitionSource(storage, now + ACQUISITION_TTL_MS), '');
  assert.equal(storage.values.has(ACQUISITION_STORAGE_KEY), false);
});

test('登录完成后一次性消费家庭来源，防止共享设备串号', () => {
  const storage = fakeStorage();
  rememberAcquisitionSource({ from: 'family_share' }, storage, 1_000_000);

  assert.equal(clearAcquisitionContext(storage), true);
  assert.equal(storage.values.has(ACQUISITION_STORAGE_KEY), false);
  assert.equal(storage.values.has(FAMILY_ENTRY_STORAGE_KEY), false);
});

test('家庭落地提示与三十天登录归因分别过期', () => {
  const storage = fakeStorage();
  const now = 2_000_000;
  rememberAcquisitionSource({ from: 'family_share' }, storage, now);
  assert.deepEqual(storage.values.get(FAMILY_ENTRY_STORAGE_KEY), {
    source: 'family_share',
    expires_at: now + FAMILY_ENTRY_TTL_MS,
  });
  assert.deepEqual(readFamilyShareEntryRecord(storage, now + FAMILY_ENTRY_TTL_MS - 1), {
    source: 'family_share',
    expiresAt: now + FAMILY_ENTRY_TTL_MS,
  });
  assert.equal(readFamilyShareEntry(storage, now + FAMILY_ENTRY_TTL_MS - 1), 'family_share');
  assert.equal(readFamilyShareEntry(storage, now + FAMILY_ENTRY_TTL_MS), '');
  assert.equal(readAcquisitionSource(storage, now + FAMILY_ENTRY_TTL_MS), 'family_share');
  assert.equal(readAcquisitionSource(storage, now + ACQUISITION_TTL_MS), '');
});

test('家庭落地提示在 hide 后回到页面会重排到期计时并自动消失', () => {
  const storage = fakeStorage();
  const originalWx = global.wx;
  const originalNow = Date.now;
  const originalSetTimeout = global.setTimeout;
  const originalClearTimeout = global.clearTimeout;
  const startedAt = 4_000_000;
  let currentTime = startedAt;
  let nextTimerId = 0;
  const timers = new Map();
  rememberAcquisitionSource({ from: 'family_share' }, storage, startedAt);
  global.wx = storage;
  Date.now = () => currentTime;
  global.setTimeout = (callback, delay) => {
    const timerId = ++nextTimerId;
    timers.set(timerId, { callback, delay, cleared: false });
    return timerId;
  };
  global.clearTimeout = (timerId) => {
    const timer = timers.get(timerId);
    if (timer) timer.cleared = true;
  };

  try {
    const page = pageInstance(loadHomePageDefinition());
    page._unloaded = false;
    page._publicPageVisible = true;
    page.loadData = () => {};
    page.updateEntryContext();
    const firstTimer = timers.get(page._familyEntryTimer);
    assert.equal(page.data.familyShareEntry, true);
    assert.equal(firstTimer.delay, FAMILY_ENTRY_TTL_MS);

    page.onHide();
    assert.equal(firstTimer.cleared, true);
    currentTime += 60_000;
    page.onShow();
    const resumedTimer = timers.get(page._familyEntryTimer);
    assert.equal(page._publicPageVisible, true);
    assert.equal(resumedTimer.delay, FAMILY_ENTRY_TTL_MS - 60_000);

    currentTime = startedAt + FAMILY_ENTRY_TTL_MS;
    resumedTimer.callback();
    assert.equal(page.data.familyShareEntry, false);
    assert.equal(storage.values.has(FAMILY_ENTRY_STORAGE_KEY), false);
  } finally {
    global.wx = originalWx;
    Date.now = originalNow;
    global.setTimeout = originalSetTimeout;
    global.clearTimeout = originalClearTimeout;
  }
});

test('未知来源不会写入本地存储', () => {
  const storage = fakeStorage();
  assert.equal(rememberAcquisitionSource({ from: 'campaign-free-text' }, storage, 100), '');
  assert.equal(storage.values.size, 0);
});
