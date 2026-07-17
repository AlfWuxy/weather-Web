const { getBootstrap } = require('../../utils/public-data');
const { freshnessView, normalizeBootstrap } = require('../../utils/format');
const {
  beginPublicPage,
  hidePublicPage,
  pageCanRender,
  schedulePublicRefresh,
  showPublicPage,
  unloadPublicPage,
} = require('../../utils/public-page-lifecycle');
const { createPageShare, createTimelineShare, showPublicShareMenu } = require('../../utils/share');

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
    beginPublicPage(this);
    showPublicShareMenu();
  },

  onShow() {
    showPublicPage(this, () => this.loadData());
  },

  onHide() {
    hidePublicPage(this);
  },

  onUnload() {
    unloadPublicPage(this);
  },

  async onPullDownRefresh() {
    await this.loadData({ force: true });
    wx.stopPullDownRefresh();
  },

  async loadData(options) {
    if (!this.data.forecast.length) this.setData({ loading: true, error: '' });
    try {
      const requestOptions = Object.assign({}, options, {
        onRevalidated: (freshResult) => {
          if (pageCanRender(this)) this.renderForecast(freshResult);
        },
      });
      const result = await getBootstrap(requestOptions);
      if (pageCanRender(this)) this.renderForecast(result);
    } catch (error) {
      if (!pageCanRender(this)) return;
      this.setData({ loading: false, error: '7 天天气正在更新，请稍后再试。' });
    }
  },

  renderForecast(result) {
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
    schedulePublicRefresh(this, result.meta, () => this.loadData());
  },

  retry() {
    this.loadData({ force: true });
  },

  onShareAppMessage() {
    return createPageShare({
      title: `${this.data.locationName} 7 天宜老天气预报`,
      route: '/pages/forecast/index',
    });
  },

  onShareTimeline() {
    return createTimelineShare({ title: `${this.data.locationName} 7 天宜老天气预报` });
  },
});
