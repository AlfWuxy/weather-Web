const { getBootstrap } = require('../../utils/public-data');
const { freshnessView, normalizeBootstrap } = require('../../utils/format');

Page({
  data: {
    loading: true,
    error: '',
    forecast: [],
    locationName: '都昌县',
    freshness: {},
    highRiskDays: 0,
  },

  onLoad() {
    this.loadData();
  },

  async onPullDownRefresh() {
    await this.loadData({ force: true });
    wx.stopPullDownRefresh();
  },

  async loadData(options) {
    if (!this.data.forecast.length) this.setData({ loading: true, error: '' });
    try {
      const result = await getBootstrap(options);
      const snapshot = normalizeBootstrap(result.data);
      const highRiskDays = snapshot.forecast.filter((day) => day.tone === 'high').length;
      this.setData({
        loading: false,
        error: '',
        forecast: snapshot.forecast,
        locationName: snapshot.location.name,
        highRiskDays,
        freshness: freshnessView(result.meta, snapshot),
      });
    } catch (error) {
      this.setData({ loading: false, error: '7 天天气正在更新，请稍后再试。' });
    }
  },

  retry() {
    this.loadData({ force: true });
  },

  onShareAppMessage() {
    return { title: `${this.data.locationName} 7 天宜老天气预报`, path: '/pages/forecast/index' };
  },
});
