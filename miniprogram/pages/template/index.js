const { api } = require('../../utils/request');
const scripts = require('../../content/caregiver_tip_scripts.json');

function formatTemp(value) {
  if (value === null || value === undefined || value === '') return '';
  return String(value);
}

function buildMessage({ trigger, elderName, relation, locationText, tmax, tmin, weatherAvailable, actionUrl, shortCode }) {
  let address = '你';
  if (relation === '母亲' || relation === '妈妈' || relation === '妈') address = '妈';
  else if (relation === '父亲' || relation === '爸爸' || relation === '爸') address = '爸';
  else if (elderName) address = elderName;

  const lines = [];
  if (!weatherAvailable) {
    lines.push(scripts.weather_unavailable.title);
    lines.push(scripts.weather_unavailable.lead);
  } else {
    const kind = trigger === 'cold' ? 'cold' : trigger === 'heat' ? 'heat' : 'daily';
    const block = scripts[kind] || scripts.daily;
    lines.push(block.title);
    let lead = (block.lead || '').replace('{address}', address);
    if (kind === 'cold' && tmin) {
      lead += (block.temp_clause || '').replace('{tmin}', tmin);
      if (!lead.endsWith('。')) lead += '。';
    } else if (kind === 'heat' && tmax) {
      lead += (block.temp_clause || '').replace('{tmax}', tmax);
      if (!lead.endsWith('。')) lead += '。';
    } else if ((kind === 'cold' || kind === 'heat') && !lead.endsWith('。')) {
      lead += '。';
    }
    lines.push(lead);
    if (block.advice) lines.push(block.advice);
  }

  if (locationText) {
    lines.push((scripts.location_line || '').replace('{location}', locationText));
  }
  if (weatherAvailable) {
    lines.push(scripts.disclaimer);
  }
  if (actionUrl || shortCode) {
    lines.push(
      (scripts.action_line || '')
        .replace('{action_link}', actionUrl || '-')
        .replace('{short_code}', shortCode || '-')
    );
  }
  return lines.filter(Boolean).join('\n');
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
      const tmax = formatTemp(item.today && item.today.temperature_max);
      const tmin = formatTemp(item.today && item.today.temperature_min);
      const trigger = item.today && item.today.trigger ? item.today.trigger : '';
      const weatherAvailable = !!(item.today && item.today.weather_available);
      const message = buildMessage({
        trigger,
        elderName,
        relation,
        locationText,
        tmax,
        tmin,
        weatherAvailable,
        actionUrl: item.action_url || '',
        shortCode: item.short_code || '',
      });
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

