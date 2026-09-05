const { api, isUnauthorizedError } = require('../../utils/request');
const { getToken } = require('../elders/care-session');
const { refreshPendingBadge } = require('../../utils/pendingBadge');
const { applyPendingFetch } = require('../../utils/pendingViewModel');

const MESSENGER_ROLES = [
  { id: 'child', label: '子女' },
  { id: 'grandchild', label: '孙辈' },
  { id: 'spouse', label: '配偶' },
  { id: 'neighbor', label: '邻居' },
  { id: 'village_cadre', label: '村干部' },
  { id: 'village_doctor', label: '村医' },
  { id: 'self', label: '本人' },
];

const CHANNELS = [
  { id: 'wechat_text', label: '微信文字' },
  { id: 'wechat_voice', label: '微信语音' },
  { id: 'phone_call', label: '电话' },
  { id: 'in_person', label: '当面' },
];

const STORAGE_KEY = 'pending_last_messenger';
const POLL_BASE_MS = 5000;
const POLL_JITTER_MS = 2000;
const STRIP_CELLS = [
  { key: 'delivered', label: '已转告', field: 'delivered' },
  { key: 'seen', label: '已打开', field: 'seen' },
  { key: 'understood', label: '已理解', field: 'understood' },
  { key: 'self_reported', label: '已做到', field: 'self_reported' },
  { key: 'verified', label: '已核验', field: 'caregiver_verified' },
  { key: 'help', label: '求助', field: 'help_requested' },
  { key: 'closed', label: '已结案', field: 'closed' },
];

function actionErrorMessage(error) {
  if (!error) return '操作失败';
  if (error.code === 'invalid_transition') return '当前不能进行此操作';
  if (error.code === 'forbidden') return '无权执行该操作';
  if (error.code === 'not_found') return '配对不存在或已解绑';
  if (error.code === 'version_conflict') return '状态已更新，请下拉刷新';
  if (error.kind === 'network') return '网络连接失败';
  return '操作失败';
}

function indexOfId(list, id, fallback) {
  const idx = list.findIndex((item) => item.id === id);
  return idx >= 0 ? idx : fallback;
}

function decoratePair(pair) {
  const today = (pair && pair.today) || {};
  return {
    pair_id: pair.pair_id,
    elder_label: pair.elder_label || '',
    displayLabel: pair.elder_label ? pair.elder_label : '未设称呼',
    today,
    cells: STRIP_CELLS.map((cell) => ({
      key: cell.key,
      label: cell.label,
      on: !!today[cell.field],
    })),
  };
}

function decorateOpenHelp(item, pairs) {
  const pair = (pairs || []).find((row) => row.pair_id === item.pair_id) || {};
  const card = decoratePair(Object.assign({ pair_id: item.pair_id, elder_label: pair.elder_label || item.elder_label, today: pair.today || {} }, pair));
  card.help_id = item.id || item.help_id || '';
  card.displayLabel = card.displayLabel || item.elder_label || pair.elder_label || '照护对象';
  card.id = card.help_id;
  card.status = item.status || '';
  card.status_label = item.status_label || item.status || '';
  card.version = item.version;
  return card;
}

