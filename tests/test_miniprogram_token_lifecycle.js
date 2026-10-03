const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadPage(pageName, failure) {
  let page;
  const calls = [];
  const source = fs.readFileSync(path.join(__dirname, '..', 'miniprogram', 'pages', pageName, 'index.js'), 'utf8');
  const context = {
    require: () => ({ api: async (request) => { calls.push(['api', request]); if (failure) throw new Error(failure); } }),
    Page: (value) => { page = value; },
    wx: {
      getStorageSync: () => 'active-token',
      removeStorageSync: (key) => calls.push(['remove', key]),
      reLaunch: () => calls.push(['navigate']),
      showModal: (data) => calls.push(['modal', data]),
    },
  };
  vm.runInNewContext(source, context);
  page.setData = (values) => Object.assign(page.data, values);
  return { page, calls };
}

for (const [pageName, handler] of [['settings', 'logout'], ['bind-token', 'onClear']]) {
  test(`${pageName}: 先获服务端撤销确认，再清本地凭证`, async () => {
    const { page, calls } = loadPage(pageName);
    await page[handler]();
    assert.equal(calls[0][0], 'api');
    assert.equal(calls[0][1].path, '/mp/api/v1/token/revoke');
    assert.equal(calls[0][1].token, 'active-token');
    assert.equal(calls[1][0], 'remove');
  });
  test(`${pageName}: 网络失败保留重试能力且明确提示未完成`, async () => {
    const { page, calls } = loadPage(pageName, 'network_failed');
    await page[handler]();
    assert.equal(calls.some(([type]) => type === 'remove' || type === 'navigate'), false);
    assert.equal(calls[1][0], 'modal');
    assert.match(calls[1][1].content, /撤销未完成/);
    assert.equal(page.data.busy, false);
  });
  test(`${pageName}: 已失效凭证允许清理本地绑定`, async () => {
    const { page, calls } = loadPage(pageName, 'unauthorized');
    await page[handler]();
    assert.equal(calls.some(([type]) => type === 'remove'), true);
  });
}
