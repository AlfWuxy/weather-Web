const { getBootstrap } = require('../../utils/public-data');
const { freshnessView, normalizeBootstrap } = require('../../utils/format');

Page({
  data: {
    sourceLoading: true,
    sources: [],
    freshness: {},
  },

  onLoad() {
    this.loadSources();
  },

  async onPullDownRefresh() {
    await this.loadSources({ force: true });
    wx.stopPullDownRefresh();
  },

  async loadSources(options) {
    try {
      const result = await getBootstrap(options);
      const snapshot = normalizeBootstrap(result.data);
      this.setData({
        sourceLoading: false,
        sources: snapshot.sources,
        freshness: freshnessView(result.meta, snapshot),
      });
    } catch (error) {
      this.setData({ sourceLoading: false, sources: [] });
    }
  },

  onShareAppMessage() {
    return { title: '宜老天气通计算方法与透明度', path: '/pages/transparency/index' };
  },
});
