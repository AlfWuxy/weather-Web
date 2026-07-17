const { getCommunity } = require('../../utils/public-data');
const { freshnessView, normalizeCommunity } = require('../../utils/format');

Page({
  data: {
    loading: true,
    error: '',
    allResources: [],
    resources: [],
    freshness: {},
    filter: 'all',
    counts: { all: 0, ac: 0, accessible: 0 },
  },

  onLoad() {
    this.loadData();
  },

  async onPullDownRefresh() {
    await this.loadData({ force: true });
    wx.stopPullDownRefresh();
  },

  async loadData(options) {
    if (!this.data.allResources.length) this.setData({ loading: true, error: '' });
    try {
      const result = await getCommunity(options);
      const normalized = normalizeCommunity(result.data);
      const allResources = normalized.cooling;
      this.setData({
        loading: false,
        error: '',
        allResources,
        counts: {
          all: allResources.length,
          ac: allResources.filter((item) => item.hasAc).length,
          accessible: allResources.filter((item) => item.accessible).length,
        },
        freshness: freshnessView(result.meta, normalized),
      });
      this.applyFilter(this.data.filter);
    } catch (error) {
      this.setData({ loading: false, error: '避暑资源暂时无法获取，请稍后再试。' });
    }
  },

  chooseFilter(event) {
    this.applyFilter(event.currentTarget.dataset.filter);
  },

  applyFilter(filter) {
    let resources = this.data.allResources;
    if (filter === 'ac') resources = resources.filter((item) => item.hasAc);
    if (filter === 'accessible') resources = resources.filter((item) => item.accessible);
    this.setData({ filter, resources });
  },

  copyAddress(event) {
    const resource = this.data.allResources.find((item) => item.id === event.currentTarget.dataset.id);
    if (!resource) return;
    wx.setClipboardData({ data: `${resource.name}\n${resource.address}\n${resource.hours}` });
  },

  callResource(event) {
    const phoneNumber = String(event.currentTarget.dataset.phone || '').trim();
    if (phoneNumber) wx.makePhoneCall({ phoneNumber });
  },

  retry() {
    this.loadData({ force: true });
  },

  onShareAppMessage() {
    return { title: '都昌县避暑资源', path: '/pages/cooling/index' };
  },
});
