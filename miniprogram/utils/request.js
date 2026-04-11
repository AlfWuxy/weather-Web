const { API_BASE_URL } = require('../config');

function request({ method, path, token, data }) {
  return new Promise((resolve, reject) => {
    if (!API_BASE_URL) {
      reject(new Error('miniapp_api_base_missing'));
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
      fail: (err) => reject(err),
    });
  });
}

async function api({ method, path, token, data }) {
  const res = await request({ method, path, token, data });
  if (res.statusCode === 401) {
    throw new Error('unauthorized');
  }
  const body = res.data || {};
  if (!body.success) {
    const msg = body.error || body.message || 'request_failed';
    throw new Error(msg);
  }
  return body.data;
}

module.exports = { api };
