const { api } = require('../../utils/request');

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

  async onLoad(options) {
    const pairId = options.pair_id ? parseInt(options.pair_id, 10) : null;
    this.setData({ pairId });
    if (pairId) {
      await this.loadAlerts(pairId);
    }
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
      wx.showToast({ title: '加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },
});

