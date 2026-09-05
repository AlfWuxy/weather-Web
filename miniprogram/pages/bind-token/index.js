const { api, getToken, setToken, clearToken, handleApiError } = require('../../utils/request');

Page({
  data: {
    tokenInput: '',
    busy: false,
  },

  onLoad() {
    const saved = getToken();
    if (saved) {
      this.setData({ tokenInput: saved });
    }
  },

  onShow() {
    const saved = getToken();
    if (saved) {
      wx.reLaunch({ url: '/pages/elders/index' });
    }
  },

  onInput(e) {
    this.setData({ tokenInput: e.detail.value || '' });
  },

  onClear() {
    this.setData({ tokenInput: '' });
    clearToken();
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
      setToken(token);
      wx.reLaunch({ url: '/pages/elders/index' });
    } catch (e) {
      handleApiError(e, { fallbackTitle: '绑定失败', redirectOnUnauthorized: false });
    } finally {
      this.setData({ busy: false });
    }
  },
});
