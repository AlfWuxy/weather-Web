const test = require('node:test');
const assert = require('node:assert/strict');

const publicDataPath = require.resolve('../utils/public-data');
const previousPublicDataModule = require.cache[publicDataPath];
let getCommunityCalls = 0;
require.cache[publicDataPath] = {
  id: publicDataPath,
  filename: publicDataPath,
  loaded: true,
  exports: {
    getCommunity: async () => {
      getCommunityCalls += 1;
      return { data: { cooling: [] }, meta: {} };
    },
  },
};

let modalCalls;
let locationCalls;
let openLocationCalls;
let storageWrites;
let toastCalls;

function loadCoolingPageDefinition() {
  const pagePath = require.resolve('../pages/cooling/index');
  const previousPage = global.Page;
  let definition;
  try {
    global.Page = (candidate) => { definition = candidate; };
    delete require.cache[pagePath];
    require(pagePath);
  } finally {
    global.Page = previousPage;
  }
  return definition;
}

function makePage() {
  const definition = loadCoolingPageDefinition();
  const page = Object.assign({}, definition);
  page.data = JSON.parse(JSON.stringify(definition.data));
  page.setData = function setData(next, callback) {
    Object.assign(this.data, next);
    if (typeof callback === 'function') callback();
  };
  page.onLoad.call(page);
  return page;
}

function renderSample(page) {
  page.renderResources({
    data: {
      coordinate_system: 'GCJ-02',
      cooling: [
        {
          id: 'missing',
          name: '无坐标活动室',
          community_code: '甲社区',
          has_ac: true,
        },
        {
          id: 'far',
          name: '较远图书馆',
          community_code: '乙社区',
          coordinate_system: 'GCJ-02',
          latitude: 29.3,
          longitude: 116.02,
          has_ac: true,
        },
        {
          id: 'near',
          name: '附近服务站',
          community_code: '甲社区',
          coordinate_system: 'GCJ-02',
          latitude: 29.3,
          longitude: 116.001,
          is_accessible: true,
        },
      ],
    },
    meta: {},
  });
}

test.beforeEach(() => {
  modalCalls = [];
  locationCalls = [];
  openLocationCalls = [];
  storageWrites = [];
  toastCalls = [];
  getCommunityCalls = 0;
  global.wx = {
    getLocation: (options) => { locationCalls.push(options); },
    makePhoneCall: () => {},
    openLocation: (options) => { openLocationCalls.push(options); },
    setClipboardData: () => {},
    setStorageSync: (...args) => { storageWrites.push(args); },
    showModal: (options) => { modalCalls.push(options); },
    showShareMenu: () => {},
    showToast: (options) => { toastCalls.push(options); },
    stopPullDownRefresh: () => {},
  };
});

test.after(() => {
  if (previousPublicDataModule) require.cache[publicDataPath] = previousPublicDataModule;
  else delete require.cache[publicDataPath];
});

test('每次点击先自定义确认，确认后仅用单次 GCJ-02 定位做端内排序', () => {
  const page = makePage();
  renderSample(page);

  page.startNearbyLocation.call(page);
  assert.equal(modalCalls.length, 1);
  assert.equal(locationCalls.length, 0);
  assert.equal(page.data.locationBusy, true);
  assert.match(modalCalls[0].content, /不会上传.*不会写入本机存储.*不会后台持续定位/);

  modalCalls[0].success({ confirm: true, cancel: false });
  assert.equal(locationCalls.length, 1);
  assert.equal(locationCalls[0].type, 'gcj02');
  locationCalls[0].success({
    latitude: 29.3005,
    longitude: 116.0005,
    accuracy: 22,
    altitude: 999,
  });

  assert.equal(page.data.locationMode, 'located');
  assert.deepEqual(page.data.resources.map((item) => item.id), ['near', 'far', 'missing']);
  assert.equal(page.data.resources[2].distanceText, '');
  assert.deepEqual(page._locationPoint, { latitude: 29.3005, longitude: 116.0005 });
  assert.doesNotMatch(JSON.stringify(page.data), /29\.3005|116\.0005|accuracy|altitude/);
  assert.deepEqual(page.onShareAppMessage.call(page), {
    title: '都昌县避暑资源',
    path: '/pages/cooling/index',
    imageUrl: '/assets/share/yilao-share-cover.jpg',
  });
  assert.equal(storageWrites.length, 0);
  assert.equal(getCommunityCalls, 0);

  page.startNearbyLocation.call(page);
  assert.equal(modalCalls.length, 2);
  assert.equal(locationCalls.length, 1);
  modalCalls[1].success({ confirm: false, cancel: true });
  assert.equal(page.data.locationMode, 'manual');
  assert.equal(page._locationPoint, null);
});

