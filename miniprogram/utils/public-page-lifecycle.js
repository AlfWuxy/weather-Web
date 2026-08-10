const MIN_TIMER_DELAY_MS = 80;
const MAX_TIMER_DELAY_MS = 0x7fffffff;
const DEFAULT_FAILURE_RETRY_DELAY_MS = 60 * 1000;

function finiteTimestamp(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function clearPublicRefreshTimer(page) {
  if (!page || !page._publicRefreshTimer) return;
  clearTimeout(page._publicRefreshTimer);
  page._publicRefreshTimer = null;
}

function beginPublicPage(page) {
  if (!page) return;
  page._unloaded = false;
  page._publicPageVisible = true;
}

function showPublicPage(page, reload) {
  if (!page || page._unloaded) return;
  clearPublicRefreshTimer(page);
  page._publicPageVisible = true;
  if (typeof reload === 'function') reload();
}

function hidePublicPage(page) {
  if (!page) return;
  page._publicPageVisible = false;
  clearPublicRefreshTimer(page);
}

function unloadPublicPage(page) {
  if (!page) return;
  page._unloaded = true;
  page._publicPageVisible = false;
  clearPublicRefreshTimer(page);
}

function pageCanRender(page) {
  return Boolean(page && !page._unloaded && page._publicPageVisible !== false);
}

function staleRetryMeta(meta, retryDelayMs) {
  const delay = Number(retryDelayMs);
  // 调用方漏传间隔时保持保守的一分钟退避，避免失败路径形成快速重试。
  const safeDelay = Number.isFinite(delay) && delay > 0 ? delay : DEFAULT_FAILURE_RETRY_DELAY_MS;
  return Object.assign({}, meta || {}, {
    stale: true,
    source: 'stale-cache',
    refreshDeferred: false,
    refreshStarted: false,
    effectiveExpiresAt: null,
    retryAfter: Date.now() + safeDelay,
  });
}

function nextRefreshAt(meta) {
  const source = meta || {};
  if (source.refreshStarted) return null;
  const effectiveExpiresAt = finiteTimestamp(source.effectiveExpiresAt);
  const retryAfter = finiteTimestamp(source.retryAfter);
  const now = Date.now();
  if (retryAfter !== null && retryAfter > now) return retryAfter;
  return effectiveExpiresAt;
}

function schedulePublicRefresh(page, meta, reload) {
  clearPublicRefreshTimer(page);
  if (!page || page._unloaded || page._publicPageVisible === false || typeof reload !== 'function') return null;
  const refreshAt = nextRefreshAt(meta);
  if (refreshAt === null) return null;
  const delay = Math.min(
    MAX_TIMER_DELAY_MS,
    Math.max(MIN_TIMER_DELAY_MS, refreshAt - Date.now() + 20)
  );
  page._publicRefreshTimer = setTimeout(() => {
    page._publicRefreshTimer = null;
    if (page._unloaded || page._publicPageVisible === false) return;
    // 到期后仍走公共数据单飞缓存。多个页面不会叠加后端请求。
    reload();
  }, delay);
  return delay;
}

module.exports = {
  beginPublicPage,
  clearPublicRefreshTimer,
  hidePublicPage,
  nextRefreshAt,
  pageCanRender,
  schedulePublicRefresh,
  showPublicPage,
  staleRetryMeta,
  unloadPublicPage,
};
