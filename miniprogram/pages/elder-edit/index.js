const { authApi, requireToken } = require('../elders/care-session');
const { FIXED_LOCATION, normalizeList, validateElderInput } = require('../elders/care-logic');

const GENDER_OPTIONS = ['未填写', '女性', '男性'];

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
    if (!requireToken()) return;
    const pairId = Number(options.pair_id || 0) || null;
    const mode = options.mode === 'create' || !pairId ? 'create' : 'edit';
    this.setData({ mode, pairId });
    if (mode === 'edit') await this.loadElder();
  },

  async loadElder() {
    this.setData({ loading: true });
    try {
      const data = await authApi({ method: 'GET', path: '/mp/api/v1/elders' });
      const item = normalizeList(data, ['items', 'elders']).find((elder) => Number(elder.pair_id) === this.data.pairId);
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
      wx.showToast({ title: '没有找到这位老人', icon: 'none' });
    } finally {
      this.setData({ loading: false });
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
    if (this.data.busy) return;
    const validation = validateElderInput(this.data, { mode: this.data.mode });
    if (!validation.valid) {
      wx.showToast({ title: validation.error, icon: 'none' });
      return;
    }
    this.setData({ busy: true });
    try {
      const options = this.data.mode === 'create'
        ? { method: 'POST', path: '/mp/api/v1/elders', data: validation.payload }
        : { method: 'PATCH', path: `/mp/api/v1/elders/${this.data.pairId}`, data: validation.payload };
      await authApi(options);
      wx.showToast({ title: this.data.mode === 'create' ? '已添加' : '已保存', icon: 'success' });
      setTimeout(() => wx.navigateBack(), 300);
    } catch (error) {
      wx.showToast({ title: '保存失败，请稍后再试', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  onCancel() {
    wx.navigateBack();
  },
});
