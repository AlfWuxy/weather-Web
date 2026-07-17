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
  assert.equal(storage.get('yl_session_v1').meta.privacy_consent_version, 'server-v2');
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
