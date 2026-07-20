const request = require('../../utils/request');
const session = require('../../utils/session');
const { getBootstrap } = require('../../utils/public-data');

let loginNavigationPending = false;
let healthConsentNavigationPending = false;
let healthConsentState = {
  token: '',
  checked: false,
  current: false,
  requiredVersion: '',
  pending: null,
};

function cleanHealthConsentVersion(value) {
  const version = String(value || '').trim();
  return version && version.length <= 64 ? version : '';
}

function extractRequiredHealthConsentVersion(payload, fallback) {
  const source = payload && typeof payload === 'object' ? payload : {};
  const nested = source.data && typeof source.data === 'object' ? source.data : {};
  return cleanHealthConsentVersion(
    source.required_health_consent_version
    || nested.required_health_consent_version
    || fallback
  );
}

function resetHealthConsentCache(token) {
  healthConsentState = {
    token: String(token || '').trim(),
    checked: false,
    current: false,
    requiredVersion: '',
    pending: null,
  };
}

function syncHealthConsentToken(token) {
  const normalized = String(token || '').trim();
  if (healthConsentState.token !== normalized) resetHealthConsentCache(normalized);
  return normalized;
}

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
    return syncHealthConsentToken('');
  }
  if (typeof value === 'string') return syncHealthConsentToken(value);
  if (value && typeof value.token === 'string') return syncHealthConsentToken(value.token);
  return syncHealthConsentToken('');
}

function getMeta() {
  return session.getSessionMeta();
}

