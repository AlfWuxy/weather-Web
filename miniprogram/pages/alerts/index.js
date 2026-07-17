const { getBootstrap } = require('../../utils/public-data');
const { freshnessView, normalizeBootstrap } = require('../../utils/format');

Page({
  data: {
    loading: true,
    error: '',
    warnings: [],
    warningsSourceAvailable: false,
    current: null,
    locationName: '都昌县',
    freshness: {},
  },

  onLoad() {
    this.loadData();
  },

  async onPullDownRefresh() {
    await this.loadData({ force: true });
    wx.stopPullDownRefresh();
  },

  async loadData(options) {
    if (!this.data.current) this.setData({ loading: true, error: '' });
    try {
      const result = await getBootstrap(options);
      const snapshot = normalizeBootstrap(result.data);
      this.setData({
        loading: false,
        error: '',
        warnings: snapshot.warnings,
        warningsSourceAvailable: snapshot.warningsSourceAvailable,
        current: snapshot.current,
        locationName: snapshot.location.name,
        freshness: freshnessView(result.meta, snapshot),
      });
    } catch (error) {
      this.setData({ loading: false, error: '预警信息暂时无法获取，请稍后再试。' });
    }
  },

  retry() {
    this.loadData({ force: true });
  },

  onShareAppMessage() {
    return { title: `${this.data.locationName}天气预警`, path: '/pages/alerts/index' };
  },
});
