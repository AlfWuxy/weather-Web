const { api, isUnauthorizedError } = require('./request');

const PENDING_TAB_INDEX = 1;

function countOpenHelp(pairs) {
  return (pairs || []).filter((pair) => {
    const today = pair && pair.today ? pair.today : {};
    return !!today.help_requested && !today.help_acknowledged;
  }).length;
}

function applyBadge(count) {
  const n = Number(count) || 0;
  if (n > 0) {
    wx.setTabBarBadge({
      index: PENDING_TAB_INDEX,
      text: n > 99 ? '99+' : String(n),
      fail() {},
    });
    return;
  }
  wx.removeTabBarBadge({
    index: PENDING_TAB_INDEX,
    fail() {},
  });
}

async function refreshPendingBadge() {
  const token = (wx.getStorageSync('api_token') || '').trim();
  if (!token) {
    applyBadge(0);
    return 0;
  }
  try {
    const data = await api({ method: 'GET', path: '/mp/api/v1/pending', token });
    const pairs = (data && data.pairs) || [];
    const count = countOpenHelp(pairs);
    applyBadge(count);
    return count;
  } catch (e) {
    if (isUnauthorizedError(e)) {
      applyBadge(0);
      return 0;
    }
    return 0;
  }
}

module.exports = { refreshPendingBadge, countOpenHelp, PENDING_TAB_INDEX };
