const { api, getToken, requireAuth, handleApiError } = require('../../utils/request');
const { parsePairId, formatTemp, buildMessage } = require('../../utils/careMessage');

Page({
  data: {
    pairId: null,
    missingPair: false,
    loading: true,
    loadError: false,
    message: '',
    locationText: '',
    elderName: '',
    relation: '',
    tmaxDisplay: '-',
    tminDisplay: '-',
    trigger: '',
    weatherAvailable: true,
  },

  async onLoad(options) {
    const pairId = parsePairId(options && options.pair_id);
    if (!pairId) {
      this.setData({ pairId: null, missingPair: true, loading: false, loadError: false, message: '' });
      return;
    }
    this.setData({ pairId, missingPair: false });
    await this.loadTemplate(pairId);
  },

  async loadTemplate(pairId) {
    const token = requireAuth();
    if (!token) return;
    this.setData({ loading: true, loadError: false });
    try {
      const elders = await api({ method: 'GET', path: '/mp/api/v1/elders', token });
      const item = (elders || []).find((x) => x.pair_id === pairId);
      if (!item) throw new Error('not_found');
      const member = item.member || {};
      const today = item.today || {};
      const weatherAvailable = today.weather_available === true;
      const elderName = member.name || '';
      const relation = member.relation || '';
      const locationText = item.location_query || item.community_code || '';
      const tmax = today.temperature_max;
      const tmin = today.temperature_min;
      const trigger = today.trigger || '';
      const chronicDiseases = member.chronic_diseases || [];
      const message = buildMessage({
        trigger,
        elderName,
        relation,
        locationText,
        tmax,
        tmin,
        chronicDiseases,
        weatherAvailable,
      });
      this.setData({
        message,
        locationText,
        elderName,
        relation,
        tmaxDisplay: formatTemp(tmax),
        tminDisplay: formatTemp(tmin),
        trigger,
        weatherAvailable,
      });
    } catch (e) {
      this.setData({ loadError: true, message: '' });
      handleApiError(e, { fallbackTitle: '加载失败' });
    } finally {
      this.setData({ loading: false });
    }
  },

  async copyMessage() {
    const message = this.data.message || '';
    if (!message) return;
    try {
      await new Promise((resolve, reject) => {
        wx.setClipboardData({
          data: message,
          success: resolve,
          fail: reject,
        });
      });
      wx.showToast({ title: '已复制', icon: 'success' });
      const token = getToken();
      if (token) {
        api({
          method: 'POST',
          path: '/mp/api/v1/events',
          token,
          data: {
            event_type: 'template_copy',
            pair_id: this.data.pairId,
            meta: { trigger: this.data.trigger },
          },
        }).catch((e) => {
          if (e && e.code === 'unauthorized') {
            handleApiError(e, { fallbackTitle: '加载失败' });
          }
        });
      }
    } catch (e) {
      wx.showToast({ title: '复制失败', icon: 'none' });
    }
  },

  back() {
    wx.navigateBack();
  },
});
