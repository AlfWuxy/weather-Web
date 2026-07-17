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
  return function load(loadOptions) {
    const now = settings.now();
    const cached = inspectEnvelope(settings.read(), now, settings.ttlMs);
    if (cached.valid && (cached.fresh || cached.retryGuarded)) {
      return Promise.resolve({
        data: cached.data,
        inspection: cached,
        source: cached.fresh ? 'cache' : 'stale-cache',
        refreshDeferred: Boolean(loadOptions && loadOptions.force),
      });
    }
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
  };
}

module.exports = { DEFAULT_TTL_MS, createCachedResourceLoader, inspectEnvelope, makeEnvelope };
