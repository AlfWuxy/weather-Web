const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let pageDefinition;
let authApiImpl = async () => ({});
let tokenApiImpl = async () => ({});
let snapshotImpl = async () => ({});
let getTokenImpl = () => 'session-token';
let clearImpl = () => {};
let lastToast = null;
const settingsStorage = new Map();

function trackHealthMutation(page, promise, kind, meta) {
  const pending = Promise.resolve(promise);
  page._healthMutationPromise = pending;
  page._healthMutationKind = String(kind || '');
  page._healthMutationMeta = meta && typeof meta === 'object' ? meta : null;
  return pending;
}

function finishHealthMutation(page, promise) {
  if (page._healthMutationPromise === promise) {
    page._healthMutationPromise = null;
    page._healthMutationKind = '';
    page._healthMutationMeta = null;
  }
}

function suspendHealthMutation(page) {
  if (!page._healthMutationPromise) return false;
  page._healthMutationResumePromise = page._healthMutationPromise;
  page._healthMutationResumeKind = page._healthMutationKind;
  page._healthMutationResumeMeta = page._healthMutationMeta;
  page._healthConsentReloadPending = true;
  return true;
}

async function resumeHealthMutation(page) {
  const pending = page._healthMutationResumePromise;
  if (!pending) return { resumed: false, ok: false, kind: '', meta: null };
  const kind = String(page._healthMutationResumeKind || '');
  const meta = page._healthMutationResumeMeta || null;
  page._healthMutationResumePromise = null;
  page._healthMutationResumeKind = '';
  page._healthMutationResumeMeta = null;
  try {
    const value = await pending;
    return { resumed: true, ok: true, value, kind, meta };
  } catch (error) {
    return { resumed: true, ok: false, error, kind, meta };
  } finally {
    finishHealthMutation(page, pending);
  }
}

const {
  ACQUISITION_STORAGE_KEY,
  FAMILY_ENTRY_STORAGE_KEY,
  readAcquisitionSource,
} = require('../utils/share');

global.Page = (definition) => { pageDefinition = definition; };
global.wx = {
  getStorageSync: (key) => settingsStorage.get(key),
  removeStorageSync: (key) => settingsStorage.delete(key),
  setStorageSync: (key, value) => settingsStorage.set(key, value),
  showToast: (options) => { lastToast = options; },
};

