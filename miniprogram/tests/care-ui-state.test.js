const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let pageDefinition;
let authApiImpl = async () => ({});
let snapshotImpl = async () => ({});
let modalOptions = null;
let clipboardOptions = null;
let toasts = [];
let navigations = [];

global.Page = (definition) => { pageDefinition = definition; };
global.wx = {
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

test('健康筛查单选项具备语义、动态标签与可见选中标记', () => {
  const view = fs.readFileSync(path.join(__dirname, '../pages/health-assessment/index.wxml'), 'utf8');
  assert.match(view, /role="radiogroup"/);
  assert.match(view, /role="radio"/);
  assert.match(view, /aria-checked="\{\{option\.active\}\}"/);
  assert.match(view, /option\.active \? '已选择' : '未选择'/);
  assert.match(view, /class="selected-label">已选</);
  assert.match(view, /latestError/);
  assert.match(view, /bindtap="loadLatest"/);
});

test('健康筛查历史读取失败会保留旧结果并支持重试恢复', async () => {
  const definition = loadPage('../pages/health-assessment/index');
  const oldLatest = { id: 8, riskLevel: '低风险', recommendations: [] };
  const page = makePage(definition, { pairId: 7, latest: oldLatest });
  page._unloaded = false;
  page._latestRequestToken = 0;

  authApiImpl = async () => { throw new Error('offline'); };
  await page.loadLatest.call(page);
  assert.equal(page.data.latest, oldLatest);
  assert.equal(page.data.latestLoading, false);
  assert.match(page.data.latestError, /仍可继续/);

  authApiImpl = async () => ({
    latest: { id: 9, risk_level: '需关注', recommendations: ['联系家人'] },
  });
  await page.loadLatest.call(page);
  assert.equal(page.data.latest.id, 9);
  assert.equal(page.data.latestError, '');
});

test('老人资料新增和编辑都拒绝空姓名', () => {
  const { validateElderInput } = require('../pages/elders/care-logic');
  const createResult = validateElderInput({ name: '   ', age: '70' }, { mode: 'create' });
  const editResult = validateElderInput({ name: '', age: '70' }, { mode: 'edit' });
  assert.equal(createResult.valid, false);
  assert.equal(editResult.valid, false);
  assert.match(editResult.error, /姓名|称呼/);
});

test('健康日记按 UTC+8 跨午夜更新默认日期并保留用户手选日期', () => {
  const definition = loadPage('../pages/diary/index');
  const page = makePage(definition);
  page._entryDateTouched = false;

  page.syncTodayDate.call(page, '2026-07-18T15:59:59Z', true);
  assert.equal(page.data.todayDate, '2026-07-18');
  assert.equal(page.data.entryDate, '2026-07-18');

  page.syncTodayDate.call(page, '2026-07-18T16:00:00Z');
  assert.equal(page.data.todayDate, '2026-07-19');
  assert.equal(page.data.entryDate, '2026-07-19');

  page.onDateChange.call(page, { detail: { value: '2026-07-18' } });
  page.syncTodayDate.call(page, '2026-07-19T16:00:00Z');
  assert.equal(page.data.todayDate, '2026-07-20');
  assert.equal(page.data.entryDate, '2026-07-18');
});

test('健康日记首错持久呈现，刷新失败保留上次成功数据且未核验时禁止保存', async () => {
  toasts = [];
  let requests = [];
  const definition = loadPage('../pages/diary/index');
  const page = makePage(definition, { pairId: 7 });
  page._unloaded = false;
  page._lifecycleGeneration = 1;

  authApiImpl = async (options) => { requests.push(options); throw new Error('offline'); };
  await page.loadDiary.call(page);
  assert.equal(page.data.contextReady, false);
  assert.match(page.data.loadError, /重试/);

  authApiImpl = async (options) => {
    requests.push(options);
    if (options.path === '/mp/api/v1/elders') {
      return { items: [{ pair_id: 7, member: { name: '奶奶' } }] };
    }
    return { items: [{ id: 12, entry_date: '2026-07-18', severity: '轻微', symptoms: '状态正常' }] };
  };
  await page.loadDiary.call(page);
  assert.equal(page.data.contextReady, true);
  assert.equal(page.data.entries.length, 1);

  authApiImpl = async () => { throw new Error('offline'); };
  await page.loadDiary.call(page);
  assert.equal(page.data.contextReady, true);
  assert.equal(page.data.entries[0].id, 12);
  assert.equal(page.data.dataStale, true);
  assert.match(page.data.loadError, /上次成功加载/);

  const blockedPage = makePage(definition, {
    pairId: 7,
    contextReady: false,
    entryDate: '2026-07-18',
    severity: '轻微',
    symptoms: '状态正常',
  });
  blockedPage._unloaded = false;
  requests = [];
  authApiImpl = async (options) => { requests.push(options); return {}; };
  await blockedPage.saveEntry.call(blockedPage);
  assert.equal(requests.length, 0);
  assert.match(toasts.at(-1).title, /重新加载/);
});

test('用药记录首错、较早数据和删除后刷新失败均保持真实状态', async () => {
  toasts = [];
  modalOptions = null;
  const definition = loadPage('../pages/medications/index');
  const page = makePage(definition, { pairId: 7 });
  page._unloaded = false;
  page._lifecycleGeneration = 1;

  authApiImpl = async () => { throw new Error('offline'); };
  await page.loadMedications.call(page);
  assert.equal(page.data.contextReady, false);
  assert.match(page.data.loadError, /重试/);

  authApiImpl = async (options) => {
    if (options.path === '/mp/api/v1/elders') {
      return { items: [{ pair_id: 7, member: { name: '爷爷' } }] };
    }
    return {
      items: [
        { id: 1, medicine_name: '药品一', frequency: 'daily' },
        { id: 2, medicine_name: '药品二', frequency: 'daily' },
      ],
    };
  };
  await page.loadMedications.call(page);
  assert.equal(page.data.medications.length, 2);

  authApiImpl = async () => { throw new Error('offline'); };
  await page.loadMedications.call(page);
  assert.equal(page.data.contextReady, true);
  assert.equal(page.data.medications.length, 2);
  assert.equal(page.data.dataStale, true);

  authApiImpl = async (options) => {
    if (options.method === 'DELETE') return { ok: true };
    throw new Error('refresh_offline');
  };
  page.deleteMedication.call(page, { currentTarget: { dataset: { id: 1, name: '药品一' } } });
  await modalOptions.success({ confirm: true });
  assert.deepEqual(page.data.medications.map((item) => item.id), [2]);
  assert.equal(page.data.contextReady, true);
  assert.equal(page.data.dataStale, true);
  assert.match(page.data.loadError, /上次成功加载/);
});

test('提醒话术仅在上下文核验且内容非空时开放复制与行动', async () => {
  toasts = [];
  navigations = [];
  clipboardOptions = null;
  const definition = loadPage('../pages/template/index');
  const page = makePage(definition, { pairId: null });
  page._unloaded = false;
  page._lifecycleGeneration = 1;

  await page.loadTemplate.call(page);
  assert.equal(page.data.contextReady, false);
  assert.match(page.data.loadError, /缺少家人信息/);

  page.copyMessage.call(page);
  page.goCheckin.call(page);
  assert.equal(clipboardOptions, null);
  assert.deepEqual(navigations, []);

  page.setData({ pairId: 7 });
  authApiImpl = async () => { throw new Error('offline'); };
  await page.loadTemplate.call(page);
  assert.equal(page.data.contextReady, false);
  assert.match(page.data.loadError, /重新|重试/);

  authApiImpl = async () => ({ items: [{ pair_id: 7, member: { name: '奶奶', relation: '祖母' } }] });
  snapshotImpl = async () => ({ current: { temperature_max: 36, temperature_min: 27 } });
  await page.loadTemplate.call(page);
  assert.equal(page.data.contextReady, true);
  assert.ok(page.data.message.trim());
  page.copyMessage.call(page);
  page.goCheckin.call(page);
  assert.ok(clipboardOptions);
  assert.deepEqual(navigations, ['/pages/action-checkin/index?pair_id=7']);

  const view = fs.readFileSync(path.join(__dirname, '../pages/template/index.wxml'), 'utf8');
  assert.match(view, /bindtap="loadTemplate"/);
  assert.match(view, /返回家庭照护/);
});

['resolve', 'reject'].forEach((settlement) => {
  test(`老人编辑页卸载后延迟${settlement === 'resolve' ? '成功' : '失败'}不再更新界面`, async () => {
    const gate = deferred();
    toasts = [];
    authApiImpl = () => gate.promise;
    const definition = loadPage('../pages/elder-edit/index');
    const page = makePage(definition, { pairId: 7, mode: 'edit' });
    page._unloaded = false;
    page._lifecycleGeneration = 1;

    const pending = page.loadElder.call(page);
    await Promise.resolve();
    page.onUnload.call(page);
    const mutationsAtUnload = page._mutations;
    if (settlement === 'resolve') {
      gate.resolve({ items: [{ pair_id: 7, member: { name: '奶奶' } }] });
    } else {
      gate.reject(new Error('offline'));
    }
    await pending;

    assert.equal(page._mutations, mutationsAtUnload);
    assert.deepEqual(toasts, []);
  });
});

test('家庭照护的查看预警跳转不再携带老人标识', () => {
  navigations = [];
  const definition = loadPage('../pages/elders/index');
  const page = makePage(definition);
  page.goAlerts.call(page);
  assert.deepEqual(navigations, ['/pages/alerts/index']);

  const view = fs.readFileSync(path.join(__dirname, '../pages/elders/index.wxml'), 'utf8');
  const warningButton = view.match(/<button[^>]*bindtap="goAlerts"[^>]*>/);
  assert.ok(warningButton);
  assert.doesNotMatch(warningButton[0], /data-pair-id/);
});
