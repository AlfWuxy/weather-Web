const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let pageDefinition;
let authApiImpl = async () => ({});
let snapshotImpl = async () => ({});
let guardHealthSensitivePageImpl = async (page, loader) => loader();
let modalOptions = null;
let clipboardOptions = null;
let toasts = [];
let navigations = [];
let currentPages = [];

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

global.Page = (definition) => { pageDefinition = definition; };
global.getCurrentPages = () => currentPages;
global.wx = {
  makePhoneCall: () => {},
  navigateBack: () => { navigations.push('back'); },
  navigateTo: (options) => { navigations.push(options.url); },
  redirectTo: (options) => { navigations.push(options.url); },
  setClipboardData: (options) => { clipboardOptions = options; },
  showModal: (options) => { modalOptions = options; },
  showToast: (options) => { toasts.push(options); },
};

const careSessionPath = require.resolve('../pages/elders/care-session');
require.cache[careSessionPath] = {
  id: careSessionPath,
  filename: careSessionPath,
  loaded: true,
  exports: {
    authApi: (options) => authApiImpl(options),
    finishHealthMutation,
    getSnapshot: () => snapshotImpl(),
    guardHealthSensitivePage: (page, loader) => guardHealthSensitivePageImpl(page, loader),
    requireToken: () => 'session-token',
    resumeHealthMutation,
    suspendHealthMutation,
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
  page.data = Object.assign({}, JSON.parse(JSON.stringify(definition.data)), overrides || {});
  page._mutations = 0;
  page.setData = (next) => {
    page._mutations += 1;
    Object.assign(page.data, next);
  };
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

const loadScenarios = [
  { name: '今日行动', module: '../pages/action-checkin/index', method: 'loadContext' },
  { name: '健康日记', module: '../pages/diary/index', method: 'loadDiary' },
  { name: '用药记录', module: '../pages/medications/index', method: 'loadMedications' },
  { name: '提醒话术', module: '../pages/template/index', method: 'loadTemplate' },
];

loadScenarios.forEach((scenario) => {
  ['resolve', 'reject'].forEach((settlement) => {
    test(`${scenario.name}卸载后延迟${settlement === 'resolve' ? '成功' : '失败'}不再更新界面`, async () => {
      const gate = deferred();
      authApiImpl = () => gate.promise;
      snapshotImpl = async () => ({});
      toasts = [];
      const definition = loadPage(scenario.module);
      const page = makePage(definition, { pairId: 7 });

      const pending = page[scenario.method].call(page);
      await Promise.resolve();
      page.onUnload.call(page);
      const mutationsAtUnload = page._mutations;
      const toastsAtUnload = toasts.length;
      if (settlement === 'resolve') {
        gate.resolve({ items: [{ pair_id: 7, member: { name: '奶奶' } }] });
      } else {
        gate.reject(new Error('offline'));
      }
      await pending;

      assert.equal(page._mutations, mutationsAtUnload);
      assert.equal(toasts.length, toastsAtUnload);
    });
  });
});

test('六个健康敏感页面在守卫拒绝时不会发出私密 API 请求', async (t) => {
  const scenarios = [
    { name: '家庭照护', module: '../pages/elders/index', method: 'onShow', options: undefined },
    { name: '老人资料', module: '../pages/elder-edit/index', method: 'onLoad', options: { mode: 'edit', pair_id: '7' } },
    { name: '健康筛查', module: '../pages/health-assessment/index', method: 'onLoad', options: { pair_id: '7' } },
    { name: '健康日记', module: '../pages/diary/index', method: 'onLoad', options: { pair_id: '7' } },
    { name: '用药记录', module: '../pages/medications/index', method: 'onLoad', options: { pair_id: '7' } },
    { name: '今日行动', module: '../pages/action-checkin/index', method: 'onLoad', options: { pair_id: '7' } },
  ];
  let guardCount = 0;
  let privateRequestCount = 0;
  let snapshotCount = 0;
  t.after(() => {
    authApiImpl = async () => ({});
    snapshotImpl = async () => ({});
    guardHealthSensitivePageImpl = async (page, loader) => loader();
  });
  guardHealthSensitivePageImpl = async () => {
    guardCount += 1;
    return false;
  };
  authApiImpl = async () => {
    privateRequestCount += 1;
    return {};
  };
  snapshotImpl = async () => {
    snapshotCount += 1;
    return {};
  };

  for (let index = 0; index < scenarios.length; index += 1) {
    const scenario = scenarios[index];
    const definition = loadPage(scenario.module);
    const page = makePage(definition);
    await page[scenario.method].call(page, scenario.options);
    assert.equal(guardCount, index + 1, `${scenario.name} 应先进入健康同意守卫`);
    assert.equal(privateRequestCount, 0, `${scenario.name} 的守卫拒绝后不得读取私密 API`);
    assert.equal(snapshotCount, 0, `${scenario.name} 的守卫拒绝后不得读取照护天气快照`);
  }
});

test('老人创建页门禁失败退出加载态，页面重试仍先经过守卫', async (t) => {
  let guardCount = 0;
  let privateRequestCount = 0;
  let gateAvailable = false;
  t.after(() => {
    authApiImpl = async () => ({});
    guardHealthSensitivePageImpl = async (page, loader) => loader();
  });
  guardHealthSensitivePageImpl = async (page, loader) => {
    guardCount += 1;
    if (!gateAvailable) {
      page.onHealthConsentGuardError(Object.assign(new Error('request:fail timeout'), {
        statusCode: 503,
      }));
      return false;
    }
    return loader();
  };
  authApiImpl = async () => {
    privateRequestCount += 1;
    return {};
  };
  const definition = loadPage('../pages/elder-edit/index');
  const page = makePage(definition);

  await page.onLoad.call(page, { mode: 'create' });
  assert.equal(page.data.loading, false);
  assert.equal(page.data.contextReady, false);
  assert.match(page.data.loadError, /授权状态/);
  assert.equal(guardCount, 1);
  assert.equal(privateRequestCount, 0);

  gateAvailable = true;
  await page.loadAuthorizedPage.call(page, { currentTarget: { id: 'retry' } });
  assert.equal(guardCount, 2);
  assert.equal(privateRequestCount, 0);
  assert.equal(page.data.loading, false);
  assert.equal(page.data.contextReady, true);
  assert.equal(page.data.loadError, '');
});

test('家庭照护门禁失败退出加载态，重试成功后显示老人资料', async (t) => {
  let guardCount = 0;
  let privateRequestCount = 0;
  let gateAvailable = false;
  t.after(() => {
    authApiImpl = async () => ({});
    snapshotImpl = async () => ({});
    guardHealthSensitivePageImpl = async (page, loader) => loader();
  });
  guardHealthSensitivePageImpl = async (page, loader) => {
    guardCount += 1;
    if (!gateAvailable) {
      page.onHealthConsentGuardError(new Error('request:fail timeout'));
      return false;
    }
    return loader();
  };
  authApiImpl = async () => {
    privateRequestCount += 1;
    return {
      items: [{ pair_id: 7, member: { name: '奶奶', relation: '祖母', age: 72 } }],
    };
  };
  snapshotImpl = async () => ({});
  const definition = loadPage('../pages/elders/index');
  const page = makePage(definition);

  await page.onShow.call(page);
  assert.equal(page.data.loading, false);
  assert.match(page.data.loadError, /授权状态/);
  assert.equal(guardCount, 1);
  assert.equal(privateRequestCount, 0);

  gateAvailable = true;
  await page.retryLoad.call(page);
  assert.equal(guardCount, 2);
  assert.equal(privateRequestCount, 1);
  assert.equal(page.data.loading, false);
  assert.equal(page.data.loadError, '');
  assert.equal(page.data.elders[0].displayName, '奶奶');

  const view = fs.readFileSync(path.join(__dirname, '../pages/elders/index.wxml'), 'utf8');
  assert.equal((view.match(/bindtap="retryLoad"/g) || []).length, 2);
  assert.doesNotMatch(view, /bindtap="loadCareHome"/);
});

test('家庭照护添加入口在加载和失败时给出反馈，资料就绪后才进入创建页', () => {
  const definition = loadPage('../pages/elders/index');
  const page = makePage(definition);
  toasts = [];
  navigations = [];

  page.goCreate.call(page);
  assert.match(toasts.at(-1).title, /仍在加载/);
  assert.deepEqual(navigations, []);

  page.setData({ loading: false, loadError: '加载失败' });
  page.goCreate.call(page);
  assert.match(toasts.at(-1).title, /先重试/);
  assert.deepEqual(navigations, []);

  page._healthConsentLoadedOnce = true;
  page.setData({ loading: false, loadError: '' });
  page.goCreate.call(page);
  assert.deepEqual(navigations, ['/pages/elder-edit/index?mode=create']);
});

test('老人资料前台保存成功后返回家庭页会重新读取当前账号资料', async (t) => {
  const originalSetTimeout = global.setTimeout;
  const originalClearTimeout = global.clearTimeout;
  let returnCallback = null;
  let getCalls = 0;
  let postCalls = 0;
  t.after(() => {
    global.setTimeout = originalSetTimeout;
    global.clearTimeout = originalClearTimeout;
    currentPages = [];
    authApiImpl = async () => ({});
    snapshotImpl = async () => ({});
    guardHealthSensitivePageImpl = async (page, loader) => loader();
  });
  global.setTimeout = (callback) => {
    returnCallback = callback;
    return 71;
  };
  global.clearTimeout = () => {};
  authApiImpl = async (options) => {
    if (options.method === 'POST') {
      postCalls += 1;
      return { pair_id: 9 };
    }
    if (options.method === 'GET' && options.path === '/mp/api/v1/elders') {
      getCalls += 1;
      return {
        items: [{ pair_id: 9, member: { name: '外婆', relation: '祖母', age: 76 } }],
      };
    }
    return {};
  };
  snapshotImpl = async () => ({});
  guardHealthSensitivePageImpl = async (page, loader) => {
    if (page._healthConsentReloadPending !== true) return false;
    page._healthConsentReloadPending = false;
    page._healthConsentLoadedOnce = true;
    return loader();
  };

  const eldersDefinition = loadPage('../pages/elders/index');
  const eldersPage = makePage(eldersDefinition, { loading: false });
  eldersPage.route = 'pages/elders/index';
  eldersPage._healthConsentLoadedOnce = true;
  eldersPage.onHide.call(eldersPage);

  const editDefinition = loadPage('../pages/elder-edit/index');
  const editPage = makePage(editDefinition, {
    mode: 'create',
    pairId: null,
    name: '外婆',
    relation: '祖母',
    age: '76',
    gender: '女性',
    chronicText: '',
    contextReady: true,
    loading: false,
  });
  editPage.route = 'pages/elder-edit/index';
  editPage._unloaded = false;
  editPage._hidden = false;
  editPage._lifecycleGeneration = 1;
  currentPages = [eldersPage, editPage];
  navigations = [];

  await editPage.onSave.call(editPage);
  assert.equal(postCalls, 1);
  assert.equal(eldersPage._healthConsentReloadPending, true);
  assert.equal(typeof returnCallback, 'function');
  returnCallback();
  assert.deepEqual(navigations, ['back']);

  currentPages = [eldersPage];
  await eldersPage.onShow.call(eldersPage);
  assert.equal(getCalls, 1);
  assert.equal(eldersPage.data.loading, false);
  assert.equal(eldersPage.data.elders[0].displayName, '外婆');
});

const mutationResumeScenarios = [
  {
    name: '老人资料',
    module: '../pages/elder-edit/index',
    method: 'onSave',
    loader: 'loadAuthorizedPage',
    busyKey: 'busy',
    busyValue: true,
    setup(definition) {
      return {
        overrides: {
          mode: 'edit',
          pairId: 7,
          name: '奶奶',
          relation: '祖母',
          age: '72',
          gender: '女性',
          chronicText: '',
          contextReady: true,
        },
        route: { _routeMode: 'edit', _routePairId: 7 },
      };
    },
  },
  {
    name: '健康筛查',
    module: '../pages/health-assessment/index',
    method: 'submitAssessment',
    loader: 'loadPage',
    busyKey: 'busy',
    busyValue: true,
    setup(definition) {
      const answers = Object.fromEntries(
        definition.data.questions.map((question) => [question.id, question.options[0].value])
      );
      return { overrides: { pairId: 7, answers, contextReady: true }, route: {} };
    },
  },
  {
    name: '健康日记',
    module: '../pages/diary/index',
    method: 'saveEntry',
    loader: 'loadDiary',
    busyKey: 'busy',
    busyValue: true,
    setup() {
      return {
        overrides: {
          pairId: 7,
          contextReady: true,
          entryDate: '2026-07-19',
          todayDate: '2026-07-19',
          severity: '轻微',
          symptoms: '状态正常',
        },
        route: { _routePairId: 7 },
      };
    },
  },
  {
    name: '用药记录',
    module: '../pages/medications/index',
    method: 'addMedication',
    loader: 'loadMedications',
    busyKey: 'busy',
    busyValue: true,
    setup() {
      return {
        overrides: { pairId: 7, contextReady: true, medicineName: '测试药品' },
        route: { _routePairId: 7 },
      };
    },
  },
  {
    name: '今日行动',
    module: '../pages/action-checkin/index',
    method: 'confirmActions',
    loader: 'loadContext',
    busyKey: 'busyAction',
    busyValue: 'confirm',
    setup() {
      return {
        overrides: {
          pairId: 7,
          contextReady: true,
          actions: [{ id: 'check_weather', checked: true }],
          selectedActions: ['check_weather'],
        },
        route: { _routePairId: 7 },
      };
    },
  },
  {
    name: '今日复盘',
    module: '../pages/action-checkin/index',
    method: 'submitDebrief',
    loader: 'loadContext',
    busyKey: 'busyAction',
    busyValue: 'debrief',
    setup() {
      return {
        overrides: {
          pairId: 7,
          contextReady: true,
          question2: '喝水最容易',
          question3: '请家人提醒',
          difficulty: '天气太热',
        },
        route: { _routePairId: 7 },
      };
    },
  },
];

mutationResumeScenarios.forEach((scenario) => {
  test(`${scenario.name}写入期间隐藏，返回后等待完成、解除忙碌并重新加载`, async () => {
    const gate = deferred();
    let requestCount = 0;
    authApiImpl = () => {
      requestCount += 1;
      return gate.promise;
    };
    const definition = loadPage(scenario.module);
    const prepared = scenario.setup(definition);
    const page = makePage(definition, prepared.overrides);
    Object.assign(page, prepared.route, {
      _hidden: false,
      _unloaded: false,
      _lifecycleGeneration: 1,
    });
    let reloads = 0;
    page[scenario.loader] = async () => {
      reloads += 1;
      page.setData({ loading: false });
    };

    const pendingMutation = page[scenario.method].call(page);
    await Promise.resolve();
    assert.equal(page.data[scenario.busyKey], scenario.busyValue);
    page.onHide.call(page);
    gate.resolve({ id: 11, ok: true });
    await pendingMutation;
    assert.equal(page.data[scenario.busyKey], scenario.busyValue);

    await page.onShow.call(page);
    assert.equal(page.data[scenario.busyKey], scenario.busyKey === 'busy' ? false : '');
    assert.equal(page.data.loading, false);
    assert.equal(reloads, 1);
    assert.equal(requestCount, 1);
    if (scenario.name === '健康筛查') assert.deepEqual(page.data.answers, {});
    if (scenario.name === '健康日记') assert.equal(page.data.symptoms, '');
    if (scenario.name === '用药记录') assert.equal(page.data.medicineName, '');
    if (scenario.name === '今日复盘') {
      assert.equal(page.data.question2, '');
      assert.equal(page.data.question3, '');
      assert.equal(page.data.difficulty, '');
    }
  });
});

mutationResumeScenarios
  .filter((scenario) => ['健康筛查', '健康日记', '用药记录', '今日复盘'].includes(scenario.name))
  .forEach((scenario) => {
    test(`${scenario.name}后台写入失败后保留用户草稿以便重试`, async () => {
      const gate = deferred();
      authApiImpl = () => gate.promise;
      const definition = loadPage(scenario.module);
      const prepared = scenario.setup(definition);
      const page = makePage(definition, prepared.overrides);
      Object.assign(page, prepared.route, {
        _hidden: false,
        _unloaded: false,
        _lifecycleGeneration: 1,
      });
      page[scenario.loader] = async () => { page.setData({ loading: false }); };

      const pendingMutation = page[scenario.method].call(page);
      await Promise.resolve();
      page.onHide.call(page);
      gate.reject(new Error('offline'));
      await pendingMutation;
      await page.onShow.call(page);

      if (scenario.name === '健康筛查') assert.ok(Object.keys(page.data.answers).length > 0);
      if (scenario.name === '健康日记') assert.equal(page.data.symptoms, '状态正常');
      if (scenario.name === '用药记录') assert.equal(page.data.medicineName, '测试药品');
      if (scenario.name === '今日复盘') {
        assert.equal(page.data.question2, '喝水最容易');
        assert.equal(page.data.question3, '请家人提醒');
        assert.equal(page.data.difficulty, '天气太热');
      }
    });
  });

test('老人资料后台保存失败后保留草稿、退出忙碌且不发 GET 覆盖', async (t) => {
  const gate = deferred();
  const requests = [];
  let retryShouldSucceed = false;
  toasts = [];
  t.after(() => {
    authApiImpl = async () => ({});
    guardHealthSensitivePageImpl = async (page, loader) => loader();
  });
  authApiImpl = (options) => {
    requests.push(options);
    if (options.method === 'GET') {
      return Promise.resolve({
        items: [{ pair_id: 7, member: { name: '服务器旧称呼', age: 70 } }],
      });
    }
    return retryShouldSucceed ? Promise.resolve({ ok: true }) : gate.promise;
  };
  const definition = loadPage('../pages/elder-edit/index');
  const page = makePage(definition, {
    mode: 'edit',
    pairId: 7,
    name: '奶奶的新称呼',
    relation: '祖母',
    age: '72',
    gender: '女性',
    genderIndex: 1,
    chronicText: '高血压、糖尿病',
    contextReady: true,
    loading: false,
  });
  Object.assign(page, {
    _routeMode: 'edit',
    _routePairId: 7,
    _hidden: false,
    _unloaded: false,
    _lifecycleGeneration: 1,
  });

  const pendingMutation = page.onSave.call(page);
  await Promise.resolve();
  assert.equal(page.data.busy, true);
  page.onHide.call(page);
  gate.reject(new Error('offline'));
  await pendingMutation;
  await page.onShow.call(page);

  assert.equal(page.data.busy, false);
  assert.equal(page.data.loading, false);
  assert.equal(page.data.contextReady, true);
  assert.equal(page.data.name, '奶奶的新称呼');
  assert.equal(page.data.chronicText, '高血压、糖尿病');
  assert.deepEqual(requests.map((item) => item.method), ['PATCH']);
  assert.match(toasts.at(-1).title, /保存失败.*重试/);

  await page.onShow.call(page);
  assert.deepEqual(requests.map((item) => item.method), ['PATCH']);
  assert.equal(page.data.name, '奶奶的新称呼');

  retryShouldSucceed = true;
  await page.onSave.call(page);
  assert.deepEqual(requests.map((item) => item.method), ['PATCH', 'PATCH']);
  assert.equal(page.data.busy, false);
  assert.equal(toasts.at(-1).title, '已保存');
  page.onUnload.call(page);
});

test('当天求助状态恢复后再次提交使用更新语义', async () => {
  const requests = [];
  const { duchangDateKey } = require('../utils/format');
  authApiImpl = async (options) => {
    requests.push(options);
    if (options.method === 'GET') {
      return {
        items: [{
          pair_id: 7,
          member: { name: '奶奶' },
          today: { status_date: duchangDateKey(), help_flag: true },
        }],
      };
    }
    return { ok: true };
  };
  snapshotImpl = async () => ({});
  toasts = [];
  modalOptions = null;
  const definition = loadPage('../pages/action-checkin/index');
  const page = makePage(definition, { pairId: 7, helpNote: '请晚点回电话' });

  await page.loadContext.call(page);
  assert.equal(page.data.helpRecorded, true);
  page.requestHelp.call(page);
  assert.match(modalOptions.title, /更新求助说明/);
  assert.equal(modalOptions.confirmText, '更新说明');
  await modalOptions.success({ confirm: true });

  const request = requests.find((item) => item.method === 'POST');
  assert.equal(request.path, '/mp/api/v1/actions/7/help');
  assert.equal(request.data.note, '请晚点回电话');
  assert.equal(toasts.at(-1).title, '求助说明已更新');

  const view = fs.readFileSync(path.join(__dirname, '../pages/action-checkin/index.wxml'), 'utf8');
  assert.match(view, /helpRecorded \? '更新求助说明' : '记录求助需求'/);
});

test('提醒话术页卸载后复制回调和导航入口全部失效', () => {
  let eventRequests = 0;
  authApiImpl = async () => { eventRequests += 1; return {}; };
  toasts = [];
  navigations = [];
  clipboardOptions = null;
  const definition = loadPage('../pages/template/index');
  const page = makePage(definition, { pairId: 7, message: '测试提醒', contextReady: true });

  page.copyMessage.call(page);
  page.onUnload.call(page);
  clipboardOptions.success();
  clipboardOptions.fail();
  page.goCheckin.call(page);
  page.back.call(page);

  assert.deepEqual(toasts, []);
  assert.deepEqual(navigations, []);
  assert.equal(eventRequests, 0);
});
