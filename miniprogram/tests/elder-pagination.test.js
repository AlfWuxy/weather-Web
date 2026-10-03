const test = require('node:test');
const assert = require('node:assert/strict');

// 保留真实 session、授权 guard、API 包装与页面，仅替代微信网络和存储边界。
const storage = new Map();
let definition;
let pages = [];
let respond;
let paths = [];
let navigation = [];
global.Page = (value) => { definition = value; };
global.getApp = () => ({ globalData: {} });
global.getCurrentPages = () => pages;
global.wx = {
  getStorageSync: (key) => storage.get(key),
  setStorageSync: (key, value) => storage.set(key, value),
  removeStorageSync: (key) => storage.delete(key),
  showToast: () => {},
  showModal: () => {},
  navigateTo: ({ url }) => navigation.push(url),
  redirectTo: ({ url }) => navigation.push(url),
  reLaunch: ({ url, complete }) => { navigation.push(url); if (complete) complete(); },
  switchTab: () => {},
  request: (options) => {
    const url = new URL(options.url);
    paths.push(url.pathname + url.search);
    Promise.resolve().then(() => respond(url, options)).then(
      (response) => options.success(response),
      (error) => options.fail(error)
    );
    return { abort() {} };
  },
};
const session = require('../pages/elders/care-session');
const ok = (data, extra = {}) => ({ statusCode: 200, data: { success: true, data, ...extra } });
const elder = (pairId) => ({ pair_id: pairId, member: { name: `家人${pairId}`, age: 70 }, today: {} });

function page(name, data = {}) {
  const modulePath = require.resolve(`../pages/${name}/index`);
  delete require.cache[modulePath];
  require(modulePath);
  const result = { ...definition, data: { ...JSON.parse(JSON.stringify(definition.data)), ...data } };
  result.setData = (patch) => Object.assign(result.data, patch);
  pages = [result];
  return result;
}

function reset(token) {
  session.clear();
  session.saveToken(token, { privacy_consent_version: 'privacy-v1', login_method: 'wechat' });
  paths = [];
  navigation = [];
  pages = [];
}

function commonResponse(url) {
  if (url.pathname.endsWith('/health-consent')) return ok({ health_consent_current: true });
  return ok({});
}

test('真实授权守卫完成首屏后仍读取第二页，重复点击合并且撤回同意清空列表', async () => {
  reset('pagination-session');
  let secondResolve;
  respond = (url) => {
    if (!url.pathname.endsWith('/elders')) return commonResponse(url);
    if (url.searchParams.get('page') === '2') {
      return new Promise((resolve) => { secondResolve = resolve; });
    }
    return ok(Array.from({ length: 20 }, (_, i) => elder(i + 1)), { page: 1, has_more: true });
  };
  const current = page('elders');
  await current.onShow();
  assert.equal(current._healthConsentLoadedOnce, true);
  assert.equal(current.data.elders.length, 20);
  const first = current.loadMoreElders();
  const duplicate = current.loadMoreElders();
  // 网络模拟保持挂起，验证 guard 确实发起了第二页请求。
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(paths.filter((value) => value === '/mp/api/v1/elders?page=2').length, 1);
  secondResolve(ok([elder(21)], { page: 2, has_more: true }));
  await Promise.all([first, duplicate]);
  assert.equal(current.data.elders.length, 21);
  assert.equal(current.data.elders[20].pair_id, 21);
  assert.equal(current.data.eldersPage, 2);
  respond = (url) => url.pathname.endsWith('/elders')
    ? { statusCode: 428, data: { success: false, error: 'health_sensitive_consent_required' } }
    : commonResponse(url);
  await current.loadMoreElders();
  assert.deepEqual(current.data.elders, []);
  assert.equal(current.data.eldersHasMore, false);
  assert.match(navigation.at(-1), /health-consent/);
});

for (const [name, loader] of [
  ['medications', 'loadMedications'], ['elder-edit', 'loadElder'], ['diary', 'loadDiary'],
  ['health-assessment', 'loadPage'], ['template', 'loadTemplate'], ['action-checkin', 'loadContext'],
]) {
  test(`${name} 精确读取第二页绑定，找不到时关闭详情上下文`, async () => {
    reset(`detail-${name}`);
    let found = true;
    respond = (url) => {
      if (!url.pathname.endsWith('/elders')) return commonResponse(url);
      // 第一页故意没有 21；只有精确过滤才能取得目标绑定。
      return ok(url.searchParams.get('pair_id') === '21'
        ? (found ? [elder(21)] : []) : [elder(1)]);
    };
    const current = page(name, { pairId: 21, mode: 'edit' });
    current.requestedPairId = 21;
    await session.guardHealthSensitivePage(current, () => current[loader]());
    assert.equal(current.data.contextReady, true, current.data.loadError);
    assert.ok(paths.includes('/mp/api/v1/elders?pair_id=21'));
    found = false;
    const missing = page(name, { pairId: 21, mode: 'edit' });
    missing.requestedPairId = 21;
    await session.guardHealthSensitivePage(missing, () => missing[loader]());
    assert.equal(missing.data.contextReady, false);
  });
}
