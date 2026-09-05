const test = require('node:test');
const assert = require('node:assert/strict');

const storage = new Map();
global.wx = {
  getStorageSync: (key) => storage.get(key),
  setStorageSync: (key, value) => storage.set(key, value),
  removeStorageSync: (key) => storage.delete(key),
};
global.getApp = () => ({ globalData: {} });

const {
  SESSION_KEY,
  getSessionToken,
  setSessionToken,
} = require('../utils/session');

test('会话过期后立即清除', () => {
  storage.clear();
  setSessionToken('session-secret', { login_method: 'wechat', expiresAt: Date.now() - 1000 });
  assert.equal(getSessionToken(), '');
  assert.equal(storage.has(SESSION_KEY), false);
});

test('旧 api_token 会迁移为 web_token 会话', () => {
  storage.clear();
  storage.set('api_token', ' legacy-token ');
  assert.equal(getSessionToken(), 'legacy-token');
  assert.equal(storage.get(SESSION_KEY).meta.login_method, 'web_token');
  assert.equal(storage.has('api_token'), false);
});

test('网页令牌会话可被接受', () => {
  storage.clear();
  setSessionToken('web-session', { login_method: 'web_token' });
  assert.equal(getSessionToken(), 'web-session');
});

test('缺少登录来源的旧会话按不可信身份清理', () => {
  storage.clear();
  storage.set(SESSION_KEY, {
    schema: 1,
    token: 'unknown-session',
    meta: { privacy_consent_version: '2026-07-18' },
    storedAt: Date.now(),
  });
  assert.equal(getSessionToken(), '');
  assert.equal(storage.has(SESSION_KEY), false);
});

test('秒级 expires_at 可正常识别', () => {
  storage.clear();
  setSessionToken('active-token', {
    login_method: 'wechat',
    expires_at: Math.floor(Date.now() / 1000) + 60,
  });
  assert.equal(getSessionToken(), 'active-token');
});