const careSessionPath = require.resolve('../pages/elders/care-session');
require.cache[careSessionPath] = {
  id: careSessionPath,
  filename: careSessionPath,
  loaded: true,
  exports: {
    authApi: (options) => authApiImpl(options),
    clear: () => clearImpl(),
    finishHealthMutation,
    getMeta: () => ({ login_method: 'wechat' }),
    getSnapshot: () => snapshotImpl(),
    getToken: () => getTokenImpl(),
    guardHealthSensitivePage: async (page, loader) => loader(),
    requireToken: () => 'session-token',
    resumeHealthMutation,
    suspendHealthMutation,
    tokenApi: (token, options) => tokenApiImpl(token, options),
    trackHealthMutation,
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

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
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

test('停止管理连续点击期间只展示一次确认并只发送一次删除请求', async (t) => {
  const deleteGate = deferred();
  const requests = [];
  const originalShowModal = global.wx.showModal;
  let modalCount = 0;
  let confirmDelete;
  t.after(() => { global.wx.showModal = originalShowModal; });
  global.wx.showModal = (options) => {
    modalCount += 1;
    confirmDelete = options.success;
  };
  authApiImpl = (options) => {
    requests.push(options);
    if (options.method === 'DELETE') return deleteGate.promise;
    return Promise.resolve({ items: [] });
  };
  snapshotImpl = async () => ({});
  const definition = loadPage('../pages/elders/index');
  const page = makePage(definition, {
    elders: [{ pair_id: 7, displayName: '奶奶' }],
    busyPairId: 0,
  });
  const event = { currentTarget: { dataset: { pairId: 7, name: '奶奶' } } };

  page.deleteElder.call(page, event);
  page.deleteElder.call(page, event);
  assert.equal(modalCount, 1);
  assert.equal(page.data.busyPairId, 7);

  const pendingDelete = confirmDelete({ confirm: true });
  await Promise.resolve();
  page.deleteElder.call(page, event);
  assert.equal(modalCount, 1);
  assert.equal(requests.filter((item) => item.method === 'DELETE').length, 1);
  deleteGate.resolve({ ok: true });
  await pendingDelete;

  assert.equal(requests.filter((item) => item.method === 'DELETE').length, 1);
  assert.equal(page.data.busyPairId, 0);
  const view = fs.readFileSync(path.join(__dirname, '../pages/elders/index.wxml'), 'utf8');
  assert.match(view, /loading="\{\{busyPairId === item\.pair_id\}\}"/);
  assert.match(view, /disabled="\{\{busyPairId !== 0\}\}"/);
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

['resolve', 'reject'].forEach((staleSettlement) => {
  test(`账号页隐藏返回后的旧请求${staleSettlement === 'resolve' ? '成功' : '失败'}不会覆盖最新结果`, async (t) => {
    const firstGet = deferred();
    const secondGet = deferred();
    let requestCount = 0;
    t.after(() => { authApiImpl = async () => ({}); });
    authApiImpl = () => {
      requestCount += 1;
      return requestCount === 1 ? firstGet.promise : secondGet.promise;
    };
    const definition = loadPage('../pages/account/index');
    const page = makePage(definition);

    const firstShow = page.onShow.call(page);
    await Promise.resolve();
    page.onHide.call(page);
    const secondShow = page.onShow.call(page);
    await Promise.resolve();
    assert.equal(requestCount, 2);

    secondGet.resolve({ display_name: '最新账号' });
    await secondShow;
    if (staleSettlement === 'resolve') {
      firstGet.resolve({ display_name: '旧账号' });
    } else {
      firstGet.reject(new Error('旧请求离线'));
    }
    await firstShow;

    assert.equal(page.data.accountVerified, true);
    assert.equal(page.data.me.display_name, '最新账号');
    assert.equal(page.data.loadError, '');
    assert.equal(page.data.loading, false);
  });
});

test('设置页只根据本机会话展示登录状态', (t) => {
  t.after(() => { getTokenImpl = () => 'session-token'; });
  const definition = loadPage('../pages/settings/index');
  const page = makePage(definition);

  getTokenImpl = () => '';
  page.onShow.call(page);
  assert.deepEqual(page.data, { busy: false, loggedIn: false });

  getTokenImpl = () => 'session-user-b';
  page.onShow.call(page);
  assert.deepEqual(page.data, { busy: false, loggedIn: true });

  page.onSessionInvalidated.call(page);
  assert.deepEqual(page.data, { busy: false, loggedIn: false });
});

test('设置页退出登录后立即切换为未登录状态', async (t) => {
  let modalSuccess;
  let relaunched = false;
  let clearCount = 0;
  const originalShowModal = global.wx.showModal;
  const originalReLaunch = global.wx.reLaunch;
  t.after(() => {
    global.wx.showModal = originalShowModal;
    global.wx.reLaunch = originalReLaunch;
    clearImpl = () => {};
    tokenApiImpl = async () => ({});
  });
  global.wx.showModal = ({ success }) => { modalSuccess = success; };
  global.wx.reLaunch = () => { relaunched = true; };
  clearImpl = () => { clearCount += 1; };
  tokenApiImpl = async () => ({ ok: true });
  const definition = loadPage('../pages/settings/index');
  const page = makePage(definition, { loggedIn: true });

  page.logout.call(page);
  await modalSuccess({ confirm: true });

  assert.equal(clearCount, 1);
  assert.equal(relaunched, true);
  assert.deepEqual(page.data, { busy: false, loggedIn: false });
});

test('设置页确认退出后不等待远端请求就清空私人数据', async (t) => {
  let modalSuccess;
  let resolveLogout;
  let relaunched = false;
  let clearCount = 0;
  let requestToken = '';
  const originalShowModal = global.wx.showModal;
  const originalReLaunch = global.wx.reLaunch;
  t.after(() => {
    global.wx.showModal = originalShowModal;
    global.wx.reLaunch = originalReLaunch;
    clearImpl = () => {};
    tokenApiImpl = async () => ({});
  });
  global.wx.showModal = ({ success }) => { modalSuccess = success; };
  global.wx.reLaunch = () => { relaunched = true; };
  clearImpl = () => { clearCount += 1; };
  tokenApiImpl = (token) => {
    requestToken = token;
    return new Promise((resolve) => { resolveLogout = resolve; });
  };
  const definition = loadPage('../pages/settings/index');
  const page = makePage(definition, { loggedIn: true });

  page.logout.call(page);
  const pendingLogout = modalSuccess({ confirm: true });

  assert.equal(requestToken, 'session-token');
  assert.equal(clearCount, 1);
  assert.equal(relaunched, true);
  assert.deepEqual(page.data, { busy: false, loggedIn: false });

  resolveLogout({ ok: true });
  await pendingLogout;
});

test('账号页确认退出后立即清理并跳转，远端失败不回滚', async (t) => {
  let modalSuccess;
  let rejectLogout;
  let relaunched = false;
  let clearCount = 0;
  let requestToken = '';
  let requestOptions = null;
  const originalShowModal = global.wx.showModal;
  const originalReLaunch = global.wx.reLaunch;
  t.after(() => {
    global.wx.showModal = originalShowModal;
    global.wx.reLaunch = originalReLaunch;
    clearImpl = () => {};
    tokenApiImpl = async () => ({});
    settingsStorage.clear();
  });
  global.wx.showModal = ({ success }) => { modalSuccess = success; };
  global.wx.reLaunch = ({ url }) => { relaunched = url === '/pages/home/index'; };
  clearImpl = () => { clearCount += 1; };
  tokenApiImpl = (token, options) => {
    requestToken = token;
    requestOptions = options;
    return new Promise((resolve, reject) => { rejectLogout = reject; });
  };
  settingsStorage.set(ACQUISITION_STORAGE_KEY, {
    source: 'family_share',
    expires_at: Date.now() + 30 * 24 * 60 * 60 * 1000,
  });
  settingsStorage.set(FAMILY_ENTRY_STORAGE_KEY, {
    source: 'family_share',
    expires_at: Date.now() + 30 * 60 * 1000,
  });
  const definition = loadPage('../pages/account/index');
  const page = makePage(definition, { busy: false });

  page.logout.call(page);
  const pendingLogout = modalSuccess({ confirm: true });

  assert.equal(requestToken, 'session-token');
  assert.deepEqual(requestOptions, {
    method: 'POST',
    path: '/mp/api/v1/auth/logout',
  });
  assert.equal(clearCount, 1);
  assert.equal(relaunched, true);
  assert.equal(settingsStorage.has(ACQUISITION_STORAGE_KEY), false);
  assert.equal(settingsStorage.has(FAMILY_ENTRY_STORAGE_KEY), false);

  rejectLogout(new Error('offline'));
  await pendingLogout;
  assert.equal(clearCount, 1);
  assert.equal(relaunched, true);
});

test('设置页退出会清除账号 A 的家庭分享来源，账号 B 不继承归因', async (t) => {
  let modalSuccess;
  const originalShowModal = global.wx.showModal;
  const originalReLaunch = global.wx.reLaunch;
  t.after(() => {
    global.wx.showModal = originalShowModal;
    global.wx.reLaunch = originalReLaunch;
    clearImpl = () => {};
    tokenApiImpl = async () => ({});
    settingsStorage.clear();
  });
  global.wx.showModal = ({ success }) => { modalSuccess = success; };
  global.wx.reLaunch = () => {};
  clearImpl = () => {};
  tokenApiImpl = async () => ({ ok: true });
  settingsStorage.set(ACQUISITION_STORAGE_KEY, {
    source: 'family_share',
    expires_at: Date.now() + 30 * 24 * 60 * 60 * 1000,
  });
  settingsStorage.set(FAMILY_ENTRY_STORAGE_KEY, {
    source: 'family_share',
    expires_at: Date.now() + 30 * 60 * 1000,
  });
  assert.equal(readAcquisitionSource(), 'family_share');

  const definition = loadPage('../pages/settings/index');
  const page = makePage(definition, { loggedIn: true });
  page.logout.call(page);
  await modalSuccess({ confirm: true });

  assert.equal(settingsStorage.has(ACQUISITION_STORAGE_KEY), false);
  assert.equal(settingsStorage.has(FAMILY_ENTRY_STORAGE_KEY), false);
  assert.equal(readAcquisitionSource(), '');
});

test('今日行动未勾选时不会提交，勾选后只保存实际选项', async () => {
  const requests = [];
  authApiImpl = async (options) => {
    requests.push(options);
    return { ok: true };
  };
  lastToast = null;
  const definition = loadPage('../pages/action-checkin/index');
  const page = makePage(definition, {
    pairId: 9,
    contextReady: true,
    actions: [{ id: 'check_weather', title: '出门前看天气', detail: '', checked: false }],
  });

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

test('今日行动提交期间锁定勾选并以提交快照显示保存结果', async () => {
  const gate = deferred();
  const requests = [];
  authApiImpl = (options) => {
    requests.push(options);
    return gate.promise;
  };
  const definition = loadPage('../pages/action-checkin/index');
  const page = makePage(definition, {
    pairId: 9,
    contextReady: true,
    actions: [
      { id: 'check_weather', title: '看天气', detail: '', checked: true },
      { id: 'carry_water', title: '带水', detail: '', checked: false },
    ],
    selectedActions: ['check_weather'],
  });

  const pending = page.confirmActions.call(page);
  await Promise.resolve();
  assert.equal(page.data.busyAction, 'confirm');
  page.onActionsChange.call(page, { detail: { value: ['carry_water'] } });
  assert.deepEqual(page.data.selectedActions, ['check_weather']);
  gate.resolve({ ok: true });
  await pending;

  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].data.actions_done, ['check_weather']);
  assert.equal(page.data.confirmed, true);
  assert.deepEqual(page.data.selectedActions, ['check_weather']);
  assert.deepEqual(page.data.actions.filter((item) => item.checked).map((item) => item.id), ['check_weather']);

  const view = fs.readFileSync(path.join(__dirname, '../pages/action-checkin/index.wxml'), 'utf8');
  assert.match(view, /disabled="\{\{!contextReady \|\| busyAction !== ''\}\}"/);
});

test('今日行动恢复当天已保存选项并忽略旧版本行动 ID', async () => {
  const requests = [];
  const { formatLocalDate } = require('../pages/elders/care-logic');
  authApiImpl = async (options) => {
    requests.push(options);
    if (options.method === 'GET') {
      return {
        items: [{
          pair_id: 9,
          member: { name: '奶奶' },
          today: {
            status_date: formatLocalDate(),
            confirmed_at: '2026-07-18T08:00:00',
            actions_done_count: 3,
            elder_actions: ['drink_water', 'old_action_id', 'drink_water'],
          },
        }],
      };
    }
    return { ok: true };
  };
  snapshotImpl = async () => ({
    current: { temperature: 38, temperature_max: 40, temperature_min: 30 },
  });
  const definition = loadPage('../pages/action-checkin/index');
  const page = makePage(definition, { pairId: 9 });

  await page.loadContext.call(page);

  assert.equal(page.data.contextReady, true);
  assert.equal(page.data.confirmed, true);
  assert.deepEqual(page.data.selectedActions, ['drink_water']);
  assert.deepEqual(
    page.data.actions.filter((item) => item.checked).map((item) => item.id),
    ['drink_water'],
  );
  assert.equal(page.data.actions.some((item) => item.id === 'old_action_id'), false);

  await page.confirmActions.call(page);
  const post = requests.find((item) => item.method === 'POST');
  assert.deepEqual(post.data.actions_done, ['drink_water']);
  assert.equal(page.data.confirmed, true);
  assert.deepEqual(page.data.selectedActions, ['drink_water']);
});

test('今日行动不会从其他日期恢复确认和勾选状态', async () => {
  const { formatLocalDate } = require('../pages/elders/care-logic');
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  authApiImpl = async () => ({
    items: [{
      pair_id: 9,
      member: { name: '奶奶' },
      today: {
        status_date: formatLocalDate(yesterday),
        confirmed_at: '2026-07-17T08:00:00',
        elder_actions: ['drink_water'],
      },
    }],
  });
  snapshotImpl = async () => ({
    current: { temperature: 38, temperature_max: 40, temperature_min: 30 },
  });
  const definition = loadPage('../pages/action-checkin/index');
  const page = makePage(definition, { pairId: 9 });

  await page.loadContext.call(page);

  assert.equal(page.data.contextReady, true);
  assert.equal(page.data.confirmed, false);
  assert.deepEqual(page.data.selectedActions, []);
  assert.equal(page.data.actions.some((item) => item.checked), false);
});

test('今日行动老人接口失败后保持上下文门禁且所有写入口 POST 为零', async () => {
  const requests = [];
  authApiImpl = async (options) => {
    requests.push(options);
    if (options.method === 'GET') throw new Error('offline');
    return { ok: true };
  };
  snapshotImpl = async () => ({ current: { temperature: 30 } });
  const definition = loadPage('../pages/action-checkin/index');
  assert.deepEqual(definition.data.actions, []);
  assert.equal(definition.data.contextReady, false);

  const page = makePage(definition, {
    pairId: 9,
    contextReady: true,
    actions: [{ id: 'check_weather', title: '旧行动', detail: '', checked: true }],
    selectedActions: ['check_weather'],
  });
  await page.loadContext.call(page);

  assert.equal(page.data.contextReady, false);
  assert.deepEqual(page.data.actions, []);
  assert.deepEqual(page.data.selectedActions, []);
  assert.match(page.data.loadError, /重试/);
  const persistentError = page.data.loadError;

  page.data.selectedActions = ['check_weather'];
  page.data.question2 = '旧复盘';
  const originalShowModal = global.wx.showModal;
  let modalCount = 0;
  global.wx.showModal = () => { modalCount += 1; };
  try {
    await page.confirmActions.call(page);
    page.requestHelp.call(page);
    await page.submitDebrief.call(page);
  } finally {
    global.wx.showModal = originalShowModal;
  }

  assert.equal(requests.filter((item) => item.method === 'GET').length, 1);
  assert.equal(requests.filter((item) => item.method === 'POST').length, 0);
  assert.equal(modalCount, 0);
  assert.equal(page.data.loadError, persistentError);
});

test('求助弹窗确认前上下文失效时二次门禁阻止写入', async () => {
  const requests = [];
  authApiImpl = async (options) => {
    requests.push(options);
    return { ok: true };
  };
  const definition = loadPage('../pages/action-checkin/index');
  const page = makePage(definition, { pairId: 9, contextReady: true });
  const originalShowModal = global.wx.showModal;
  let confirmModal;
  global.wx.showModal = ({ success }) => { confirmModal = success; };
  try {
    page.requestHelp.call(page);
    page.data.contextReady = false;
    await confirmModal({ confirm: true });
  } finally {
    global.wx.showModal = originalShowModal;
  }
  assert.equal(requests.filter((item) => item.method === 'POST').length, 0);
});

test('天气快照失败时保留已核验家人并显示待更新通用状态', async () => {
  authApiImpl = async () => ({
    items: [{ pair_id: 9, member: { name: '奶奶' } }],
  });
  snapshotImpl = async () => { throw new Error('weather offline'); };
  const definition = loadPage('../pages/action-checkin/index');
  const page = makePage(definition, { pairId: 9 });

  await page.loadContext.call(page);

  assert.equal(page.data.contextReady, true);
  assert.equal(page.data.elderName, '奶奶');
  assert.equal(page.data.weather.available, false);
  assert.equal(page.data.weatherStatus, '天气待更新');
  assert.ok(page.data.actions.length > 0);
  assert.equal(page.data.loadError, '');
});

test('较早天气明确标注并退回通用安全行动', async () => {
  authApiImpl = async () => ({
    items: [{ pair_id: 9, member: { name: '奶奶' } }],
  });
  snapshotImpl = async () => ({
    data: {
      current: { temperature: 38, temperature_max: 40, temperature_min: 30 },
    },
    meta: { source: 'stale-cache', stale: true },
  });
  const definition = loadPage('../pages/action-checkin/index');
  const page = makePage(definition, { pairId: 9 });

  await page.loadContext.call(page);

  assert.equal(page.data.contextReady, true);
  assert.equal(page.data.weather.stale, true);
  assert.equal(page.data.weatherStatus, '较早天气，待刷新');
  assert.deepEqual(page.data.actions.map((item) => item.id), [
    'check_weather',
    'carry_water',
    'contact_family',
  ]);
});

test('提醒话术遇到较早或不可用天气时退回通用提醒', async () => {
  authApiImpl = async () => ({
    items: [{ pair_id: 9, member: { name: '奶奶', relation: '祖母' } }],
  });
  snapshotImpl = async () => ({
    data: {
      current: { temperature: 38, temperature_max: 40, temperature_min: 30 },
    },
    meta: { source: 'stale-cache', stale: true },
  });
  const definition = loadPage('../pages/template/index');
  const stalePage = makePage(definition, { pairId: 9 });

  await stalePage.loadTemplate.call(stalePage);

  assert.equal(stalePage.data.weather.stale, true);
  assert.equal(stalePage.data.trigger, '');
  assert.match(stalePage.data.weatherNotice, /较早/);
  assert.match(stalePage.data.message, /【都昌县天气提醒】/);
  assert.doesNotMatch(stalePage.data.message, /高温提醒/);

  snapshotImpl = async () => ({});
  const unavailablePage = makePage(definition, { pairId: 9 });
  await unavailablePage.loadTemplate.call(unavailablePage);

  assert.equal(unavailablePage.data.weather.available, false);
  assert.equal(unavailablePage.data.trigger, '');
  assert.match(unavailablePage.data.weatherNotice, /待更新/);
  assert.match(unavailablePage.data.message, /【都昌县天气提醒】/);
});

test('公共天气页面失败会安全降级、统一退避并提供真实重试入口', async () => {
  const publicDataPath = require.resolve('../utils/public-data');
  const lifecyclePath = require.resolve('../utils/public-page-lifecycle');
  const originalPublicDataModule = require.cache[publicDataPath];
  const originalLifecycleModule = require.cache[lifecyclePath];
  const pageModules = [
    require.resolve('../pages/home/index'),
    require.resolve('../pages/forecast/index'),
    require.resolve('../pages/alerts/index'),
    require.resolve('../pages/actions/index'),
    require.resolve('../pages/transparency/index'),
  ];
  const schedules = [];
  const bootstrapOptions = [];
  const hadActionStorage = settingsStorage.has('yl_actions');
  const actionStorageBefore = settingsStorage.get('yl_actions');
  require.cache[publicDataPath] = {
    id: publicDataPath,
    filename: publicDataPath,
    loaded: true,
    exports: {
      getBootstrap: async (options) => {
        bootstrapOptions.push(options || {});
        throw new Error('offline');
      },
      PUBLIC_RETRY_DELAY_MS: 60 * 1000,
    },
  };
  require.cache[lifecyclePath] = {
    id: lifecyclePath,
    filename: lifecyclePath,
    loaded: true,
    exports: {
      beginPublicPage: () => {},
      hidePublicPage: () => {},
      pageCanRender: () => true,
      schedulePublicRefresh: (page, meta, reload) => {
        schedules.push({ page, meta, reload });
        return 60 * 1000;
      },
      showPublicPage: () => {},
      staleRetryMeta: (meta, retryDelayMs) => Object.assign({}, meta || {}, {
        stale: true,
        source: 'stale-cache',
        refreshDeferred: false,
        refreshStarted: false,
        effectiveExpiresAt: null,
        retryAfter: Date.now() + retryDelayMs,
      }),
      unloadPublicPage: () => {},
    },
  };

  try {
    const homeDefinition = loadPage('../pages/home/index');
    const home = makePage(homeDefinition, {
      snapshot: {
        available: true,
        current: { available: true, temperatureText: '35°C' },
        warnings: [{ id: 'warning-1', title: '旧预警' }],
        warningsSourceAvailable: true,
        warningsStatusText: '1 条有效预警',
        risk: { available: true, score: 88, scoreText: '88', label: '高风险', tone: 'high', summary: '旧风险' },
      },
      topActions: [{ id: 'old-action', title: '旧定制行动' }],
      freshness: { source: 'network', stale: false, updatedText: '刚刚更新' },
    });
    await home.loadData.call(home);
    assert.equal(home.data.freshness.stale, true);
    assert.equal(home.data.freshness.source, 'stale-cache');
    assert.deepEqual(home.data.snapshot.warnings, []);
    assert.equal(home.data.snapshot.warningsSourceAvailable, false);
    assert.equal(home.data.snapshot.risk.available, false);
    assert.equal(home.data.snapshot.risk.score, null);
    assert.deepEqual(home.data.topActions, []);
    assert.match(home.data.error, /风险、预警和定制行动已暂停/);

    const forecastDefinition = loadPage('../pages/forecast/index');
    const forecast = makePage(forecastDefinition, {
      forecast: [{ id: 'day-1', score: 91, scoreText: '91', tone: 'high', riskLabel: '高风险' }],
      highRiskDays: 1,
      freshness: { source: 'network', stale: false, updatedText: '刚刚更新' },
    });
    await forecast.loadData.call(forecast);
    assert.equal(forecast.data.freshness.stale, true);
    assert.equal(forecast.data.forecast[0].score, null);
    assert.equal(forecast.data.forecast[0].tone, 'unknown');
    assert.equal(forecast.data.highRiskDays, 0);
    assert.match(forecast.data.error, /风险等级已暂停/);

    const alertsDefinition = loadPage('../pages/alerts/index');
    const alerts = makePage(alertsDefinition, {
      current: { available: true, temperatureText: '35°C' },
      warnings: [{ id: 'warning-1', title: '旧预警' }],
      warningsSourceAvailable: true,
      freshness: { source: 'network', stale: false, updatedText: '刚刚更新' },
    });
    await alerts.loadData.call(alerts);
    assert.equal(alerts.data.freshness.stale, true);
    assert.deepEqual(alerts.data.warnings, []);
    assert.equal(alerts.data.warningsSourceAvailable, false);
    assert.match(alerts.data.error, /无法确认是否存在有效预警/);

    const actionsDefinition = loadPage('../pages/actions/index');
    const actions = makePage(actionsDefinition);
    await actions.loadData.call(actions);
    assert.equal(actions.data.generalMode, true);
    assert.equal(actions.data.actions.length, 4);
    assert.equal(actions.data.freshness.stale, true);
    assert.match(actions.data.error, /自动重试/);

    const transparencyDefinition = loadPage('../pages/transparency/index');
    const transparency = makePage(transparencyDefinition);
    await transparency.loadSources.call(transparency);
    assert.equal(transparency.data.sourceLoading, false);
    assert.equal(transparency.data.sources.length, 0);
    assert.equal(transparency.data.freshness.stale, true);
    assert.match(transparency.data.sourceError, /自动重试/);

    assert.equal(schedules.length, 5);
    schedules.forEach((scheduled) => {
      assert.ok(scheduled.meta.retryAfter > Date.now());
      assert.equal(scheduled.meta.refreshStarted, false);
      assert.equal(typeof scheduled.reload, 'function');
    });

    await actions.retry.call(actions);
    await transparency.retrySources.call(transparency);
    assert.equal(schedules.length, 7);
    assert.equal(bootstrapOptions.at(-2).force, true);
    assert.equal(bootstrapOptions.at(-1).force, true);

    const miniRoot = path.resolve(__dirname, '..');
    const homeView = fs.readFileSync(path.join(miniRoot, 'pages/home/index.wxml'), 'utf8');
    const forecastView = fs.readFileSync(path.join(miniRoot, 'pages/forecast/index.wxml'), 'utf8');
    const alertsView = fs.readFileSync(path.join(miniRoot, 'pages/alerts/index.wxml'), 'utf8');
    const actionsView = fs.readFileSync(path.join(miniRoot, 'pages/actions/index.wxml'), 'utf8');
    const transparencyView = fs.readFileSync(path.join(miniRoot, 'pages/transparency/index.wxml'), 'utf8');
    assert.match(homeView, /wx:if="\{\{error\}\}"[^>]*title="天气更新失败，风险信息已暂停"/);
    assert.match(forecastView, /wx:if="\{\{error\}\}"[^>]*title="预报更新失败，风险等级已暂停"/);
    assert.match(alertsView, /error \? '预警更新失败，有效性待核对'/);
    assert.match(actionsView, /action-label="\{\{error \? '重新获取天气' : ''\}\}"[^>]*bind:action="retry"/);
    assert.match(transparencyView, /state="error"[^>]*action-label="重新读取"[^>]*bind:action="retrySources"/);
  } finally {
    pageModules.forEach((modulePath) => { delete require.cache[modulePath]; });
    if (originalPublicDataModule) require.cache[publicDataPath] = originalPublicDataModule;
    else delete require.cache[publicDataPath];
    if (originalLifecycleModule) require.cache[lifecyclePath] = originalLifecycleModule;
    else delete require.cache[lifecyclePath];
    if (hadActionStorage) settingsStorage.set('yl_actions', actionStorageBefore);
    else settingsStorage.delete('yl_actions');
  }
});

test('健康筛查首次加载失败时保留错误入口并禁止提交空白上下文', async () => {
  const requests = [];
  authApiImpl = async (options) => {
    requests.push(options);
    throw new Error('offline');
  };
  const definition = loadPage('../pages/health-assessment/index');
  const page = makePage(definition, {
    answers: {
      outdoor_exposure: 'low',
      symptom_level: 'none',
      hydration: 'good',
      medication_adherence: 'good',
      sleep_quality: 'good',
    },
  });

  await page.loadPage.call(page);
  assert.equal(page.data.contextReady, false);
  assert.equal(page.data.loading, false);
  assert.match(page.data.loadError, /重试/);
  await page.submitAssessment.call(page);
  assert.equal(requests.filter((item) => item.method === 'POST').length, 0);

  const view = fs.readFileSync(path.join(__dirname, '../pages/health-assessment/index.wxml'), 'utf8');
  assert.match(view, /wx:elif="\{\{loadError\}\}"[^>]*role="alert"/);
  assert.match(view, /bindtap="loadPage"/);
  assert.match(view, /wx:elif="\{\{contextReady\}\}"/);
  assert.match(view, /class="result-card"[^>]*role="status"[^>]*aria-live="polite"/);
});

test('编辑家人读取失败时不展示空白表单且保存请求为零', async () => {
  const requests = [];
  authApiImpl = async (options) => {
    requests.push(options);
    throw new Error('offline');
  };
  const definition = loadPage('../pages/elder-edit/index');
  const page = makePage(definition, {
    mode: 'edit',
    pairId: 7,
    name: '旧称呼',
    relation: '祖母',
    age: '72',
    contextReady: true,
  });

  await page.loadElder.call(page);
  assert.equal(page.data.contextReady, false);
  assert.equal(page.data.loading, false);
  assert.equal(page.data.name, '');
  assert.match(page.data.loadError, /重试/);
  page.setData({ name: '奶奶', relation: '祖母', age: '72' });
  await page.onSave.call(page);
  assert.equal(requests.filter((item) => item.method === 'PATCH').length, 0);

  const view = fs.readFileSync(path.join(__dirname, '../pages/elder-edit/index.wxml'), 'utf8');
  assert.match(view, /wx:elif="\{\{loadError\}\}"[^>]*role="alert"/);
  assert.match(view, /wx:elif="\{\{contextReady\}\}" class="form-card"/);
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
    contextReady: true,
    loading: false,
  });
  page._unloaded = false;
  page._latestRequestToken = 0;

  const firstLoad = page.loadLatest.call(page);
  const secondLoad = page.onElderChange.call(page, { detail: { value: 1 } });
  assert.equal(page.data.pairId, 2);
  assert.deepEqual(page.data.answers, {});
  assert.equal(page.data.completedCount, 0);
  assert.equal(page.requestedPairId, 2);

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
    contextReady: true,
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
    const page = makePage(definition, {
      mode: 'create',
      name: '妈妈',
      relation: '母亲',
      age: '68',
      contextReady: true,
      loading: false,
    });
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
  const actionStyles = fs.readFileSync(path.join(miniRoot, 'pages/action-checkin/index.wxss'), 'utf8');
  const communityView = fs.readFileSync(path.join(miniRoot, 'pages/community/index.wxml'), 'utf8');
  const alertsView = fs.readFileSync(path.join(miniRoot, 'pages/alerts/index.wxml'), 'utf8');
  const accountView = fs.readFileSync(path.join(miniRoot, 'pages/account/index.wxml'), 'utf8');
  const settingsView = fs.readFileSync(path.join(miniRoot, 'pages/settings/index.wxml'), 'utf8');
  const freshnessView = fs.readFileSync(path.join(miniRoot, 'components/freshness-bar/index.wxml'), 'utf8');

  assert.match(actionView, /aria-checked=/);
  assert.match(actionView, /保存已完成行动/);
  assert.doesNotMatch(actionView, /确认今日平安/);
  assert.doesNotMatch(actionView, /日常留意/);
  assert.match(actionView, /wx:elif="\{\{contextReady\}\}"/);
  assert.match(actionView, /bindtap="loadContext"/);
  assert.match(actionView, /天气数据待更新，当前显示通用安全行动/);
  assert.match(actionStyles, /\.danger-title\s*\{[^}]*font-size:\s*16px/s);
  assert.match(actionStyles, /\.help-disclaimer\s*\{[^}]*font-size:\s*16px/s);
  assert.match(actionStyles, /\.help-input\s*\{[^}]*font-size:\s*16px/s);
  assert.match(actionStyles, /\.help-button,\s*\.emergency-button\s*\{[^}]*font-size:\s*16px/s);
  assert.match(actionStyles, /\.help-button,\s*\.emergency-button\s*\{[^}]*line-height:\s*1\.4/s);
  assert.match(actionStyles, /@media screen and \(max-width:\s*340px\)[\s\S]*\.help-buttons\s*\{[^}]*flex-direction:\s*column/s);
  assert.match(actionStyles, /\.medical-note\s*\{[^}]*font-size:\s*16px/s);
  assert.match(communityView, /aria-selected=/);
  assert.match(communityView, /65\+ 人口占比/);
  assert.match(alertsView, /发布单位：/);
  assert.match(alertsView, /发布时间：/);
  assert.match(alertsView, /生效时间：/);
  assert.match(alertsView, /未提供，请以当地主管部门通知为准/);
  assert.match(accountView, /bindtap="loadAccount"/);
  assert.match(accountView, /wx:elif="\{\{accountVerified\}\}"/);
  assert.match(settingsView, /wx:if="\{\{loggedIn\}\}" class="logout-button"[\s\S]*bindtap="logout"/);
  assert.match(freshnessView, /当前离线，正在显示上次保存的数据/);
});
