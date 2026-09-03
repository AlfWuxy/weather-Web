const { api } = require('../../utils/request');

function formatTemp(value) {
  if (value === null || value === undefined || value === '') return '';
  const n = Number(value);
  if (!Number.isFinite(n)) return '';
  return String(Math.round(n));
}

Page({
  data: {
    elders: [],
    loading: false,
  },

  async onShow() {
    await this.loadElders();
  },

  getToken() {
    return (wx.getStorageSync('api_token') || '').trim();
  },

  async loadElders() {
    const token = this.getToken();
    if (!token) {
      wx.reLaunch({ url: '/pages/bind-token/index' });
      return;
    }
    this.setData({ loading: true });
    try {
      const data = await api({ method: 'GET', path: '/mp/api/v1/elders', token });
      const elders = (data || []).map((item) => {
        const today = Object.assign({}, item.today || {});
        if (today.weather_available) {
          const tmax = formatTemp(today.temperature_max);
          const tmin = formatTemp(today.temperature_min);
          if (tmax) today.temperature_max = tmax;
          if (tmin) today.temperature_min = tmin;
        }
        return Object.assign({}, item, { today });
      });
      this.setData({ elders });
    } catch (e) {
      if (String(e && e.message) === 'unauthorized') {
        wx.removeStorageSync('api_token');
        wx.reLaunch({ url: '/pages/bind-token/index' });
        return;
      }
      wx.showToast({ title: '加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  goAlerts(e) {
    const pairId = e.currentTarget.dataset.pairId;
    wx.navigateTo({ url: `/pages/alerts/index?pair_id=${pairId}` });
  },

  goTemplate(e) {
    const pairId = e.currentTarget.dataset.pairId;
    wx.navigateTo({ url: `/pages/template/index?pair_id=${pairId}` });
  },

  goEdit(e) {
    const pairId = e.currentTarget.dataset.pairId;
    wx.navigateTo({ url: `/pages/elder-edit/index?pair_id=${pairId}` });
  },

  unbindElder(e) {
    const pairId = e.currentTarget.dataset.pairId;
    if (!pairId) return;
    wx.showModal({
      title: '解除照护',
      content: '将删除该照护对象档案并停止提醒。配对记录会保留为已停用。',
      success: (res) => {
        if (!res.confirm) return;
        const token = this.getToken();
        api({ method: 'DELETE', path: `/mp/api/v1/elders/${pairId}`, token })
          .then(() => this.loadElders())
          .catch(() => {
            wx.showToast({ title: '解除失败', icon: 'none' });
          });
      },
    });
  },

  goCreate() {
    wx.navigateTo({ url: '/pages/elder-edit/index?mode=create' });
  },

  goSettings() {
    wx.navigateTo({ url: '/pages/settings/index' });
  },
});

