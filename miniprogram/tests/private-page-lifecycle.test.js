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
