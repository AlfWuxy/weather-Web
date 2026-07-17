const { getBootstrap } = require('../../utils/public-data');
const { freshnessView, normalizeBootstrap } = require('../../utils/format');

Page({
  data: {
    loading: true,
    error: '',
    snapshot: null,
    freshness: {},
    topActions: [],
  },

  onLoad() {
    this.loadData();
  },

  onShow() {
    if (this.data.snapshot) this.loadData();
  },

  async onPullDownRefresh() {
    await this.loadData({ force: true });
    wx.stopPullDownRefresh();
  },

  async loadData(options) {
    if (!this.data.snapshot) this.setData({ loading: true, error: '' });
    try {
      const result = await getBootstrap(options);
      const snapshot = normalizeBootstrap(result.data);
      this.setData({
        loading: false,
        error: '',
        snapshot,
        topActions: snapshot.actions.slice(0, 3),
        freshness: freshnessView(result.meta, snapshot),
      });
    } catch (error) {
      this.setData({
        loading: false,
        error: '天气数据暂时无法获取。请检查网络，稍后再试。',
      });
    }
  },

  retry() {
    this.loadData({ force: true });
  },

  onShareAppMessage() {
    return {
      title: '宜老天气通：把天气预警变成今天能做的事',
      path: '/pages/home/index',
    };
  },
});
