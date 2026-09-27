const { api, requireAuth, handleApiError } = require('../../utils/request');
const { parsePairId, formatTemp, warningListKey, thresholdKind } = require('../../utils/careMessage');

Page({
  data: {
    pairId: null,
    missingPair: false,
    loading: true,
    loadError: false,
    warnings: [],
    location: {},
    weather: {},
    tmaxDisplay: '-',
    tminDisplay: '-',
    thresholdKind: '',
  },

  async onLoad(options) {
    const pairId = parsePairId(options && options.pair_id);
    if (!pairId) {
      this.setData({ pairId: null, missingPair: true, loading: false, loadError: false });
      return;
    }
    this.setData({ pairId, missingPair: false });
    await this.loadAlerts(pairId);
  },

  async loadAlerts(pairId) {
    const token = requireAuth();
    if (!token) return;
    this.setData({ loading: true, loadError: false });
    try {
      const data = await api({ method: 'GET', path: `/mp/api/v1/alerts?pair_id=${pairId}`, token });
      const warnings = (data.warnings || []).map((item, index) => Object.assign({}, item, {
        listKey: warningListKey(item, index),
      }));
      const weather = data.weather || {};
      this.setData({
        warnings,
        location: data.location || {},
        weather,
        tmaxDisplay: formatTemp(weather.temperature_max),
        tminDisplay: formatTemp(weather.temperature_min),
        thresholdKind: weather.trigger || thresholdKind(weather.temperature_max, weather.temperature_min, weather.weather_available),
      });
    } catch (e) {
      this.setData({ loadError: true, warnings: [] });
      handleApiError(e, { fallbackTitle: '加载失败' });
    } finally {
      this.setData({ loading: false });
    }
  },
});