test('取消确认或定位失败后回退社区 picker，手选不读取设备位置', () => {
  const page = makePage();
  renderSample(page);

  page.startNearbyLocation.call(page);
  modalCalls[0].success({ confirm: false, cancel: true });
  assert.equal(page.data.locationMode, 'manual');
  assert.equal(locationCalls.length, 0);
  assert.deepEqual(page.data.communityOptions.slice().sort(), ['乙社区', '甲社区'].sort());

  const communityIndex = page.data.communityOptions.indexOf('甲社区');
  page.chooseCommunity.call(page, { detail: { value: String(communityIndex) } });
  assert.equal(page.data.selectedCommunity, '甲社区');
  assert.deepEqual(page.data.resources.map((item) => item.id), ['missing', 'near']);
  assert.equal(page.data.resources.every((item) => item.distanceMeters === null), true);
  assert.match(page.data.locationHint, /未使用设备定位/);

  page.startNearbyLocation.call(page);
  modalCalls[1].success({ confirm: true, cancel: false });
  assert.equal(locationCalls.length, 1);
  locationCalls[0].fail({ errMsg: 'getLocation:fail auth deny' });
  assert.equal(page.data.locationMode, 'manual');
  assert.equal(page.data.selectedCommunity, '');
  assert.equal(page._locationPoint, null);
  assert.match(page.data.locationHint, /手动选择社区/);
  assert.equal(storageWrites.length, 0);
});

test('定位排序后筛选保持距离顺序，缺坐标继续排在有坐标资源之后', () => {
  const page = makePage();
  renderSample(page);
  global.wx.getLocation = (options) => {
    locationCalls.push(options);
    options.success({ latitude: 29.3, longitude: 116 });
  };

  page.startNearbyLocation.call(page);
  modalCalls[0].success({ confirm: true, cancel: false });
  page.applyFilter.call(page, 'ac');

  assert.deepEqual(page.data.resources.map((item) => item.id), ['far', 'missing']);
  assert.ok(Number.isFinite(page.data.resources[0].distanceMeters));
  assert.equal(page.data.resources[1].distanceMeters, null);
});

test('onHide 和 onUnload 清除本次坐标并使迟到回调失效', () => {
  const page = makePage();
  renderSample(page);

  page.startNearbyLocation.call(page);
  modalCalls[0].success({ confirm: true, cancel: false });
  const delayedSuccess = locationCalls[0].success;
  page.onHide.call(page);
  assert.equal(page._locationPoint, null);
  assert.equal(page.data.locationMode, 'idle');
  assert.equal(page.data.resources.every((item) => item.distanceMeters === null), true);

  delayedSuccess({ latitude: 29.3005, longitude: 116.0005 });
  assert.equal(page._locationPoint, null);
  assert.equal(page.data.locationMode, 'idle');

  page.onUnload.call(page);
  assert.equal(page._locationPoint, null);
  assert.deepEqual(page._allResources, []);
});

test('有公共坐标的资源可打开微信地图，缺坐标资源保持诚实失败', () => {
  const page = makePage();
  renderSample(page);

  page.openResourceLocation.call(page, { currentTarget: { dataset: { id: 'near' } } });
  assert.equal(openLocationCalls.length, 1);
  assert.deepEqual({
    latitude: openLocationCalls[0].latitude,
    longitude: openLocationCalls[0].longitude,
    name: openLocationCalls[0].name,
    scale: openLocationCalls[0].scale,
  }, {
    latitude: 29.3,
    longitude: 116.001,
    name: '附近服务站',
    scale: 16,
  });

  page.openResourceLocation.call(page, { currentTarget: { dataset: { id: 'missing' } } });
  assert.equal(openLocationCalls.length, 1);
  assert.match(toastCalls.at(-1).title, /无法打开地图/);
});

