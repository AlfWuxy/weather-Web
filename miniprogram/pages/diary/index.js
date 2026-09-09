const {
  authApi,
  finishHealthMutation,
  guardHealthSensitivePage,
  requireToken,
  resumeHealthMutation,
  suspendHealthMutation,
  trackHealthMutation,
} = require('../elders/care-session');
const { normalizeList, validateDiaryInput } = require('../elders/care-logic');
const { duchangDateKey } = require('../../utils/format');

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
  return page._hidden !== true
    && lifecycleIsActive(page, request.lifecycle)
    && page._loadRequestId === request.requestId;
}

Page({
  data: {
    pairId: null,
    elderName: '家人',
    entryDate: '',
    todayDate: '',
    severityOptions: SEVERITY_OPTIONS,
    severityIndex: 0,
    severity: '轻微',
    symptoms: '',
    notes: '',
    entries: [],
    contextReady: false,
    loadError: '',
    dataStale: false,
    loading: true,
    busy: false,
  },

  async onLoad(options) {
    this._unloaded = false;
    this._hidden = false;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._entryDateTouched = false;
    this.syncTodayDate(undefined, true);
    if (!requireToken()) return;
    const pairId = Number(options.pair_id || 0) || null;
    this._routePairId = pairId;
    this.setData({ pairId, contextReady: false, loadError: '', dataStale: false });
    if (!pairId) {
      this.setData({ loadError: '缺少家人信息，请返回家庭照护重新选择。', loading: false });
      return;
    }
    await guardHealthSensitivePage(this, () => this.loadDiary());
  },

  async onShow() {
    this._hidden = false;
    if (!requireToken()) return;
    const resumed = await resumeHealthMutation(this);
    if (this._unloaded || this._hidden) return;
    const resumedSave = resumed.resumed && resumed.kind === 'diary-save';
    if (resumed.resumed) {
      const nextData = { busy: false, loading: true };
      if (resumedSave && resumed.ok) {
        this._entryDateTouched = false;
        Object.assign(nextData, {
          entryDate: this.data.todayDate || duchangDateKey(),
          severityIndex: 0,
          severity: '轻微',
          symptoms: '',
          notes: '',
        });
      }
      this.setData(nextData);
    }
    this.syncTodayDate();
    if (resumed.resumed && !requireToken()) return;
    if (!this.data.pairId && this._routePairId) this.setData({ pairId: this._routePairId, loading: true });
    await guardHealthSensitivePage(this, () => this.loadDiary());
    if (this._unloaded || this._hidden || !resumedSave) return;
    wx.showToast({
      title: resumed.ok ? '日记已保存并重新核对' : '保存未完成，请重试',
      icon: resumed.ok ? 'success' : 'none',
    });
  },

  onHide() {
    suspendHealthMutation(this);
    this._hidden = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
  },

  syncTodayDate(value, resetEntryDate) {
    const today = duchangDateKey(value);
    if (!today) return;
    const previousToday = String(this.data.todayDate || '');
    const currentEntryDate = String(this.data.entryDate || '');
    const shouldResetEntryDate = resetEntryDate === true
      || !currentEntryDate
      || (!this._entryDateTouched && currentEntryDate === previousToday);
    this.setData(Object.assign(
      { todayDate: today },
      shouldResetEntryDate ? { entryDate: today } : {}
    ));
  },

  onUnload() {
    this._unloaded = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
  },

  onSessionInvalidated() {
    this._healthConsentLoadedOnce = false;
    this._healthConsentLoadedToken = '';
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
    if (this._unloaded) return;
    const today = duchangDateKey();
    this._entryDateTouched = false;
    this._routePairId = null;
    this.setData({
      pairId: null,
      elderName: '家人',
      entryDate: today,
      todayDate: today,
      severityIndex: 0,
      severity: '轻微',
      symptoms: '',
      notes: '',
      entries: [],
      contextReady: false,
      loadError: '',
      dataStale: false,
      loading: false,
      busy: false,
    });
  },

  onHealthConsentRequired() {
    this._healthConsentReloadPending = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._loadRequestId = Number(this._loadRequestId || 0) + 1;
    if (this._unloaded) return;
    const today = duchangDateKey();
    this._entryDateTouched = false;
    this.setData({
      pairId: null,
      elderName: '家人',
      entryDate: today,
      todayDate: today,
      severityIndex: 0,
      severity: '轻微',
      symptoms: '',
      notes: '',
      entries: [],
      contextReady: false,
      loadError: '',
      dataStale: false,
      loading: true,
      busy: false,
    });
  },

  async loadDiary() {
    if (this._unloaded || this._hidden) return;
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
      const [elderData, diaryData] = await Promise.all([
        authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
        authApi({ method: 'GET', path: `/mp/api/v1/health/diary?pair_id=${pairId}&limit=30` }),
      ]);
      const elder = normalizeList(elderData, ['items', 'elders'])
        .find((item) => Number(item.pair_id) === pairId);
      const entries = normalizeList(diaryData, ['items', 'entries']).map(normalizeEntry);
      if (!loadIsActive(this, request)) return;
      if (!elder) throw new Error('elder_not_found');
      this.setData({
        elderName: elder.member && elder.member.name ? elder.member.name : '家人',
        entries,
        contextReady: true,
        loadError: '',
        dataStale: false,
      });
    } catch (error) {
      if (loadIsActive(this, request)) {
        this.setData({
          contextReady: hadVerifiedContext,
          loadError: hadVerifiedContext
            ? '刷新失败，以下仍显示上次成功加载的健康日记。'
            : '健康日记暂时没有加载出来，请检查网络后重试。',
          dataStale: hadVerifiedContext,
        });
      }
    } finally {
      if (loadIsActive(this, request)) this.setData({ loading: false });
    }
  },

  onDateChange(event) {
    this._entryDateTouched = true;
    this.setData({ entryDate: event.detail.value });
  },
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
    if (this._unloaded) return;
    if (this.data.busy) return;
    if (!this.data.contextReady || !Number(this.data.pairId || 0)) {
      wx.showToast({ title: '请先重新加载家人信息', icon: 'none' });
      return;
    }
    const validation = validateDiaryInput(this.data);
    if (!validation.valid) {
      wx.showToast({ title: validation.error, icon: 'none' });
      return;
    }
    const lifecycle = Number(this._lifecycleGeneration || 0);
    const pairId = Number(this.data.pairId || 0);
    this.setData({ busy: true });
    let mutation = null;
    try {
      mutation = trackHealthMutation(
        this,
        authApi({
          method: 'POST',
          path: '/mp/api/v1/health/diary',
          data: Object.assign({ pair_id: pairId }, validation.payload),
        }),
        'diary-save'
      );
      const result = await mutation;
      if (!lifecycleIsActive(this, lifecycle)) return;
      const directId = result && result.id;
      const savedRecord = result && (
        result.entry
        || (directId !== undefined && directId !== null
          ? Object.assign({}, validation.payload, { id: directId })
          : null)
      );
      const savedEntry = savedRecord ? normalizeEntry(savedRecord) : null;
      const hasAuthoritativeEntry = Boolean(
        savedEntry && savedEntry.id !== undefined && savedEntry.id !== null
      );
      const entries = hasAuthoritativeEntry
        ? [savedEntry].concat(this.data.entries.filter((item) => String(item.id) !== String(savedEntry.id)))
        : this.data.entries;
      this._entryDateTouched = false;
      this.setData({
        entryDate: this.data.todayDate,
        severityIndex: 0,
        severity: '轻微',
        symptoms: '',
        notes: '',
        entries,
        loadError: '',
        dataStale: false,
      });
      wx.showToast({ title: '日记已保存', icon: 'success' });
      // 服务端已返回权威记录时直接完成，缺少对象才回退重载。
      if (!hasAuthoritativeEntry) await this.loadDiary();
    } catch (error) {
      if (lifecycleIsActive(this, lifecycle)) wx.showToast({ title: '保存失败，请稍后再试', icon: 'none' });
    } finally {
      finishHealthMutation(this, mutation);
      if (lifecycleIsActive(this, lifecycle)) this.setData({ busy: false });
    }
  },
});
