const {
  authApi,
  finishHealthMutation,
  getSnapshot,
  guardHealthSensitivePage,
  requireToken,
  resumeHealthMutation,
  suspendHealthMutation,
  trackHealthMutation,
} = require('../elders/care-session');
const { normalizeList, normalizeSnapshot } = require('../elders/care-logic');
const { duchangDateKey } = require('../../utils/format');

const ACTION_PLANS = {
  heat: [
    { id: 'drink_water', title: '少量多次喝水', detail: '现在先喝一杯温水' },
    { id: 'avoid_noon', title: '避开中午外出', detail: '尽量在早晚办事' },
    { id: 'cool_rest', title: '到凉快处休息', detail: '开风扇或空调，留意身体反应' },
  ],
  cold: [
    { id: 'keep_warm', title: '及时添衣保暖', detail: '重点护好头、颈和手脚' },
    { id: 'avoid_fall', title: '减少湿滑路面外出', detail: '穿防滑鞋，走路慢一些' },
    { id: 'safe_heating', title: '安全使用取暖设备', detail: '保持通风，防止烫伤' },
  ],
  normal: [
    { id: 'check_weather', title: '出门前看天气', detail: '按都昌县天气准备衣物' },
    { id: 'carry_water', title: '随身带水', detail: '保持平常的喝水节奏' },
    { id: 'contact_family', title: '和家人报个平安', detail: '让家人知道今天的状态' },
  ],
};

const COMPLETION_OPTIONS = ['全部完成', '完成一部分', '暂时没完成'];
const CONTEXT_LOAD_ERROR = '未能核对这位家人的信息，请检查网络后重试。';

function actionPlan(trigger) {
  return (ACTION_PLANS[trigger] || ACTION_PLANS.normal)
    .map((item) => ({ ...item, checked: false }));
}

function weatherStatus(weather) {
  if (!weather || !weather.available) return '天气待更新';
  if (weather.stale) return '较早天气，待刷新';
  if (weather.trigger === 'heat') return '高温留意';
  if (weather.trigger === 'cold') return '低温留意';
  return '常规天气提示';
}

function restoreTodayActions(actions, today) {
  const status = today && typeof today === 'object' ? today : {};
  if (String(status.status_date || '') !== duchangDateKey()) {
    return { actions, selectedActions: [], confirmed: false, helpRecorded: false };
  }
  const stored = new Set(
    (Array.isArray(status.elder_actions) ? status.elder_actions : [])
      .slice(0, 20)
      .filter((item) => typeof item === 'string'),
  );
  // 只恢复当前清单仍认识的行动，旧版本 ID 不进入新的提交。
  const selectedActions = actions
    .map((item) => item.id)
    .filter((itemId) => stored.has(itemId));
  const selected = new Set(selectedActions);
  return {
    actions: actions.map((item) => ({ ...item, checked: selected.has(item.id) })),
    selectedActions,
    confirmed: typeof status.confirmed_at === 'string' && Boolean(status.confirmed_at.trim()),
    helpRecorded: Boolean(status.help_flag),
  };
}

function beginRequest(page, key) {
  const requestId = Number(page[key] || 0) + 1;
  page[key] = requestId;
  return { requestId, lifecycle: Number(page._lifecycleGeneration || 0) };
}

function requestIsActive(page, key, request) {
  return page._unloaded !== true
    && page._hidden !== true
    && Number(page._lifecycleGeneration || 0) === request.lifecycle
    && Number(page[key] || 0) === request.requestId;
}

function lifecycleIsActive(page, lifecycle) {
  return page._unloaded !== true && Number(page._lifecycleGeneration || 0) === lifecycle;
}

