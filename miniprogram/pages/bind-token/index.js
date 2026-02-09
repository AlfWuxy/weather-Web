const { api } = require('../../utils/request');

Page({
  data: {
    tokenInput: '',
    busy: false,
  },

  onLoad() {
    const saved = wx.getStorageSync('api_token') || '';
    if (saved) {
      this.setData({ tokenInput: saved });
    }
  },

  onInput(e) {
    this.setData({ tokenInput: (e.detail.value || '').trim() });
  },

  onClear() {
    this.setData({ tokenInput: '' });
    wx.removeStorageSync('api_token');
  },

  async onBind() {
    if (this.data.busy) return;
    const token = (this.data.tokenInput || '').trim();
    if (!token) {
      wx.showToast({ title: '请先输入 Token', icon: 'none' });
      return;
    }
    this.setData({ busy: true });
    try {
      await api({ method: 'GET', path: '/mp/api/v1/me', token });
      wx.setStorageSync('api_token', token);
      wx.reLaunch({ url: '/pages/elders/index' });
    } catch (e) {
      wx.showToast({ title: '绑定失败：Token 无效', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },
});