function saveToken(token, metadata) {
  const normalized = String(token || '').trim();
  if (!normalized) throw new Error('missing_token');
  if (getToken() !== normalized) resetHealthConsentCache(normalized);
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

function scrubHealthSensitivePages() {
  const pages = typeof getCurrentPages === 'function' ? getCurrentPages() : [];
  pages.forEach((page) => {
    if (!page || typeof page.onHealthConsentRequired !== 'function') return;
    page._healthConsentReloadPending = true;
    try {
      page.onHealthConsentRequired();
    } catch (error) {
      // 单个页面已经卸载时继续清理其他健康敏感页面。
    }
  });
}

function clear() {
  session.clearSession();
  resetHealthConsentCache('');
  healthConsentNavigationPending = false;
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

function isHealthConsentRequired(error) {
  return Number(error && error.statusCode) === 428
    && String(error && error.code || '') === 'health_sensitive_consent_required';
}

function releaseHealthConsentNavigation() {
  healthConsentNavigationPending = false;
}

function goHealthConsent(requiredVersion) {
  const pages = typeof getCurrentPages === 'function' ? getCurrentPages() : [];
  const current = pages.length ? pages[pages.length - 1] : null;
  if ((current && current.route === 'pages/health-consent/index') || healthConsentNavigationPending) return;
  healthConsentNavigationPending = true;
  const version = cleanHealthConsentVersion(requiredVersion);
  const query = version ? `?required_version=${encodeURIComponent(version)}` : '';
  wx.navigateTo({
    url: `/pages/health-consent/index${query}`,
    fail: () => {
      healthConsentNavigationPending = false;
      wx.reLaunch({ url: '/pages/home/index' });
    },
  });
}

function requireHealthConsentFromError(error, token) {
  const activeToken = syncHealthConsentToken(token || getToken());
  if (!activeToken) return false;
  const requiredVersion = extractRequiredHealthConsentVersion(error && error.data, '');
  healthConsentState.checked = true;
  healthConsentState.current = false;
  healthConsentState.requiredVersion = requiredVersion;
  scrubHealthSensitivePages();
  goHealthConsent(requiredVersion);
  return true;
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
    if (isHealthConsentRequired(error)) {
      requireHealthConsentFromError(error, token);
      throw error;
    }
    if (isUnauthorized(error)) {
      clear();
      goLogin();
    }
    throw error;
  }
  if (rejectChangedSession(token)) throw sessionChangedError();
  return data;
}

async function ensureHealthConsent() {
  const token = requireToken();
  if (!token) return false;
  syncHealthConsentToken(token);
  if (healthConsentState.checked && healthConsentState.current) return true;
  if (healthConsentState.pending) return healthConsentState.pending;

  const state = healthConsentState;
  state.pending = (async () => {
    try {
      const data = await request.api({
        method: 'GET',
        path: '/mp/api/v1/health-consent',
        token,
      });
      if (rejectChangedSession(token)) throw sessionChangedError();
      const requiredVersion = extractRequiredHealthConsentVersion(data, state.requiredVersion);
      const current = data && data.health_consent_current === true;
      state.checked = true;
      state.current = current;
      state.requiredVersion = requiredVersion;
      if (current) return true;
      scrubHealthSensitivePages();
      goHealthConsent(requiredVersion);
      return false;
    } catch (error) {
      if (rejectChangedSession(token)) throw sessionChangedError();
      if (isHealthConsentRequired(error)) {
        requireHealthConsentFromError(error, token);
        return false;
      }
      if (isUnauthorized(error)) {
        clear();
        goLogin();
      }
      throw error;
    } finally {
      if (healthConsentState === state) state.pending = null;
    }
  })();
  return state.pending;
}

function markHealthConsentCurrent(version) {
  const token = getToken();
  if (!token) return;
  syncHealthConsentToken(token);
  healthConsentState.checked = true;
  healthConsentState.current = true;
  healthConsentState.requiredVersion = cleanHealthConsentVersion(version)
    || healthConsentState.requiredVersion;
}

function invalidateHealthConsent() {
  const token = getToken();
  resetHealthConsentCache(token);
  healthConsentNavigationPending = false;
  scrubHealthSensitivePages();
}

function trackHealthMutation(page, promise, kind, meta) {
  const pending = Promise.resolve(promise);
  if (page) {
    page._healthMutationPromise = pending;
    page._healthMutationKind = String(kind || '');
    page._healthMutationMeta = meta && typeof meta === 'object' ? meta : null;
  }
  return pending;
}

function suspendHealthMutation(page) {
  if (!page) return false;
  // 每次隐藏都推进可见性世代。读取尚未结束时，旧 guard 返回后必须重新加载。
  page._healthConsentVisibilityGeneration = Number(page._healthConsentVisibilityGeneration || 0) + 1;
  if (page._healthConsentGuardPromise) page._healthConsentReloadPending = true;
  if (!page._healthMutationPromise) return false;
  page._healthMutationResumePromise = page._healthMutationPromise;
  page._healthMutationResumeKind = String(page._healthMutationKind || '');
  page._healthMutationResumeMeta = page._healthMutationMeta && typeof page._healthMutationMeta === 'object'
    ? page._healthMutationMeta
    : null;
  page._healthConsentReloadPending = true;
  return true;
}

function finishHealthMutation(page, promise) {
  if (page && page._healthMutationPromise === promise) {
    page._healthMutationPromise = null;
    page._healthMutationKind = '';
    page._healthMutationMeta = null;
  }
}

async function resumeHealthMutation(page) {
  const pending = page && page._healthMutationResumePromise;
  if (!pending) {
    return {
      resumed: false,
      ok: false,
      value: null,
      error: null,
      kind: '',
      meta: null,
    };
  }
  const kind = String(page._healthMutationResumeKind || '');
  const meta = page._healthMutationResumeMeta && typeof page._healthMutationResumeMeta === 'object'
    ? page._healthMutationResumeMeta
    : null;
  page._healthMutationResumePromise = null;
  page._healthMutationResumeKind = '';
  page._healthMutationResumeMeta = null;
  try {
    const value = await pending;
    return { resumed: true, ok: true, value, error: null, kind, meta };
  } catch (error) {
    return { resumed: true, ok: false, value: null, error, kind, meta };
  } finally {
    finishHealthMutation(page, pending);
  }
}

async function guardHealthSensitivePage(page, loader) {
  if (!page || page._unloaded === true || page._hidden === true) return false;
  if (page._healthConsentGuardPromise) {
    const existing = page._healthConsentGuardPromise;
    const result = await existing;
    if (
      !result
      && page._unloaded !== true
      && page._hidden !== true
      && page._healthConsentReloadPending === true
      && healthConsentState.current === true
    ) {
      // 已完成的旧 promise 可以安全让位；并发调用仍会在下一轮 guard 上合并。
      if (page._healthConsentGuardPromise === existing) page._healthConsentGuardPromise = null;
      return guardHealthSensitivePage(page, loader);
    }
    return result;
  }
  const visibilityGeneration = Number(page._healthConsentVisibilityGeneration || 0);
  const guard = (async () => {
    let allowed = false;
    try {
      allowed = await ensureHealthConsent();
    } catch (error) {
      if (page._unloaded !== true && page._hidden !== true && !isUnauthorized(error)) {
        wx.showToast({ title: '健康资料授权状态核验失败', icon: 'none' });
      }
      return false;
    }
    if (
      !allowed
      || page._unloaded === true
      || page._hidden === true
      || Number(page._healthConsentVisibilityGeneration || 0) !== visibilityGeneration
    ) {
      page._healthConsentReloadPending = true;
      return false;
    }
    const token = getToken();
    const shouldLoad = page._healthConsentLoadedToken !== token
      || page._healthConsentLoadedOnce !== true
      || page._healthConsentReloadPending === true;
    if (!shouldLoad) return true;
    page._healthConsentLoadedToken = token;
    page._healthConsentLoadedOnce = false;
    page._healthConsentReloadPending = false;
    await loader();
    if (
      page._unloaded === true
      || page._hidden === true
      || page._healthConsentReloadPending === true
      || Number(page._healthConsentVisibilityGeneration || 0) !== visibilityGeneration
    ) {
      page._healthConsentReloadPending = true;
      return false;
    }
    page._healthConsentLoadedOnce = true;
    return true;
  })();
  page._healthConsentGuardPromise = guard;
  let result = false;
  try {
    result = await guard;
  } finally {
    if (page._healthConsentGuardPromise === guard) page._healthConsentGuardPromise = null;
  }
  if (
    !result
    && page._unloaded !== true
    && page._hidden !== true
    && page._healthConsentReloadPending === true
    && healthConsentState.current === true
  ) {
    return guardHealthSensitivePage(page, loader);
  }
  return result;
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
  ensureHealthConsent,
  extractRequiredHealthConsentVersion,
  extractRequiredPrivacyVersion,
  extractAuthToken,
  getSnapshot,
  getMeta,
  getToken,
  goLogin,
  guardHealthSensitivePage,
  finishHealthMutation,
  invalidateHealthConsent,
  isHealthConsentRequired,
  isUnauthorized,
  markHealthConsentCurrent,
  publicApi,
  releaseHealthConsentNavigation,
  resumeHealthMutation,
  requireToken,
  saveToken,
  scrubPrivatePages,
  suspendHealthMutation,
  tokenApi,
  trackHealthMutation,
};
