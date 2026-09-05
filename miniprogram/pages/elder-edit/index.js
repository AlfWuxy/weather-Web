const { api, requireAuth, handleApiError } = require('../../utils/request');

const TOAST_MS = 1500;

function splitChronic(text) {
  const raw = (text || '').split(/[,，]/).map((s) => s.trim()).filter(Boolean);
  const seen = new Set();
  const out = [];
  raw.forEach((x) => {
    if (seen.has(x)) return;
    seen.add(x);
    out.push(x);
  });
  return out;
}

function parsePairId(raw) {
  if (raw === undefined || raw === null || raw === '') return null;
  const n = parseInt(raw, 10);
  if (!Number.isInteger(n) || n <= 0) return null;
  return n;
}

function toastThenBack(title, icon) {
  wx.showToast({ title, icon, duration: TOAST_MS, mask: true });
  setTimeout(() => wx.navigateBack(), TOAST_MS);
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
    loading: false,
  },

  async onLoad(options) {
    const mode = options.mode === 'create' ? 'create' : 'edit';
    const pairId = parsePairId(options.pair_id);

    if (mode === 'edit' && !pairId) {
      this.setData({ mode, pairId: null, loading: true });
      toastThenBack('缺少老人信息', 'none');
      return;
    }

    this.setData({ mode, pairId, loading: mode === 'edit' });
    if (mode === 'edit') {
      await this.loadPair(pairId);
    }
  },

  async loadPair(pairId) {
    const token = requireAuth();
    if (!token) return;
    try {
      const elders = await api({ method: 'GET', path: '/mp/api/v1/elders', token });
      const item = (elders || []).find((x) => Number(x.pair_id) === pairId);
      if (!item) {
        toastThenBack('未找到该老人', 'none');
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
        loading: false,
      });
    } catch (e) {
      if (handleApiError(e, { fallbackTitle: '加载失败' })) return;
      toastThenBack('加载失败', 'none');
    }
  },

  onName(e) { this.setData({ name: e.detail.value || '' }); },
  onRelation(e) { this.setData({ relation: e.detail.value || '' }); },
  onAge(e) { this.setData({ age: e.detail.value || '' }); },
  onGender(e) { this.setData({ gender: e.detail.value || '' }); },
  onLocation(e) { this.setData({ locationQuery: e.detail.value || '' }); },
  onChronic(e) { this.setData({ chronicText: e.detail.value || '' }); },

  async onSave() {
    if (this.data.busy || this.data.loading) return;
    const token = requireAuth();
    if (!token) return;

    const name = (this.data.name || '').trim();
    const relation = (this.data.relation || '').trim();
    const gender = (this.data.gender || '').trim();
    const locationQuery = (this.data.locationQuery || '').trim();
    const ageRaw = (this.data.age || '').trim();
    const age = ageRaw ? parseInt(ageRaw, 10) : null;
    const ageValue = Number.isInteger(age) ? age : null;

    if (!locationQuery) {
      wx.showToast({ title: '请填写所在地', icon: 'none' });
      return;
    }
    if (this.data.mode === 'create' && !name) {
      wx.showToast({ title: '请填写称呼/姓名', icon: 'none' });
      return;
    }
    if (this.data.mode === 'edit' && !this.data.pairId) {
      toastThenBack('缺少老人信息', 'none');
      return;
    }

    this.setData({ busy: true });
    try {
      if (this.data.mode === 'create') {
        await api({
          method: 'POST',
          path: '/mp/api/v1/elders',
          token,
          data: {
            name,
            relation,
            age: ageValue,
            gender,
            location_query: locationQuery,
            chronic_diseases: splitChronic(this.data.chronicText),
          },
        });
        toastThenBack('已创建', 'success');
        return;
      }

      await api({
        method: 'PATCH',
        path: `/mp/api/v1/elders/${this.data.pairId}`,
        token,
        data: {
          location_query: locationQuery,
          chronic_diseases: splitChronic(this.data.chronicText),
        },
      });
      toastThenBack('已保存', 'success');
    } catch (e) {
      if (handleApiError(e, { fallbackTitle: '保存失败' })) return;
      this.setData({ busy: false });
    }
  },

  onCancel() {
    if (this.data.busy) return;
    wx.navigateBack();
  },
});
