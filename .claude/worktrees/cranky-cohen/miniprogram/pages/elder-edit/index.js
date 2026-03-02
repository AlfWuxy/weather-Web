const { api } = require('../../utils/request');

function splitChronic(text) {
  const raw = (text || '').split(/[,，]/).map((s) => s.trim()).filter(Boolean);
  // de-dupe
  const seen = new Set();
  const out = [];
  raw.forEach((x) => {
    if (seen.has(x)) return;
    seen.add(x);
    out.push(x);
  });
  return out;
}

Page({
  data: {
    mode: 'edit',
    pairId: null,
    name: '',
    relation: '',
    age: '',
    gender: '',
    locationQuery: '',
    chronicText: '',
    busy: false,
  },

  getToken() {
    return (wx.getStorageSync('api_token') || '').trim();
  },

  async onLoad(options) {
    const mode = options.mode === 'create' ? 'create' : 'edit';
    const pairId = options.pair_id ? parseInt(options.pair_id, 10) : null;
    this.setData({ mode, pairId });

    if (mode === 'edit' && pairId) {
      await this.loadPair(pairId);
    }
  },

  async loadPair(pairId) {
    const token = this.getToken();
    if (!token) {
      wx.reLaunch({ url: '/pages/bind-token/index' });
      return;
    }
    try {
      const elders = await api({ method: 'GET', path: '/mp/api/v1/elders', token });
      const item = (elders || []).find((x) => x.pair_id === pairId);
      if (!item) {
        wx.showToast({ title: '未找到该老人', icon: 'none' });
        return;
      }
      const chronic = (item.member && item.member.chronic_diseases) ? item.member.chronic_diseases : [];
      this.setData({
        locationQuery: item.location_query || item.community_code || '',
        chronicText: (chronic || []).join(', '),
        name: (item.member && item.member.name) ? item.member.name : '',
        relation: (item.member && item.member.relation) ? item.member.relation : '',
        age: (item.member && item.member.age) ? String(item.member.age) : '',
        gender: (item.member && item.member.gender) ? item.member.gender : '',
      });
    } catch (e) {
      wx.showToast({ title: '加载失败', icon: 'none' });
    }
  },

  onName(e) { this.setData({ name: (e.detail.value || '').trim() }); },
  onRelation(e) { this.setData({ relation: (e.detail.value || '').trim() }); },
  onAge(e) { this.setData({ age: (e.detail.value || '').trim() }); },
  onGender(e) { this.setData({ gender: (e.detail.value || '').trim() }); },
  onLocation(e) { this.setData({ locationQuery: (e.detail.value || '').trim() }); },
  onChronic(e) { this.setData({ chronicText: e.detail.value || '' }); },

  async onSave() {
    if (this.data.busy) return;
    const token = this.getToken();
    if (!token) {
      wx.reLaunch({ url: '/pages/bind-token/index' });
      return;
    }
    if (!this.data.locationQuery) {
      wx.showToast({ title: '请填写所在地', icon: 'none' });
      return;
    }
    this.setData({ busy: true });
    try {
      if (this.data.mode === 'create') {
        if (!this.data.name) {
          wx.showToast({ title: '请填写称呼/姓名', icon: 'none' });
          return;
        }
        const chronic = splitChronic(this.data.chronicText);
        await api({
          method: 'POST',
          path: '/mp/api/v1/elders',
          token,
          data: {
            name: this.data.name,
            relation: this.data.relation,
            age: this.data.age ? parseInt(this.data.age, 10) : null,
            gender: this.data.gender,
            location_query: this.data.locationQuery,
            chronic_diseases: chronic,
          },
        });
        wx.showToast({ title: '已创建', icon: 'success' });
        wx.navigateBack();
      } else {
        const chronic = splitChronic(this.data.chronicText);
        await api({
          method: 'PATCH',
          path: `/mp/api/v1/elders/${this.data.pairId}`,
          token,
          data: {
            location_query: this.data.locationQuery,
            chronic_diseases: chronic,
          },
        });
        wx.showToast({ title: '已保存', icon: 'success' });
        wx.navigateBack();
      }
    } catch (e) {
      wx.showToast({ title: '保存失败', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  onCancel() {
    wx.navigateBack();
  },
});

