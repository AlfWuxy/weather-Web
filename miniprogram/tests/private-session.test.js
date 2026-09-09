const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const storage = new Map();
let requestImpl = async () => ({});
let currentPages = [];
let relaunchedTo = '';

global.getApp = () => ({ globalData: {} });
global.getCurrentPages = () => currentPages;
global.wx = {
  getStorageSync: (key) => storage.get(key),
  setStorageSync: (key, value) => storage.set(key, value),
  removeStorageSync: (key) => storage.delete(key),
  reLaunch: (options) => {
    relaunchedTo = options.url;
    if (typeof options.complete === 'function') options.complete();
  },
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

function privatePage() {
  return {
    scrubCount: 0,
    onSessionInvalidated() {
      this.scrubCount += 1;
    },
  };
}

test('401 和常见失效代码都被识别为未授权', () => {
  assert.equal(careSession.isUnauthorized({ statusCode: 401, code: 'custom_error' }), true);
  assert.equal(careSession.isUnauthorized({ code: 'invalid_token' }), true);
  assert.equal(careSession.isUnauthorized(new Error('session_expired')), true);
  assert.equal(careSession.isUnauthorized({ statusCode: 503, code: 'offline' }), false);
});

test('缺少会话时先清空私人页面再重启登录页', () => {
  storage.clear();
  relaunchedTo = '';
  const page = privatePage();
  currentPages = [page];

  assert.equal(careSession.requireToken(), '');
  assert.equal(page.scrubCount, 1);
  assert.equal(relaunchedTo, '/pages/bind-token/index');
});

test('请求返回 401 时清除令牌和页面内存数据', async () => {
  storage.clear();
  relaunchedTo = '';
  careSession.saveToken('session-a', {
    login_method: 'wechat',
    privacy_consent_version: 'privacy-v1',
  });
  const page = privatePage();
  currentPages = [page];
  requestImpl = async () => {
    const error = new Error('token rejected');
    error.statusCode = 401;
    error.code = 'invalid_token';
    throw error;
  };

  await assert.rejects(
    careSession.authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
    /token rejected/
  );

  assert.equal(storage.has(SESSION_KEY), false);
  assert.equal(page.scrubCount, 1);
  assert.equal(relaunchedTo, '/pages/bind-token/index');
});

test('旧会话延迟响应不能写回新账号', async () => {
  storage.clear();
  relaunchedTo = '';
  careSession.saveToken('session-a', {
    login_method: 'wechat',
    privacy_consent_version: 'privacy-v1',
  });
  const page = privatePage();
  currentPages = [page];
  let resolveRequest;
  requestImpl = () => new Promise((resolve) => { resolveRequest = resolve; });

  const pending = careSession.authApi({ method: 'GET', path: '/mp/api/v1/elders' });
  careSession.saveToken('session-b', {
    login_method: 'wechat',
    privacy_consent_version: 'privacy-v1',
  });
  resolveRequest({ items: [{ pair_id: 7, member: { name: '账号 A 家人' } }] });

  await assert.rejects(pending, /session_changed/);
  assert.equal(storage.get(SESSION_KEY).token, 'session-b');
  assert.equal(page.scrubCount, 1);
  assert.equal(relaunchedTo, '');
});

test('旧会话延迟 401 不会清除新账号', async () => {
  storage.clear();
  relaunchedTo = '';
  careSession.saveToken('session-a', {
    login_method: 'wechat',
    privacy_consent_version: 'privacy-v1',
  });
  const page = privatePage();
  currentPages = [page];
  let rejectRequest;
  requestImpl = () => new Promise((resolve, reject) => { rejectRequest = reject; });

  const pending = careSession.authApi({ method: 'GET', path: '/mp/api/v1/elders' });
  careSession.saveToken('session-b', {
    login_method: 'wechat',
    privacy_consent_version: 'privacy-v1',
  });
  const oldError = new Error('token rejected');
  oldError.statusCode = 401;
  oldError.code = 'invalid_token';
  rejectRequest(oldError);

  await assert.rejects(pending, /session_changed/);
  assert.equal(storage.get(SESSION_KEY).token, 'session-b');
  assert.equal(page.scrubCount, 1);
  assert.equal(relaunchedTo, '');
});

test('全部私人页面都提供返回校验和内存清理入口', () => {
  const pages = [
    'account',
    'action-checkin',
    'diary',
    'elder-edit',
    'elders',
    'health-assessment',
    'medications',
    'settings',
    'template',
  ];
  pages.forEach((name) => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'pages', name, 'index.js'), 'utf8');
    assert.match(source, /(?:async\s+)?onShow\(\)[\s\S]{0,220}requireToken\(\)|(?:async\s+)?onShow\(\)[\s\S]{0,220}getToken\(\)/, `${name} 返回页面时应重新核验会话`);
    assert.match(source, /onSessionInvalidated\(\)/, `${name} 应提供私人数据清理入口`);
  });
});

test('1.1 登录页只保留微信登录并提供重试和公共天气回退', () => {
  const loginScript = fs.readFileSync(
    path.join(__dirname, '..', 'pages', 'bind-token', 'index.js'),
    'utf8'
  );
  const loginView = fs.readFileSync(
    path.join(__dirname, '..', 'pages', 'bind-token', 'index.wxml'),
    'utf8'
  );
  const settingsView = fs.readFileSync(
    path.join(__dirname, '..', 'pages', 'settings', 'index.wxml'),
    'utf8'
  );

  assert.match(loginScript, /wx\.login\(/);
  assert.match(loginScript, /loginFailed:\s*true/);
  assert.match(loginScript, /检查网络后重试/);
  assert.match(loginView, /重新微信登录/);
  assert.match(loginView, /bindtap="goPublicHome"[\s\S]*先查看公共天气/);

  assert.doesNotMatch(loginScript, /\btokenApi\b|\bonBind\s*\(|legacy_token|tokenInput|showTokenFallback/);
  assert.doesNotMatch(loginView, /备用登录码|password="true"|bindtap="(?:onBind|toggleTokenFallback|onClear)"/);
  assert.doesNotMatch(settingsView, /备用登录码/);
});

test('微信登录失败后显示重试状态并保留公共天气回退提示', async () => {
  const loginPagePath = require.resolve('../pages/bind-token/index');
  const previousPage = global.Page;
  const previousLogin = global.wx.login;
  const previousShowToast = global.wx.showToast;
  let loginPageDefinition;
  let toast;

  try {
    global.Page = (definition) => { loginPageDefinition = definition; };
    global.wx.login = ({ fail }) => fail(new Error('wechat unavailable'));
    global.wx.showToast = (options) => { toast = options; };
    delete require.cache[loginPagePath];
    require(loginPagePath);

    const page = Object.assign({}, loginPageDefinition);
    page.data = Object.assign({}, loginPageDefinition.data, { privacyAgreed: true });
    page.setData = (next) => Object.assign(page.data, next);
    page._unloaded = false;

    await page.onWechatLogin.call(page);

    assert.equal(page.data.busy, false);
    assert.equal(page.data.loginFailed, true);
    assert.match(page.data.loginHint, /检查网络后重试/);
    assert.match(page.data.loginHint, /公共天气和预警/);
    assert.equal(toast.title, '登录失败，请重试');
  } finally {
    delete require.cache[loginPagePath];
    global.Page = previousPage;
    global.wx.login = previousLogin;
    global.wx.showToast = previousShowToast;
  }
});
