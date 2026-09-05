const { api, isUnauthorizedError } = require('../../utils/request');

function parsePairId(value) {
  const text = String(value || '').trim();
  if (!/^[1-9]\d*$/.test(text)) return null;
  const pairId = Number(text);
  return Number.isSafeInteger(pairId) ? pairId : null;
}

function showInvalidPairAndReturn() {
  wx.showModal({
    title: '无法打开',
    content: '监测对象不存在或链接已失效，将返回监测对象列表。',
    showCancel: false,
    complete: () => wx.reLaunch({ url: '/pages/elders/index' }),
  });
}

Page({
  data: {
    pairId: null,
    loading: false,
    warnings: [],
    location: {},
    weather: {},
  },

  getToken() {
    return (wx.getStorageSync('api_token') || '').trim();
  },

  async onLoad(options = {}) {
    const pairId = parsePairId(options.pair_id);
    this.setData({ pairId });
    if (!pairId) {
      showInvalidPairAndReturn();
      return;
    }
    await this.loadAlerts(pairId);
  },

  async loadAlerts(pairId) {
    const token = this.getToken();
    if (!token) {
      wx.reLaunch({ url: '/pages/bind-token/index' });
      return;
    }
    this.setData({ loading: true });
    try {
      const data = await api({ method: 'GET', path: `/mp/api/v1/alerts?pair_id=${pairId}`, token });
      this.setData({
        warnings: data.warnings || [],
        location: data.location || {},
        weather: data.weather || {},
      });
    } catch (e) {
      if (isUnauthorizedError(e)) return;
      if (e && e.code === 'not_found') {
        showInvalidPairAndReturn();
        return;
      }
      wx.showToast({ title: '加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },
});
