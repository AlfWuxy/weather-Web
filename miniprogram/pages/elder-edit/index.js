const { authApi, requireToken } = require('../elders/care-session');
const { FIXED_LOCATION, normalizeList, validateElderInput } = require('../elders/care-logic');

const GENDER_OPTIONS = ['未填写', '女性', '男性'];

function lifecycleIsActive(page, lifecycle) {
  return page._unloaded !== true && Number(page._lifecycleGeneration || 0) === lifecycle;
}

function beginLoad(page) {
  page._loadRequestId = Number(page._loadRequestId || 0) + 1;
  return {
    lifecycle: Number(page._lifecycleGeneration || 0),
    requestId: page._loadRequestId,
  };
}

function loadIsActive(page, request) {
  return lifecycleIsActive(page, request.lifecycle) && page._loadRequestId === request.requestId;
}

Page({
  data: {
    mode: 'create',
    pairId: null,
    name: '',
    relation: '',
    age: '',
    gender: '未填写',
    genderOptions: GENDER_OPTIONS,
    genderIndex: 0,
    chronicText: '',
    fixedLocation: FIXED_LOCATION,
    loading: false,
    busy: false,
  },

  async onLoad(options) {
    this._unloaded = false;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
    if (!requireToken()) return;
    const pairId = Number(options.pair_id || 0) || null;
    const mode = options.mode === 'create' || !pairId ? 'create' : 'edit';
    this.setData({ mode, pairId });
    if (mode === 'edit') await this.loadElder();
  },

  onShow() {
    requireToken();
  },

  onSessionInvalidated() {
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
    if (this._returnTimer) clearTimeout(this._returnTimer);
    this._returnTimer = null;
    if (this._unloaded) return;
    this.setData({
      mode: 'create',
      pairId: null,
      name: '',
      relation: '',
      age: '',
      gender: '未填写',
      genderIndex: 0,
      chronicText: '',
      loading: false,
      busy: false,
    });
  },

  onUnload() {
    this._unloaded = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
    if (this._returnTimer) clearTimeout(this._returnTimer);
    this._returnTimer = null;
  },

  async loadElder() {
    if (this._unloaded) return;
    const request = beginLoad(this);
    const pairId = Number(this.data.pairId || 0);
    this.setData({ loading: true });
    try {
      const data = await authApi({ method: 'GET', path: '/mp/api/v1/elders' });
      if (!loadIsActive(this, request)) return;
      const item = normalizeList(data, ['items', 'elders']).find((elder) => Number(elder.pair_id) === pairId);
      if (!item) throw new Error('not_found');
      const member = item.member || {};
      const genderIndex = Math.max(0, GENDER_OPTIONS.indexOf(member.gender || '未填写'));
      this.setData({
        name: member.name || '',
        relation: member.relation || '',
        age: member.age ? String(member.age) : '',
        gender: GENDER_OPTIONS[genderIndex],
        genderIndex,
        chronicText: Array.isArray(member.chronic_diseases) ? member.chronic_diseases.join('、') : '',
      });
    } catch (error) {
      if (loadIsActive(this, request)) wx.showToast({ title: '没有找到这位老人', icon: 'none' });
    } finally {
      if (loadIsActive(this, request)) this.setData({ loading: false });
    }
  },

  onName(event) { this.setData({ name: event.detail.value || '' }); },
  onRelation(event) { this.setData({ relation: event.detail.value || '' }); },
  onAge(event) { this.setData({ age: event.detail.value || '' }); },
  onChronic(event) { this.setData({ chronicText: event.detail.value || '' }); },

  onGender(event) {
    const genderIndex = Number(event.detail.value || 0);
    this.setData({ genderIndex, gender: GENDER_OPTIONS[genderIndex] });
  },

  async onSave() {
    if (this.data.busy || this._unloaded) return;
    const validation = validateElderInput(this.data, { mode: this.data.mode });
    if (!validation.valid) {
      wx.showToast({ title: validation.error, icon: 'none' });
      return;
    }
    const lifecycle = Number(this._lifecycleGeneration || 0);
    const mode = this.data.mode;
    const pairId = this.data.pairId;
    this.setData({ busy: true });
    try {
      const options = mode === 'create'
        ? { method: 'POST', path: '/mp/api/v1/elders', data: validation.payload }
        : { method: 'PATCH', path: `/mp/api/v1/elders/${pairId}`, data: validation.payload };
      await authApi(options);
      if (!lifecycleIsActive(this, lifecycle)) return;
      wx.showToast({ title: mode === 'create' ? '已添加' : '已保存', icon: 'success' });
      if (this._returnTimer) clearTimeout(this._returnTimer);
      this._returnTimer = setTimeout(() => {
        this._returnTimer = null;
        if (lifecycleIsActive(this, lifecycle)) wx.navigateBack();
      }, 300);
    } catch (error) {
      if (lifecycleIsActive(this, lifecycle)) wx.showToast({ title: '保存失败，请稍后再试', icon: 'none' });
    } finally {
      if (lifecycleIsActive(this, lifecycle)) this.setData({ busy: false });
    }
  },

  onCancel() {
    if (this._returnTimer) clearTimeout(this._returnTimer);
    this._returnTimer = null;
    wx.navigateBack();
  },
});
