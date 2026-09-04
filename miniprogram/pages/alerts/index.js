const { api } = require('../../utils/request');

function formatTemp(value) {
  if (value === null || value === undefined || value === '') return '';
  const n = Number(value);
  if (!Number.isFinite(n)) return '';
  return String(Math.round(n));
}

Page({
  data: {
    pairId: null,
    loading: false,
    warnings: [],
    location: {},
    weather: {},
    missingPairId: false,
  },

  getToken() {
    return (wx.getStorageSync('api_token') || '').trim();
  },

  async onLoad(options) {
    const pairId = options.pair_id ? parseInt(options.pair_id, 10) : null;
    this.setData({ pairId, missingPairId: !pairId });
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
      const weather = Object.assign({}, data.weather || {});
      if (weather.weather_available) {
        const tmax = formatTemp(weather.temperature_max);
        const tmin = formatTemp(weather.temperature_min);
        if (tmax) weather.temperature_max = tmax;
        if (tmin) weather.temperature_min = tmin;
      }
      this.setData({
        warnings: data.warnings || [],
        location: data.location || {},
        weather,
      });
    } catch (e) {
      wx.showToast({ title: '加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },
});

