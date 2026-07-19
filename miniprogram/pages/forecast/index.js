const { getBootstrap, PUBLIC_RETRY_DELAY_MS } = require('../../utils/public-data');
const { freshnessView, normalizeBootstrap } = require('../../utils/format');
const {
  beginPublicPage,
  hidePublicPage,
  pageCanRender,
  schedulePublicRefresh,
  showPublicPage,
  staleRetryMeta,
  unloadPublicPage,
} = require('../../utils/public-page-lifecycle');
const { createPageShare, createTimelineShare, showPublicShareMenu } = require('../../utils/share');

function staleForecast(forecast) {
  return (Array.isArray(forecast) ? forecast : []).map((day) => Object.assign({}, day, {
    available: false,
    score: null,
    scoreText: '待刷新',
    tone: 'unknown',
    riskLabel: '风险待刷新',
  }));
}

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
      const hasForecast = this.data.forecast.length > 0;
      const freshness = staleRetryMeta(this.data.freshness, PUBLIC_RETRY_DELAY_MS);
      this.setData({
        loading: false,
        error: hasForecast
          ? '7 天预报更新失败，日期与温度仅供参考，风险等级已暂停。稍后会自动重试。'
          : '7 天天气正在更新，请稍后再试。',
        forecast: hasForecast ? staleForecast(this.data.forecast) : [],
        highRiskDays: 0,
        freshness,
      });
      schedulePublicRefresh(this, freshness, () => this.loadData());
    }
  },

  renderForecast(result) {
    const snapshot = normalizeBootstrap(result.data);
    const freshness = freshnessView(result.meta, snapshot);
    const forecast = freshness.stale
      ? staleForecast(snapshot.forecast)
      : snapshot.forecast;
    const highRiskDays = freshness.stale ? 0 : forecast.filter((day) => day.tone === 'high').length;
    this.setData({
      loading: false,
      error: result.meta && result.meta.networkError
        ? '7 天预报更新失败，日期与温度仅供参考，风险等级已暂停。稍后会自动重试。'
        : '',
      forecast,
      locationName: snapshot.location.name,
      highRiskDays,
      freshness,
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
