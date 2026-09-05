const test = require('node:test');
const assert = require('node:assert/strict');

const {
  createCachedResourceLoader,
  inspectEnvelope,
  makeEnvelope,
} = require('../utils/cache');

function flushAsync() {
  return new Promise((resolve) => setImmediate(resolve));
}

test('30 分钟硬缓存按 29:59 和 30:01 正确切换', () => {
  const start = 1_700_000_000_000;
  const envelope = makeEnvelope({ value: 1 }, start, 30 * 60 * 1000);
  assert.equal(inspectEnvelope(envelope, start + 29 * 60 * 1000 + 59 * 1000).fresh, true);
  assert.equal(inspectEnvelope(envelope, start + 30 * 60 * 1000 + 1000).fresh, false);
});

test('服务端绝对过期时间会缩短本机缓存', () => {
  const start = 1_700_000_000_000;
  const serverExpiresAt = start + 60 * 1000;
  const envelope = makeEnvelope({ value: 1 }, start, 30 * 60 * 1000, serverExpiresAt);
  const before = inspectEnvelope(envelope, serverExpiresAt - 1);
  const after = inspectEnvelope(envelope, serverExpiresAt + 1);
  assert.equal(before.fresh, true);
  assert.equal(after.fresh, false);
  assert.equal(after.effectiveExpiresAt, serverExpiresAt);
});

test('并发读取只产生一个进行中的请求', async () => {
  let now = 1_700_000_000_000;
  let envelope = null;
  let calls = 0;
  let release;
  const waitForRelease = new Promise((resolve) => { release = resolve; });
  const loader = createCachedResourceLoader({
    ttlMs: 30 * 60 * 1000,
    now: () => now,
    read: () => envelope,
    write: (next) => { envelope = next; },
    fetch: async () => {
      calls += 1;
      await waitForRelease;
      return { ok: true };
    },
  });
  const first = loader();
  const second = loader();
  assert.equal(calls, 0);
  release();
  const [left, right] = await Promise.all([first, second]);
  assert.equal(calls, 1);
  assert.deepEqual(left.data, { ok: true });
  assert.deepEqual(right.data, { ok: true });
  now += 1000;
  const cached = await loader({ force: true });
  assert.equal(cached.source, 'cache');
  assert.equal(cached.refreshDeferred, true);
});

test('网络失败时返回过期缓存，并在短重试窗内停止重复请求', async () => {
  const start = 1_700_000_000_000;
  let now = start + 2000;
  let envelope = makeEnvelope({ old: true }, start, 1000);
  let calls = 0;
  const loader = createCachedResourceLoader({
    ttlMs: 1000,
    staleRetryMs: 60 * 1000,
    now: () => now,
    read: () => envelope,
    write: (next) => { envelope = next; },
    fetch: async () => { calls += 1; throw new Error('offline'); },
  });
  const result = await loader();
  assert.equal(result.source, 'stale-cache');
  assert.deepEqual(result.data, { old: true });
  const failedRefresh = await result.revalidated;
  assert.equal(failedRefresh.source, 'stale-cache');
  assert.match(failedRefresh.error.message, /offline/);
  assert.equal(calls, 1);

  const deferred = await loader();
  assert.equal(deferred.source, 'stale-cache');
  assert.equal(calls, 1);

  now += 60 * 1000 + 1;
  await loader();
  await flushAsync();
  assert.equal(calls, 2);
});

test('过期缓存立即返回且并发读取只启动一次后台刷新', async () => {
  const start = 1_700_000_000_000;
  let now = start + 2000;
  let envelope = makeEnvelope({ version: 'old' }, start, 1000);
  let calls = 0;
  let release;
  const refreshResult = new Promise((resolve) => { release = resolve; });
  const loader = createCachedResourceLoader({
    ttlMs: 1000,
    staleRetryMs: 60 * 1000,
    now: () => now,
    read: () => envelope,
    write: (next) => { envelope = next; },
    fetch: async () => {
      calls += 1;
      return refreshResult;
    },
  });

  const first = await loader();
  const second = await loader();
  assert.equal(first.source, 'stale-cache');
  assert.equal(first.refreshStarted, true);
  assert.equal(typeof first.revalidated.then, 'function');
  assert.deepEqual(first.data, { version: 'old' });
  assert.deepEqual(second.data, { version: 'old' });
  assert.equal(calls, 1);

  release({ version: 'new' });
  const backgroundResult = await first.revalidated;
  assert.equal(backgroundResult.source, 'network');
  assert.deepEqual(backgroundResult.data, { version: 'new' });
  const refreshed = await loader();
  assert.equal(refreshed.source, 'cache');
  assert.deepEqual(refreshed.data, { version: 'new' });
  now += 1;
});

test('用户主动刷新过期缓存时等待并返回本轮新数据', async () => {
  const start = 1_700_000_000_000;
  let envelope = makeEnvelope({ version: 'old' }, start, 1000);
  let calls = 0;
  let release;
  const refreshResult = new Promise((resolve) => { release = resolve; });
  const loader = createCachedResourceLoader({
    ttlMs: 1000,
    now: () => start + 2000,
    read: () => envelope,
    write: (next) => { envelope = next; },
    fetch: async () => {
      calls += 1;
      return refreshResult;
    },
  });

  let settled = false;
  const pendingRefresh = loader({ force: true }).then((result) => {
    settled = true;
    return result;
  });
  await flushAsync();
  assert.equal(calls, 1);
  assert.equal(settled, false);

  release({ version: 'new' });
  const refreshed = await pendingRefresh;
  assert.equal(refreshed.source, 'network');
  assert.deepEqual(refreshed.data, { version: 'new' });
});

test('服务端 428 纠正可绕过一次新鲜缓存', async () => {
  const start = 1_700_000_000_000;
  let envelope = makeEnvelope({ version: 'old' }, start, 30 * 60 * 1000);
  let calls = 0;
  const loader = createCachedResourceLoader({
    ttlMs: 30 * 60 * 1000,
    now: () => start + 1000,
    read: () => envelope,
    write: (next) => { envelope = next; },
    fetch: async () => {
      calls += 1;
      return { version: 'new' };
    },
  });

  const ordinary = await loader({ force: true });
  assert.equal(ordinary.source, 'cache');
  assert.equal(calls, 0);

  const corrected = await loader({ revalidate: true });
  assert.equal(corrected.source, 'network');
  assert.deepEqual(corrected.data, { version: 'new' });
  assert.equal(calls, 1);
});

test('服务端已过期响应启用 60 秒短重试窗', async () => {
  let now = 1_700_000_000_000;
  let envelope = null;
  let calls = 0;
  const loader = createCachedResourceLoader({
    ttlMs: 30 * 60 * 1000,
    staleRetryMs: 60 * 1000,
    now: () => now,
    read: () => envelope,
    write: (next) => { envelope = next; },
    absoluteExpiresAt: (data) => data.expiresAt,
    fetch: async () => { calls += 1; return { expiresAt: now - 1000 }; },
  });
  const first = await loader();
  assert.equal(first.inspection.fresh, false);
  const second = await loader();
  assert.equal(second.source, 'stale-cache');
  assert.equal(calls, 1);
  now += 60 * 1000 + 1;
  await loader();
  await flushAsync();
  assert.equal(calls, 2);
});
