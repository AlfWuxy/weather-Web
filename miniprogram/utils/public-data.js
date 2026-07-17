const { PUBLIC_CACHE_TTL_MS } = require('../config');
const { api } = require('./request');
const { createCachedResourceLoader } = require('./cache');

const CACHE_KEYS = {
  bootstrap: 'yl_public_bootstrap_v1',
  community: 'yl_public_community_v1',
};

const RESOURCE_CONFIG = {
  bootstrap: {
    key: CACHE_KEYS.bootstrap,
    path: '/mp/api/v1/bootstrap',
    staleRetryMs: 60 * 1000,
  },
  community: {
    key: CACHE_KEYS.community,
    path: '/mp/api/v1/public/community',
    staleRetryMs: 60 * 1000,
  },
};

const loaders = {};

function timestampFromIso(value) {
  if (!value) return null;
  const timestamp = Date.parse(String(value));
  return Number.isFinite(timestamp) ? timestamp : null;
}

function readEnvelope(key) {
  try {
    return wx.getStorageSync(key) || null;
  } catch (error) {
    return null;
  }
}

function writeEnvelope(key, envelope) {
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

module.exports = {
  CACHE_KEYS,
  getBootstrap,
  getCommunity,
  getCachedPublic,
  timestampFromIso,
};
