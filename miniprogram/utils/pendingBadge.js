const { api, isUnauthorizedError } = require('./request');
const session = require('./session');

const PENDING_TAB_INDEX = 2; // 照护 Tab；待处理页不是独立 Tab，角标挂在照护上。

function countOpenHelp(pairs) {
  return (pairs || []).filter((pair) => {
    const today = pair && pair.today ? pair.today : {};
    return !!today.help_requested && !today.help_acknowledged;
  }).length;
}

function applyBadge(count) {
  const n = Number(count) || 0;
  if (typeof wx === 'undefined' || typeof wx.setTabBarBadge !== 'function' || typeof wx.removeTabBarBadge !== 'function') {
    return;
  }
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

function countFromPayload(data) {
  const payload = data && data.data && typeof data.data === 'object' ? data.data : data;
  if (payload && typeof payload.open_count === 'number') {
    return payload.open_count;
  }
  if (payload && Array.isArray(payload.help_requests)) {
    return payload.help_requests.filter((item) => {
      const status = item && item.status;
      return status === 'pending_ack' || status === 'acknowledged' || status === 'in_progress' || status === 'requested' || status === 'open';
    }).length;
  }
  return countOpenHelp((payload && payload.pairs) || []);
}

async function refreshPendingBadge() {
  const token = session.getSessionToken();
  if (!token) {
    applyBadge(0);
    return 0;
  }
  if (typeof wx.request !== 'function') {
    applyBadge(0);
    return 0;
  }
  try {
    const data = await api({ method: 'GET', path: '/mp/api/v1/pending', token });
    const count = countFromPayload(data);
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
