const { getCommunity } = require('../../utils/public-data');
const { freshnessView, normalizeCommunity } = require('../../utils/format');
const {
  normalizePoint,
  sortResourcesByDistance,
} = require('../../utils/location-distance');
const {
  beginPublicPage,
  hidePublicPage,
  pageCanRender,
  schedulePublicRefresh,
  showPublicPage,
  unloadPublicPage,
} = require('../../utils/public-page-lifecycle');
const { createPageShare, createTimelineShare, showPublicShareMenu } = require('../../utils/share');

const LOCATION_IDLE_HINT = '定位默认关闭。你可逐次确认本次定位，或直接手动选择社区。';

function hasGcj02Coordinates(resource) {
  const source = resource && typeof resource === 'object' ? resource : {};
  return source.coordinateSystem === 'GCJ-02' && Boolean(normalizePoint(source));
}

function resourceForLocation(resource) {
  if (hasGcj02Coordinates(resource)) return resource;
  // 坐标系缺失或不匹配时保留文字资料，只关闭距离和原生地图能力。
  return Object.assign({}, resource, { latitude: null, longitude: null });
}

Page({
  data: {
    loading: true,
    error: '',
    resources: [],
    freshness: {},
    filter: 'all',
    counts: { all: 0, ac: 0, accessible: 0 },
    locationMode: 'idle',
    locationBusy: false,
    locationHint: LOCATION_IDLE_HINT,
    communityOptions: [],
    communityIndex: 0,
    selectedCommunity: '',
  },

  onLoad() {
    this._allResources = [];
    this._locationPoint = null;
    this._locationFlowToken = 0;
    this._locationFlowActive = false;
    beginPublicPage(this);
    showPublicShareMenu();
  },

  onShow() {
    showPublicPage(this, () => this.loadData());
  },

  onHide() {
    this.clearLocationUse();
    hidePublicPage(this);
  },

  onUnload() {
    this._locationFlowToken = (Number(this._locationFlowToken) || 0) + 1;
    this._locationFlowActive = false;
    this._locationPoint = null;
    unloadPublicPage(this);
    this._allResources = [];
  },

  async onPullDownRefresh() {
    await this.loadData({ force: true });
    wx.stopPullDownRefresh();
  },

  async loadData(options) {
    if (!Array.isArray(this._allResources) || !this._allResources.length) {
      this.setData({ loading: true, error: '' });
    }
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
    const communityOptions = Array.from(new Set(
      allResources.map((item) => String(item.community || '').trim()).filter(Boolean)
    )).sort((left, right) => left.localeCompare(right, 'zh-CN'));
    this._allResources = allResources;
    const coordinatesAvailable = allResources.some(hasGcj02Coordinates);
    let locationReset = {};
    if (
      !coordinatesAvailable
      && (this._locationPoint || this._locationFlowActive || this.data.locationMode === 'located')
    ) {
      this._locationFlowToken = (Number(this._locationFlowToken) || 0) + 1;
      this._locationFlowActive = false;
      this._locationPoint = null;
      locationReset = {
        locationMode: 'manual',
        locationBusy: false,
        locationHint: '资源坐标刚刚更新，已清除本次位置，请手动选择社区。',
      };
    }

    const selectedCommunity = communityOptions.includes(this.data.selectedCommunity)
      ? this.data.selectedCommunity
      : '';
    const communityIndex = selectedCommunity
      ? communityOptions.indexOf(selectedCommunity)
      : 0;
    const resources = this.resourcesFor(
      this.data.filter,
      selectedCommunity,
      this._locationPoint
    );
    this.setData({
      loading: false,
      error: '',
      resources,
      counts: {
        all: allResources.length,
        ac: allResources.filter((item) => item.hasAc).length,
        accessible: allResources.filter((item) => item.accessible).length,
      },
      freshness: freshnessView(result.meta, normalized),
      communityOptions,
      communityIndex,
      selectedCommunity,
      ...locationReset,
    });
    schedulePublicRefresh(this, result.meta, () => this.loadData());
  },

  chooseFilter(event) {
    this.applyFilter(event.currentTarget.dataset.filter);
  },

  applyFilter(filter) {
    const resources = this.filteredResources(filter);
    this.setData({ filter, resources });
  },

  resourcesFor(filter, selectedCommunity, locationPoint) {
    let resources = Array.isArray(this._allResources) ? this._allResources : [];
    if (selectedCommunity) {
      resources = resources.filter((item) => item.community === selectedCommunity);
    }
    if (filter === 'ac') resources = resources.filter((item) => item.hasAc);
    if (filter === 'accessible') resources = resources.filter((item) => item.accessible);
    return sortResourcesByDistance(resources.map(resourceForLocation), locationPoint);
  },

  filteredResources(filter) {
    return this.resourcesFor(filter, this.data.selectedCommunity, this._locationPoint);
  },

  startNearbyLocation() {
    if (this._locationFlowActive) return;
    if (!Array.isArray(this._allResources) || !this._allResources.length) {
      this.setData({ locationHint: '暂无可排序的真实避暑资源，请联系当地社区确认可用场所。' });
      return;
    }
    if (!this._allResources.some(hasGcj02Coordinates)) {
      this.activateManualFallback('当前资源尚无已核验坐标，已切换为手动选择社区；不会读取设备位置。');
      return;
    }
    if (typeof wx.showModal !== 'function') {
      this.activateManualFallback('当前无法显示定位确认，请手动选择社区。');
      return;
    }

    const flowToken = (Number(this._locationFlowToken) || 0) + 1;
    this._locationFlowToken = flowToken;
    this._locationFlowActive = true;
    this.setData({
      locationBusy: true,
      locationHint: '请先确认本次是否使用当前位置。',
    });
    wx.showModal({
      title: '本次使用当前位置？',
      content: '仅在本页按直线距离排列避暑资源。位置不会上传，不会写入本机存储，也不会后台持续定位，离开页面后清除。',
      confirmText: '仅本次',
      cancelText: '手动选择',
      success: (result) => {
        if (!this.locationFlowCanContinue(flowToken)) return;
        if (!result || !result.confirm) {
          this.activateManualFallback('已取消本次定位，请手动选择社区。');
          return;
        }
        this.requestCurrentLocation(flowToken);
      },
      fail: () => {
        if (!this.locationFlowCanContinue(flowToken)) return;
        this.activateManualFallback('定位确认暂不可用，请手动选择社区。');
      },
    });
  },

  locationFlowCanContinue(flowToken) {
    return Boolean(
      this._locationFlowActive
      && this._locationFlowToken === flowToken
      && pageCanRender(this)
    );
  },

  requestCurrentLocation(flowToken) {
    if (!Array.isArray(this._allResources) || !this._allResources.some(hasGcj02Coordinates)) {
      this.activateManualFallback('资源坐标刚刚更新，请手动选择社区；本次未读取设备位置。');
      return;
    }
    if (typeof wx.getLocation !== 'function') {
      this.activateManualFallback('当前设备无法取得位置，请手动选择社区。');
      return;
    }
    this.setData({ locationHint: '正在读取本次位置…' });
    wx.getLocation({
      type: 'gcj02',
      success: (result) => {
        if (!this.locationFlowCanContinue(flowToken)) return;
        // 只提取本次排序需要的两项坐标，不保留微信接口的完整返回对象。
        const point = normalizePoint({
          latitude: result && result.latitude,
          longitude: result && result.longitude,
        });
        if (!point) {
          this.activateManualFallback('本次位置无效，请手动选择社区。');
          return;
        }
        this._locationPoint = point;
        this._locationFlowActive = false;
        this.setData({
          locationMode: 'located',
          locationBusy: false,
          locationHint: '已按本次当前位置估算直线距离，离开页面后自动清除。',
          selectedCommunity: '',
          communityIndex: 0,
          resources: this.resourcesFor(this.data.filter, '', point),
        });
      },
      fail: () => {
        if (!this.locationFlowCanContinue(flowToken)) return;
        this.activateManualFallback('未取得本次位置，请手动选择社区继续查看。');
      },
    });
  },

  showManualSelection() {
    this.activateManualFallback('请手动选择社区；此方式不会读取设备位置。');
  },

  activateManualFallback(message) {
    this._locationFlowToken = (Number(this._locationFlowToken) || 0) + 1;
    this._locationFlowActive = false;
    this._locationPoint = null;
    this.setData({
      locationMode: 'manual',
      locationBusy: false,
      locationHint: message || '请手动选择社区。',
      selectedCommunity: '',
      communityIndex: 0,
      resources: this.resourcesFor(this.data.filter, '', null),
    });
  },

  chooseCommunity(event) {
    const index = Number(event && event.detail && event.detail.value);
    const options = Array.isArray(this.data.communityOptions) ? this.data.communityOptions : [];
    if (!Number.isInteger(index) || index < 0 || index >= options.length) return;
    const selectedCommunity = options[index];
    this._locationFlowToken = (Number(this._locationFlowToken) || 0) + 1;
    this._locationFlowActive = false;
    this._locationPoint = null;
    this.setData({
      locationMode: 'manual',
      locationBusy: false,
      locationHint: `正在查看${selectedCommunity}的已录入资源，未使用设备定位。`,
      selectedCommunity,
      communityIndex: index,
      resources: this.resourcesFor(this.data.filter, selectedCommunity, null),
    });
  },

  clearManualCommunity() {
    this.activateManualFallback('已显示全部社区资源，未使用设备定位。');
  },

  clearLocationUse() {
    this._locationFlowToken = (Number(this._locationFlowToken) || 0) + 1;
    this._locationFlowActive = false;
    this._locationPoint = null;
    if (typeof this.setData !== 'function') return;
    this.setData({
      locationMode: 'idle',
      locationBusy: false,
      locationHint: LOCATION_IDLE_HINT,
      selectedCommunity: '',
      communityIndex: 0,
      resources: this.resourcesFor(this.data.filter, '', null),
    });
  },

  findResource(resourceId) {
    const allResources = Array.isArray(this._allResources) ? this._allResources : [];
    return allResources.find((item) => String(item.id) === String(resourceId));
  },

  copyAddress(event) {
    const resource = this.findResource(event.currentTarget.dataset.id);
    if (!resource) return;
    wx.setClipboardData({ data: `${resource.name}\n${resource.address}\n${resource.hours}` });
  },

  openResourceLocation(event) {
    const resource = this.findResource(event.currentTarget.dataset.id);
    const point = hasGcj02Coordinates(resource) ? normalizePoint(resource) : null;
    if (!resource || !point || typeof wx.openLocation !== 'function') {
      if (typeof wx.showToast === 'function') wx.showToast({ title: '该地点暂无法打开地图', icon: 'none' });
      return;
    }
    try {
      wx.openLocation({
        latitude: point.latitude,
        longitude: point.longitude,
        name: resource.name,
        address: resource.address,
        scale: 16,
        fail: () => {
          if (typeof wx.showToast === 'function') wx.showToast({ title: '地图暂时无法打开', icon: 'none' });
        },
      });
    } catch (error) {
      if (typeof wx.showToast === 'function') wx.showToast({ title: '地图暂时无法打开', icon: 'none' });
    }
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
