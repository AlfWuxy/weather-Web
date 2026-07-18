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
    warnings: [],
    warningsSourceAvailable: false,
    current: null,
    locationName: '都昌县',
    freshness: {},
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
    if (!this.data.current) this.setData({ loading: true, error: '' });
    try {
      const requestOptions = Object.assign({}, options, {
        onRevalidated: (freshResult) => {
          if (pageCanRender(this)) this.renderWarnings(freshResult);
        },
      });
      const result = await getBootstrap(requestOptions);
      if (pageCanRender(this)) this.renderWarnings(result);
    } catch (error) {
      if (!pageCanRender(this)) return;
      this.setData({ loading: false, error: '预警信息暂时无法获取，请稍后再试。' });
    }
  },

  renderWarnings(result) {
    const snapshot = normalizeBootstrap(result.data);
    const freshness = freshnessView(result.meta, snapshot);
    this.setData({
      loading: false,
      error: '',
      // 较早缓存中的预警可能已经失效，刷新前不继续标成有效预警。
      warnings: freshness.stale ? [] : snapshot.warnings,
      warningsSourceAvailable: freshness.stale ? false : snapshot.warningsSourceAvailable,
      current: snapshot.current,
      locationName: snapshot.location.name,
      freshness,
    });
    schedulePublicRefresh(this, result.meta, () => this.loadData());
  },

  retry() {
    this.loadData({ force: true });
  },

  onShareAppMessage() {
    return createPageShare({
      title: `${this.data.locationName}天气预警`,
      route: '/pages/alerts/index',
    });
  },

  onShareTimeline() {
    return createTimelineShare({ title: `${this.data.locationName}天气预警` });
  },
});
