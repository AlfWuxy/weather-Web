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
    sourceLoading: true,
    sources: [],
    freshness: {},
  },

  onLoad() {
    beginPublicPage(this);
    showPublicShareMenu();
  },

  onShow() {
    showPublicPage(this, () => this.loadSources());
  },

  onHide() {
    hidePublicPage(this);
  },

  onUnload() {
    unloadPublicPage(this);
  },

  async onPullDownRefresh() {
    await this.loadSources({ force: true });
    wx.stopPullDownRefresh();
  },

  async loadSources(options) {
    try {
      const requestOptions = Object.assign({}, options, {
        onRevalidated: (freshResult) => {
          if (pageCanRender(this)) this.renderSources(freshResult);
        },
      });
      const result = await getBootstrap(requestOptions);
      if (pageCanRender(this)) this.renderSources(result);
    } catch (error) {
      if (!pageCanRender(this)) return;
      this.setData({ sourceLoading: false, sources: [] });
    }
  },

  renderSources(result) {
    const snapshot = normalizeBootstrap(result.data);
    this.setData({
      sourceLoading: false,
      sources: snapshot.sources,
      freshness: freshnessView(result.meta, snapshot),
    });
    schedulePublicRefresh(this, result.meta, () => this.loadSources());
  },

  onShareAppMessage() {
    return createPageShare({
      title: '宜老天气通计算方法与透明度',
      route: '/pages/transparency/index',
    });
  },

  onShareTimeline() {
    return createTimelineShare({ title: '宜老天气通计算方法与透明度' });
  },
});
