const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const storage = new Map();
let requestImpl = async () => ({});
let currentPages = [];
let navigations = [];
let relaunchedTo = '';
let pageDefinition = null;
let toasts = [];
let modalOptions = null;

global.getApp = () => ({ globalData: {} });
global.getCurrentPages = () => currentPages;
global.Page = (definition) => { pageDefinition = definition; };
global.wx = {
  getStorageSync: (key) => storage.get(key),
  setStorageSync: (key, value) => storage.set(key, value),
  removeStorageSync: (key) => storage.delete(key),
  navigateBack: (options) => {
    navigations.push('back');
    if (options && typeof options.success === 'function') options.success();
  },
  navigateTo: (options) => {
    navigations.push(options.url);
    if (typeof options.success === 'function') options.success();
  },
  reLaunch: (options) => { relaunchedTo = options.url; },
  showModal: (options) => { modalOptions = options; },
  showToast: (options) => { toasts.push(options); },
  switchTab: () => {},
};

const requestPath = require.resolve('../utils/request');
require.cache[requestPath] = {
  id: requestPath,
  filename: requestPath,
  loaded: true,
  exports: { api: (options) => requestImpl(options) },
};

const careSession = require('../pages/elders/care-session');
const { SESSION_KEY } = require('../utils/session');

function makePage(definition, overrides) {
  const page = Object.assign({}, definition);
  page.data = Object.assign({}, JSON.parse(JSON.stringify(definition.data)), overrides || {});
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

function resetSession(token) {
  storage.clear();
  navigations = [];
  relaunchedTo = '';
  toasts = [];
  modalOptions = null;
  currentPages = [];
  careSession.releaseHealthConsentNavigation();
  careSession.saveToken(token, {
    login_method: 'wechat',
    privacy_consent_version: 'privacy-v1',
  });
}

test('真实健康写入 helper 在隐藏恢复后保留成功结果、类型和元数据', async () => {
  const gate = deferred();
  const page = { _healthConsentVisibilityGeneration: 3 };
  const meta = { pairId: 7, draft: '测试草稿' };
  const pending = careSession.trackHealthMutation(page, gate.promise, 'diary-save', meta);

  assert.equal(careSession.suspendHealthMutation(page), true);
  assert.equal(page._healthConsentVisibilityGeneration, 4);
  assert.equal(page._healthConsentReloadPending, true);
  const recovery = careSession.resumeHealthMutation(page);
  gate.resolve({ id: 11 });
  const result = await recovery;

  assert.deepEqual(result, {
    resumed: true,
    ok: true,
    value: { id: 11 },
    error: null,
    kind: 'diary-save',
    meta,
  });
  assert.equal(page._healthMutationPromise, null);
  assert.equal(page._healthMutationKind, '');
  assert.equal(page._healthMutationMeta, null);
  assert.equal(page._healthMutationResumePromise, null);
});

test('真实健康写入 helper 在隐藏恢复失败时返回原始错误并清理状态', async () => {
  const gate = deferred();
  const page = {};
  const error = new Error('offline');
  careSession.trackHealthMutation(page, gate.promise, 'medication-add', { pairId: 8 });
  assert.equal(careSession.suspendHealthMutation(page), true);

  const recovery = careSession.resumeHealthMutation(page);
  gate.reject(error);
  const result = await recovery;

  assert.equal(result.resumed, true);
  assert.equal(result.ok, false);
  assert.equal(result.value, null);
  assert.equal(result.error, error);
  assert.equal(result.kind, 'medication-add');
  assert.deepEqual(result.meta, { pairId: 8 });
  assert.equal(page._healthMutationPromise, null);
  assert.equal(page._healthMutationResumePromise, null);
});

test('结束旧健康写入不会清除后来开始的新写入', async () => {
  const firstGate = deferred();
  const secondGate = deferred();
  const page = {};
  const first = careSession.trackHealthMutation(page, firstGate.promise, 'first');
  const second = careSession.trackHealthMutation(page, secondGate.promise, 'second', { current: true });

  careSession.finishHealthMutation(page, first);
  assert.equal(page._healthMutationPromise, second);
  assert.equal(page._healthMutationKind, 'second');
  assert.deepEqual(page._healthMutationMeta, { current: true });

  firstGate.resolve({ id: 1 });
  secondGate.resolve({ id: 2 });
  await Promise.all([first, second]);
  careSession.finishHealthMutation(page, second);
  assert.equal(page._healthMutationPromise, null);
  assert.equal(page._healthMutationKind, '');
  assert.equal(page._healthMutationMeta, null);
});

test('隐藏时即使没有健康写入也推进可见性世代并标记未结算守卫重载', () => {
  const page = {
    _healthConsentVisibilityGeneration: 8,
    _healthConsentGuardPromise: Promise.resolve(false),
  };

  assert.equal(careSession.suspendHealthMutation(page), false);
  assert.equal(page._healthConsentVisibilityGeneration, 9);
  assert.equal(page._healthConsentReloadPending, true);
});

test('健康同意页默认不勾选并完整说明范围、拒绝影响与删除方式', () => {
  delete require.cache[require.resolve('../pages/health-consent/index')];
  pageDefinition = null;
  require('../pages/health-consent/index');

  assert.equal(pageDefinition.data.agreed, false);
  const view = fs.readFileSync(path.join(__dirname, '../pages/health-consent/index.wxml'), 'utf8');
  assert.match(view, /为什么需要/);
  assert.match(view, /会处理哪些资料/);
  assert.match(view, /保存、影响与删除/);
  assert.match(view, /拒绝后仍可正常使用/);
  assert.match(view, /年满 18 岁的家人/);
  assert.match(view, /checked="\{\{agreed\}\}"/);
  assert.match(view, /账号、隐私与注销/);
  assert.match(view, /UTC 同意时间：\{\{consentTimeUtc\}\}/);
  const source = fs.readFileSync(path.join(__dirname, '../pages/health-consent/index.js'), 'utf8');
  assert.match(source, /如需逐条删除，请先取消本次撤回并到对应功能删除；撤回后仍可通过账号注销删除账号关联资料，重新单独同意后也可进入各功能管理。/);
  const style = fs.readFileSync(path.join(__dirname, '../pages/health-consent/index.wxss'), 'utf8');
  assert.match(style, /\.text-link\s*\{[\s\S]*?min-height:\s*88rpx/);
});

test('健康同意页先读取服务端版本，再原样提交单独同意', async () => {
  resetSession('consent-page-token');
  const requests = [];
  requestImpl = async (options) => {
    requests.push(options);
    if (options.method === 'GET') {
      return {
        required_health_consent_version: 'health-v3',
        health_consent_current: false,
        health_consented_at: null,
      };
    }
    return { health_consent_current: true };
  };
  delete require.cache[require.resolve('../pages/health-consent/index')];
  pageDefinition = null;
  require('../pages/health-consent/index');
  const page = makePage(pageDefinition);
  currentPages = [{ route: 'pages/elders/index' }, { route: 'pages/health-consent/index' }];

  await page.onLoad.call(page, { required_version: 'stale-client-version' });
  assert.equal(page.data.requiredVersion, 'health-v3');
  assert.equal(page.data.agreed, false);
  page.onAgreementChange.call(page, { detail: { value: ['health-consent'] } });
  await page.submitConsent.call(page);

  assert.deepEqual(requests.map((item) => [item.method, item.path]), [
    ['GET', '/mp/api/v1/health-consent'],
    ['POST', '/mp/api/v1/health-consent'],
  ]);
  assert.deepEqual(requests[1].data, {
    consent: true,
    health_consent_version: 'health-v3',
  });
  assert.equal(navigations.at(-1), 'back');
});

test('未勾选时不提交，取消会销毁私密页面栈并回公开首页', async () => {
  resetSession('decline-token');
  let requestCount = 0;
  requestImpl = async () => { requestCount += 1; return {}; };
  const page = makePage(pageDefinition, { requiredVersion: 'health-v1', loading: false });

  await page.submitConsent.call(page);
  assert.equal(requestCount, 0);
  assert.match(page.data.statusHint, /有权管理/);

  page.goPublicHome.call(page);
  assert.equal(relaunchedTo, '/pages/home/index');
});

test('管理模式保留当前回执页面，确认撤回后只关闭私密功能并回公开首页', async () => {
  resetSession('manage-consent-token');
  const methods = [];
  const paths = [];
  requestImpl = async (options) => {
    methods.push(options.method);
    paths.push(options.path);
    if (options.method === 'GET') {
      return {
        required_health_consent_version: 'health-v4',
        health_consent_current: true,
        health_consented_at: '2026-07-19T10:00:00Z',
      };
    }
    return { health_consent_current: false };
  };
  delete require.cache[require.resolve('../pages/health-consent/index')];
  pageDefinition = null;
  require('../pages/health-consent/index');
  const page = makePage(pageDefinition);
  currentPages = [{ route: 'pages/health-consent/index' }];

  await page.onLoad.call(page, { manage: '1' });
  assert.equal(page.data.manageMode, true);
  assert.equal(page.data.consentCurrent, true);
  assert.equal(page.data.consentTimeUtc, '2026-07-19T10:00:00Z');
  assert.deepEqual(navigations, []);

  page.withdrawConsent.call(page);
  assert.match(modalOptions.title, /撤回/);
  await modalOptions.success({ confirm: true });

  assert.deepEqual(methods, ['GET', 'DELETE']);
  assert.deepEqual(paths, ['/mp/api/v1/health-consent', '/mp/api/v1/health-consent']);
  assert.equal(relaunchedTo, '/pages/home/index');
  assert.equal(storage.get(SESSION_KEY).token, 'manage-consent-token');
});

test('精确 428 会清空页面健康数据并只导航一次，登录会话保持有效', async () => {
  resetSession('exact-428-token');
  const privatePage = {
    scrubCount: 0,
    onHealthConsentRequired() { this.scrubCount += 1; },
  };
  currentPages = [privatePage];
  requestImpl = async () => {
    const error = new Error('请先单独同意处理健康敏感个人信息');
    error.statusCode = 428;
    error.code = 'health_sensitive_consent_required';
    error.data = { required_health_consent_version: 'health-v2' };
    throw error;
  };

  await Promise.allSettled([
    careSession.authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
    careSession.authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
  ]);

  assert.equal(privatePage.scrubCount, 2);
  assert.equal(navigations.filter((url) => url.startsWith('/pages/health-consent/index')).length, 1);
  assert.equal(storage.get(SESSION_KEY).token, 'exact-428-token');
});

test('同为 428 但错误码不匹配时不会误跳健康同意页', async () => {
  resetSession('other-428-token');
  requestImpl = async () => {
    const error = new Error('other precondition');
    error.statusCode = 428;
    error.code = 'other_precondition';
    throw error;
  };

  await assert.rejects(
    careSession.authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
    /other precondition/
  );
  assert.deepEqual(navigations, []);
  assert.equal(storage.get(SESSION_KEY).token, 'other-428-token');
});

test('健康同意缓存随会话切换清除，页面守卫并发时只加载一次', async () => {
  resetSession('account-a');
  const checkedTokens = [];
  requestImpl = async (options) => {
    checkedTokens.push(options.token);
    return {
      required_health_consent_version: 'health-v1',
      health_consent_current: true,
    };
  };
  assert.equal(await careSession.ensureHealthConsent(), true);
  assert.equal(await careSession.ensureHealthConsent(), true);

  careSession.saveToken('account-b', {
    login_method: 'wechat',
    privacy_consent_version: 'privacy-v1',
  });
  const page = { _unloaded: false, _hidden: false };
  let loads = 0;
  await Promise.all([
    careSession.guardHealthSensitivePage(page, async () => { loads += 1; }),
    careSession.guardHealthSensitivePage(page, async () => { loads += 1; }),
  ]);

  assert.deepEqual(checkedTokens, ['account-a', 'account-b']);
  assert.equal(loads, 1);
});

test('六个健康敏感页面都在加载前守卫，并在隐藏或撤回时停止异步写回', () => {
  const pages = ['elders', 'elder-edit', 'health-assessment', 'diary', 'medications', 'action-checkin'];
  pages.forEach((name) => {
    const source = fs.readFileSync(path.join(__dirname, `../pages/${name}/index.js`), 'utf8');
    assert.match(source, /guardHealthSensitivePage\(this,/, `${name} 应在加载前执行健康同意守卫`);
    assert.match(source, /onHealthConsentRequired\(\)/, `${name} 应在撤回或版本过期时清空健康数据`);
    assert.match(source, /onHide\(\)/, `${name} 应在隐藏后让异步结果失效`);
  });
});

test('同意页返回后 onShow 并发只重载一次，后续普通 onShow 不重复请求', async () => {
  resetSession('return-reload-token');
  requestImpl = async () => ({
    required_health_consent_version: 'health-v5',
    health_consent_current: false,
  });
  const page = { _unloaded: false, _hidden: false };
  currentPages = [page];
  let loads = 0;

  assert.equal(await careSession.guardHealthSensitivePage(page, async () => { loads += 1; }), false);
  assert.equal(loads, 0);
  careSession.markHealthConsentCurrent('health-v5');
  page._hidden = false;
  await Promise.all([
    careSession.guardHealthSensitivePage(page, async () => { loads += 1; }),
    careSession.guardHealthSensitivePage(page, async () => { loads += 1; }),
  ]);
  await careSession.guardHealthSensitivePage(page, async () => { loads += 1; });

  assert.equal(loads, 1);
});

test('首次私密加载期间隐藏页面，返回后会重新加载且不沿用未完成标记', async () => {
  resetSession('hidden-reload-token');
  requestImpl = async () => ({
    required_health_consent_version: 'health-v6',
    health_consent_current: true,
  });
  const page = { _unloaded: false, _hidden: false };
  let finishFirstLoad;
  let loads = 0;
  const first = careSession.guardHealthSensitivePage(page, () => new Promise((resolve) => {
    loads += 1;
    finishFirstLoad = resolve;
  }));
  await new Promise((resolve) => setImmediate(resolve));
  page._hidden = true;
  finishFirstLoad();
  assert.equal(await first, false);

  page._hidden = false;
  assert.equal(await careSession.guardHealthSensitivePage(page, async () => { loads += 1; }), true);
  assert.equal(loads, 2);
});

test('首次私密加载未结算就隐藏再返回时合并重载且最终标记可用', async () => {
  resetSession('hidden-return-before-settle-token');
  requestImpl = async () => ({
    required_health_consent_version: 'health-v6b',
    health_consent_current: true,
  });
  const page = { _unloaded: false, _hidden: false };
  const firstLoad = deferred();
  let loads = 0;
  const loader = async () => {
    loads += 1;
    if (loads === 1) await firstLoad.promise;
  };

  const firstGuard = careSession.guardHealthSensitivePage(page, loader);
  await new Promise((resolve) => setImmediate(resolve));
  careSession.suspendHealthMutation(page);
  page._hidden = true;
  page._hidden = false;
  const returnGuard = careSession.guardHealthSensitivePage(page, loader);
  firstLoad.resolve();

  assert.deepEqual(await Promise.all([firstGuard, returnGuard]), [true, true]);
  assert.equal(loads, 2);
  assert.equal(page._healthConsentLoadedOnce, true);
  assert.equal(page._healthConsentReloadPending, false);
  assert.equal(await careSession.guardHealthSensitivePage(page, loader), true);
  assert.equal(loads, 2);
});

test('同意状态 GET 期间隐藏，返回后重新读取并解除 loading', async () => {
  resetSession('hidden-consent-get-token');
  const firstGet = deferred();
  let getCount = 0;
  requestImpl = async (options) => {
    assert.equal(options.method, 'GET');
    getCount += 1;
    if (getCount === 1) return firstGet.promise;
    return {
      required_health_consent_version: 'health-v7',
      health_consent_current: false,
    };
  };
  delete require.cache[require.resolve('../pages/health-consent/index')];
  pageDefinition = null;
  require('../pages/health-consent/index');
  const page = makePage(pageDefinition);
  currentPages = [{ route: 'pages/health-consent/index' }];

  const pendingLoad = page.onLoad.call(page, { manage: '1' });
  await Promise.resolve();
  page.onHide.call(page);
  firstGet.resolve({
    required_health_consent_version: 'health-v7',
    health_consent_current: false,
  });
  await pendingLoad;
  assert.equal(page.data.loading, true);

  await page.onShow.call(page);
  assert.equal(getCount, 2);
  assert.equal(page.data.loading, false);
  assert.equal(page.data.requiredVersion, 'health-v7');
});

test('单独同意 POST 期间隐藏，返回后等待写入并重新读取权威状态', async () => {
  resetSession('hidden-consent-post-token');
  const postGate = deferred();
  let getCount = 0;
  requestImpl = async (options) => {
    if (options.method === 'POST') return postGate.promise;
    getCount += 1;
    return {
      required_health_consent_version: 'health-v8',
      health_consent_current: getCount > 1,
    };
  };
  delete require.cache[require.resolve('../pages/health-consent/index')];
  pageDefinition = null;
  require('../pages/health-consent/index');
  const page = makePage(pageDefinition);
  currentPages = [{ route: 'pages/health-consent/index' }];

  await page.onLoad.call(page, { manage: '1' });
  page.onAgreementChange.call(page, { detail: { value: ['health-consent'] } });
  const pendingSubmit = page.submitConsent.call(page);
  await Promise.resolve();
  page.onHide.call(page);
  postGate.resolve({ health_consent_current: true });
  await pendingSubmit;
  assert.equal(page.data.busy, true);

  await page.onShow.call(page);
  assert.equal(getCount, 2);
  assert.equal(page.data.busy, false);
  assert.equal(page.data.loading, false);
  assert.equal(page.data.consentCurrent, true);
});

test('隐藏期间单独同意失败且版本更新，返回后必须重新阅读并勾选', async () => {
  resetSession('hidden-consent-version-token');
  const postGate = deferred();
  let getCount = 0;
  requestImpl = async (options) => {
    if (options.method === 'POST') return postGate.promise;
    getCount += 1;
    return {
      required_health_consent_version: getCount === 1 ? 'health-v1' : 'health-v2',
      health_consent_current: false,
    };
  };
  delete require.cache[require.resolve('../pages/health-consent/index')];
  pageDefinition = null;
  require('../pages/health-consent/index');
  const page = makePage(pageDefinition);
  currentPages = [{ route: 'pages/health-consent/index' }];

  await page.onLoad.call(page, { manage: '1' });
  page.onAgreementChange.call(page, { detail: { value: ['health-consent'] } });
  const pendingSubmit = page.submitConsent.call(page);
  await Promise.resolve();
  page.onHide.call(page);
  postGate.reject(new Error('offline'));
  await pendingSubmit;

  await page.onShow.call(page);
  assert.equal(getCount, 2);
  assert.equal(page.data.requiredVersion, 'health-v2');
  assert.equal(page.data.consentCurrent, false);
  assert.equal(page.data.agreed, false);
  assert.match(page.data.statusHint, /重新阅读并勾选/);
});

test('撤回 DELETE 期间隐藏，返回后确认权威状态并回公开首页', async () => {
  resetSession('hidden-consent-delete-token');
  const deleteGate = deferred();
  let getCount = 0;
  requestImpl = async (options) => {
    if (options.method === 'DELETE') return deleteGate.promise;
    getCount += 1;
    return {
      required_health_consent_version: 'health-v9',
      health_consent_current: getCount === 1,
    };
  };
  delete require.cache[require.resolve('../pages/health-consent/index')];
  pageDefinition = null;
  require('../pages/health-consent/index');
  const page = makePage(pageDefinition);
  currentPages = [{ route: 'pages/health-consent/index' }];

  await page.onLoad.call(page, { manage: '1' });
  page.withdrawConsent.call(page);
  const pendingDelete = modalOptions.success({ confirm: true });
  await Promise.resolve();
  page.onHide.call(page);
  deleteGate.resolve({ health_consent_current: false });
  await pendingDelete;
  assert.equal(page.data.busy, true);

  await page.onShow.call(page);
  assert.equal(getCount, 2);
  assert.equal(page.data.busy, false);
  assert.equal(relaunchedTo, '/pages/home/index');
  assert.equal(storage.get(SESSION_KEY).token, 'hidden-consent-delete-token');
});
