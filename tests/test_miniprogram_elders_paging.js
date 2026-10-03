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
