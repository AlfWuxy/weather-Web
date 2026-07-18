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
  const nestedPrivacy = nested.privacy && typeof nested.privacy === 'object' ? nested.privacy : {};
  const nestedAuth = nested.auth && typeof nested.auth === 'object' ? nested.auth : {};
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
    nestedPrivacy.required_privacy_consent_version,
    nestedPrivacy.required_version,
    nestedPrivacy.requiredVersion,
    nestedPrivacy.version,
    nestedAuth.required_privacy_consent_version,
    nestedAuth.required_version,
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

function scrubPrivatePages() {
  const pages = typeof getCurrentPages === 'function' ? getCurrentPages() : [];
  pages.forEach((page) => {
    if (!page || typeof page.onSessionInvalidated !== 'function') return;
    try {
      page.onSessionInvalidated();
    } catch (error) {
      // 单个页面已经卸载时继续清理其他私人页面。
    }
  });
}

function clear() {
  session.clearSession();
  scrubPrivatePages();
}

function goLogin() {
  const pages = typeof getCurrentPages === 'function' ? getCurrentPages() : [];
  const current = pages.length ? pages[pages.length - 1] : null;
  if ((current && current.route === 'pages/bind-token/index') || loginNavigationPending) return;
  loginNavigationPending = true;
  // 会话失效时销毁私人页面栈，防止共享设备返回后继续看到上一账号资料。
  wx.reLaunch({
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
  if (!token) {
    scrubPrivatePages();
    goLogin();
  }
  return token;
}

function isUnauthorized(error) {
  const statusCode = Number(error && (error.statusCode || error.status_code || error.status));
  const message = String(error && (error.code || error.message) || error || '').toLowerCase();
  return statusCode === 401
    || message.includes('unauthorized')
    || message.includes('invalid_token')
    || message.includes('missing_session')
    || message.includes('401')
    || message.includes('session_expired');
}

function sessionChangedError() {
  const error = new Error('session_changed');
  error.code = 'session_changed';
  return error;
}

function rejectChangedSession(token) {
  const activeToken = getToken();
  if (activeToken === token) return false;
  // 旧会话的延迟结果不得影响新账号；退出后返回的结果也只负责清理旧页面。
  scrubPrivatePages();
  if (!activeToken) goLogin();
  return true;
}

async function authApi(options) {
  const token = requireToken();
  if (!token) throw new Error('missing_session');
  let data;
  try {
    data = await request.api(Object.assign({}, options, { token }));
  } catch (error) {
    if (rejectChangedSession(token)) throw sessionChangedError();
    if (isUnauthorized(error)) {
      clear();
      goLogin();
    }
    throw error;
  }
  if (rejectChangedSession(token)) throw sessionChangedError();
  return data;
}

function publicApi(options) {
  return request.api(Object.assign({}, options, { token: '' }));
}

function tokenApi(token, options) {
  return request.api(Object.assign({}, options, { token: String(token || '').trim() }));
}

function getSnapshot(options) {
  // 保留公共缓存的 stale、source 和更新时间元数据，由照护页决定可见状态。
  return getBootstrap(options);
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
  isUnauthorized,
  publicApi,
  requireToken,
  saveToken,
  scrubPrivatePages,
  tokenApi,
};
