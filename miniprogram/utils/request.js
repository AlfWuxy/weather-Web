const {
  API_BASE_URL,
  REQUEST_TIMEOUT_MS,
  GIS_REQUEST_TIMEOUT_MS,
} = require('../config');

function trimTrailingSlash(value) {
  return String(value || '').replace(/\/+$/, '');
}

function buildBackendUrl(pathOrUrl) {
  const base = trimTrailingSlash(API_BASE_URL);
  if (!base) throw new Error('miniapp_api_base_missing');
  const value = String(pathOrUrl || '').trim();
  if (!value) throw new Error('request_path_missing');
  const fullUrl = /^https:\/\//i.test(value)
    ? value
    : `${base}${value.charAt(0) === '/' ? value : `/${value}`}`;
  const baseHost = base.replace(/^https:\/\//i, '').split('/')[0].toLowerCase();
  const targetHost = fullUrl.replace(/^https:\/\//i, '').split('/')[0].toLowerCase();
  if (!/^https:\/\//i.test(fullUrl) || targetHost !== baseHost) {
    throw new Error('backend_url_not_allowed');
  }
  return fullUrl;
}

function request({ method, path, token, data, timeout }) {
  let url;
  try {
    url = buildBackendUrl(path);
  } catch (error) {
    return Promise.reject(error);
  }
  let requestTask = null;
  const pending = new Promise((resolve, reject) => {
    requestTask = wx.request({
      url,
      method: method || 'GET',
      data: data || undefined,
      timeout: timeout || REQUEST_TIMEOUT_MS,
      header: Object.assign(
        { Accept: 'application/json', 'Content-Type': 'application/json' },
        token ? { Authorization: `Bearer ${token}` } : {}
      ),
      success: (response) => resolve(response),
      fail: (error) => reject(error),
    });
  });
  pending.abort = () => {
    if (requestTask && typeof requestTask.abort === 'function') requestTask.abort();
  };
  return pending;
}

async function api({ method, path, token, data, timeout }) {
  const response = await request({ method, path, token, data, timeout });
  if (response.statusCode < 200 || response.statusCode >= 300) {
    throw createApiError(response);
  }
  const body = response.data || {};
  if (!body.success) {
    throw createApiError(response, 'request_failed');
  }
  return body.data;
}

function createApiError(response, fallbackCode) {
  const statusCode = Number(response && response.statusCode) || 0;
  const body = response && response.data && typeof response.data === 'object' ? response.data : {};
  const code = String(body.error || body.code || fallbackCode || (statusCode === 401 ? 'unauthorized' : `http_${statusCode || 'error'}`));
  const message = String(body.message || code);
  const error = new Error(message);
  error.code = code;
  error.statusCode = statusCode;
  if (body.data !== undefined) error.data = body.data;
  return error;
}

function mapAbortable(pending, transform) {
  const mapped = pending.then(transform);
  mapped.abort = () => {
    if (pending && typeof pending.abort === 'function') pending.abort();
  };
  return mapped;
}

function backendJson(pathOrUrl) {
  const pending = request({
    method: 'GET',
    path: pathOrUrl,
    timeout: GIS_REQUEST_TIMEOUT_MS,
  });
  return mapAbortable(pending, (response) => {
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw new Error(`http_${response.statusCode}`);
    }
    return response.data;
  });
}

module.exports = { api, backendJson, buildBackendUrl, createApiError, mapAbortable, request };