test('坐标系缺失或不是 GCJ-02 的资源保留文字，但不参与距离排序和地图打开', () => {
  const page = makePage();
  page.renderResources({
    data: {
      coordinate_system: 'GCJ-02',
      cooling: [
        {
          id: 'wgs84',
          name: '坐标系不匹配的活动室',
          address: '仍应展示的地址',
          coordinate_system: 'WGS84',
          latitude: 29.3001,
          longitude: 116.0001,
        },
        {
          id: 'missing-system',
          name: '未声明坐标系的服务站',
          address: '另一条文字地址',
          latitude: 29.3002,
          longitude: 116.0002,
        },
        {
          id: 'gcj02',
          name: '有效坐标服务站',
          address: '可打开地图的地址',
          coordinate_system: 'GCJ-02',
          latitude: 29.31,
          longitude: 116.01,
        },
      ],
    },
    meta: {},
  });

  const resources = page.resourcesFor.call(page, 'all', '', { latitude: 29.3, longitude: 116 });
  assert.deepEqual(resources.map((item) => item.id), ['gcj02', 'wgs84', 'missing-system']);
  assert.equal(resources[0].hasCoordinates, true);
  assert.ok(Number.isFinite(resources[0].distanceMeters));
  assert.equal(resources[1].hasCoordinates, false);
  assert.equal(resources[1].distanceMeters, null);
  assert.equal(resources[1].name, '坐标系不匹配的活动室');
  assert.equal(resources[1].address, '仍应展示的地址');
  assert.equal(resources[1].coordinateSystem, 'WGS84');
  assert.equal(resources[2].hasCoordinates, false);
  assert.equal(resources[2].coordinateSystem, '');

  page.openResourceLocation.call(page, { currentTarget: { dataset: { id: 'wgs84' } } });
  page.openResourceLocation.call(page, { currentTarget: { dataset: { id: 'missing-system' } } });
  assert.equal(openLocationCalls.length, 0);
  assert.match(toastCalls.at(-1).title, /无法打开地图/);

  page.openResourceLocation.call(page, { currentTarget: { dataset: { id: 'gcj02' } } });
  assert.equal(openLocationCalls.length, 1);
  assert.equal(openLocationCalls[0].name, '有效坐标服务站');
});

test('没有真实资源时不请求定位并保留诚实空态提示', () => {
  const page = makePage();
  page.renderResources({ data: { cooling: [] }, meta: {} });
  page.startNearbyLocation.call(page);

  assert.equal(page.data.counts.all, 0);
  assert.deepEqual(page.data.resources, []);
  assert.equal(modalCalls.length, 0);
  assert.equal(locationCalls.length, 0);
  assert.match(page.data.locationHint, /暂无可排序的真实避暑资源/);
});

test('只有文字资源时直接手选社区且不申请定位', () => {
  const page = makePage();
  page.renderResources({
    data: {
      coordinate_system: 'GCJ-02',
      cooling: [
        {
          id: 'text-only',
          name: '待核验社区活动室',
          community_code: '甲社区',
          address_hint: '社区服务中心一楼',
        },
      ],
    },
    meta: {},
  });

  page.startNearbyLocation.call(page);

  assert.equal(page.data.locationMode, 'manual');
  assert.equal(modalCalls.length, 0);
  assert.equal(locationCalls.length, 0);
  assert.match(page.data.locationHint, /尚无已核验坐标.*不会读取设备位置/);
  assert.deepEqual(page.data.communityOptions, ['甲社区']);
});

test('确认弹窗期间坐标失效时取消定位并回退手选', () => {
  const page = makePage();
  renderSample(page);
  page.startNearbyLocation.call(page);
  assert.equal(modalCalls.length, 1);

  page.renderResources({
    data: {
      coordinate_system: 'GCJ-02',
      cooling: [
        {
          id: 'text-only-after-refresh',
          name: '刷新后仅文字的活动室',
          community_code: '甲社区',
        },
      ],
    },
    meta: {},
  });
  modalCalls[0].success({ confirm: true, cancel: false });

  assert.equal(locationCalls.length, 0);
  assert.equal(page.data.locationMode, 'manual');
  assert.match(page.data.locationHint, /已清除本次位置/);
});

test('定位成功后资源失去核验坐标会立即清除本次位置', () => {
  const page = makePage();
  renderSample(page);
  page.startNearbyLocation.call(page);
  modalCalls[0].success({ confirm: true, cancel: false });
  locationCalls[0].success({ latitude: 29.3005, longitude: 116.0005 });
  assert.equal(page.data.locationMode, 'located');
  assert.deepEqual(page._locationPoint, { latitude: 29.3005, longitude: 116.0005 });

  page.renderResources({
    data: {
      coordinate_system: 'GCJ-02',
      cooling: [
        {
          id: 'text-only-after-location',
          name: '核验状态变化后的活动室',
          community_code: '甲社区',
        },
      ],
    },
    meta: {},
  });

  assert.equal(page._locationPoint, null);
  assert.equal(page.data.locationMode, 'manual');
  assert.equal(page.data.resources[0].distanceMeters, null);
  assert.match(page.data.locationHint, /已清除本次位置/);
});
