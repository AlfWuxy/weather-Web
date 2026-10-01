const { api } = require('../../utils/request');
const { WEB_ACTION_URL } = require('../../config');

Page({
  data: {
    elders: [],
    loading: false,
    webActionUrl: WEB_ACTION_URL || '/action',
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
      this.setData({ elders: data || [] });
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

  goCreate() {
    wx.navigateTo({ url: '/pages/elder-edit/index?mode=create' });
  },

  goSettings() {
    wx.navigateTo({ url: '/pages/settings/index' });
  },

  copyWebAction() {
    const url = (this.data.webActionUrl || '/action').trim();
    wx.setClipboardData({ data: url });
  },
});

