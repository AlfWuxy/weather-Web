const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

function loadCommunityPageDefinition() {
  const pagePath = require.resolve('../pages/community/index');
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

function pageInstance(definition) {
  const instance = Object.assign({}, definition);
  instance.data = JSON.parse(JSON.stringify(definition.data));
  instance.setData = function setData(next, callback) {
    Object.assign(this.data, next);
    if (typeof callback === 'function') callback();
  };
  return instance;
}

test('社区筛选后继续显示完整列表中的全局排名', () => {
  const page = pageInstance(loadCommunityPageDefinition());
  page.renderCommunities({
    data: {
      communities: [
        { id: 'high', name: '甲社区', risk_score: 90, risk_level: '高风险' },
        { id: 'score-b', name: '乙社区', risk_score: 80, risk_level: '中等脆弱性' },
        { id: 'score-a', name: '丁社区', risk_score: 80, risk_level: '高脆弱性' },
        { id: 'lower', name: '丙社区', risk_score: 70, risk_level: '高脆弱性' },
        { id: 'missing-z', name: '戊社区' },
        { id: 'missing-y', name: '己社区' },
      ],
    },
    meta: {},
  });

  assert.equal(Object.hasOwn(page.data, 'allCommunities'), false);
  assert.deepEqual(page._allCommunities.map((item) => item.name), [
    '甲社区',
    '丁社区',
    '乙社区',
    '丙社区',
    '己社区',
    '戊社区',
  ]);
  assert.deepEqual(page._allCommunities.map((item) => item.globalRank), [1, 2, 3, 4, 5, 6]);
  page.applyFilter('high');
  assert.deepEqual(page.data.communities.map((item) => item.name), ['甲社区', '丁社区', '丙社区']);
  assert.deepEqual(page.data.communities.map((item) => item.globalRank), [1, 2, 4]);

  const view = fs.readFileSync(path.join(__dirname, '..', 'pages/community/index.wxml'), 'utf8');
  assert.match(view, /全县排名第 \{\{item\.globalRank\}\}/);
  assert.match(view, /wx:if="\{\{counts\.all\}\}"/);
  assert.doesNotMatch(view, /allCommunities/);
  assert.doesNotMatch(view, /class="rank">\{\{index \+ 1\}\}/);
});

test('社区与避暑全集只保存在非响应式字段', () => {
  const community = pageInstance(loadCommunityPageDefinition());
  community.renderCommunities({
    data: { communities: [{ id: 'one', name: '甲社区', risk_score: 90 }] },
    meta: {},
  });
  assert.equal(Object.hasOwn(community.data, 'allCommunities'), false);
  assert.equal(community._allCommunities.length, 1);

  const cooling = pageInstance(loadCoolingPageDefinition());
  cooling.renderResources({
    data: { cooling: [{ id: 'one', name: '社区中心', has_ac: true }] },
    meta: {},
  });
  assert.equal(Object.hasOwn(cooling.data, 'allResources'), false);
  assert.equal(cooling._allResources.length, 1);
  assert.equal(cooling.data.resources.length, 1);

  const coolingView = fs.readFileSync(path.join(__dirname, '..', 'pages/cooling/index.wxml'), 'utf8');
  assert.match(coolingView, /wx:if="\{\{counts\.all\}\}"/);
  assert.doesNotMatch(coolingView, /allResources/);
});

test('社区原生地图不请求定位并可从标记或列表查看聚合脆弱性', () => {
  const page = pageInstance(loadCommunityPageDefinition());
  page.renderCommunities({
    data: {
      communities: [
        {
          id: 'mapped',
          name: '已维护社区',
          vulnerability_index: 0.82,
          elderly_ratio: 0.31,
          latitude: 29.331309,
          longitude: 116.204529,
          coordinate_system: 'GCJ-02',
        },
        {
          id: 'missing',
          name: '坐标待维护社区',
          vulnerability_index: 0.2,
          latitude: 29.27,
          longitude: 116.2,
        },
      ],
    },
    meta: {},
  });

  assert.equal(page.data.mapReady, true);
  assert.equal(page.data.mappableCount, 1);
  assert.equal(page.data.mapMarkers.length, 1);
  assert.equal(page.data.mapMarkers[0].title, '已维护社区');
  assert.equal(page.data.mapMarkers[0].callout.content, '已维护社区');
  assert.equal(Object.hasOwn(page.data.mapMarkers[0], 'score'), false);
  assert.equal(Object.hasOwn(page.data.mapMarkers[0], 'risk'), false);

  page.onMarkerTap({ detail: { markerId: 1 } });
  assert.equal(page.data.selectedCommunity.name, '已维护社区');
  assert.equal(page.data.selectedCommunity.metricLabel, '脆弱性指数');

  page.resetMapView();
  assert.equal(page.data.selectedCommunity, null);
  page.focusCommunity({ currentTarget: { dataset: { communityId: 'mapped' } } });
  assert.equal(page.data.selectedCommunity.name, '已维护社区');

  page.applyFilter('low');
  assert.equal(page.data.communities.length, 1);
  assert.equal(page.data.communities[0].name, '坐标待维护社区');
  assert.equal(page.data.mapReady, false);
  assert.equal(page.data.mappableCount, 0);

  const pageSource = fs.readFileSync(path.join(__dirname, '..', 'pages/community/index.js'), 'utf8');
  const view = fs.readFileSync(path.join(__dirname, '..', 'pages/community/index.wxml'), 'utf8');
  assert.match(view, /<map[\s\S]*markers="\{\{mapMarkers\}\}"[\s\S]*bindmarkertap="onMarkerTap"/);
  assert.match(view, /aria-label="社区位置地图/);
  assert.match(view, /bindtap="focusCommunity"/);
  assert.match(view, /位置坐标待维护/);
  assert.doesNotMatch(view, /show-location/);
  assert.doesNotMatch(pageSource, /getLocation|chooseLocation/);
});
