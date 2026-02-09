const { api } = require('../../utils/request');

Page({
  data: {
    loading: false,
    wxpusherUid: '',
    pushEnabled: false,
    busy: false,
  },

  getToken() {
    return (wx.getStorageSync('api_token') || '').trim();
  },

  async onShow() {
    await this.loadMe();
  },

  async loadMe() {
    const token = this.getToken();
    if (!token) {
      wx.reLaunch({ url: '/pages/bind-token/index' });
      return;
    }
    this.setData({ loading: true });
    try {
      const me = await api({ method: 'GET', path: '/mp/api/v1/me', token });
      this.setData({
        wxpusherUid: me.wxpusher_uid || '',
        pushEnabled: !!me.push_enabled,
      });
    } catch (e) {
      wx.showToast({ title: '加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  onUid(e) {
    this.setData({ wxpusherUid: (e.detail.value || '').trim() });
  },

  onToggle(e) {
    this.setData({ pushEnabled: !!e.detail.value });
  },

  async onSave() {
    if (this.data.busy) return;
    const token = this.getToken();
    if (!token) return;
    this.setData({ busy: true });
    try {
      await api({
        method: 'PATCH',
        path: '/mp/api/v1/me',
        token,
        data: {
          wxpusher_uid: this.data.wxpusherUid,
          push_enabled: this.data.pushEnabled,
        },
      });
      wx.showToast({ title: '已保存', icon: 'success' });
      await this.loadMe();
    } catch (e) {
      wx.showToast({ title: '保存失败', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  logout() {
    wx.removeStorageSync('api_token');
    wx.reLaunch({ url: '/pages/bind-token/index' });
  },
});

