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
    scriptVersion: '',
    scriptHash: '',
    scenario: '',
    messengerRole: 'child',
    channel: 'wechat_text',
    loading: false,
    contextReady: false,
    canCopyAdvice: false,
    loadError: '',
    locationLabel: '',
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
      canCopyAdvice: false,
      loadError: '',
      locationLabel: '',
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
      const [elderData, snapshot, scripts] = await Promise.all([
        authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
        getSnapshot().catch(() => ({})),
        authApi({ method: 'GET', path: '/mp/api/v1/scripts' }).catch(() => null),
      ]);
      const item = normalizeList(elderData, ['items', 'elders'])
        .find((elder) => Number(elder.pair_id) === pairId);
      if (!loadIsActive(this, request)) return;
      if (!item) throw new Error('not_found');
      const member = item.member || {};
      const weather = normalizeSnapshot(snapshot);
      const weatherUnavailable = weather.stale || !weather.available;
      const locationLabel = item.location_query || member.location_query || '';
      const heatTriggered = !weatherUnavailable && weather.trigger === 'heat';
      const weatherNotice = weather.stale
        ? '天气数据已过期，不能当作今天情况正常，也不会自动生成日常防护建议。'
        : (!weather.available
          ? '天气数据暂不可用，不能当作今天情况正常，也不会自动生成日常防护建议。'
          : (heatTriggered ? '' : '当前没有高温触发，不会生成高温防护建议。天气可用也不等于今天需要按高温行动。'));
      const scenario = weatherUnavailable ? 'unavailable' : (heatTriggered ? 'heat' : '');
      const trigger = heatTriggered ? 'heat' : '';
      const catalog = scripts || {};
      const version = catalog.default || 'v2_gist_why';
      const versions = catalog.versions || {};
      let message = '';
      if (weatherUnavailable) {
        message = '天气数据暂不可用或已过期，不能显示为正常，也不能生成今天的肯定防护建议。请改用电话或当面提醒家人注意防暑，并以医生已审核内容为准。';
      } else if (!heatTriggered) {
        message = '今天没有适用的高温提醒，不能复制高温防护建议。请先确认老人所在地是否发布高温预警或达到高温阈值。';
      } else {
        const template = (versions[version] && versions[version].heat)
          || (versions.v2_gist_why && versions.v2_gist_why.heat)
          || '';
        const placeholders = {
          elder_call: member.name || member.relation || '家里',
          tmax: weather.temperatureMax == null ? '--' : weather.temperatureMax,
          tmin: weather.temperatureMin == null ? '--' : weather.temperatureMin,
          window: '中午前后',
          messenger_self: '家里人',
          callback_time: '傍晚',
        };
        message = template;
        Object.keys(placeholders).forEach((key) => {
          message = String(message).split('{' + key + '}').join(String(placeholders[key]));
        });
        if (!message || !String(message).trim()) {
          message = buildReminderMessage({
            trigger: 'heat',
            elderName: member.name,
            relation: member.relation,
            tmax: weather.temperatureMax,
            tmin: weather.temperatureMin,
          });
        }
      }
      if (!message || !String(message).trim()) throw new Error('empty_message');
      this.setData({
        elderName: member.name || '家人',
        trigger,
        weather,
        weatherNotice,
        locationLabel,
        message,
        scriptVersion: version,
        scriptHash: catalog.version_hash || '',
        scenario,
        messengerRole: 'child',
        channel: 'wechat_text',
        contextReady: true,
        canCopyAdvice: heatTriggered,
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
    if (!this.data.canCopyAdvice) {
      wx.showToast({ title: '天气不可用，不能复制日常建议', icon: 'none' });
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
            meta: {
              script_version: this.data.scriptVersion || 'v2_gist_why',
              messenger_role: this.data.messengerRole || 'child',
              channel: this.data.channel || 'wechat_text',
              scenario: this.data.scenario || '',
            },
          },
        }).catch(() => {
          if (lifecycleIsActive(this, lifecycle)) {
            wx.showToast({ title: '已复制，但记录未保存', icon: 'none' });
          }
        });
      },
      fail: () => {
        if (lifecycleIsActive(this, lifecycle)) wx.showToast({ title: '复制失败，请重试' , icon: 'none' });
      },
    });
  },

  goCheckin() {
    if (this._unloaded) return;
    if (!this.data.contextReady || !this.data.message || !this.data.pairId || !this.data.canCopyAdvice) {
      wx.showToast({ title: this.data.canCopyAdvice ? '请先生成提醒话术' : '当前没有可记录的高温建议', icon: 'none' });
      return;
    }
    wx.redirectTo({ url: `/pages/action-checkin/index?pair_id=${this.data.pairId}` });
  },

  back() {
    if (this._unloaded) return;
    wx.navigateBack();
  },
});
