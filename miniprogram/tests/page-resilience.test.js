const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let pageDefinition;
let authApiImpl = async () => ({});
let snapshotImpl = async () => ({});
let lastToast = null;

global.Page = (definition) => { pageDefinition = definition; };
global.wx = {
  showToast: (options) => { lastToast = options; },
};

const careSessionPath = require.resolve('../pages/elders/care-session');
require.cache[careSessionPath] = {
  id: careSessionPath,
  filename: careSessionPath,
  loaded: true,
  exports: {
    authApi: (options) => authApiImpl(options),
    clear: () => {},
    getMeta: () => ({ login_method: 'wechat' }),
    getSnapshot: () => snapshotImpl(),
    requireToken: () => 'session-token',
  },
};

function loadPage(relativePath) {
  const modulePath = require.resolve(relativePath);
  delete require.cache[modulePath];
  pageDefinition = null;
  require(modulePath);
  return pageDefinition;
}

function makePage(definition, overrides) {
  const page = Object.assign({}, definition);
  page.data = Object.assign({}, definition.data, overrides || {});
  page.setData = (next) => Object.assign(page.data, next);
  return page;
}

test('家庭照护刷新失败时保留上次成功加载的卡片', async () => {
  authApiImpl = async () => ({
    items: [{ pair_id: 7, member: { name: '奶奶', relation: '祖母', age: 72 } }],
  });
  snapshotImpl = async () => ({
    current: { temperature: 34, temperature_max: 36, temperature_min: 27 },
  });
  const definition = loadPage('../pages/elders/index');
  const page = makePage(definition);

  await page.loadCareHome.call(page);
  assert.equal(page.data.elders.length, 1);
  assert.equal(page.data.elders[0].displayName, '奶奶');

  authApiImpl = async () => { throw new Error('offline'); };
  await page.loadCareHome.call(page);
  assert.equal(page.data.elders.length, 1);
  assert.match(page.data.loadError, /上次成功加载/);
});

test('账号接口失败后清空已验证展示并提供内联错误', async () => {
  authApiImpl = async () => ({ display_name: '测试用户' });
  const definition = loadPage('../pages/account/index');
  const page = makePage(definition);

  await page.loadAccount.call(page);
  assert.equal(page.data.accountVerified, true);
  assert.equal(page.data.me.display_name, '测试用户');

  authApiImpl = async () => { throw new Error('offline'); };
  await page.loadAccount.call(page);
  assert.equal(page.data.accountVerified, false);
  assert.equal(page.data.me, null);
  assert.match(page.data.loadError, /重新|重试/);
});

test('今日行动未勾选时不会提交，勾选后只保存实际选项', async () => {
  const requests = [];
  authApiImpl = async (options) => {
    requests.push(options);
    return { ok: true };
  };
  lastToast = null;
  const definition = loadPage('../pages/action-checkin/index');
  const page = makePage(definition, { pairId: 9 });

  await page.confirmActions.call(page);
  assert.equal(requests.length, 0);
  assert.match(lastToast.title, /至少/);

  page.onActionsChange.call(page, { detail: { value: ['check_weather'] } });
  await page.confirmActions.call(page);
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].data.actions_done, ['check_weather']);
  assert.equal(page.data.confirmed, true);
  assert.equal(lastToast.title, '今日行动已记录');
});

test('健康筛查切换家人会清空答案且不被旧人的乱序响应覆盖', async () => {
  const pending = new Map();
  authApiImpl = (options) => new Promise((resolve) => {
    const pairId = Number(new URL(`https://local${options.path}`).searchParams.get('pair_id'));
    pending.set(pairId, resolve);
  });
  const definition = loadPage('../pages/health-assessment/index');
  const page = makePage(definition, {
    pairId: 1,
    elders: [{ pair_id: 1 }, { pair_id: 2 }],
    elderIndex: 0,
    answers: { outdoor_exposure: 'high' },
    completedCount: 1,
  });
  page._unloaded = false;
  page._latestRequestToken = 0;

  const firstLoad = page.loadLatest.call(page);
  const secondLoad = page.onElderChange.call(page, { detail: { value: 1 } });
  assert.equal(page.data.pairId, 2);
  assert.deepEqual(page.data.answers, {});
  assert.equal(page.data.completedCount, 0);

  pending.get(2)({ latest: { id: 'elder-2', risk_level: '留意' } });
  await secondLoad;
  assert.equal(page.data.latest.id, 'elder-2');
  pending.get(1)({ latest: { id: 'elder-1', risk_level: '旧结果' } });
  await firstLoad;

  assert.equal(page.data.pairId, 2);
  assert.equal(page.data.latest.id, 'elder-2');
});

