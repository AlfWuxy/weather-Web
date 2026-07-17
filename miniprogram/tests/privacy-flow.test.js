const test = require('node:test');
const assert = require('node:assert/strict');

const storage = new Map();
let pageDefinition;
let publicApiImpl;

global.getApp = () => ({ globalData: {} });
global.Page = (definition) => { pageDefinition = definition; };
global.wx = {
  getStorageSync: (key) => storage.get(key),
  setStorageSync: (key, value) => storage.set(key, value),
  removeStorageSync: (key) => storage.delete(key),
};

const careSession = require('../pages/elders/care-session');
careSession.publicApi = (options) => publicApiImpl(options);
require('../pages/bind-token/index');

function makePage(overrides) {
  const page = Object.assign({}, pageDefinition);
  page.data = Object.assign({}, pageDefinition.data, overrides || {});
  page.setData = (next) => Object.assign(page.data, next);
  return page;
}

test('隐私版本兼容 bootstrap 与 428 的常见字段', () => {
  assert.equal(careSession.extractRequiredPrivacyVersion({
    required_privacy_consent_version: 'bootstrap-v2',
    privacy_consent_version: 'old-v1',
  }, 'bundle-v1'), 'bootstrap-v2');
  assert.equal(careSession.extractRequiredPrivacyVersion({
    required_version: 'response-v3',
  }, 'bundle-v1'), 'response-v3');
  assert.equal(careSession.extractRequiredPrivacyVersion({
    privacy: { required_version: 'privacy-v4' },
  }, 'bundle-v1'), 'privacy-v4');
  assert.equal(careSession.extractRequiredPrivacyVersion({}, 'bundle-v1'), 'bundle-v1');
});

test('微信登录提交页面当前要求的隐私版本', async () => {
  storage.clear();
  storage.set('yl_acquisition_source_v1', {
    source: 'family_share',
    expires_at: Date.now() + 60_000,
  });
  let submitted;
  publicApiImpl = async (options) => {
    submitted = options;
    return { session_token: 'session-token', expires_in: 3600 };
  };
  global.wx.login = ({ success }) => success({ code: 'wechat-code' });
  global.wx.showToast = () => {};
  global.wx.switchTab = () => {};

  const page = makePage({ privacyAgreed: true, requiredPrivacyVersion: 'server-v2' });
  await page.onWechatLogin.call(page);

  assert.equal(submitted.data.privacy_consent_version, 'server-v2');
  assert.equal(submitted.data.acquisition_source, 'family_share');
  assert.equal(storage.get('yl_session_v1').meta.privacy_consent_version, 'server-v2');
  assert.equal(storage.has('yl_acquisition_source_v1'), false);
});

test('未同意协议时同时提供短 Toast 与读屏可见提示', () => {
  let toast;
  global.wx.showToast = (options) => { toast = options; };
  const page = makePage({ privacyAgreed: false, loginHint: '' });

  assert.equal(page.requireConsent.call(page), false);
  assert.equal(toast.title, '请先同意协议');
  assert.match(page.data.loginHint, /隐私说明.*用户协议/);
});

test('登录请求完成前页面卸载时不再写入会话或跳转', async () => {
  storage.clear();
  let resolveLogin;
  publicApiImpl = () => new Promise((resolve) => { resolveLogin = resolve; });
  global.wx.login = ({ success }) => success({ code: 'wechat-code' });
  global.wx.showToast = () => {};
  let switched = false;
  global.wx.switchTab = () => { switched = true; };

  const page = makePage({ privacyAgreed: true, requiredPrivacyVersion: 'server-v2' });
  page._unloaded = false;
  const pending = page.onWechatLogin.call(page);
  await new Promise((resolve) => setImmediate(resolve));
  page.onUnload.call(page);
  resolveLogin({ session_token: 'late-session-token' });
  await pending;

  assert.equal(storage.has('yl_session_v1'), false);
  assert.equal(switched, false);
});

