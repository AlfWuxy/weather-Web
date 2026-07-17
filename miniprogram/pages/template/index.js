const { authApi, getSnapshot, requireToken } = require('../elders/care-session');
const { buildReminderMessage, normalizeList, normalizeSnapshot } = require('../elders/care-logic');

Page({
  data: {
    pairId: null,
    elderName: '家人',
    trigger: '',
    message: '',
    weather: normalizeSnapshot({}),
    loading: false,
  },

  async onLoad(options) {
    if (!requireToken()) return;
    const pairId = Number(options.pair_id || 0) || null;
    this.setData({ pairId });
    if (pairId) await this.loadTemplate();
  },

  async loadTemplate() {
    this.setData({ loading: true });
    try {
      const [elderData, snapshot] = await Promise.all([
        authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
        getSnapshot().catch(() => ({})),
      ]);
      const item = normalizeList(elderData, ['items', 'elders'])
        .find((elder) => Number(elder.pair_id) === this.data.pairId);
      if (!item) throw new Error('not_found');
      const member = item.member || {};
      const weather = normalizeSnapshot(snapshot);
      const message = buildReminderMessage({
        trigger: weather.trigger,
        elderName: member.name,
        relation: member.relation,
        tmax: weather.temperatureMax,
        tmin: weather.temperatureMin,
      });
      this.setData({
        elderName: member.name || '家人',
        trigger: weather.trigger,
        weather,
        message,
      });
    } catch (error) {
      wx.showToast({ title: '提醒话术暂时无法生成', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  copyMessage() {
    if (!this.data.message) return;
    wx.setClipboardData({
      data: this.data.message,
      success: () => {
        wx.showToast({ title: '已复制，可以发给家人', icon: 'success' });
        authApi({
          method: 'POST',
          path: '/mp/api/v1/events',
          data: {
            event_type: 'template_copy',
            pair_id: this.data.pairId,
            meta: { trigger: this.data.trigger, location: '都昌县' },
          },
        }).catch(() => {});
      },
      fail: () => wx.showToast({ title: '复制失败，请重试', icon: 'none' }),
    });
  },

  goCheckin() {
    wx.redirectTo({ url: `/pages/action-checkin/index?pair_id=${this.data.pairId}` });
  },

  back() {
    wx.navigateBack();
  },
});
