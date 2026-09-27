const { API_BASE_URL } = require('../config');

const TOKEN_KEY = 'api_token';
const BIND_URL = '/pages/bind-token/index';

function apiError(code, message, details) {
  const err = new Error(message || code);
  err.code = code;
  if (details) {
    if (details.statusCode != null) err.statusCode = details.statusCode;
    if (details.raw !== undefined) err.raw = details.raw;
  }
  return err;
}

function isApiConfigured() {
  return !!(API_BASE_URL || '').trim();
}

function getToken() {
  return (wx.getStorageSync(TOKEN_KEY) || '').trim();
}

function setToken(token) {
  const value = (token || '').trim();
  wx.setStorageSync(TOKEN_KEY, value);
  try {
    const app = getApp();
    if (app && app.globalData) app.globalData.apiToken = value || null;
  } catch (e) {
    // ignore
  }
}

function clearToken() {
  wx.removeStorageSync(TOKEN_KEY);
  try {
    const app = getApp();
    if (app && app.globalData) app.globalData.apiToken = null;
  } catch (e) {
    // ignore
  }
}

function requireAuth() {
  const token = getToken();
  if (!token) {
    wx.reLaunch({ url: BIND_URL });
    return '';
  }
  return token;
}

function parseBody(data) {
  if (data == null) return {};
  if (typeof data === 'object') return data;
  if (typeof data === 'string') {
    const trimmed = data.trim();
    if (!trimmed) return {};
    if (trimmed.charAt(0) === '{' || trimmed.charAt(0) === '[') {
      try {
        return JSON.parse(trimmed);
      } catch (e) {
        return {};
      }
    }
  }
  return {};
}

function classifyHttp(statusCode, data) {
  if (statusCode === 401) {
    return { ok: false, error: apiError('unauthorized', 'unauthorized', { statusCode, raw: data }) };
  }
  if (statusCode === 429) {
    return { ok: false, error: apiError('rate_limited', 'rate_limited', { statusCode, raw: data }) };
  }
  const body = parseBody(data);
  if (body && body.success === true) {
    return { ok: true, data: body.data };
  }
  let msg = 'request_failed';
  if (typeof body.error === 'string' && body.error && body.error.length <= 80) {
    msg = body.error;
  } else if (typeof body.message === 'string' && body.message && body.message.length <= 80) {
    msg = body.message;
  }
  return { ok: false, error: apiError('request_failed', msg, { statusCode, raw: data }) };
}

function request({ method, path, token, data }) {
  return new Promise((resolve, reject) => {
    const base = (API_BASE_URL || '').trim();
    if (!base) {
      reject(apiError('miniapp_api_base_missing', 'miniapp_api_base_missing'));
      return;
    }
    wx.request({
      url: `${base}${path}`,
      method: method || 'GET',
      data: data || undefined,
      header: Object.assign(
        { 'Content-Type': 'application/json' },
        token ? { Authorization: `Bearer ${token}` } : {}
      ),
      dataType: 'json',
      success: (res) => resolve(res),
      fail: (err) => reject(apiError('network', 'network', { raw: err })),
    });
  });
}

async function api({ method, path, token, data }) {
  let res;
  try {
    res = await request({ method, path, token, data });
  } catch (err) {
    if (err && err.code) throw err;
    throw apiError('network', 'network', { raw: err });
  }
  const classified = classifyHttp(res.statusCode, res.data);
  if (!classified.ok) throw classified.error;
  return classified.data;
}

function toastTitle(code, fallbackTitle) {
  if (code === 'miniapp_api_base_missing') return '未配置 API 地址';
  if (code === 'unauthorized') return '绑定失败：Token 无效';
  if (code === 'rate_limited') return '请求过于频繁';
  if (code === 'network') return '网络异常';
  return fallbackTitle || '加载失败';
}

function handleApiError(err, opts) {
  const options = opts || {};
  const redirectOnUnauthorized = options.redirectOnUnauthorized !== false;
  const code = (err && err.code) || String((err && err.message) || '');
  if (code === 'unauthorized' && redirectOnUnauthorized) {
    clearToken();
    wx.reLaunch({ url: BIND_URL });
    return true;
  }
  wx.showToast({
    title: toastTitle(code, options.fallbackTitle || '加载失败'),
    icon: 'none',
  });
  return false;
}

module.exports = {
  api,
  getToken,
  setToken,
  clearToken,
  requireAuth,
  handleApiError,
  apiError,
  classifyHttp,
  isApiConfigured,
};
