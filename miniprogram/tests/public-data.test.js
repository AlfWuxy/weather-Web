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

const { getBootstrap } = require('../utils/public-data');

test('真实 getBootstrap 覆盖 absolute expiry、并发去重和 stale backoff', async (context) => {
  context.after(() => { Date.now = originalNow; });
  storage.clear();
  requestCalls = 0;

  const first = getBootstrap();
  const concurrent = getBootstrap();
  const [firstResult, concurrentResult] = await Promise.all([first, concurrent]);
  assert.equal(requestCalls, 1);
  assert.equal(firstResult.data.snapshot_id, 'snapshot-1');
  assert.equal(concurrentResult.data.snapshot_id, 'snapshot-1');

  now += 59 * 1000;
  const stillFresh = await getBootstrap({ force: true });
  assert.equal(requestCalls, 1);
  assert.equal(stillFresh.meta.refreshDeferred, true);

  now += 2 * 1000;
  const staleNetworkResult = await getBootstrap();
  assert.equal(requestCalls, 2);
  assert.equal(staleNetworkResult.meta.stale, true);

  const nextPage = await getBootstrap();
  assert.equal(requestCalls, 2);
  assert.equal(nextPage.meta.source, 'stale-cache');
  assert.equal(nextPage.meta.stale, true);

  now += 60 * 1000 + 1;
  await getBootstrap();
  assert.equal(requestCalls, 3);
});
