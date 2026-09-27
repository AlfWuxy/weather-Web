const { api } = require('../../utils/request');

function bindingErrorMessage(error) {
  if (error && error.kind === 'token') return 'Token 无效或已失效';
  if (error && error.kind === 'config') return '服务配置有误，请联系管理员';
  if (error && error.kind === 'network') return '网络连接失败，请检查网络';
  if (error && error.kind === 'service') return '服务暂时不可用，请稍后重试';
  return '绑定失败，请稍后重试';
}

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
      wx.showToast({ title: bindingErrorMessage(e), icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },
});
