const { api, isUnauthorizedError } = require('../../utils/request');

function parsePairId(value) {
  const text = String(value || '').trim();
  if (!/^[1-9]\d*$/.test(text)) return null;
  const pairId = Number(text);
  return Number.isSafeInteger(pairId) ? pairId : null;
}

function showInvalidPairAndReturn() {
  wx.showModal({
    title: '无法打开',
    content: '监测对象不存在或链接已失效，将返回监测对象列表。',
    showCancel: false,
    complete: () => wx.reLaunch({ url: '/pages/elders/index' }),
  });
}

function splitChronic(text) {
  const raw = (text || '').split(/[,，]/).map((s) => s.trim()).filter(Boolean);
  // 慢病类别去重，保留用户原始顺序。
  const seen = new Set();
  const out = [];
  raw.forEach((x) => {
    if (seen.has(x)) return;
    seen.add(x);
    out.push(x);
  });
  return out;
}

function parseOptionalAge(value) {
  const text = String(value || '').trim();
  if (!text) return { valid: true, value: null };
  if (!/^\d{1,3}$/.test(text)) return { valid: false, value: null };
  const age = Number(text);
  return { valid: age >= 1 && age <= 150, value: age };
}

function normalizeOptionalGender(value) {
  const text = String(value || '').trim();
  if (!text) return { valid: true, value: '' };
  const aliases = {
    男: '男性',
    男性: '男性',
    女: '女性',
    女性: '女性',
    其他: '其他',
    未知: '未知',
  };
  return { valid: !!aliases[text], value: aliases[text] || '' };
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

  async onLoad(options = {}) {
    const mode = options.mode === 'create' ? 'create' : 'edit';
    const pairId = parsePairId(options.pair_id);
    this.setData({ mode, pairId });

    if (mode === 'edit' && !pairId) {
      showInvalidPairAndReturn();
      return;
    }
    if (mode === 'edit') {
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
        showInvalidPairAndReturn();
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
      if (isUnauthorizedError(e)) return;
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
    const chronic = splitChronic(this.data.chronicText);
    if (chronic.length > 20 || chronic.some((item) => item.length > 50)) {
      wx.showToast({ title: '慢病类别填写过多或过长', icon: 'none' });
      return;
    }
    const age = parseOptionalAge(this.data.age);
    if (this.data.mode === 'create' && !age.valid) {
      wx.showToast({ title: '年龄需为 1-150 的整数', icon: 'none' });
      return;
    }
    const gender = normalizeOptionalGender(this.data.gender);
    if (this.data.mode === 'create' && !gender.valid) {
      wx.showToast({ title: '性别请填写男性、女性、其他或未知', icon: 'none' });
      return;
    }
    this.setData({ busy: true });
    try {
      if (this.data.mode === 'create') {
        if (!this.data.name) {
          wx.showToast({ title: '请填写称呼/姓名', icon: 'none' });
          return;
        }
        await api({
          method: 'POST',
          path: '/mp/api/v1/elders',
          token,
          data: {
            name: this.data.name,
            relation: this.data.relation,
            age: age.value,
            gender: gender.value,
            location_query: this.data.locationQuery,
            chronic_diseases: chronic,
          },
        });
        wx.showToast({ title: '已创建', icon: 'success' });
        wx.navigateBack();
      } else {
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
      if (isUnauthorizedError(e)) return;
      if (e && e.code === 'not_found') {
        showInvalidPairAndReturn();
        return;
      }
      wx.showToast({ title: '保存失败', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  onCancel() {
    wx.navigateBack();
  },
});
