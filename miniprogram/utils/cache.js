const DEFAULT_TTL_MS = 30 * 60 * 1000;

function finiteTimestamp(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function makeEnvelope(data, storedAt, ttlMs, absoluteExpiresAt, retryAfter) {
  const timestamp = finiteTimestamp(storedAt);
  const absoluteExpiry = finiteTimestamp(absoluteExpiresAt);
  const retryGuard = finiteTimestamp(retryAfter);
  return {
    schema: 1,
    storedAt: timestamp === null ? Date.now() : timestamp,
    ttlMs: Number(ttlMs) > 0 ? Number(ttlMs) : DEFAULT_TTL_MS,
    absoluteExpiresAt: absoluteExpiry,
    retryAfter: retryGuard,
    data,
  };
}

function inspectEnvelope(envelope, now, fallbackTtlMs) {
  const current = finiteTimestamp(now);
  const storedAt = envelope && finiteTimestamp(envelope.storedAt);
  const ttlMs = envelope && Number(envelope.ttlMs) > 0
    ? Number(envelope.ttlMs)
    : (Number(fallbackTtlMs) > 0 ? Number(fallbackTtlMs) : DEFAULT_TTL_MS);
  if (!envelope || envelope.schema !== 1 || storedAt === null || envelope.data === undefined) {
    return { valid: false, fresh: false, ageMs: null, ttlMs, data: null };
  }
  const ageMs = Math.max(0, (current === null ? Date.now() : current) - storedAt);
  const relativeExpiresAt = storedAt + ttlMs;
  const absoluteExpiresAt = finiteTimestamp(envelope.absoluteExpiresAt);
  const retryAfter = finiteTimestamp(envelope.retryAfter);
  const effectiveExpiresAt = absoluteExpiresAt === null
    ? relativeExpiresAt
    : Math.min(relativeExpiresAt, absoluteExpiresAt);
  return {
    valid: true,
    fresh: (current === null ? Date.now() : current) < effectiveExpiresAt,
    retryGuarded: retryAfter !== null && (current === null ? Date.now() : current) < retryAfter,
    ageMs,
    ttlMs,
    storedAt,
    absoluteExpiresAt,
    effectiveExpiresAt,
    retryAfter,
    data: envelope.data,
  };
}

function createCachedResourceLoader(options) {
  const settings = options || {};
  let pending = null;

  function refresh(cached) {
    if (pending) return pending;
    pending = Promise.resolve()
      .then(() => settings.fetch())
      .then((data) => {
        const savedAt = settings.now();
        const absoluteExpiresAt = settings.absoluteExpiresAt ? settings.absoluteExpiresAt(data) : null;
        const retryAfter = Number.isFinite(absoluteExpiresAt)
          && absoluteExpiresAt <= savedAt
          && Number(settings.staleRetryMs) > 0
          ? savedAt + Number(settings.staleRetryMs)
          : null;
        const envelope = makeEnvelope(data, savedAt, settings.ttlMs, absoluteExpiresAt, retryAfter);
        settings.write(envelope);
        return { data, inspection: inspectEnvelope(envelope, settings.now(), settings.ttlMs), source: 'network' };
      }, (error) => {
        if (cached.valid) {
          let inspection = cached;
          const staleRetryMs = Number(settings.staleRetryMs);
          if (staleRetryMs > 0) {
            const retryEnvelope = makeEnvelope(
              cached.data,
              cached.storedAt,
              cached.ttlMs,
              cached.absoluteExpiresAt,
              settings.now() + staleRetryMs
            );
            settings.write(retryEnvelope);
            inspection = inspectEnvelope(retryEnvelope, settings.now(), settings.ttlMs);
          }
          return { data: cached.data, inspection, source: 'stale-cache', error };
        }
        throw error;
      });
    pending = pending.then(
      (result) => { pending = null; return result; },
      (error) => { pending = null; throw error; }
    );
    return pending;
  }

  return function load(loadOptions) {
    const now = settings.now();
    const cached = inspectEnvelope(settings.read(), now, settings.ttlMs);
    if (loadOptions && loadOptions.revalidate) {
      // 只供服务端明确要求纠正版本时使用；普通刷新继续遵守 30 分钟硬缓存。
      return refresh(cached);
    }
    if (cached.valid && (cached.fresh || cached.retryGuarded)) {
      return Promise.resolve({
        data: cached.data,
        inspection: cached,
        source: cached.fresh ? 'cache' : 'stale-cache',
        refreshDeferred: Boolean(loadOptions && loadOptions.force),
      });
    }
    if (cached.valid) {
      const refreshPromise = refresh(cached);
      if (loadOptions && loadOptions.force) {
        // 用户主动刷新时等待本轮请求完成，让页面拿到确定的新结果。
        return refreshPromise;
      }
      // 过期缓存先立即展示，后台刷新由 pending 保证同一时刻只有一个请求。
      refreshPromise.catch(() => {
        // 后台刷新异常不能打断已经返回的旧数据，下一次读取仍可重试。
      });
      return Promise.resolve({
        data: cached.data,
        inspection: cached,
        source: 'stale-cache',
        refreshStarted: true,
        revalidated: refreshPromise,
      });
    }
    if (pending) return pending;
    return refresh(cached);
  };
}

module.exports = { DEFAULT_TTL_MS, createCachedResourceLoader, inspectEnvelope, makeEnvelope };
