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
  setSessionToken('session-secret', { expiresAt: Date.now() - 1000 });
  assert.equal(getSessionToken(), '');
  assert.equal(storage.has(SESSION_KEY), false);
});

test('旧 api_token 首次读取时迁移', () => {
  storage.clear();
  storage.set('api_token', ' legacy-token ');
  assert.equal(getSessionToken(), 'legacy-token');
  assert.equal(storage.get(SESSION_KEY).token, 'legacy-token');
  assert.equal(storage.has('api_token'), false);
});

test('秒级 expires_at 可正常识别', () => {
  storage.clear();
  setSessionToken('active-token', { expires_at: Math.floor(Date.now() / 1000) + 60 });
  assert.equal(getSessionToken(), 'active-token');
});
