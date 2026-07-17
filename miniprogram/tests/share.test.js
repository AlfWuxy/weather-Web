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

test('家庭分享落地页解释来源并提供主动照护入口', () => {
  const script = fs.readFileSync(path.join(__dirname, '..', 'pages/home/index.js'), 'utf8');
  const view = fs.readFileSync(path.join(__dirname, '..', 'pages/home/index.wxml'), 'utf8');

  assert.match(script, /readFamilyShareEntryRecord\(\)/);
  assert.match(script, /entryRecord\.expiresAt - Date\.now\(\)/);
  assert.match(script, /startFamilyCare\(\)[\s\S]*\/pages\/bind-token\/index/);
  assert.match(view, /wx:if="\{\{familyShareEntry\}\}"/);
  assert.match(view, /家人把这份天气提醒分享给你/);
  assert.match(view, /登录并开启家庭照护/);
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
