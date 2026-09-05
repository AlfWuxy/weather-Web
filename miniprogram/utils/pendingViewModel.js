// 待处理页纯函数：错误态与未结求助分组。页面必须调用这里，禁止把失败渲染成“暂无”。

const OPEN_HELP_STATUSES = {
  requested: true,
  pending_ack: true,
  acknowledged: true,
  in_progress: true,
  open: true,
};

function groupPairsByTodayFlags(pairs) {
  const openHelp = [];
  const toVerify = [];
  const closable = [];
  (pairs || []).forEach((pair) => {
    const today = (pair && pair.today) || {};
    const card = {
      pair_id: pair.pair_id,
      elder_label: pair.elder_label || '',
      displayLabel: pair.elder_label ? pair.elder_label : '未设称呼',
      today,
      help_request: pair.help_request || null,
    };
    if (today.help_requested && !today.help_acknowledged) openHelp.push(card);
    if (today.self_reported && !today.caregiver_verified) toVerify.push(card);
    if ((today.caregiver_verified || today.help_acknowledged) && !today.closed) closable.push(card);
  });
  return { openHelp, toVerify, closable };
}

function openHelpFromPayload(payload) {
  const data = payload || {};
  const requests = data.help_requests;
  if (Array.isArray(requests)) {
    return requests.filter((item) => item && OPEN_HELP_STATUSES[item.status]);
  }
  return groupPairsByTodayFlags(data.pairs || []).openHelp;
}

function applyPendingFetch(result) {
  const ok = !!(result && result.ok);
  if (!ok) {
    return {
      loading: false,
      loadError: true,
      showEmptyOpenHelp: false,
      openHelp: [],
      toVerify: [],
      closable: [],
    };
  }
  const payload = result.payload || {};
  const grouped = groupPairsByTodayFlags(payload.pairs || []);
  const openHelp = openHelpFromPayload(payload);
  return {
    loading: false,
    loadError: false,
    showEmptyOpenHelp: openHelp.length === 0,
    openHelp,
    toVerify: grouped.toVerify,
    closable: grouped.closable,
  };
}

module.exports = {
  groupPairsByTodayFlags,
  openHelpFromPayload,
  applyPendingFetch,
};
