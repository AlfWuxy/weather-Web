const { api } = require('../../utils/request');

function bindingHint(saved) {
  const value = saved || '';
  return {
    bound: value.length > 0,
    tokenTail: value.length > 4 ? value.slice(-4) : '',
  };
}

function isMaskedToken(token) {
  return token.includes('已绑定') || token.includes('末尾') || /^\*+$/.test(token);
}

Page({
  data: {
    tokenInput: '',
    busy: false,
    bound: false,
    tokenTail: '',
    showToken: false,
  },

  syncBindingHint() {
    const saved = wx.getStorageSync('api_token') || '';
    this.setData(bindingHint(saved));
  },

  onLoad() {
    this.syncBindingHint();
  },

  onShow() {
    this.syncBindingHint();
  },

  onInput(e) {
    this.setData({ tokenInput: (e.detail.value || '').trim() });
  },

  onToggleToken() {
    this.setData({ showToken: !this.data.showToken });
  },

  async onClear() {
    if (this.data.busy) return;
    const token = (wx.getStorageSync('api_token') || '').trim();
    this.setData({ busy: true });
    try {
      if (token) await api({ method: 'POST', path: '/mp/api/v1/token/revoke', token });
      this.setData({ tokenInput: '', bound: false, tokenTail: '' });
      wx.removeStorageSync('api_token');
    } catch (e) {
      if (e.message === 'unauthorized') {
        this.setData({ tokenInput: '', bound: false, tokenTail: '' });
        wx.removeStorageSync('api_token');
      } else {
        wx.showModal({ title: '尚未解除绑定', content: '服务端撤销未完成。请检查网络后重试，或在网页个人设置中撤销此凭证。', showCancel: false });
      }
    } finally {
      this.setData({ busy: false });
    }
  },

  async onBind() {
    if (this.data.busy) return;
    const token = (this.data.tokenInput || '').trim();
    if (!token) {
      if (this.data.bound) {
        wx.reLaunch({ url: '/pages/elders/index' });
        return;
      }
      wx.showToast({ title: '请先输入 Token', icon: 'none' });
      return;
    }
    if (isMaskedToken(token)) {
      wx.showToast({ title: '请重新输入新 Token', icon: 'none' });
      return;
    }
    this.setData({ busy: true });
    try {
      await api({ method: 'GET', path: '/mp/api/v1/me', token });
      wx.setStorageSync('api_token', token);
      this.setData({ tokenInput: '', showToken: false });
      this.syncBindingHint();
      wx.reLaunch({ url: '/pages/elders/index' });
    } catch (e) {
      wx.showToast({ title: '绑定失败：Token 无效', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },
});