test('页面卸载后到达的 428 不再触发隐私版本重验或弹窗', async () => {
  let rejectLogin;
  let refreshCalls = 0;
  let modalCalls = 0;
  publicApiImpl = () => new Promise((resolve, reject) => { rejectLogin = reject; });
  global.wx.login = ({ success }) => success({ code: 'wechat-code' });
  global.wx.showModal = () => { modalCalls += 1; };

  const page = makePage({ privacyAgreed: true, requiredPrivacyVersion: 'server-v2' });
  page._unloaded = false;
  page.loadRequiredPrivacyVersion = async () => {
    refreshCalls += 1;
    return 'server-v3';
  };
  const pending = page.onWechatLogin.call(page);
  await new Promise((resolve) => setImmediate(resolve));
  page.onUnload.call(page);
  const error = new Error('privacy_consent_required');
  error.statusCode = 428;
  rejectLogin(error);
  await pending;

  assert.equal(refreshCalls, 0);
  assert.equal(modalCalls, 0);
});

test('428 返回新版本后重置勾选并用于下一次提交', async () => {
  storage.clear();
  const submittedVersions = [];
  publicApiImpl = async (options) => {
    submittedVersions.push(options.data.privacy_consent_version);
    if (submittedVersions.length === 1) {
      const error = new Error('privacy_consent_required');
      error.statusCode = 428;
      error.data = { required_version: 'server-v3' };
      throw error;
    }
    return { session_token: 'refreshed-session-token' };
  };
  global.wx.login = ({ success }) => success({ code: 'wechat-code' });
  global.wx.openPrivacyContract = () => {};
  global.wx.showToast = () => {};
  global.wx.showModal = ({ success }) => success({ confirm: true });
  global.wx.switchTab = () => {};

  const page = makePage({ privacyAgreed: true, requiredPrivacyVersion: 'server-v2' });
  await page.onWechatLogin.call(page);

  assert.equal(page.data.privacyAgreed, false);
  assert.equal(page.data.requiredPrivacyVersion, 'server-v3');
  assert.match(page.data.loginHint, /版本已更新/);

  page.data.privacyAgreed = true;
  await page.onWechatLogin.call(page);
  assert.deepEqual(submittedVersions, ['server-v2', 'server-v3']);
  assert.equal(storage.get('yl_session_v1').meta.privacy_consent_version, 'server-v3');
});

test('登录页使用 navigateTo 保留原 Tab 页面栈', () => {
  let navigation;
  let switchTabCalled = false;
  global.getCurrentPages = () => [{ route: 'pages/home/index' }];
  global.wx.navigateTo = (options) => { navigation = options; };
  global.wx.switchTab = () => { switchTabCalled = true; };
  global.wx.reLaunch = () => { throw new Error('不应清空页面栈'); };

  careSession.goLogin();
  assert.equal(navigation.url, '/pages/bind-token/index');
  assert.equal(switchTabCalled, false);
  navigation.complete();
});

test('平台隐私协议不可用时打开完整本地隐私页和用户协议', () => {
  const navigations = [];
  global.wx.navigateTo = ({ url }) => navigations.push(url);
  const page = makePage();

  page.showLocalPrivacy.call(page);
  page.openAgreement.call(page);

  assert.deepEqual(navigations, ['/pages/privacy/index', '/pages/agreement/index']);
});

test('页面卸载后平台隐私协议的延迟失败不再打开新页', () => {
  let failCallback;
  let navigations = 0;
  global.wx.openPrivacyContract = ({ fail }) => { failCallback = fail; };
  global.wx.navigateTo = () => { navigations += 1; };
  const page = makePage();
  page._unloaded = false;

  page.openPrivacy.call(page);
  page.onUnload.call(page);
  failCallback();

  assert.equal(navigations, 0);
});

test('登录协议控件提供可读名称与实时状态区', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const view = fs.readFileSync(path.join(__dirname, '..', 'pages/bind-token/index.wxml'), 'utf8');
  assert.match(view, /勾选表示已阅读并同意隐私说明和用户协议/);
  assert.match(view, /aria-label="阅读隐私说明"/);
  assert.match(view, /aria-label="阅读用户协议"/);
  assert.match(view, /class="status-hint"[^>]*aria-live="polite"/);
});
