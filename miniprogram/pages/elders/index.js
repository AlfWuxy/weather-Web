const { authApi, getSnapshot, requireToken } = require('./care-session');
const {
  FIXED_LOCATION,
  markSnapshotStale,
  normalizeList,
  normalizeSnapshot,
} = require('./care-logic');

function memberName(item) {
  return item && item.member && item.member.name ? item.member.name : '家中老人';
}

Page({
  data: {
    elders: [],
    weather: normalizeSnapshot({}),
    loading: false,
    loadError: '',
    fixedLocation: FIXED_LOCATION,
  },

  async onShow() {
    if (!requireToken()) return;
    await this.loadCareHome();
  },

  onSessionInvalidated() {
    this.setData({
      elders: [],
      weather: normalizeSnapshot({}),
      loading: false,
      loadError: '',
    });
  },

  async onPullDownRefresh() {
    try {
      await this.loadCareHome();
    } finally {
      wx.stopPullDownRefresh();
    }
  },

  async loadCareHome() {
    if (this.data.loading) return;
    this.setData({ loading: true, loadError: '' });
    try {
      // 都昌县天气只读取共享 30 分钟快照，不按老人重复请求。
      const [elderData, snapshot] = await Promise.all([
        authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
        getSnapshot().catch(() => null),
      ]);
      const weather = snapshot ? normalizeSnapshot(snapshot) : markSnapshotStale(this.data.weather);
      const elders = normalizeList(elderData, ['items', 'elders']).map((item) => ({
        ...item,
        displayName: memberName(item),
        initial: memberName(item).slice(0, 1),
        displayRelation: item.member && item.member.relation ? item.member.relation : '家人',
        displayAge: item.member && item.member.age ? `${item.member.age} 岁` : '年龄未填写',
        today: weather,
      }));
      this.setData({ elders, weather });
    } catch (error) {
      const loadError = this.data.elders.length
        ? '刷新失败，以下仍显示上次成功加载的照护资料。'
        : '照护资料暂时没有加载出来，请稍后再试。';
      const weather = markSnapshotStale(this.data.weather);
      const elders = this.data.elders.map((item) => ({ ...item, today: weather }));
      this.setData({ loadError, elders, weather });
    } finally {
      this.setData({ loading: false });
    }
  },

  goCreate() {
    wx.navigateTo({ url: '/pages/elder-edit/index?mode=create' });
  },

  goSettings() {
    wx.switchTab({ url: '/pages/settings/index' });
  },

  goAlerts(event) {
    const pairId = event.currentTarget.dataset.pairId;
    wx.navigateTo({ url: `/pages/alerts/index?pair_id=${pairId}` });
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
    const pairId = Number(event.currentTarget.dataset.pairId);
    const name = event.currentTarget.dataset.name || '这位老人';
    wx.showModal({
      title: '停止管理这位老人？',
      content: `将停止展示${name}的照护资料。历史记录会按服务规则保留。`,
      confirmText: '停止管理',
      confirmColor: '#b42318',
      success: async (result) => {
        if (!result.confirm) return;
        try {
          await authApi({ method: 'DELETE', path: `/mp/api/v1/elders/${pairId}` });
          wx.showToast({ title: '已停止管理', icon: 'success' });
          await this.loadCareHome();
        } catch (error) {
          wx.showToast({ title: '操作失败，请稍后再试', icon: 'none' });
        }
      },
    });
  },
});
