const { api } = require('../../utils/request');

Page({
  data: {
    elders: [],
    loading: false,
    page: 0,
    hasMore: false,
  },

  async onShow() {
    await this.loadElders();
  },

  getToken() {
    return (wx.getStorageSync('api_token') || '').trim();
  },

  async loadElders(reset = true) {
    if (this.data.loading) return;
    const token = this.getToken();
    if (!token) {
      wx.reLaunch({ url: '/pages/bind-token/index' });
      return;
    }
    this.setData({ loading: true });
    try {
      const page = reset ? 1 : this.data.page + 1;
      const body = await api({ method: 'GET', path: `/mp/api/v1/elders?page=${page}`, token, includeMeta: true });
      const rows = body.data || [];
      const existing = reset ? [] : this.data.elders;
      const seen = new Set(existing.map((item) => item.pair_id));
      this.setData({
        elders: existing.concat(rows.filter((item) => !seen.has(item.pair_id))),
        page: body.page || page,
        hasMore: Boolean(body.has_more),
      });
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

  async loadMore() {
    if (this.data.hasMore && !this.data.loading) await this.loadElders(false);
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
});

