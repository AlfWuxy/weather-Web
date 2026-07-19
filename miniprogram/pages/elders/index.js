const {
  authApi,
  finishHealthMutation,
  getSnapshot,
  guardHealthSensitivePage,
  requireToken,
  resumeHealthMutation,
  suspendHealthMutation,
  trackHealthMutation,
} = require('./care-session');
const {
  FIXED_LOCATION,
  markSnapshotStale,
  normalizeList,
  normalizeSnapshot,
} = require('./care-logic');

function memberName(item) {
  return item && item.member && item.member.name ? item.member.name : '家中老人';
}

function beginLoad(page) {
  page._loadRequestId = Number(page._loadRequestId || 0) + 1;
  return {
    lifecycle: Number(page._lifecycleGeneration || 0),
    requestId: page._loadRequestId,
  };
}

function loadIsActive(page, request) {
  return page._unloaded !== true
    && page._hidden !== true
    && Number(page._lifecycleGeneration || 0) === request.lifecycle
    && Number(page._loadRequestId || 0) === request.requestId;
}

function lifecycleIsActive(page, lifecycle) {
  return page._unloaded !== true
    && page._hidden !== true
    && Number(page._lifecycleGeneration || 0) === lifecycle;
}

Page({
  data: {
    elders: [],
    weather: normalizeSnapshot({}),
    loading: true,
    loadError: '',
    busyPairId: 0,
    fixedLocation: FIXED_LOCATION,
  },

  async onShow() {
    this._unloaded = false;
    this._hidden = false;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    if (!requireToken()) return;
    const resumed = await resumeHealthMutation(this);
    if (this._unloaded || this._hidden) return;
    const resumedDelete = resumed.resumed && resumed.kind === 'elder-delete';
    this.setData({ busyPairId: 0 });
    if (resumed.resumed) this.setData({ loading: true });
    await guardHealthSensitivePage(this, () => this.loadCareHome());
    if (this._unloaded || this._hidden || !resumedDelete) return;
    wx.showToast({
      title: resumed.ok ? '已停止管理并重新核对' : '操作未完成，请重试',
      icon: resumed.ok ? 'success' : 'none',
    });
  },

  onHide() {
    suspendHealthMutation(this);
    this._hidden = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
  },

  onUnload() {
    this._unloaded = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
  },

  onSessionInvalidated() {
    this._healthConsentLoadedOnce = false;
    this._healthConsentLoadedToken = '';
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
    this.setData({
      elders: [],
      weather: normalizeSnapshot({}),
      loading: true,
      loadError: '',
      busyPairId: 0,
    });
  },

  onHealthConsentRequired() {
    this._healthConsentReloadPending = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
    if (this._unloaded) return;
    this.setData({
      elders: [],
      weather: normalizeSnapshot({}),
      loading: true,
      loadError: '',
      busyPairId: 0,
    });
  },

  async onPullDownRefresh() {
    try {
      if (!requireToken()) return;
      this._healthConsentReloadPending = true;
      await guardHealthSensitivePage(this, () => this.loadCareHome());
    } finally {
      wx.stopPullDownRefresh();
    }
  },

  async loadCareHome() {
    if (this._unloaded || this._hidden) return;
    const request = beginLoad(this);
    this.setData({ loading: true, loadError: '' });
    try {
      // 都昌县天气只读取共享 30 分钟快照，不按老人重复请求。
      const [elderData, snapshot] = await Promise.all([
        authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
        getSnapshot().catch(() => null),
      ]);
      if (!loadIsActive(this, request)) return;
      const weather = snapshot ? normalizeSnapshot(snapshot) : markSnapshotStale(this.data.weather);
      const elders = normalizeList(elderData, ['items', 'elders']).map((item) => {
        // 列表页不渲染当天私有记录，避免把完整状态重复写入视图层。
        const { today: _unusedToday, ...elder } = item;
        return {
          ...elder,
          displayName: memberName(item),
          initial: memberName(item).slice(0, 1),
          displayRelation: item.member && item.member.relation ? item.member.relation : '家人',
          displayAge: item.adult_profile_incomplete || !(item.member && item.member.age)
            ? '请补充 18 岁以上年龄'
            : `${item.member.age} 岁`,
        };
      });
      this.setData({ elders, weather });
    } catch (error) {
      if (!loadIsActive(this, request)) return;
      const loadError = this.data.elders.length
        ? '刷新失败，以下仍显示上次成功加载的照护资料。'
        : '照护资料暂时没有加载出来，请稍后再试。';
      const weather = markSnapshotStale(this.data.weather);
      this.setData({ loadError, weather });
    } finally {
      if (loadIsActive(this, request)) this.setData({ loading: false });
    }
  },

  goCreate() {
    if (this._healthConsentLoadedOnce !== true) return;
    wx.navigateTo({ url: '/pages/elder-edit/index?mode=create' });
  },

  goSettings() {
    wx.switchTab({ url: '/pages/settings/index' });
  },

  goAlerts() {
    wx.navigateTo({ url: '/pages/alerts/index' });
  },

  goTemplate(event) {
    const pairId = event.currentTarget.dataset.pairId;
    wx.navigateTo({ url: `/pages/template/index?pair_id=${pairId}` });
  },

  goEdit(event) {
    const pairId = event.currentTarget.dataset.pairId;
    wx.navigateTo({ url: `/pages/elder-edit/index?pair_id=${pairId}` });
  },

  goAssessment(event) {
    const pairId = event.currentTarget.dataset.pairId;
    wx.navigateTo({ url: `/pages/health-assessment/index?pair_id=${pairId}` });
  },

  goDiary(event) {
    const pairId = event.currentTarget.dataset.pairId;
    wx.navigateTo({ url: `/pages/diary/index?pair_id=${pairId}` });
  },

  goMedications(event) {
    const pairId = event.currentTarget.dataset.pairId;
    wx.navigateTo({ url: `/pages/medications/index?pair_id=${pairId}` });
  },

  goCheckin(event) {
    const pairId = event.currentTarget.dataset.pairId;
    wx.navigateTo({ url: `/pages/action-checkin/index?pair_id=${pairId}` });
  },

  deleteElder(event) {
    if (this.data.busyPairId) return;
    const pairId = Number(event.currentTarget.dataset.pairId);
    if (!pairId) return;
    const name = event.currentTarget.dataset.name || '这位老人';
    const lifecycle = Number(this._lifecycleGeneration || 0);
    this.setData({ busyPairId: pairId });
    wx.showModal({
      title: '停止管理这位老人？',
      content: `将停止展示${name}的照护资料。历史记录会按服务规则保留。`,
      confirmText: '停止管理',
      confirmColor: '#b42318',
      success: async (result) => {
        if (!lifecycleIsActive(this, lifecycle)) return;
        if (!result.confirm) {
          if (Number(this.data.busyPairId) === pairId) this.setData({ busyPairId: 0 });
          return;
        }
        let mutation = null;
        try {
          mutation = trackHealthMutation(
            this,
            authApi({ method: 'DELETE', path: `/mp/api/v1/elders/${pairId}` }),
            'elder-delete'
          );
          await mutation;
          if (!lifecycleIsActive(this, lifecycle)) return;
          wx.showToast({ title: '已停止管理', icon: 'success' });
          await this.loadCareHome();
        } catch (error) {
          if (lifecycleIsActive(this, lifecycle)) wx.showToast({ title: '操作失败，请稍后再试', icon: 'none' });
        } finally {
          finishHealthMutation(this, mutation);
          if (lifecycleIsActive(this, lifecycle) && Number(this.data.busyPairId) === pairId) {
            this.setData({ busyPairId: 0 });
          }
        }
      },
      fail: () => {
        if (lifecycleIsActive(this, lifecycle) && Number(this.data.busyPairId) === pairId) {
          this.setData({ busyPairId: 0 });
        }
      },
    });
  },
});