Page({
  data: {
    loading: false,
    loadError: false,
    showEmptyOpenHelp: false,
    loadErrorText: '',
    busy: false,
    openHelp: [],
    toVerify: [],
    closable: [],
    pickerVisible: false,
    pendingPairId: null,
    roleIndex: 0,
    channelIndex: 0,
    roleLabels: MESSENGER_ROLES.map((item) => item.label),
    channelLabels: CHANNELS.map((item) => item.label),
  },

  _pollTimer: null,
  _stopped: true,

  getToken() {
    return getToken();
  },

  onShow() {
    this._stopped = false;
    this.loadPending();
    this.schedulePoll();
  },

  onHide() {
    this.stopPoll();
  },

  onUnload() {
    this.stopPoll();
  },

  onPullDownRefresh() {
    this.loadPending().finally(() => wx.stopPullDownRefresh());
  },

  stopPoll() {
    this._stopped = true;
    if (this._pollTimer) {
      clearTimeout(this._pollTimer);
      this._pollTimer = null;
    }
  },

  schedulePoll() {
    if (this._pollTimer) {
      clearTimeout(this._pollTimer);
      this._pollTimer = null;
    }
    if (this._stopped) return;
    const delay = POLL_BASE_MS + Math.floor(Math.random() * POLL_JITTER_MS);
    this._pollTimer = setTimeout(() => {
      this._pollTimer = null;
      if (this._stopped) return;
      this.loadPending({ silent: true }).finally(() => this.schedulePoll());
    }, delay);
    if (this._pollTimer && typeof this._pollTimer.unref === 'function') {
      this._pollTimer.unref();
    }
  },

  restoreMessengerChoice() {
    let stored = null;
    try {
      stored = wx.getStorageSync(STORAGE_KEY) || {};
    } catch (e) {
      stored = {};
    }
    const roleIndex = indexOfId(MESSENGER_ROLES, stored.messenger_role, 0);
    const channelIndex = indexOfId(CHANNELS, stored.channel, 0);
    this.setData({ roleIndex, channelIndex });
  },

  persistMessengerChoice() {
    const messenger_role = MESSENGER_ROLES[this.data.roleIndex].id;
    const channel = CHANNELS[this.data.channelIndex].id;
    try {
      wx.setStorageSync(STORAGE_KEY, { messenger_role, channel });
    } catch (e) {}
  },

  async loadPending(options) {
    const silent = !!(options && options.silent);
    const token = this.getToken();
    if (!token) {
      wx.reLaunch({ url: '/pages/bind-token/index' });
      return;
    }
    this.restoreMessengerChoice();
    if (!silent) this.setData({ loading: true });
    try {
      const data = await api({ method: 'GET', path: '/mp/api/v1/pending', token });
      const payload = data && data.data ? data.data : data;
      const state = applyPendingFetch({ ok: true, payload });
      const pairs = (payload && payload.pairs) || [];
      state.openHelp = (state.openHelp || []).map((item) => decorateOpenHelp(item, pairs));
      state.toVerify = (state.toVerify || []).map((item) => (item.cells ? item : decoratePair(item)));
      state.closable = (state.closable || []).map((item) => (item.cells ? item : decoratePair(item)));
      this.setData(state);
      await refreshPendingBadge();
    } catch (e) {
      if (isUnauthorizedError(e)) return;
      const state = applyPendingFetch({ ok: false, error: e });
      state.loadErrorText = e && e.kind === 'network' ? '网络连接失败，请重试' : '待处理列表暂时无法读取';
      this.setData(state);
      if (!silent) wx.showToast({ title: '加载失败', icon: 'none' });
    } finally {
      if (!silent) this.setData({ loading: false });
    }
  },

  onRoleChange(e) {
    this.setData({ roleIndex: Number(e.detail.value) || 0 });
  },

  onChannelChange(e) {
    this.setData({ channelIndex: Number(e.detail.value) || 0 });
  },

  openDeliver(e) {
    const pairId = Number(e.currentTarget.dataset.pairId);
    if (!pairId) return;
    this.restoreMessengerChoice();
    this.setData({ pickerVisible: true, pendingPairId: pairId });
  },

  closePicker() {
    this.setData({ pickerVisible: false, pendingPairId: null });
  },

  noop() {},

  async confirmDeliver() {
    const pairId = this.data.pendingPairId;
    if (!pairId) return;
    this.persistMessengerChoice();
    const ok = await this.postStage(pairId, 'delivered', {
      messenger_role: MESSENGER_ROLES[this.data.roleIndex].id,
      channel: CHANNELS[this.data.channelIndex].id,
    });
    if (ok) this.closePicker();
  },

  onAck(e) {
    const helpId = e.currentTarget.dataset.helpId;
    const version = Number(e.currentTarget.dataset.version);
    const pairId = Number(e.currentTarget.dataset.pairId);
    if (helpId) {
      this.postHelp(helpId, 'ack', { expected_version: version });
      return;
    }
    this.postStage(pairId, 'help_acknowledged');
  },

  onVerify(e) {
    this.postStage(Number(e.currentTarget.dataset.pairId), 'caregiver_verified');
  },

  onClose(e) {
    const helpId = e.currentTarget.dataset.helpId;
    const version = Number(e.currentTarget.dataset.version);
    const pairId = Number(e.currentTarget.dataset.pairId);
    if (helpId) {
      this.postHelp(helpId, 'resolve', { expected_version: version, resolution_code: 'reached_elder' });
      return;
    }
    this.postStage(pairId, 'closed');
  },

  async postHelp(helpId, action, extra) {
    if (this.data.busy || !helpId) return false;
    const token = this.getToken();
    if (!token) {
      wx.reLaunch({ url: '/pages/bind-token/index' });
      return false;
    }
    this.setData({ busy: true });
    try {
      await api({
        method: 'POST',
        path: `/mp/api/v1/help-requests/${helpId}/${action}`,
        token,
        data: extra || {},
      });
      wx.showToast({ title: '已记录', icon: 'success' });
      await this.loadPending();
      return true;
    } catch (e) {
      if (isUnauthorizedError(e)) return false;
      wx.showToast({ title: actionErrorMessage(e), icon: 'none' });
      return false;
    } finally {
      this.setData({ busy: false });
    }
  },

  async postStage(pairId, stage, extra) {
    if (this.data.busy || !pairId) return false;
    const token = this.getToken();
    if (!token) {
      wx.reLaunch({ url: '/pages/bind-token/index' });
      return false;
    }
    this.setData({ busy: true });
    try {
      await api({
        method: 'POST',
        path: `/mp/api/v1/pairs/${pairId}/events`,
        token,
        data: Object.assign({ stage }, extra || {}),
      });
      wx.showToast({ title: '已记录', icon: 'success' });
      await this.loadPending();
      return true;
    } catch (e) {
      if (isUnauthorizedError(e)) return false;
      wx.showToast({ title: actionErrorMessage(e), icon: 'none' });
      return false;
    } finally {
      this.setData({ busy: false });
    }
  },
});
