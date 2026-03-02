const { api } = require('../../utils/request');

function buildMessage({ trigger, elderName, relation, locationText, tmax, tmin }) {
  let address = '你';
  if (relation === '母亲' || relation === '妈妈' || relation === '妈') address = '妈';
  else if (relation === '父亲' || relation === '爸爸' || relation === '爸') address = '爸';
  else if (elderName) address = elderName;

  if (trigger === 'cold') {
    let line1 = `【寒潮提醒】${address}，我看到你那边今天可能比较冷`;
    if (tmin) line1 += `（最低约 ${tmin}°C）`;
    line1 += '。';
    return [
      line1,
      '建议：尽量少出门，外出注意保暖防滑；室内注意保暖，别受凉。',
      `地点：${locationText || '-'}`,
      '说明：这是行动提醒，不提供医疗诊断/治疗建议；如明显不适请及时就医。',
    ].join('\n');
  }
  if (trigger === 'heat') {
    let line1 = `【高温提醒】${address}，我看到你那边今天可能会很热`;
    if (tmax) line1 += `（最高约 ${tmax}°C）`;
    line1 += '。';
    return [
      line1,
      '建议：避开中午外出，多喝水；室内开风扇/空调或找阴凉处休息。',
      `地点：${locationText || '-'}`,
      '说明：这是行动提醒，不提供医疗诊断/治疗建议；如明显不适请及时就医。',
    ].join('\n');
  }
  return [
    `【日常提醒】${address}，我这边看看你那边天气有变化，注意劳逸结合，出门记得带水/外套。`,
    `地点：${locationText || '-'}`,
    '说明：这是行动提醒，不提供医疗诊断/治疗建议；如明显不适请及时就医。',
  ].join('\n');
}

Page({
  data: {
    pairId: null,
    loading: false,
    message: '',
    locationText: '',
    elderName: '',
    relation: '',
    tmax: '',
    tmin: '',
    trigger: '',
  },

  getToken() {
    return (wx.getStorageSync('api_token') || '').trim();
  },

  async onLoad(options) {
    const pairId = options.pair_id ? parseInt(options.pair_id, 10) : null;
    this.setData({ pairId });
    if (pairId) {
      await this.loadTemplate(pairId);
    }
  },

  async loadTemplate(pairId) {
    const token = this.getToken();
    if (!token) {
      wx.reLaunch({ url: '/pages/bind-token/index' });
      return;
    }
    this.setData({ loading: true });
    try {
      const elders = await api({ method: 'GET', path: '/mp/api/v1/elders', token });
      const item = (elders || []).find((x) => x.pair_id === pairId);
      if (!item) throw new Error('not_found');
      const elderName = item.member && item.member.name ? item.member.name : '';
      const relation = item.member && item.member.relation ? item.member.relation : '';
      const locationText = item.location_query || item.community_code || '';
      const tmax = item.today && item.today.temperature_max ? String(item.today.temperature_max) : '';
      const tmin = item.today && item.today.temperature_min ? String(item.today.temperature_min) : '';
      const trigger = item.today && item.today.trigger ? item.today.trigger : '';
      const message = buildMessage({ trigger, elderName, relation, locationText, tmax, tmin });
      this.setData({ message, locationText, elderName, relation, tmax, tmin, trigger });
    } catch (e) {
      wx.showToast({ title: '加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  async copyMessage() {
    const token = this.getToken();
    const message = this.data.message || '';
    if (!message) return;
    try {
      await new Promise((resolve, reject) => {
        wx.setClipboardData({
          data: message,
          success: resolve,
          fail: reject,
        });
      });
      wx.showToast({ title: '已复制', icon: 'success' });
      if (token) {
        // fire-and-forget
        api({
          method: 'POST',
          path: '/mp/api/v1/events',
          token,
          data: {
            event_type: 'template_copy',
            pair_id: this.data.pairId,
            meta: { trigger: this.data.trigger },
          },
        }).catch(() => {});
      }
    } catch (e) {
      wx.showToast({ title: '复制失败', icon: 'none' });
    }
  },

  back() {
    wx.navigateBack();
  },
});

