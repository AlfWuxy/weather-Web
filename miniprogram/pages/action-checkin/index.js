const { authApi, getSnapshot, requireToken } = require('../elders/care-session');
const { normalizeList, normalizeSnapshot } = require('../elders/care-logic');

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

Page({
  data: {
    pairId: null,
    elderName: '家人',
    weather: normalizeSnapshot({}),
    actions: ACTION_PLANS.normal,
    selectedActions: [],
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
    loading: false,
  },

  async onLoad(options) {
    if (!requireToken()) return;
    const pairId = Number(options.pair_id || 0) || null;
    this.setData({ pairId });
    if (!pairId) {
      wx.showToast({ title: '请选择一位家人', icon: 'none' });
      return;
    }
    await this.loadContext();
  },

  async loadContext() {
    this.setData({ loading: true });
    try {
      const [elderData, snapshot] = await Promise.all([
        authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
        getSnapshot().catch(() => ({})),
      ]);
      const elder = normalizeList(elderData, ['items', 'elders'])
        .find((item) => Number(item.pair_id) === this.data.pairId);
      if (!elder) throw new Error('not_found');
      const weather = normalizeSnapshot(snapshot);
      this.setData({
        elderName: elder.member && elder.member.name ? elder.member.name : '家人',
        weather,
        actions: ACTION_PLANS[weather.trigger] || ACTION_PLANS.normal,
      });
    } catch (error) {
      wx.showToast({ title: '行动清单加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  onActionsChange(event) {
    this.setData({ selectedActions: event.detail.value || [] });
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
    if (this.data.busyAction) return;
    this.setData({ busyAction: 'confirm' });
    try {
      await authApi({
        method: 'POST',
        path: `/mp/api/v1/actions/${this.data.pairId}/confirm`,
        data: { actions_done: this.data.selectedActions },
      });
      this.setData({ confirmed: true });
      wx.showToast({ title: '今日行动已确认', icon: 'success' });
    } catch (error) {
      wx.showToast({ title: '确认失败，请稍后再试', icon: 'none' });
    } finally {
      this.setData({ busyAction: '' });
    }
  },

  requestHelp() {
    if (this.data.busyAction) return;
    wx.showModal({
      title: '记录一条求助需求？',
      content: '本操作只保存求助记录，不会通过微信、短信或电话自动通知家人。请同时直接联系家人，紧急情况请拨打 120。',
      confirmText: '保存记录',
      confirmColor: '#b42318',
      success: async (result) => {
        if (!result.confirm) return;
        this.setData({ busyAction: 'help' });
        try {
          await authApi({
            method: 'POST',
            path: `/mp/api/v1/actions/${this.data.pairId}/help`,
            data: { note: String(this.data.helpNote || '').trim().slice(0, 300) },
          });
          this.setData({ helpRecorded: true });
          wx.showToast({ title: '求助需求已记录', icon: 'success' });
        } catch (error) {
          wx.showToast({ title: '记录失败，请直接联系家人', icon: 'none' });
        } finally {
          this.setData({ busyAction: '' });
        }
      },
    });
  },

  callEmergency() {
    wx.showModal({
      title: '拨打 120',
      content: '如有胸痛、呼吸困难、意识异常等紧急情况，请立即求助。确认后将打开电话拨号。',
      confirmText: '拨打 120',
      confirmColor: '#b42318',
      success: (result) => {
        if (result.confirm) wx.makePhoneCall({ phoneNumber: '120' });
      },
    });
  },

  async submitDebrief() {
    if (this.data.busyAction) return;
    if (!this.data.question2 && !this.data.question3 && !this.data.difficulty) {
      wx.showToast({ title: '请至少填写一项复盘内容', icon: 'none' });
      return;
    }
    this.setData({ busyAction: 'debrief' });
    try {
      await authApi({
        method: 'POST',
        path: `/mp/api/v1/actions/${this.data.pairId}/debrief`,
        data: {
          question_1: this.data.question1,
          question_2: String(this.data.question2 || '').trim().slice(0, 200),
          question_3: String(this.data.question3 || '').trim().slice(0, 200),
          difficulty: String(this.data.difficulty || '').trim().slice(0, 500),
          debrief_optin: this.data.debriefOptin,
        },
      });
      this.setData({ question2: '', question3: '', difficulty: '' });
      wx.showToast({ title: '复盘已保存', icon: 'success' });
    } catch (error) {
      wx.showToast({ title: '复盘提交失败', icon: 'none' });
    } finally {
      this.setData({ busyAction: '' });
    }
  },
});
