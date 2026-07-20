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
global.wx = {
  makePhoneCall: () => {},
  navigateBack: () => { navigations.push('back'); },
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
