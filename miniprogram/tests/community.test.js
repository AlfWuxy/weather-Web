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

  assert.deepEqual(page.data.allCommunities.map((item) => item.name), [
    '甲社区',
    '丁社区',
    '乙社区',
    '丙社区',
    '己社区',
    '戊社区',
  ]);
  assert.deepEqual(page.data.allCommunities.map((item) => item.globalRank), [1, 2, 3, 4, 5, 6]);
  page.applyFilter('high');
  assert.deepEqual(page.data.communities.map((item) => item.name), ['甲社区', '丁社区', '丙社区']);
  assert.deepEqual(page.data.communities.map((item) => item.globalRank), [1, 2, 4]);

  const view = fs.readFileSync(path.join(__dirname, '..', 'pages/community/index.wxml'), 'utf8');
  assert.match(view, /全县排名第 \{\{item\.globalRank\}\}/);
  assert.doesNotMatch(view, /class="rank">\{\{index \+ 1\}\}/);
});
