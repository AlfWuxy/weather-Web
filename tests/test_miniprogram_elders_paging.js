const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

test('小程序加载更多保留第一页，失败可重试，刷新回到第一页', async () => {
  let page;
  let fail = false;
  const calls = [];
  const context = {
    require: () => ({ api: async (options) => {
      calls.push(options);
      if (fail) throw new Error('offline');
      const number = Number(options.path.split('page=')[1]);
      return { data: [{ pair_id: number }], page: number, has_more: number === 1 };
    } }),
    Page: (value) => { page = value; },
    wx: { getStorageSync: () => 'test-token', showToast: () => {} },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../miniprogram/pages/elders/index.js'), 'utf8'), context);
  page.setData = (data) => Object.assign(page.data, data);
  await page.onShow();
  assert.equal(page.data.elders.length, 1);
  assert.equal(page.data.hasMore, true);
  fail = true;
  await page.loadMore();
  assert.equal(page.data.page, 1);
  assert.equal(page.data.elders.length, 1);
  fail = false;
  await page.loadMore();
  assert.equal(page.data.elders.length, 2);
  assert.equal(page.data.elders[1].pair_id, 2);
  assert.equal(page.data.hasMore, false);
  const count = calls.length;
  await page.loadMore();
  assert.equal(calls.length, count);
  await page.onShow();
  assert.equal(page.data.elders.length, 1);
  assert.ok(calls.every((options) => options.includeMeta));
});

test('request 辅助函数仅在显式请求时返回分页元数据', async () => {
  const context = {
    require: () => ({ API_BASE_URL: 'https://example.invalid' }),
    module: { exports: {} },
    wx: { request: (options) => options.success({ statusCode: 200,
      data: { success: true, data: [{ pair_id: 1 }], page: 1, has_more: true } }) },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../miniprogram/utils/request.js'), 'utf8'), context);
  const api = context.module.exports.api;
  assert.equal((await api({ path: '/elders' })).length, 1);
  const result = await api({ path: '/elders', includeMeta: true });
  assert.equal(result.has_more, true);
  assert.equal(result.data.length, 1);
});

for (const pageName of ['elder-edit', 'template']) {
  test(`${pageName} 可通过真实请求辅助函数定位第一页之外的家人`, async () => {
    let page;
    const calls = [];
    const elders = Array.from({ length: 25 }, (_, index) => ({
      pair_id: 25 - index, community_code: '九江',
      member: { name: `家人${25 - index}` }, today: {},
    }));
    const wxMock = {
      getStorageSync: () => 'test-token',
      showToast: () => assert.fail('有效详情不应显示加载失败'),
      request: (options) => {
        calls.push(options);
        const url = new URL(options.url);
        const target = Number(url.searchParams.get('pair_id'));
        const rows = target ? elders.filter((item) => item.pair_id === target) : elders.slice(0, 20);
        options.success({ statusCode: 200, data: { success: true, data: rows, page: 1, has_more: !target } });
      },
    };
    const requestContext = { require: () => ({ API_BASE_URL: 'https://example.invalid' }), module: { exports: {} }, wx: wxMock };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../miniprogram/utils/request.js'), 'utf8'), requestContext);
    const context = { require: () => requestContext.module.exports, Page: (value) => { page = value; }, wx: wxMock };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, `../miniprogram/pages/${pageName}/index.js`), 'utf8'), context);
    page.setData = (data) => Object.assign(page.data, data);
    await page.onLoad({ pair_id: '1' });
    assert.equal(calls.length, 1);
    assert.equal(new URL(calls[0].url).searchParams.get('pair_id'), '1');
    assert.equal(calls[0].header.Authorization, 'Bearer test-token');
    assert.equal(pageName === 'elder-edit' ? page.data.name : page.data.elderName, '家人1');
  });
}
