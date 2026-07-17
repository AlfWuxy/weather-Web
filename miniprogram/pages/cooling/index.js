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
    allResources: [],
    resources: [],
    freshness: {},
    filter: 'all',
    counts: { all: 0, ac: 0, accessible: 0 },
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
    if (!this.data.allResources.length) this.setData({ loading: true, error: '' });
    try {
      const requestOptions = Object.assign({}, options, {
        onRevalidated: (freshResult) => {
          if (pageCanRender(this)) this.renderResources(freshResult);
        },
      });
      const result = await getCommunity(requestOptions);
      if (pageCanRender(this)) this.renderResources(result);
    } catch (error) {
      if (!pageCanRender(this)) return;
      this.setData({ loading: false, error: '避暑资源暂时无法获取，请稍后再试。' });
    }
  },

  renderResources(result) {
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
    schedulePublicRefresh(this, result.meta, () => this.loadData());
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
    return createPageShare({ title: '都昌县避暑资源', route: '/pages/cooling/index' });
  },

  onShareTimeline() {
    return createTimelineShare({ title: '都昌县避暑资源' });
  },
});
