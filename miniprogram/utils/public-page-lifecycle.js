const MIN_TIMER_DELAY_MS = 80;
const MAX_TIMER_DELAY_MS = 0x7fffffff;

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
  unloadPublicPage,
};
