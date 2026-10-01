const { api } = require('../../utils/request');
const { WEB_ACTION_URL } = require('../../config');

Page({
  data: {
    tokenInput: '',
    busy: false,
    webActionUrl: WEB_ACTION_URL || '/action',
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

  copyWebAction() {
    const url = (this.data.webActionUrl || '/action').trim();
    wx.setClipboardData({ data: url });
  },
});

