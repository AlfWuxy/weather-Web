const ACQUISITION_STORAGE_KEY = 'yl_acquisition_source_v1';
const ACQUISITION_TTL_MS = 30 * 24 * 60 * 60 * 1000;
const FAMILY_ENTRY_STORAGE_KEY = 'yl_family_share_entry_v1';
const FAMILY_ENTRY_TTL_MS = 30 * 60 * 1000;
const SHARE_COVER_PATH = '/assets/share/yilao-share-cover.jpg';

const ALLOWED_SOURCES = new Set(['family_share']);
// 分享白名单只保留不含个人资料的公开页面。
const PUBLIC_SHARE_ROUTES = Object.freeze([
  '/pages/home/index',
  '/pages/forecast/index',
  '/pages/community/index',
  '/pages/alerts/index',
  '/pages/actions/index',
  '/pages/cooling/index',
  '/pages/gis/index',
  '/pages/transparency/index',
  '/pages/privacy/index',
  '/pages/about/index',
]);
const ALLOWED_ROUTES = new Set(PUBLIC_SHARE_ROUTES);

function normalizeSource(value) {
  const source = String(value || '').trim().toLowerCase();
  return ALLOWED_SOURCES.has(source) ? source : '';
}

function normalizeRoute(value) {
  const route = String(value || '').trim();
  return ALLOWED_ROUTES.has(route) ? route : '/pages/home/index';
}

function createPageShare(options) {
  const settings = options || {};
  const route = normalizeRoute(settings.route);
  const source = normalizeSource(settings.source);
  return {
    title: String(settings.title || '宜老天气通：把天气预警变成今天能做的事'),
    path: source === 'family_share' ? `${route}?from=family_share` : route,
    imageUrl: SHARE_COVER_PATH,
  };
}

function createTimelineShare(options) {
  const settings = options || {};
  return {
    title: String(settings.title || '宜老天气通：都昌县天气与今日行动'),
    imageUrl: SHARE_COVER_PATH,
  };
}

function sourceFromShareEvent(options) {
  const event = options || {};
  const dataset = event.target && event.target.dataset;
  if (event.from !== 'button' || !dataset) return '';
  return normalizeSource(dataset.shareSource);
}

function showPublicShareMenu(wxApi) {
  const api = wxApi || (typeof wx !== 'undefined' ? wx : null);
  if (!api || typeof api.showShareMenu !== 'function') return false;
  try {
    api.showShareMenu({ menus: ['shareAppMessage', 'shareTimeline'] });
    return true;
  } catch (error) {
    return false;
  }
}

function rememberAcquisitionSource(query, storageApi, nowMs) {
  const source = normalizeSource(query && query.from);
  const api = storageApi || (typeof wx !== 'undefined' ? wx : null);
  if (!source || !api || typeof api.setStorageSync !== 'function') return '';
  const receivedAt = Number.isFinite(nowMs) ? nowMs : Date.now();
  try {
    api.setStorageSync(ACQUISITION_STORAGE_KEY, {
      source,
      expires_at: receivedAt + ACQUISITION_TTL_MS,
    });
    // 落地提示只保留一次短入口上下文，登录归因仍按 30 天独立保存。
    api.setStorageSync(FAMILY_ENTRY_STORAGE_KEY, {
      source,
      expires_at: receivedAt + FAMILY_ENTRY_TTL_MS,
    });
    return source;
  } catch (error) {
    return '';
  }
}

function readFamilyShareEntryRecord(storageApi, nowMs) {
  const api = storageApi || (typeof wx !== 'undefined' ? wx : null);
  if (!api || typeof api.getStorageSync !== 'function') return null;
  try {
    const record = api.getStorageSync(FAMILY_ENTRY_STORAGE_KEY);
    const source = normalizeSource(record && record.source);
    const expiresAt = Number(record && record.expires_at);
    const currentTime = Number.isFinite(nowMs) ? nowMs : Date.now();
    if (!source || !Number.isFinite(expiresAt) || expiresAt <= currentTime) {
      if (typeof api.removeStorageSync === 'function') api.removeStorageSync(FAMILY_ENTRY_STORAGE_KEY);
      return null;
    }
    return { source, expiresAt };
  } catch (error) {
    return null;
  }
}

function readFamilyShareEntry(storageApi, nowMs) {
  const record = readFamilyShareEntryRecord(storageApi, nowMs);
  return record ? record.source : '';
}

function readAcquisitionSource(storageApi, nowMs) {
  const api = storageApi || (typeof wx !== 'undefined' ? wx : null);
  if (!api || typeof api.getStorageSync !== 'function') return '';
  try {
    const record = api.getStorageSync(ACQUISITION_STORAGE_KEY);
    const source = normalizeSource(record && record.source);
    const expiresAt = Number(record && record.expires_at);
    const currentTime = Number.isFinite(nowMs) ? nowMs : Date.now();
    if (!source || !Number.isFinite(expiresAt) || expiresAt <= currentTime) {
      if (typeof api.removeStorageSync === 'function') api.removeStorageSync(ACQUISITION_STORAGE_KEY);
      return '';
    }
    return source;
  } catch (error) {
    return '';
  }
}

function clearAcquisitionContext(storageApi) {
  const api = storageApi || (typeof wx !== 'undefined' ? wx : null);
  if (!api || typeof api.removeStorageSync !== 'function') return false;
  try {
    api.removeStorageSync(ACQUISITION_STORAGE_KEY);
    api.removeStorageSync(FAMILY_ENTRY_STORAGE_KEY);
    return true;
  } catch (error) {
    return false;
  }
}

module.exports = {
  ACQUISITION_STORAGE_KEY,
  ACQUISITION_TTL_MS,
  FAMILY_ENTRY_STORAGE_KEY,
  FAMILY_ENTRY_TTL_MS,
  PUBLIC_SHARE_ROUTES,
  SHARE_COVER_PATH,
  clearAcquisitionContext,
  createPageShare,
  createTimelineShare,
  normalizeSource,
  readAcquisitionSource,
  readFamilyShareEntry,
  readFamilyShareEntryRecord,
  rememberAcquisitionSource,
  showPublicShareMenu,
  sourceFromShareEvent,
};
