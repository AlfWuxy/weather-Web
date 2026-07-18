const { authApi, getSnapshot, requireToken } = require('../elders/care-session');
const { buildReminderMessage, normalizeList, normalizeSnapshot } = require('../elders/care-logic');

Page({
  data: {
    pairId: null,
    elderName: '家人',
    trigger: '',
    message: '',
    weather: normalizeSnapshot({}),
    weatherNotice: '',
    loading: false,
  },

  async onLoad(options) {
    if (!requireToken()) return;
    const pairId = Number(options.pair_id || 0) || null;
    this.setData({ pairId });
    if (pairId) await this.loadTemplate();
  },

  onShow() {
    requireToken();
  },

  onSessionInvalidated() {
    this.setData({
      pairId: null,
      elderName: '家人',
      trigger: '',
      message: '',
      weather: normalizeSnapshot({}),
      weatherNotice: '',
      loading: false,
    });
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
      const usesGenericWeather = weather.stale || !weather.available;
      const weatherNotice = weather.stale
        ? '天气数据较早，复制内容已切换为通用提醒。'
        : (!weather.available ? '天气数据待更新，复制内容使用通用提醒。' : '');
      const trigger = usesGenericWeather ? '' : weather.trigger;
      const message = buildReminderMessage({
        trigger,
        elderName: member.name,
        relation: member.relation,
        tmax: weather.temperatureMax,
        tmin: weather.temperatureMin,
      });
      this.setData({
        elderName: member.name || '家人',
        trigger,
        weather,
        weatherNotice,
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