Page({
  data: {
    pairId: null,
    elderName: '家人',
    weather: normalizeSnapshot({}),
    weatherStatus: '天气待更新',
    actions: [],
    selectedActions: [],
    contextReady: false,
    loadError: '',
    confirmed: false,
    helpRecorded: false,
    helpNote: '',
    completionOptions: COMPLETION_OPTIONS,
    completionIndex: 0,
    question1: '全部完成',
    question2: '',
    question3: '',
    difficulty: '',
    debriefOptin: true,
    busyAction: '',
    loading: true,
  },

  async onLoad(options) {
    this._unloaded = false;
    this._hidden = false;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    if (!requireToken()) return;
    const pairId = Number(options.pair_id || 0) || null;
    this._routePairId = pairId;
    this.setData({ pairId });
    if (!pairId) {
      this.setData({
        contextReady: false,
        actions: [],
        selectedActions: [],
        loadError: '缺少家人信息，请返回上一页重新选择。',
        loading: false,
      });
      wx.showToast({ title: '请选择一位家人', icon: 'none' });
      return;
    }
    await guardHealthSensitivePage(this, () => this.loadContext());
  },

  async onShow() {
    this._hidden = false;
    if (!requireToken()) return;
    const resumed = await resumeHealthMutation(this);
    if (this._unloaded || this._hidden) return;
    const resumedDebrief = resumed.resumed && resumed.kind === 'action-debrief';
    if (resumed.resumed) {
      const nextData = { busyAction: '', loading: true };
      if (resumedDebrief && resumed.ok) {
        Object.assign(nextData, { question2: '', question3: '', difficulty: '' });
      }
      this.setData(nextData);
    }
    if (resumed.resumed && !requireToken()) return;
    if (!this.data.pairId && this._routePairId) this.setData({ pairId: this._routePairId, loading: true });
    await guardHealthSensitivePage(this, () => this.loadContext());
    if (this._unloaded || this._hidden || !resumedDebrief) return;
    wx.showToast({
      title: resumed.ok ? '复盘已保存并重新核对' : '复盘未保存，请重试',
      icon: resumed.ok ? 'success' : 'none',
    });
  },

  onHide() {
    suspendHealthMutation(this);
    this._hidden = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._contextRequestId = Number(this._contextRequestId || 0) + 1;
  },

  onUnload() {
    this._unloaded = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._contextRequestId = Number(this._contextRequestId || 0) + 1;
  },

  onSessionInvalidated() {
    this._healthConsentLoadedOnce = false;
    this._healthConsentLoadedToken = '';
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._contextRequestId = Number(this._contextRequestId || 0) + 1;
    this._routePairId = null;
    if (this._unloaded) return;
    this.setData({
      pairId: null,
      elderName: '家人',
      weather: normalizeSnapshot({}),
      weatherStatus: '天气待更新',
      actions: [],
      selectedActions: [],
      contextReady: false,
      loadError: '',
      confirmed: false,
      helpRecorded: false,
      helpNote: '',
      completionIndex: 0,
      question1: '全部完成',
      question2: '',
      question3: '',
      difficulty: '',
      debriefOptin: true,
      busyAction: '',
      loading: false,
    });
  },

  onHealthConsentRequired() {
    this._healthConsentReloadPending = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._contextRequestId = Number(this._contextRequestId || 0) + 1;
    if (this._unloaded) return;
    this.setData({
      pairId: null,
      elderName: '家人',
      weather: normalizeSnapshot({}),
      weatherStatus: '天气待更新',
      actions: [],
      selectedActions: [],
      contextReady: false,
      loadError: '',
      confirmed: false,
      helpRecorded: false,
      helpNote: '',
      completionIndex: 0,
      question1: '全部完成',
      question2: '',
      question3: '',
      difficulty: '',
      debriefOptin: true,
      busyAction: '',
      loading: true,
    });
  },

  async loadContext() {
    if (this._unloaded || this._hidden) return;
    const request = beginRequest(this, '_contextRequestId');
    const pairId = Number(this.data.pairId || 0);
    if (!pairId) {
      this.setData({
        loading: false,
        contextReady: false,
        actions: [],
        selectedActions: [],
        loadError: '缺少家人信息，请返回上一页重新选择。',
      });
      return;
    }

    // 身份重新核验完成前先关闭所有个性化写入口，避免沿用旧人的行动上下文。
    this.setData({
      loading: true,
      contextReady: false,
      loadError: '',
      elderName: '家人',
      weather: normalizeSnapshot({}),
      weatherStatus: '天气待更新',
      actions: [],
      selectedActions: [],
      confirmed: false,
      helpRecorded: false,
    });
    try {
      const [elderData, snapshot] = await Promise.all([
        authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
        getSnapshot().catch(() => ({})),
      ]);
      const elder = normalizeList(elderData, ['items', 'elders'])
        .find((item) => Number(item.pair_id) === pairId);
      if (!requestIsActive(this, '_contextRequestId', request)) return;
      if (!elder) throw new Error('not_found');
      const weather = normalizeSnapshot(snapshot);
      const plan = actionPlan(weather.stale ? '' : weather.trigger);
      const restored = restoreTodayActions(plan, elder.today);
      this.setData({
        elderName: elder.member && elder.member.name ? elder.member.name : '家人',
        weather,
        weatherStatus: weatherStatus(weather),
        // 较早天气只作可见参考，行动清单退回通用安全项，避免沿用过期风险判断。
        actions: restored.actions,
        selectedActions: restored.selectedActions,
        contextReady: true,
        loadError: '',
        confirmed: restored.confirmed,
        helpRecorded: restored.helpRecorded,
      });
    } catch (error) {
      if (!requestIsActive(this, '_contextRequestId', request)) return;
      this.setData({
        elderName: '家人',
        weather: normalizeSnapshot({}),
        weatherStatus: '天气待更新',
        actions: [],
        selectedActions: [],
        contextReady: false,
        loadError: CONTEXT_LOAD_ERROR,
        confirmed: false,
        helpRecorded: false,
      });
    } finally {
      if (requestIsActive(this, '_contextRequestId', request)) this.setData({ loading: false });
    }
  },

  hasVerifiedContext() {
    return this._unloaded !== true && this.data.contextReady === true && Number(this.data.pairId || 0) > 0;
  },

  ensureContextReady() {
    if (this.hasVerifiedContext()) return true;
    if (!this._unloaded) wx.showToast({ title: '请先重新加载家人信息', icon: 'none' });
    return false;
  },

  onActionsChange(event) {
    if (!this.hasVerifiedContext() || this.data.busyAction) return;
    const allowed = new Set(this.data.actions.map((item) => item.id));
    const selectedActions = Array.isArray(event.detail.value)
      ? event.detail.value.filter((value) => allowed.has(value))
      : [];
    const selected = new Set(selectedActions);
    this.setData({
      selectedActions,
      confirmed: false,
      actions: this.data.actions.map((item) => ({ ...item, checked: selected.has(item.id) })),
    });
  },

  onHelpNote(event) { this.setData({ helpNote: event.detail.value || '' }); },
  onQuestion2(event) { this.setData({ question2: event.detail.value || '' }); },
  onQuestion3(event) { this.setData({ question3: event.detail.value || '' }); },
  onDifficulty(event) { this.setData({ difficulty: event.detail.value || '' }); },
  onDebriefOptin(event) { this.setData({ debriefOptin: !!event.detail.value }); },

  onCompletionChange(event) {
    const completionIndex = Number(event.detail.value || 0);
    this.setData({ completionIndex, question1: COMPLETION_OPTIONS[completionIndex] });
  },

  async confirmActions() {
    if (!this.ensureContextReady()) return;
    if (this.data.busyAction) return;
    const allowed = new Set(this.data.actions.map((item) => item.id));
    const selectedActions = this.data.selectedActions.filter((value) => allowed.has(value));
    if (!selectedActions.length) {
      wx.showToast({ title: '请至少勾选一项', icon: 'none' });
      return;
    }
    const lifecycle = Number(this._lifecycleGeneration || 0);
    const pairId = Number(this.data.pairId);
    this.setData({ busyAction: 'confirm' });
    let mutation = null;
    try {
      mutation = trackHealthMutation(
        this,
        authApi({
          method: 'POST',
          path: `/mp/api/v1/actions/${pairId}/confirm`,
          data: { actions_done: selectedActions },
        }),
        'action-confirm',
        { selectedActions: selectedActions.slice() }
      );
      await mutation;
      if (!lifecycleIsActive(this, lifecycle)) return;
      const selected = new Set(selectedActions);
      this.setData({
        selectedActions: selectedActions.slice(),
        actions: this.data.actions.map((item) => ({ ...item, checked: selected.has(item.id) })),
        confirmed: true,
      });
      wx.showToast({ title: '今日行动已记录', icon: 'success' });
    } catch (error) {
      if (lifecycleIsActive(this, lifecycle)) wx.showToast({ title: '确认失败，请稍后再试', icon: 'none' });
    } finally {
      finishHealthMutation(this, mutation);
      if (lifecycleIsActive(this, lifecycle)) this.setData({ busyAction: '' });
    }
  },

  requestHelp() {
    if (!this.ensureContextReady()) return;
    if (this.data.busyAction) return;
    const lifecycle = Number(this._lifecycleGeneration || 0);
    const updating = this.data.helpRecorded === true;
    wx.showModal({
      title: updating ? '更新求助说明？' : '记录一条求助需求？',
      content: `${updating ? '本操作会更新已保存的求助说明' : '本操作只保存求助记录'}，不会通过微信、短信或电话自动通知家人。请同时直接联系家人，紧急情况请拨打 120。`,
      confirmText: updating ? '更新说明' : '保存记录',
      confirmColor: '#b42318',
      success: async (result) => {
        if (!lifecycleIsActive(this, lifecycle)) return;
        if (!result.confirm) return;
        // 弹窗停留期间上下文可能失效，真正写入前必须再次核验。
        if (!this.ensureContextReady() || this.data.busyAction) return;
        this.setData({ busyAction: 'help' });
        const pairId = Number(this.data.pairId);
        let mutation = null;
        try {
          mutation = trackHealthMutation(
            this,
            authApi({
              method: 'POST',
              path: `/mp/api/v1/actions/${pairId}/help`,
              data: { note: String(this.data.helpNote || '').trim().slice(0, 300) },
            }),
            'action-help'
          );
          await mutation;
          if (!lifecycleIsActive(this, lifecycle)) return;
          this.setData({ helpRecorded: true });
          wx.showToast({ title: updating ? '求助说明已更新' : '求助需求已记录', icon: 'success' });
        } catch (error) {
          if (lifecycleIsActive(this, lifecycle)) wx.showToast({ title: '记录失败，请直接联系家人', icon: 'none' });
        } finally {
          finishHealthMutation(this, mutation);
          if (lifecycleIsActive(this, lifecycle)) this.setData({ busyAction: '' });
        }
      },
    });
  },

  callEmergency() {
    const lifecycle = Number(this._lifecycleGeneration || 0);
    wx.showModal({
      title: '拨打 120',
      content: '如有胸痛、呼吸困难、意识异常等紧急情况，请立即求助。确认后将打开电话拨号。',
      confirmText: '拨打 120',
      confirmColor: '#b42318',
      success: (result) => {
        if (lifecycleIsActive(this, lifecycle) && result.confirm) wx.makePhoneCall({ phoneNumber: '120' });
      },
    });
  },

  async submitDebrief() {
    if (!this.ensureContextReady()) return;
    if (this.data.busyAction) return;
    if (!this.data.question2 && !this.data.question3 && !this.data.difficulty) {
      wx.showToast({ title: '请至少填写一项复盘内容', icon: 'none' });
      return;
    }
    const lifecycle = Number(this._lifecycleGeneration || 0);
    const pairId = Number(this.data.pairId);
    this.setData({ busyAction: 'debrief' });
    let mutation = null;
    try {
      mutation = trackHealthMutation(
        this,
        authApi({
          method: 'POST',
          path: `/mp/api/v1/actions/${pairId}/debrief`,
          data: {
            question_1: this.data.question1,
            question_2: String(this.data.question2 || '').trim().slice(0, 200),
            question_3: String(this.data.question3 || '').trim().slice(0, 200),
            difficulty: String(this.data.difficulty || '').trim().slice(0, 500),
            debrief_optin: this.data.debriefOptin,
          },
        }),
        'action-debrief'
      );
      await mutation;
      if (!lifecycleIsActive(this, lifecycle)) return;
      this.setData({ question2: '', question3: '', difficulty: '' });
      wx.showToast({ title: '复盘已保存', icon: 'success' });
    } catch (error) {
      if (lifecycleIsActive(this, lifecycle)) wx.showToast({ title: '复盘提交失败', icon: 'none' });
    } finally {
      finishHealthMutation(this, mutation);
      if (lifecycleIsActive(this, lifecycle)) this.setData({ busyAction: '' });
    }
  },
});
