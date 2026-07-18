const { authApi, requireToken } = require('../elders/care-session');
const { normalizeList, validateMedicationInput } = require('../elders/care-logic');

const FREQUENCY_OPTIONS = [
  { value: 'daily', label: '每天' },
  { value: 'weekly', label: '每周' },
];

function parseTriggers(value) {
  if (value && typeof value === 'object') return value;
  if (typeof value !== 'string') return {};
  try {
    return JSON.parse(value) || {};
  } catch (error) {
    return {};
  }
}

function triggerSummary(value) {
  const triggers = parseTriggers(value);
  const parts = [];
  if (triggers.high_temp !== undefined) parts.push(`高温≥${triggers.high_temp}°C`);
  if (triggers.low_temp !== undefined) parts.push(`低温≤${triggers.low_temp}°C`);
  if (triggers.high_humidity !== undefined) parts.push(`湿度≥${triggers.high_humidity}%`);
  if (triggers.high_aqi !== undefined) parts.push(`AQI≥${triggers.high_aqi}`);
  return parts.join('；') || '未记录天气条件';
}

function normalizeMedication(item) {
  return {
    id: item.id,
    medicineName: item.medicine_name || '未命名药品',
    dosage: item.dosage || '按医嘱',
    frequencyLabel: item.frequency === 'weekly' ? '每周' : '每天',
    timeOfDay: item.time_of_day || '未记录时间',
    triggerText: triggerSummary(item.weather_triggers),
    active: item.is_active !== false,
  };
}

Page({
  data: {
    pairId: null,
    elderName: '家人',
    medicineName: '',
    dosage: '',
    frequencyOptions: FREQUENCY_OPTIONS.map((item) => item.label),
    frequencyIndex: 0,
    frequency: 'daily',
    timeOfDay: '08:00',
    showWeatherTriggers: false,
    highTemp: '',
    lowTemp: '',
    highHumidity: '',
    highAqi: '',
    medications: [],
    loading: false,
    busy: false,
  },

  async onLoad(options) {
    if (!requireToken()) return;
    const pairId = Number(options.pair_id || 0) || null;
    this.setData({ pairId });
    if (!pairId) {
      wx.showToast({ title: '请选择一位家人', icon: 'none' });
      return;
    }
    await this.loadMedications();
  },

  onShow() {
    requireToken();
  },

  onSessionInvalidated() {
    this.setData({
      pairId: null,
      elderName: '家人',
      medicineName: '',
      dosage: '',
      frequencyIndex: 0,
      frequency: 'daily',
      timeOfDay: '08:00',
      showWeatherTriggers: false,
      highTemp: '',
      lowTemp: '',
      highHumidity: '',
      highAqi: '',
      medications: [],
      loading: false,
      busy: false,
    });
  },

  async loadMedications() {
    this.setData({ loading: true });
    try {
      const [elderData, medicationData] = await Promise.all([
        authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
        authApi({ method: 'GET', path: `/mp/api/v1/medications?pair_id=${this.data.pairId}` }),
      ]);
      const elder = normalizeList(elderData, ['items', 'elders'])
        .find((item) => Number(item.pair_id) === this.data.pairId);
      this.setData({
        elderName: elder && elder.member && elder.member.name ? elder.member.name : '家人',
        medications: normalizeList(medicationData, ['items', 'medications']).map(normalizeMedication),
      });
    } catch (error) {
      wx.showToast({ title: '用药记录加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  onMedicineName(event) { this.setData({ medicineName: event.detail.value || '' }); },
  onDosage(event) { this.setData({ dosage: event.detail.value || '' }); },
  onTimeChange(event) { this.setData({ timeOfDay: event.detail.value }); },
  onHighTemp(event) { this.setData({ highTemp: event.detail.value || '' }); },
  onLowTemp(event) { this.setData({ lowTemp: event.detail.value || '' }); },
  onHighHumidity(event) { this.setData({ highHumidity: event.detail.value || '' }); },
  onHighAqi(event) { this.setData({ highAqi: event.detail.value || '' }); },

  onFrequencyChange(event) {
    const frequencyIndex = Number(event.detail.value || 0);
    this.setData({ frequencyIndex, frequency: FREQUENCY_OPTIONS[frequencyIndex].value });
  },

  toggleWeatherTriggers() {
    this.setData({ showWeatherTriggers: !this.data.showWeatherTriggers });
  },

  async addMedication() {
    if (this.data.busy) return;
    const validation = validateMedicationInput(this.data);
    if (!validation.valid) {
      wx.showToast({ title: validation.error, icon: 'none' });
      return;
    }
    this.setData({ busy: true });
    try {
      await authApi({
        method: 'POST',
        path: '/mp/api/v1/medications',
        data: Object.assign({ pair_id: this.data.pairId }, validation.payload),
      });
      this.setData({
        medicineName: '',
        dosage: '',
        frequencyIndex: 0,
        frequency: 'daily',
        timeOfDay: '08:00',
        highTemp: '',
        lowTemp: '',
        highHumidity: '',
        highAqi: '',
        showWeatherTriggers: false,
      });
      wx.showToast({ title: '用药记录已保存', icon: 'success' });
      await this.loadMedications();
    } catch (error) {
      wx.showToast({ title: '保存失败，请稍后再试', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  deleteMedication(event) {
    const id = Number(event.currentTarget.dataset.id);
    const name = event.currentTarget.dataset.name || '这条记录';
    wx.showModal({
      title: '删除用药记录？',
      content: `确定删除“${name}”的用药记录吗？`,
      confirmText: '删除',
      confirmColor: '#b42318',
      success: async (result) => {
        if (!result.confirm) return;
        try {
          await authApi({ method: 'DELETE', path: `/mp/api/v1/medications/${id}` });
          wx.showToast({ title: '已删除', icon: 'success' });
          await this.loadMedications();
        } catch (error) {
          wx.showToast({ title: '删除失败', icon: 'none' });
        }
      },
    });
  },
});
