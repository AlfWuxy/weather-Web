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
    contextReady: false,
    loadError: '',
    dataStale: false,
    loading: false,
    busy: false,
  },

  async onLoad(options) {
    this._unloaded = false;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    if (!requireToken()) return;
    const pairId = Number(options.pair_id || 0) || null;
    this.setData({ pairId, contextReady: false, loadError: '', dataStale: false });
    if (!pairId) {
      this.setData({ loadError: '缺少家人信息，请返回家庭照护重新选择。' });
      return;
    }
    await this.loadMedications();
  },

  onShow() {
    requireToken();
  },

  onUnload() {
    this._unloaded = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
  },

  onSessionInvalidated() {
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
    if (this._unloaded) return;
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
      contextReady: false,
      loadError: '',
      dataStale: false,
      loading: false,
      busy: false,
    });
  },

  async loadMedications() {
    if (this._unloaded) return;
    const request = beginLoad(this);
    const pairId = Number(this.data.pairId || 0);
    if (!pairId) {
      this.setData({
        contextReady: false,
        loadError: '缺少家人信息，请返回家庭照护重新选择。',
        dataStale: false,
        loading: false,
      });
      return;
    }
    const hadVerifiedContext = this.data.contextReady === true;
    this.setData({ loading: true, loadError: '', dataStale: false });
    try {
      const [elderData, medicationData] = await Promise.all([
        authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
        authApi({ method: 'GET', path: `/mp/api/v1/medications?pair_id=${pairId}` }),
      ]);
      const elder = normalizeList(elderData, ['items', 'elders'])
        .find((item) => Number(item.pair_id) === pairId);
      if (!loadIsActive(this, request)) return;
      if (!elder) throw new Error('elder_not_found');
      this.setData({
        elderName: elder.member && elder.member.name ? elder.member.name : '家人',
        medications: normalizeList(medicationData, ['items', 'medications']).map(normalizeMedication),
        contextReady: true,
        loadError: '',
        dataStale: false,
      });
    } catch (error) {
      if (loadIsActive(this, request)) {
        this.setData({
          contextReady: hadVerifiedContext,
          loadError: hadVerifiedContext
            ? '刷新失败，以下仍显示上次成功加载的用药记录。'
            : '用药记录暂时没有加载出来，请检查网络后重试。',
          dataStale: hadVerifiedContext,
        });
      }
    } finally {
      if (loadIsActive(this, request)) this.setData({ loading: false });
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
    if (!this.data.contextReady || this.data.busy) return;
    this.setData({ showWeatherTriggers: !this.data.showWeatherTriggers });
  },

  async addMedication() {
    if (this._unloaded) return;
    if (this.data.busy) return;
    if (!this.data.contextReady || !Number(this.data.pairId || 0)) {
      wx.showToast({ title: '请先重新加载家人信息', icon: 'none' });
      return;
    }
    const validation = validateMedicationInput(this.data);
    if (!validation.valid) {
      wx.showToast({ title: validation.error, icon: 'none' });
      return;
    }
    const lifecycle = Number(this._lifecycleGeneration || 0);
    const pairId = Number(this.data.pairId || 0);
    this.setData({ busy: true });
    try {
      const result = await authApi({
        method: 'POST',
        path: '/mp/api/v1/medications',
        data: Object.assign({ pair_id: pairId }, validation.payload),
      });
      if (!lifecycleIsActive(this, lifecycle)) return;
      const directId = result && result.id;
      const savedRecord = result && (
        result.medication
        || (directId !== undefined && directId !== null
          ? Object.assign({}, validation.payload, { id: directId, is_active: true })
          : null)
      );
      const savedMedication = savedRecord ? normalizeMedication(savedRecord) : null;
      const hasAuthoritativeMedication = Boolean(
        savedMedication && savedMedication.id !== undefined && savedMedication.id !== null
      );
      const medications = hasAuthoritativeMedication
        ? [savedMedication].concat(this.data.medications.filter((item) => String(item.id) !== String(savedMedication.id)))
        : this.data.medications;
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
        medications,
        loadError: '',
        dataStale: false,
      });
      wx.showToast({ title: '用药记录已保存', icon: 'success' });
      // 服务端已返回权威记录时避免立即再发两个 GET。
      if (!hasAuthoritativeMedication) await this.loadMedications();
    } catch (error) {
      if (lifecycleIsActive(this, lifecycle)) wx.showToast({ title: '保存失败，请稍后再试', icon: 'none' });
    } finally {
      if (lifecycleIsActive(this, lifecycle)) this.setData({ busy: false });
    }
  },

  deleteMedication(event) {
    if (this._unloaded) return;
    if (this.data.busy || !this.data.contextReady) return;
    const id = Number(event.currentTarget.dataset.id);
    const name = event.currentTarget.dataset.name || '这条记录';
    const lifecycle = Number(this._lifecycleGeneration || 0);
    wx.showModal({
      title: '删除用药记录？',
      content: `确定删除“${name}”的用药记录吗？`,
      confirmText: '删除',
      confirmColor: '#b42318',
      success: async (result) => {
        if (!lifecycleIsActive(this, lifecycle)) return;
        if (!result.confirm) return;
        this.setData({ busy: true });
        try {
          const deleteResult = await authApi({ method: 'DELETE', path: `/mp/api/v1/medications/${id}` });
          if (!lifecycleIsActive(this, lifecycle)) return;
          const deletedId = deleteResult && (
            deleteResult.deleted_id !== undefined
              ? deleteResult.deleted_id
              : deleteResult.id
          );
          const hasAuthoritativeDelete = deletedId !== undefined
            && deletedId !== null
            && String(deletedId) === String(id);
          // 服务端已确认删除后先本地移除，后续刷新失败也不会继续伪显示已删除记录。
          this.setData({
            medications: this.data.medications.filter((item) => Number(item.id) !== id),
            loadError: '',
            dataStale: false,
          });
          wx.showToast({ title: '已删除', icon: 'success' });
          if (!hasAuthoritativeDelete) await this.loadMedications();
        } catch (error) {
          if (lifecycleIsActive(this, lifecycle)) wx.showToast({ title: '删除失败', icon: 'none' });
        } finally {
          if (lifecycleIsActive(this, lifecycle)) this.setData({ busy: false });
        }
      },
    });
  },
});
