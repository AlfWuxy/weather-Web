const test = require('node:test');
const assert = require('node:assert/strict');

const originalNow = Date.now;
let now = 1_784_242_800_000;
Date.now = () => now;

const configPath = require.resolve('../config');
require.cache[configPath] = {
  id: configPath,
  filename: configPath,
  loaded: true,
  exports: {
    API_BASE_URL: 'https://api.example.com',
    PUBLIC_CACHE_TTL_MS: 30 * 60 * 1000,
    REQUEST_TIMEOUT_MS: 12000,
    GIS_REQUEST_TIMEOUT_MS: 30000,
  },
};

const storage = new Map();
let requestCalls = 0;
let requestFailure = false;

function flushAsync() {
  return new Promise((resolve) => setImmediate(resolve));
}

function iso(timestamp) {
  return new Date(timestamp).toISOString();
}

global.wx = {
  getStorageSync: (key) => storage.get(key),
  setStorageSync: (key, value) => storage.set(key, value),
  request: (options) => {
    requestCalls += 1;
    const call = requestCalls;
    Promise.resolve().then(() => {
      if (requestFailure) {
        options.fail({ errMsg: 'offline' });
        return;
      }
      const expiresAt = call === 1 ? now + 60 * 1000 : now - 1000;
      options.success({
        statusCode: 200,
        data: {
          success: true,
          data: {
            snapshot_id: `snapshot-${call}`,
            fetched_at: iso(now - 29 * 60 * 1000),
            expires_at: iso(expiresAt),
            stale: expiresAt <= now,
          },
        },
      });
    });
  },
};

const { getBootstrap, getCommunity } = require('../utils/public-data');

test.after(() => { Date.now = originalNow; });

test('真实 getBootstrap 覆盖 absolute expiry、并发去重和 stale backoff', async () => {
  Date.now = () => now;
  storage.clear();
  requestCalls = 0;
  requestFailure = false;

  const first = getBootstrap();
  const concurrent = getBootstrap();
  const [firstResult, concurrentResult] = await Promise.all([first, concurrent]);
  assert.equal(requestCalls, 1);
  assert.equal(firstResult.data.snapshot_id, 'snapshot-1');
  assert.equal(concurrentResult.data.snapshot_id, 'snapshot-1');
  assert.equal(firstResult.meta.effectiveExpiresAt, now + 60 * 1000);

  now += 59 * 1000;
  const stillFresh = await getBootstrap({ force: true });
  assert.equal(requestCalls, 1);
  assert.equal(stillFresh.meta.refreshDeferred, true);

  now += 2 * 1000;
  const staleNetworkResult = await getBootstrap();
  assert.equal(staleNetworkResult.meta.source, 'stale-cache');
  assert.equal(requestCalls, 2);
  assert.equal(staleNetworkResult.meta.stale, true);
  assert.equal(staleNetworkResult.meta.refreshStarted, true);
  const revalidated = await staleNetworkResult.revalidated;
  assert.equal(revalidated.data.snapshot_id, 'snapshot-2');
  assert.equal(revalidated.meta.refreshStarted, false);

  const nextPage = await getBootstrap();
  assert.equal(requestCalls, 2);
  assert.equal(nextPage.meta.source, 'stale-cache');
  assert.equal(nextPage.meta.stale, true);

  now += 60 * 1000 + 1;
  await getBootstrap();
  await flushAsync();
  assert.equal(requestCalls, 3);
});

test('社区旧数据刷新失败后 60 秒内不重复请求', async () => {
  Date.now = () => now;
  storage.clear();
  requestCalls = 0;
  requestFailure = false;

  const first = await getCommunity();
  assert.equal(first.meta.source, 'network');
  assert.equal(requestCalls, 1);

  now += 30 * 60 * 1000 + 1;
  requestFailure = true;
  const stale = await getCommunity();
  assert.equal(stale.meta.source, 'stale-cache');
  assert.equal(stale.meta.refreshStarted, true);
  const failedRefresh = await stale.revalidated;
  assert.equal(failedRefresh.meta.source, 'stale-cache');
  assert.match(failedRefresh.meta.networkError, /offline/);
  assert.equal(requestCalls, 2);

  const guarded = await getCommunity({ force: true });
  assert.equal(guarded.meta.source, 'stale-cache');
  assert.equal(requestCalls, 2);

  now += 60 * 1000 + 1;
  await getCommunity();
  await flushAsync();
  assert.equal(requestCalls, 3);
});

test('后台刷新完成后会通知当前页面替换旧快照', async () => {
  Date.now = () => now;
  storage.clear();
  requestCalls = 0;
  requestFailure = false;

  await getCommunity();
  now += 30 * 60 * 1000 + 1;
  let callbackResult = null;
  const stale = await getCommunity({
    onRevalidated: (result) => { callbackResult = result; },
  });

  assert.equal(stale.meta.source, 'stale-cache');
  const refreshed = await stale.revalidated;
  await flushAsync();
  assert.equal(callbackResult.data.snapshot_id, refreshed.data.snapshot_id);
  assert.equal(callbackResult.meta.refreshStarted, false);
});

test('隐私版本纠正只额外请求一次并覆盖新鲜缓存', async () => {
  Date.now = () => now;
  storage.clear();
  requestCalls = 0;
  requestFailure = false;

  const first = await getBootstrap();
  assert.equal(first.data.snapshot_id, 'snapshot-1');
  const corrected = await getBootstrap({ revalidate: true });

  assert.equal(requestCalls, 2);
  assert.equal(corrected.meta.source, 'network');
  assert.equal(corrected.data.snapshot_id, 'snapshot-2');
});
