const { api, requireAuth, handleApiError, clearToken } = require('../../utils/request');

Page({
  data: {
    loading: false,
    wxpusherUid: '',
    pushEnabled: false,
    busy: false,
  },

  async onShow() {
    await this.loadMe();
  },

  async loadMe() {
    const token = requireAuth();
    if (!token) return;
    this.setData({ loading: true });
    try {
      const me = await api({ method: 'GET', path: '/mp/api/v1/me', token });
      this.setData({
        wxpusherUid: me.wxpusher_uid || '',
        pushEnabled: !!me.push_enabled,
      });
    } catch (e) {
      handleApiError(e, { fallbackTitle: '加载失败' });
    } finally {
      this.setData({ loading: false });
    }
  },

  onUid(e) {
    this.setData({ wxpusherUid: e.detail.value || '' });
  },

  onToggle(e) {
    const on = !!e.detail.value;
    const uid = (this.data.wxpusherUid || '').trim();
    if (on && !uid) {
      wx.showToast({ title: '请先填写 UID', icon: 'none' });
      this.setData({ pushEnabled: false });
      return;
    }
    this.setData({ pushEnabled: on });
  },

  async onSave() {
    if (this.data.busy || this.data.loading) return;
    const token = requireAuth();
    if (!token) return;
    const uid = (this.data.wxpusherUid || '').trim();
    if (this.data.pushEnabled && !uid) {
      wx.showToast({ title: '请先填写 UID', icon: 'none' });
      this.setData({ pushEnabled: false });
      return;
    }
    this.setData({ busy: true });
    try {
      const wantedPush = this.data.pushEnabled;
      const saved = await api({
        method: 'PATCH',
        path: '/mp/api/v1/me',
        token,
        data: {
          wxpusher_uid: uid,
          push_enabled: wantedPush,
        },
      });
      this.setData({
        wxpusherUid: (saved && saved.wxpusher_uid) || '',
        pushEnabled: !!(saved && saved.push_enabled),
      });
      if (wantedPush && !(saved && saved.push_enabled)) {
        wx.showToast({ title: '未填写 UID，推送未开启', icon: 'none' });
      } else {
        wx.showToast({ title: '已保存', icon: 'success' });
      }
    } catch (e) {
      handleApiError(e, { fallbackTitle: '保存失败' });
    } finally {
      this.setData({ busy: false });
    }
  },

  logout() {
    clearToken();
    wx.reLaunch({ url: '/pages/bind-token/index' });
  },
});
