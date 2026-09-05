const { api, isUnauthorizedError } = require('../../utils/request');
const { refreshPendingBadge } = require('../../utils/pendingBadge');

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
const STRIP_CELLS = [
  { key: 'delivered', label: 'delivered', field: 'delivered' },
  { key: 'seen', label: 'seen', field: 'seen' },
  { key: 'understood', label: 'understood', field: 'understood' },
  { key: 'self_reported', label: 'self_reported', field: 'self_reported' },
  { key: 'verified', label: 'verified', field: 'caregiver_verified' },
  { key: 'help', label: 'help', field: 'help_requested' },
  { key: 'closed', label: 'closed', field: 'closed' },
];

function actionErrorMessage(error) {
  if (!error) return '操作失败';
  if (error.code === 'invalid_transition') return '当前不能进行此操作';
  if (error.code === 'forbidden') return '无权执行该操作';
  if (error.code === 'not_found') return '配对不存在或已解绑';
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

function groupPairs(pairs) {
  const openHelp = [];
  const toVerify = [];
  const closable = [];
  (pairs || []).forEach((pair) => {
    const card = decoratePair(pair);
    const today = card.today;
    if (today.help_requested && !today.help_acknowledged) openHelp.push(card);
    if (today.self_reported && !today.caregiver_verified) toVerify.push(card);
    if ((today.caregiver_verified || today.help_acknowledged) && !today.closed) closable.push(card);
  });
  return { openHelp, toVerify, closable };
}

Page({
  data: {
    loading: false,
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

  getToken() {
    return (wx.getStorageSync('api_token') || '').trim();
  },

  onShow() {
    this.loadPending();
  },

  onPullDownRefresh() {
    this.loadPending().finally(() => wx.stopPullDownRefresh());
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

  async loadPending() {
    const token = this.getToken();
    if (!token) {
      wx.reLaunch({ url: '/pages/bind-token/index' });
      return;
    }
    this.restoreMessengerChoice();
    this.setData({ loading: true });
    try {
      const data = await api({ method: 'GET', path: '/mp/api/v1/pending', token });
      const grouped = groupPairs((data && data.pairs) || []);
      this.setData(grouped);
      await refreshPendingBadge();
    } catch (e) {
      if (isUnauthorizedError(e)) return;
      wx.showToast({ title: '加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
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
    this.postStage(Number(e.currentTarget.dataset.pairId), 'help_acknowledged');
  },

  onVerify(e) {
    this.postStage(Number(e.currentTarget.dataset.pairId), 'caregiver_verified');
  },

  onClose(e) {
    this.postStage(Number(e.currentTarget.dataset.pairId), 'closed');
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
