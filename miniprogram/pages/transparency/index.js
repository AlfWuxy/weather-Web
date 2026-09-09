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

Page({
  data: {
    sourceLoading: true,
    sourceError: '',
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
    if (!this.data.sources.length) this.setData({ sourceLoading: true, sourceError: '' });
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
      const freshness = staleRetryMeta(this.data.freshness, PUBLIC_RETRY_DELAY_MS);
      this.setData({
        sourceLoading: false,
        sourceError: '数据源状态暂时无法读取。稍后会自动重试。',
        freshness,
      });
      schedulePublicRefresh(this, freshness, () => this.loadSources());
    }
  },

  renderSources(result) {
    const snapshot = normalizeBootstrap(result.data);
    this.setData({
      sourceLoading: false,
      sourceError: '',
      sources: snapshot.sources,
      freshness: freshnessView(result.meta, snapshot),
    });
    schedulePublicRefresh(this, result.meta, () => this.loadSources());
  },

  retrySources() {
    return this.loadSources({ force: true });
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
