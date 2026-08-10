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

function mapScaleFor(communities) {
  if (!communities.length) return 12;
  const latitudes = communities.map((item) => item.latitude);
  const longitudes = communities.map((item) => item.longitude);
  const span = Math.max(
    Math.max(...latitudes) - Math.min(...latitudes),
    Math.max(...longitudes) - Math.min(...longitudes)
  );
  if (span <= 0.01) return 14;
  if (span <= 0.03) return 13;
  if (span <= 0.08) return 12;
  return 10;
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
    mapReady: false,
    mapMarkers: [],
    mapLatitude: null,
    mapLongitude: null,
    mapScale: 12,
    mappableCount: 0,
    selectedCommunity: null,
  },

  onLoad() {
    this._allCommunities = [];
    this._markerCommunityById = {};
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
    this._markerCommunityById = {};
  },

  async onPullDownRefresh() {
    await this.loadData({ force: true, revalidate: true });
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
    const mapState = this.buildMapState(communities);
    this.setData({
      loading: false,
      error: '',
      communities,
      summary: normalized.summary,
      freshness: freshnessView(result.meta, normalized),
      counts,
      ...mapState,
      selectedCommunity: null,
    });
    schedulePublicRefresh(this, result.meta, () => this.loadData());
  },

  chooseFilter(event) {
    this.applyFilter(event.currentTarget.dataset.filter);
  },

  applyFilter(filter) {
    const communities = this.filteredCommunities(filter);
    this.setData({
      filter,
      communities,
      ...this.buildMapState(communities),
      selectedCommunity: null,
    });
  },

  filteredCommunities(filter) {
    const allCommunities = Array.isArray(this._allCommunities) ? this._allCommunities : [];
    return filter === 'all'
      ? allCommunities
      : allCommunities.filter((item) => item.tone === filter);
  },

  buildMapState(communities) {
    const mappable = (Array.isArray(communities) ? communities : [])
      .filter((item) => item.hasCoordinates);
    const markerCommunityById = {};
    const markers = mappable.map((item, index) => {
      const markerId = index + 1;
      markerCommunityById[String(markerId)] = stableCommunityKey(item);
      return {
        id: markerId,
        latitude: item.latitude,
        longitude: item.longitude,
        iconPath: '/assets/icons/place.png',
        width: 30,
        height: 30,
        title: item.name,
        // 地图标记只显示社区名称，聚合指标统一在地图外的详情卡展示。
        callout: {
          content: item.name,
          color: '#44362d',
          fontSize: 12,
          borderRadius: 6,
          bgColor: '#fffdf9',
          padding: 6,
          display: 'BYCLICK',
          textAlign: 'center',
        },
      };
    });
    this._markerCommunityById = markerCommunityById;
    if (!mappable.length) {
      return {
        mapReady: false,
        mapMarkers: [],
        mapLatitude: null,
        mapLongitude: null,
        mapScale: 12,
        mappableCount: 0,
      };
    }
    const latitudes = mappable.map((item) => item.latitude);
    const longitudes = mappable.map((item) => item.longitude);
    return {
      mapReady: true,
      mapMarkers: markers,
      mapLatitude: (Math.min(...latitudes) + Math.max(...latitudes)) / 2,
      mapLongitude: (Math.min(...longitudes) + Math.max(...longitudes)) / 2,
      mapScale: mapScaleFor(mappable),
      mappableCount: mappable.length,
    };
  },

  selectCommunity(item) {
    if (!item || !item.hasCoordinates) return;
    this.setData({
      selectedCommunity: item,
      mapLatitude: item.latitude,
      mapLongitude: item.longitude,
      mapScale: 15,
    });
  },

  onMarkerTap(event) {
    const markerId = String(event && event.detail && event.detail.markerId || '');
    const communityKey = this._markerCommunityById[markerId];
    const item = this._allCommunities.find(
      (community) => stableCommunityKey(community) === communityKey
    );
    this.selectCommunity(item);
  },

  focusCommunity(event) {
    const communityId = String(
      event && event.currentTarget && event.currentTarget.dataset
        && event.currentTarget.dataset.communityId || ''
    );
    const item = this._allCommunities.find(
      (community) => stableCommunityKey(community) === communityId
    );
    this.selectCommunity(item);
  },

  resetMapView() {
    this.setData({
      ...this.buildMapState(this.data.communities),
      selectedCommunity: null,
    });
  },

  retry() {
    this.loadData({ force: true, revalidate: true });
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