test('健康筛查提交固定开始时的家人且不覆盖已切换页面', async () => {
  let resolveSubmit;
  let submitted;
  authApiImpl = (options) => {
    submitted = options;
    return new Promise((resolve) => { resolveSubmit = resolve; });
  };
  const definition = loadPage('../pages/health-assessment/index');
  const page = makePage(definition, {
    pairId: 2,
    answers: {
      outdoor_exposure: 'low',
      symptom_level: 'none',
      hydration: 'good',
      medication_adherence: 'good',
      sleep_quality: 'good',
    },
  });
  page._unloaded = false;
  page._submitRequestToken = 0;

  const request = page.submitAssessment.call(page);
  page.data.pairId = 1;
  resolveSubmit({ id: 'elder-2-result', risk_level: '正常' });
  await request;

  assert.equal(submitted.data.pair_id, 2);
  assert.equal(page.data.latest, null);
});

test('老人资料保存后手动离页不会被延迟定时器再返回一层', async () => {
  const originalSetTimeout = global.setTimeout;
  const originalClearTimeout = global.clearTimeout;
  const originalNavigateBack = global.wx.navigateBack;
  let timerCallback;
  let clearedTimer = null;
  let navigations = 0;
  global.setTimeout = (callback) => { timerCallback = callback; return 77; };
  global.clearTimeout = (timerId) => { clearedTimer = timerId; };
  global.wx.navigateBack = () => { navigations += 1; };
  authApiImpl = async () => ({});
  try {
    const definition = loadPage('../pages/elder-edit/index');
    const page = makePage(definition, { mode: 'create', name: '妈妈', relation: '母亲' });
    page._unloaded = false;
    await page.onSave.call(page);
    page.onUnload.call(page);
    timerCallback();
    assert.equal(clearedTimer, 77);
    assert.equal(navigations, 0);
  } finally {
    global.setTimeout = originalSetTimeout;
    global.clearTimeout = originalClearTimeout;
    global.wx.navigateBack = originalNavigateBack;
  }
});

test('账号页卸载后平台隐私协议的延迟失败不再跳页', () => {
  const originalContract = global.wx.openPrivacyContract;
  const originalNavigateTo = global.wx.navigateTo;
  let failCallback;
  let navigations = 0;
  global.wx.openPrivacyContract = ({ fail }) => { failCallback = fail; };
  global.wx.navigateTo = () => { navigations += 1; };
  try {
    const definition = loadPage('../pages/account/index');
    const page = makePage(definition);
    page._unloaded = false;
    page.openPrivacy.call(page);
    page.onUnload.call(page);
    failCallback();
    assert.equal(navigations, 0);
  } finally {
    global.wx.openPrivacyContract = originalContract;
    global.wx.navigateTo = originalNavigateTo;
  }
});

test('关键选择控件和公开信息带有可读语义', () => {
  const miniRoot = path.resolve(__dirname, '..');
  const actionView = fs.readFileSync(path.join(miniRoot, 'pages/action-checkin/index.wxml'), 'utf8');
  const communityView = fs.readFileSync(path.join(miniRoot, 'pages/community/index.wxml'), 'utf8');
  const alertsView = fs.readFileSync(path.join(miniRoot, 'pages/alerts/index.wxml'), 'utf8');
  const accountView = fs.readFileSync(path.join(miniRoot, 'pages/account/index.wxml'), 'utf8');
  const freshnessView = fs.readFileSync(path.join(miniRoot, 'components/freshness-bar/index.wxml'), 'utf8');

  assert.match(actionView, /aria-checked=/);
  assert.match(actionView, /保存已完成行动/);
  assert.doesNotMatch(actionView, /确认今日平安/);
  assert.match(communityView, /aria-selected=/);
  assert.match(communityView, /65\+ 人口占比/);
  assert.match(alertsView, /发布单位：/);
  assert.match(alertsView, /发布时间：/);
  assert.match(alertsView, /生效时间：/);
  assert.match(alertsView, /未提供，请以当地主管部门通知为准/);
  assert.match(accountView, /bindtap="loadAccount"/);
  assert.match(accountView, /wx:elif="\{\{accountVerified\}\}"/);
  assert.match(freshnessView, /当前离线，正在显示上次保存的数据/);
});
