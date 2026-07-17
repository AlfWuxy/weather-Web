const { getCommunity } = require('../../utils/public-data');
const { freshnessView, normalizeCommunity } = require('../../utils/format');
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
    allCommunities: [],
    communities: [],
    summary: {},
    freshness: {},
    filter: 'all',
    counts: { all: 0, high: 0, mid: 0, low: 0 },
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
    if (!this.data.allCommunities.length) this.setData({ loading: true, error: '' });
    try {
      const requestOptions = Object.assign({}, options, {
        onRevalidated: (freshResult) => {
          if (pageCanRender(this)) this.renderCommunities(freshResult);
        },
      });
      const result = await getCommunity(requestOptions);
      if (pageCanRender(this)) this.renderCommunities(result);
    } catch (error) {
      if (!pageCanRender(this)) return;
      this.setData({ loading: false, error: '社区公开数据暂时无法获取，请稍后再试。' });
    }
  },

  renderCommunities(result) {
    const normalized = normalizeCommunity(result.data);
    const allCommunities = normalized.communities.slice().sort((left, right) => {
      if (left.score === null) return 1;
      if (right.score === null) return -1;
      return right.score - left.score;
    });
    const counts = {
      all: allCommunities.length,
      high: allCommunities.filter((item) => item.tone === 'high').length,
      mid: allCommunities.filter((item) => item.tone === 'mid').length,
      low: allCommunities.filter((item) => item.tone === 'low').length,
    };
    this.setData({
      loading: false,
      error: '',
      allCommunities,
      summary: normalized.summary,
      freshness: freshnessView(result.meta, normalized),
      counts,
    });
    this.applyFilter(this.data.filter);
    schedulePublicRefresh(this, result.meta, () => this.loadData());
  },

  chooseFilter(event) {
    this.applyFilter(event.currentTarget.dataset.filter);
  },

  applyFilter(filter) {
    const communities = filter === 'all'
      ? this.data.allCommunities
      : this.data.allCommunities.filter((item) => item.tone === filter);
    this.setData({ filter, communities });
  },

  retry() {
    this.loadData({ force: true });
  },

  onShareAppMessage() {
    return createPageShare({
      title: '都昌县社区脆弱性与行动参考',
      route: '/pages/community/index',
    });
  },

  onShareTimeline() {
    return createTimelineShare({ title: '都昌县社区脆弱性与行动参考' });
  },
});
