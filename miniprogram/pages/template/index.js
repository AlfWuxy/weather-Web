const { authApi, getSnapshot, requireToken } = require('../elders/care-session');
const { buildReminderMessage, normalizeList, normalizeSnapshot } = require('../elders/care-logic');

function lifecycleIsActive(page, lifecycle) {
  return page._unloaded !== true && Number(page._lifecycleGeneration || 0) === lifecycle;
}

function beginLoad(page) {
  page._loadRequestId = Number(page._loadRequestId || 0) + 1;
  return {
    lifecycle: Number(page._lifecycleGeneration || 0),
    requestId: page._loadRequestId,
  };
}

function loadIsActive(page, request) {
  return lifecycleIsActive(page, request.lifecycle) && page._loadRequestId === request.requestId;
}

Page({
  data: {
    pairId: null,
    elderName: '家人',
    trigger: '',
    message: '',
    weather: normalizeSnapshot({}),
    weatherNotice: '',
    loading: false,
    contextReady: false,
    loadError: '',
  },

  async onLoad(options) {
    this._unloaded = false;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    if (!requireToken()) return;
    const pairId = Number(options.pair_id || 0) || null;
    this.setData({
      pairId,
      contextReady: false,
      loadError: pairId ? '' : '缺少家人信息，请返回家庭照护重新选择。',
    });
    if (pairId) await this.loadTemplate();
  },

  onShow() {
    requireToken();
  },

  onUnload() {
    this._unloaded = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
  },

  onSessionInvalidated() {
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
    if (this._unloaded) return;
    this.setData({
      pairId: null,
      elderName: '家人',
      trigger: '',
      message: '',
      weather: normalizeSnapshot({}),
      weatherNotice: '',
      loading: false,
      contextReady: false,
      loadError: '',
    });
  },

  async loadTemplate() {
    if (this._unloaded) return;
    const pairId = Number(this.data.pairId || 0);
    if (!pairId) {
      this.setData({
        message: '',
        loading: false,
        contextReady: false,
        loadError: '缺少家人信息，请返回家庭照护重新选择。',
      });
      return;
    }
    const request = beginLoad(this);
    this.setData({
      message: '',
      loading: true,
      contextReady: false,
      loadError: '',
    });
    try {
      const [elderData, snapshot] = await Promise.all([
        authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
        getSnapshot().catch(() => ({})),
      ]);
      const item = normalizeList(elderData, ['items', 'elders'])
        .find((elder) => Number(elder.pair_id) === pairId);
      if (!loadIsActive(this, request)) return;
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
      if (!message || !String(message).trim()) throw new Error('empty_message');
      this.setData({
        elderName: member.name || '家人',
        trigger,
        weather,
        weatherNotice,
        message,
        contextReady: true,
        loadError: '',
      });
    } catch (error) {
      if (loadIsActive(this, request)) {
        this.setData({
          message: '',
          contextReady: false,
          loadError: '提醒话术暂时无法生成，请检查网络后重试。',
        });
      }
    } finally {
      if (loadIsActive(this, request)) this.setData({ loading: false });
    }
  },

  copyMessage() {
    if (!this.data.contextReady || !this.data.message || this._unloaded) {
      if (!this._unloaded) wx.showToast({ title: '提醒话术尚未准备好', icon: 'none' });
      return;
    }
    const lifecycle = Number(this._lifecycleGeneration || 0);
    wx.setClipboardData({
      data: this.data.message,
      success: () => {
        if (!lifecycleIsActive(this, lifecycle)) return;
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
      fail: () => {
        if (lifecycleIsActive(this, lifecycle)) wx.showToast({ title: '复制失败，请重试', icon: 'none' });
      },
    });
  },

  goCheckin() {
    if (this._unloaded) return;
    if (!this.data.contextReady || !this.data.message || !this.data.pairId) {
      wx.showToast({ title: '请先生成提醒话术', icon: 'none' });
      return;
    }
    wx.redirectTo({ url: `/pages/action-checkin/index?pair_id=${this.data.pairId}` });
  },

  back() {
    if (this._unloaded) return;
    wx.navigateBack();
  },
});
