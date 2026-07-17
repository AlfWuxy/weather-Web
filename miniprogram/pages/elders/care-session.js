const request = require('../../utils/request');
const session = require('../../utils/session');
const { getBootstrap } = require('../../utils/public-data');

let loginNavigationPending = false;

function cleanPrivacyVersion(value) {
  const version = String(value || '').trim();
  return version && version.length <= 64 ? version : '';
}

function extractRequiredPrivacyVersion(payload, fallback) {
  const source = payload && typeof payload === 'object' ? payload : {};
  const privacy = source.privacy && typeof source.privacy === 'object' ? source.privacy : {};
  const auth = source.auth && typeof source.auth === 'object' ? source.auth : {};
  const nested = source.data && typeof source.data === 'object' ? source.data : {};
  const candidates = [
    source.required_privacy_consent_version,
    source.required_version,
    source.privacy_consent_version,
    privacy.required_privacy_consent_version,
    privacy.required_version,
    privacy.requiredVersion,
    privacy.version,
    auth.required_privacy_consent_version,
    auth.required_version,
    nested.required_privacy_consent_version,
    nested.required_version,
    nested.privacy_consent_version,
    fallback,
  ];
  for (let index = 0; index < candidates.length; index += 1) {
    const version = cleanPrivacyVersion(candidates[index]);
    if (version) return version;
  }
  return '';
}

function getToken() {
  const value = session.getSessionToken();
  const meta = session.getSessionMeta();
  if (meta.migratedFrom && !meta.privacy_consent_version) {
    // 旧存储没有可验证的隐私同意记录，必须回到登录页让用户主动确认。
    session.clearSession();
    return '';
  }
  if (typeof value === 'string') return value.trim();
  if (value && typeof value.token === 'string') return value.token.trim();
  return '';
}

function getMeta() {
  return session.getSessionMeta();
}

function saveToken(token, metadata) {
  const normalized = String(token || '').trim();
  if (!normalized) throw new Error('missing_token');
  session.setSessionToken(normalized, metadata || {});
}

function clear() {
  session.clearSession();
}

function goLogin() {
  const pages = typeof getCurrentPages === 'function' ? getCurrentPages() : [];
  const current = pages.length ? pages[pages.length - 1] : null;
  if ((current && current.route === 'pages/bind-token/index') || loginNavigationPending) return;
  loginNavigationPending = true;
  // navigateTo 保留原来的 Tab 根页面，游客可以随时回到公共首页。
  wx.navigateTo({
    url: '/pages/bind-token/index',
    fail: () => {
      wx.switchTab({ url: '/pages/settings/index' });
    },
    complete: () => {
      loginNavigationPending = false;
    },
  });
}

function requireToken() {
  const token = getToken();
  if (!token) goLogin();
  return token;
}

function isUnauthorized(error) {
  const message = String(error && (error.code || error.message) || error || '').toLowerCase();
  return message.includes('unauthorized') || message.includes('401') || message.includes('session_expired');
}

async function authApi(options) {
  const token = requireToken();
  if (!token) throw new Error('missing_session');
  try {
    return await request.api(Object.assign({}, options, { token }));
  } catch (error) {
    if (isUnauthorized(error)) {
      clear();
      goLogin();
    }
    throw error;
  }
}

function publicApi(options) {
  return request.api(Object.assign({}, options, { token: '' }));
}

function tokenApi(token, options) {
  return request.api(Object.assign({}, options, { token: String(token || '').trim() }));
}

function getSnapshot(options) {
  return getBootstrap(options).then((result) => (result && result.data !== undefined ? result.data : result));
}

function extractAuthToken(data) {
  if (!data || typeof data !== 'object') return '';
  return String(data.session_token || data.token || data.api_token || data.access_token || '').trim();
}

module.exports = {
  authApi,
  clear,
  extractRequiredPrivacyVersion,
  extractAuthToken,
  getSnapshot,
  getMeta,
  getToken,
  goLogin,
  publicApi,
  requireToken,
  saveToken,
  tokenApi,
};
