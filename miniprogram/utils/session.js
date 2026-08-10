const SESSION_KEY = 'yl_session_v1';
const LEGACY_TOKEN_KEY = 'api_token';

function normalizeToken(token) {
  return typeof token === 'string' ? token.trim() : '';
}

function setSessionToken(token, meta) {
  const normalized = normalizeToken(token);
  if (!normalized) {
    clearSession();
    return '';
  }
  const session = {
    schema: 1,
    token: normalized,
    meta: meta && typeof meta === 'object' ? meta : {},
    storedAt: Date.now(),
  };
  wx.setStorageSync(SESSION_KEY, session);
  try {
    wx.removeStorageSync(LEGACY_TOKEN_KEY);
  } catch (error) {
    console.warn('旧令牌清理失败', error);
  }
  try {
    getApp().globalData.apiToken = normalized;
  } catch (error) {
    // 单元测试和独立工具环境没有 App 实例。
  }
  return normalized;
}

function getSessionToken() {
  let session = null;
  try {
    session = wx.getStorageSync(SESSION_KEY);
  } catch (error) {
    session = null;
  }
  const stored = normalizeToken(session && session.token);
  if (stored) {
    const meta = session && session.meta && typeof session.meta === 'object' ? session.meta : {};
    if (meta.login_method !== 'wechat') {
      // 1.1 只接受微信登录会话，清理旧体验版遗留的手动 Web Token。
      clearSession();
      return '';
    }
    const expiryValue = meta.expiresAt || meta.expires_at;
    if (expiryValue) {
      let expiresAt = Number(expiryValue);
      if (Number.isFinite(expiresAt) && expiresAt > 0 && expiresAt < 100000000000) expiresAt *= 1000;
      if (!Number.isFinite(expiresAt)) expiresAt = Date.parse(String(expiryValue));
      if (Number.isFinite(expiresAt) && Date.now() >= expiresAt) {
        clearSession();
        return '';
      }
    }
    return stored;
  }

  // 旧体验版可能遗留明文 Web Token；1.1 不再把它迁移为小程序身份。
  let legacy = '';
  try {
    legacy = normalizeToken(wx.getStorageSync(LEGACY_TOKEN_KEY));
  } catch (error) {
    legacy = '';
  }
  if (legacy) clearSession();
  return '';
}

function getSessionMeta() {
  try {
    const session = wx.getStorageSync(SESSION_KEY);
    return session && session.meta && typeof session.meta === 'object' ? session.meta : {};
  } catch (error) {
    return {};
  }
}

function clearSession() {
  try {
    wx.removeStorageSync(SESSION_KEY);
    wx.removeStorageSync(LEGACY_TOKEN_KEY);
  } catch (error) {
    console.warn('会话清理失败', error);
  }
  try {
    getApp().globalData.apiToken = null;
  } catch (error) {
    // 单元测试和独立工具环境没有 App 实例。
  }
}

module.exports = {
  SESSION_KEY,
  clearSession,
  getSessionMeta,
  getSessionToken,
  setSessionToken,
};
