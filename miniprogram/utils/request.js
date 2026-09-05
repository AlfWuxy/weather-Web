const { API_BASE_URL } = require('../config');

const BIND_TOKEN_ROUTE = 'pages/bind-token/index';

function createApiError(code, kind, statusCode, detail) {
  const error = new Error(code);
  error.code = code;
  error.kind = kind;
  error.statusCode = statusCode || 0;
  error.detail = detail || '';
  return error;
}

function currentRoute() {
  if (typeof getCurrentPages !== 'function') return '';
  const pages = getCurrentPages();
  const current = pages && pages.length ? pages[pages.length - 1] : null;
  return current && current.route ? current.route : '';
}

function clearTokenAndRebind() {
  wx.removeStorageSync('api_token');
  // 绑定页自己展示 Token 错误，其他页面统一回到绑定入口。
  if (currentRoute() === BIND_TOKEN_ROUTE) return;
  wx.reLaunch({ url: `/${BIND_TOKEN_ROUTE}` });
}

function normalizeRequestFailure(error) {
  const detail = String((error && error.errMsg) || (error && error.message) || '');
  if (/domain list|合法域名|invalid url/i.test(detail)) {
    return createApiError('request_domain_not_configured', 'config', 0, detail);
  }
  return createApiError('network_error', 'network', 0, detail);
}

function request({ method, path, token, data }) {
  return new Promise((resolve, reject) => {
    if (!API_BASE_URL) {
      reject(createApiError('miniapp_api_base_missing', 'config'));
      return;
    }
    wx.request({
      url: `${API_BASE_URL}${path}`,
      method: method || 'GET',
      data: data || undefined,
      header: Object.assign(
        { 'Content-Type': 'application/json' },
        token ? { Authorization: `Bearer ${token}` } : {}
      ),
      success: (res) => resolve(res),
      fail: (err) => reject(normalizeRequestFailure(err)),
    });
  });
}

async function api({ method, path, token, data }) {
  const res = await request({ method, path, token, data });
  if (res.statusCode === 401) {
    clearTokenAndRebind();
    throw createApiError('unauthorized', 'token', 401);
  }
  const body = res.data && typeof res.data === 'object' && !Array.isArray(res.data)
    ? res.data : {};
  if (res.statusCode >= 500) {
    throw createApiError('service_unavailable', 'service', res.statusCode);
  }
  if (res.statusCode < 200 || res.statusCode >= 300) {
    const code = body.error || body.message || 'service_request_failed';
    throw createApiError(code, 'service', res.statusCode);
  }
  if (!body.success) {
    const code = body.error || body.message || 'invalid_service_response';
    throw createApiError(code, 'service', res.statusCode);
  }
  return body.data;
}

function isUnauthorizedError(error) {
  return !!error && error.code === 'unauthorized';
}

module.exports = { api, isUnauthorizedError };
