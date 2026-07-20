const { PUBLIC_CACHE_TTL_MS } = require('../config');
const { api } = require('./request');
const { createCachedResourceLoader } = require('./cache');

const CACHE_KEYS = {
  bootstrap: 'yl_public_bootstrap_v1',
  community: 'yl_public_community_v1',
};

const PUBLIC_RETRY_DELAY_MS = 60 * 1000;

const RESOURCE_CONFIG = {
  bootstrap: {
    key: CACHE_KEYS.bootstrap,
    path: '/mp/api/v1/bootstrap',
    staleRetryMs: PUBLIC_RETRY_DELAY_MS,
  },
  community: {
    key: CACHE_KEYS.community,
    path: '/mp/api/v1/public/community',
    staleRetryMs: PUBLIC_RETRY_DELAY_MS,
  },
};

const loaders = {};
const memoryEnvelopes = {};

function timestampFromIso(value) {
  if (!value) return null;
  const timestamp = Date.parse(String(value));
  return Number.isFinite(timestamp) ? timestamp : null;
}

function envelopeTimestamp(envelope) {
  if (!envelope || envelope.schema !== 1 || envelope.data === undefined) return null;
  const timestamp = Number(envelope.storedAt);
  return Number.isFinite(timestamp) && timestamp >= 0 ? timestamp : null;
}

function newestEnvelope(stored, memory) {
  const storedAt = envelopeTimestamp(stored);
  const memoryAt = envelopeTimestamp(memory);
  if (storedAt === null) return memoryAt === null ? null : memory;
  if (memoryAt === null) return stored;
  return memoryAt >= storedAt ? memory : stored;
}

function readEnvelope(key) {
  let stored = null;
  try {
    stored = wx.getStorageSync(key) || null;
  } catch (error) {
    stored = null;
  }
  // 本机存储失败时保留当前进程最后一份完整公共快照，避免弱网下退回无状态页面。
  const envelope = newestEnvelope(stored, memoryEnvelopes[key]);
  if (envelope) memoryEnvelopes[key] = envelope;
  return envelope;
}

function writeEnvelope(key, envelope) {
  // 先写进程内存，再尝试持久化。持久化失败不会丢掉本轮已验证的公共快照。
  memoryEnvelopes[key] = envelope;
  try {
    wx.setStorageSync(key, envelope);
  } catch (error) {
    console.warn('公共数据缓存写入失败', error);
  }
}

function loaderFor(resource) {
  const config = RESOURCE_CONFIG[resource];
  if (!config) throw new Error('unknown_public_resource');
  if (!loaders[resource]) {
    loaders[resource] = createCachedResourceLoader({
      ttlMs: PUBLIC_CACHE_TTL_MS,
      staleRetryMs: config.staleRetryMs,
      now: () => Date.now(),
      read: () => readEnvelope(config.key),
      write: (envelope) => writeEnvelope(config.key, envelope),
      fetch: () => api({ method: 'GET', path: config.path }),
      absoluteExpiresAt: resource === 'bootstrap'
        ? (data) => timestampFromIso(data && data.expires_at)
        : null,
    });
  }
  return loaders[resource];
}

function resultFrom(result) {
  const inspection = result.inspection || {};
  const mapped = {
    data: result.data,
    meta: {
      source: result.source,
      stale: result.source === 'stale-cache' || Boolean(inspection.valid && !inspection.fresh),
      ageMs: inspection.ageMs,
      storedAt: inspection.storedAt,
      absoluteExpiresAt: inspection.absoluteExpiresAt,
      effectiveExpiresAt: inspection.effectiveExpiresAt,
      retryAfter: inspection.retryAfter,
      ttlMs: PUBLIC_CACHE_TTL_MS,
      refreshDeferred: Boolean(result.refreshDeferred),
      refreshStarted: Boolean(result.refreshStarted),
      networkError: result.error && (result.error.errMsg || result.error.message) || '',
    },
  };
  if (result.revalidated && typeof result.revalidated.then === 'function') {
    mapped.revalidated = result.revalidated.then(resultFrom);
  }
  return mapped;
}

function getCachedPublic(resource, options) {
  let loader;
  try {
    loader = loaderFor(resource);
  } catch (error) {
    return Promise.reject(error);
  }
  return loader(options).then((result) => {
    const mapped = resultFrom(result);
    if (mapped.revalidated && options && typeof options.onRevalidated === 'function') {
      mapped.revalidated.then(
        (freshResult) => options.onRevalidated(freshResult),
        (error) => {
          if (typeof options.onRevalidationError === 'function') options.onRevalidationError(error);
        }
      ).catch(() => {
        // 页面回调异常不影响缓存状态，也不产生未处理的 Promise 拒绝。
      });
    }
    return mapped;
  });
}

function getBootstrap(options) {
  return getCachedPublic('bootstrap', options);
}

function getCommunity(options) {
  return getCachedPublic('community', options);
}

function resetPublicDataForTests() {
  // 只清理模块进程内状态，不触碰用户本机存储。
  Object.keys(loaders).forEach((key) => { delete loaders[key]; });
  Object.keys(memoryEnvelopes).forEach((key) => { delete memoryEnvelopes[key]; });
}

module.exports = {
  CACHE_KEYS,
  PUBLIC_RETRY_DELAY_MS,
  __resetPublicDataForTests: resetPublicDataForTests,
  getBootstrap,
  getCommunity,
  getCachedPublic,
  timestampFromIso,
};
