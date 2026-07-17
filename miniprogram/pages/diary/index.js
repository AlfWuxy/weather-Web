const { authApi, requireToken } = require('../elders/care-session');
const { formatLocalDate, normalizeList, validateDiaryInput } = require('../elders/care-logic');

const SEVERITY_OPTIONS = ['轻微', '中等', '严重'];

function normalizeEntry(item) {
  return {
    id: item.id,
    entryDate: item.entry_date || item.date || '',
    severity: item.severity || '未填写',
    symptoms: item.symptoms || '无症状描述',
    notes: item.notes || '',
    weatherText: item.weather_text || item.weather || '',
  };
}

Page({
  data: {
    pairId: null,
    elderName: '家人',
    entryDate: formatLocalDate(new Date()),
    todayDate: formatLocalDate(new Date()),
    severityOptions: SEVERITY_OPTIONS,
    severityIndex: 0,
    severity: '轻微',
    symptoms: '',
    notes: '',
    entries: [],
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
    await this.loadDiary();
  },

  async loadDiary() {
    this.setData({ loading: true });
    try {
      const [elderData, diaryData] = await Promise.all([
        authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
        authApi({ method: 'GET', path: `/mp/api/v1/health/diary?pair_id=${this.data.pairId}&limit=30` }),
      ]);
      const elder = normalizeList(elderData, ['items', 'elders'])
        .find((item) => Number(item.pair_id) === this.data.pairId);
      const entries = normalizeList(diaryData, ['items', 'entries']).map(normalizeEntry);
      this.setData({
        elderName: elder && elder.member && elder.member.name ? elder.member.name : '家人',
        entries,
      });
    } catch (error) {
      wx.showToast({ title: '日记加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  onDateChange(event) { this.setData({ entryDate: event.detail.value }); },
  onSymptoms(event) { this.setData({ symptoms: event.detail.value || '' }); },
  onNotes(event) { this.setData({ notes: event.detail.value || '' }); },

  onSeverityChange(event) {
    const severityIndex = Number(event.detail.value || 0);
    const severity = SEVERITY_OPTIONS[severityIndex];
    this.setData({ severityIndex, severity });
    if (severity === '严重') {
      wx.showModal({
        title: '严重不适请优先求助',
        content: '日记只用于记录，不作诊断。若出现胸痛、呼吸困难、意识异常、持续高热等严重症状，请立即联系家人并及时就医。',
        showCancel: false,
        confirmText: '我知道了',
      });
    }
  },

  async saveEntry() {
    if (this.data.busy) return;
    const validation = validateDiaryInput(this.data);
    if (!validation.valid) {
      wx.showToast({ title: validation.error, icon: 'none' });
      return;
    }
    this.setData({ busy: true });
    try {
      await authApi({
        method: 'POST',
        path: '/mp/api/v1/health/diary',
        data: Object.assign({ pair_id: this.data.pairId }, validation.payload),
      });
      this.setData({
        entryDate: this.data.todayDate,
        severityIndex: 0,
        severity: '轻微',
        symptoms: '',
        notes: '',
      });
      wx.showToast({ title: '日记已保存', icon: 'success' });
      await this.loadDiary();
    } catch (error) {
      wx.showToast({ title: '保存失败，请稍后再试', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },
});
