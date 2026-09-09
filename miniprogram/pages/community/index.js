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

function stableCommunityKey(item) {
  const source = item && typeof item === 'object' ? item : {};
  return String(source.id || source.code || source.name || '');
}

function compareCommunityRank(left, right) {
  const leftMissing = left.score === null;
  const rightMissing = right.score === null;
  if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
  if (!leftMissing && left.score !== right.score) return right.score - left.score;
  const leftKey = stableCommunityKey(left);
  const rightKey = stableCommunityKey(right);
  if (leftKey === rightKey) return 0;
  return leftKey < rightKey ? -1 : 1;
}

Page({
  data: {
    loading: true,
    error: '',
    communities: [],
    summary: {},
    freshness: {},
    filter: 'all',
    counts: { all: 0, high: 0, mid: 0, low: 0 },
  },

  onLoad() {
    this._allCommunities = [];
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
    this._allCommunities = [];
  },

  async onPullDownRefresh() {
    await this.loadData({ force: true });
    wx.stopPullDownRefresh();
  },

  async loadData(options) {
    if (!Array.isArray(this._allCommunities) || !this._allCommunities.length) {
      this.setData({ loading: true, error: '' });
    }
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
    const allCommunities = normalized.communities.slice()
      .sort(compareCommunityRank)
      // 排名在完整列表上一次生成，切换筛选时仍显示全县位置。
      .map((item, index) => Object.assign({}, item, { globalRank: index + 1 }));
    const counts = {
      all: allCommunities.length,
      high: allCommunities.filter((item) => item.tone === 'high').length,
      mid: allCommunities.filter((item) => item.tone === 'mid').length,
      low: allCommunities.filter((item) => item.tone === 'low').length,
    };
    this._allCommunities = allCommunities;
    const communities = this.filteredCommunities(this.data.filter);
    this.setData({
      loading: false,
      error: '',
      communities,
      summary: normalized.summary,
      freshness: freshnessView(result.meta, normalized),
      counts,
    });
    schedulePublicRefresh(this, result.meta, () => this.loadData());
  },

  chooseFilter(event) {
    this.applyFilter(event.currentTarget.dataset.filter);
  },

  applyFilter(filter) {
    const communities = this.filteredCommunities(filter);
    this.setData({ filter, communities });
  },

  filteredCommunities(filter) {
    const allCommunities = Array.isArray(this._allCommunities) ? this._allCommunities : [];
    return filter === 'all'
      ? allCommunities
      : allCommunities.filter((item) => item.tone === filter);
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
