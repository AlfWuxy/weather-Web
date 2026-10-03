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

  async logout() {
    if (this.data.busy) return;
    const token = this.getToken();
    this.setData({ busy: true });
    try {
      if (token) await api({ method: 'POST', path: '/mp/api/v1/token/revoke', token });
      wx.removeStorageSync('api_token');
      wx.reLaunch({ url: '/pages/bind-token/index' });
    } catch (e) {
      if (e.message === 'unauthorized') {
        wx.removeStorageSync('api_token');
        wx.reLaunch({ url: '/pages/bind-token/index' });
      } else {
        // 撤销未获服务端确认时保留凭证，以便重试或从网页撤销。
        wx.showModal({ title: '尚未退出绑定', content: '服务端撤销未完成。请检查网络后重试，或在网页个人设置中撤销此凭证。', showCancel: false });
      }
    } finally {
      this.setData({ busy: false });
    }
  },
});
