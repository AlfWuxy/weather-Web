const { getCommunity } = require('../../utils/public-data');
const { freshnessView, normalizeCommunity } = require('../../utils/format');

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
    this.loadData();
  },

  async onPullDownRefresh() {
    await this.loadData({ force: true });
    wx.stopPullDownRefresh();
  },

  async loadData(options) {
    if (!this.data.allCommunities.length) this.setData({ loading: true, error: '' });
    try {
      const result = await getCommunity(options);
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
    } catch (error) {
      this.setData({ loading: false, error: '社区公开数据暂时无法获取，请稍后再试。' });
    }
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
    return { title: '都昌县社区脆弱性与行动参考', path: '/pages/community/index' };
  },
});
