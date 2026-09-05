const { api, requireAuth, handleApiError } = require('../../utils/request');

Page({
  data: {
    elders: [],
    loading: true,
    loadError: false,
  },

  async onShow() {
    await this.loadElders();
  },

  formatTemp(value) {
    if (value === null || value === undefined || value === '') return '-';
    return String(value);
  },

  shapeElder(item) {
    const row = item || {};
    const member = row.member || null;
    const today = row.today || {};
    return {
      pair_id: row.pair_id,
      displayName: (member && member.name) ? member.name : '老人',
      relationText: (member && member.relation) ? (' · ' + member.relation) : '',
      locationText: row.location_query || row.community_code || '-',
      tempMaxText: this.formatTemp(today.temperature_max),
      tempMinText: this.formatTemp(today.temperature_min),
      badgeKind: (today.trigger === 'heat' || today.trigger === 'cold') ? today.trigger : 'normal',
      hasOfficialWarning: !!today.has_official_warning,
    };
  },

  async loadElders() {
    const token = requireAuth();
    if (!token) return;
    this.setData({ loading: true, loadError: false });
    try {
      const data = await api({ method: 'GET', path: '/mp/api/v1/elders', token });
      const list = Array.isArray(data) ? data : [];
      this.setData({
        elders: list.map((row) => this.shapeElder(row)),
        loading: false,
        loadError: false,
      });
    } catch (e) {
      if (handleApiError(e, { fallbackTitle: '加载失败' })) return;
      this.setData({ loading: false, loadError: true });
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
});
