const {
  authApi,
  finishHealthMutation,
  guardHealthSensitivePage,
  requireToken,
  resumeHealthMutation,
  suspendHealthMutation,
  trackHealthMutation,
} = require('../elders/care-session');
const { ASSESSMENT_QUESTIONS, normalizeList, validateAssessment } = require('../elders/care-logic');

function cleanRecommendation(item) {
  if (typeof item === 'string') return item;
  if (!item || typeof item !== 'object') return '';
  return item.advice || item.description || item.category || '';
}

function normalizeAssessment(value) {
  const item = value && value.assessment ? value.assessment : (value || null);
  if (!item) return null;
  const rawRecommendations = Array.isArray(item.recommendations) ? item.recommendations : [];
  return {
    id: item.id,
    riskLevel: item.risk_level || item.riskLevel || '已完成',
    riskScore: item.risk_score === 0 || item.risk_score ? item.risk_score : '',
    assessmentDate: item.assessment_date || item.created_at || '',
    weatherCondition: item.weather_condition || '',
    recommendations: rawRecommendations.map(cleanRecommendation).filter(Boolean).slice(0, 4),
  };
}

function submitErrorView(error) {
  const source = error && typeof error === 'object' ? error : {};
  const nested = source.data && typeof source.data === 'object' ? source.data : {};
  const statusCode = Number(source.statusCode || source.status_code || source.status) || 0;
  const fingerprint = [
    source.code,
    source.error,
    source.message,
    source.errMsg,
    nested.code,
    nested.error,
  ].filter(Boolean).join(' ').toLowerCase();

  if (fingerprint.includes('weather_snapshot_unavailable')) {
    return {
      kind: 'weather-unavailable',
      title: '天气数据待更新',
      detail: '天气快照尚未准备好。请稍后重新提交；身体明显不适时，请及时联系家人并就医或求助。',
      retry: true,
    };
  }
  if (fingerprint.includes('weather_snapshot_stale')) {
    return {
      kind: 'weather-stale',
      title: '天气数据已经过期',
      detail: '天气快照正在刷新。请稍后重新提交，页面会保留你已经选择的内容。',
      retry: true,
    };
  }
  if (
    statusCode === 401
    || fingerprint.includes('unauthorized')
    || fingerprint.includes('invalid_token')
    || fingerprint.includes('missing_session')
    || fingerprint.includes('session_expired')
    || fingerprint.includes('session_changed')
  ) {
    return {
      kind: 'session',
      title: '登录状态已失效',
      detail: '为保护家人资料，请重新登录后再进行筛查。',
      retry: false,
    };
  }
  if (statusCode === 428 && fingerprint.includes('health_sensitive_consent_required')) {
    return {
      kind: 'consent',
      title: '健康资料授权需要重新确认',
      detail: '请完成新的单独同意后，再重新进行本次筛查。',
      retry: false,
    };
  }
  if (
    fingerprint.includes('request:fail')
    || fingerprint.includes('timeout')
    || fingerprint.includes('network')
    || fingerprint.includes('offline')
    || fingerprint.includes('socket')
    || fingerprint.includes('dns')
    || fingerprint.includes('connection')
  ) {
    return {
      kind: 'network',
      title: '网络连接失败',
      detail: '没有连接到服务。请检查网络后重新提交，页面会保留你已经选择的内容。',
      retry: true,
    };
  }
  return {
    kind: 'service',
    title: '提交暂时没有完成',
    detail: '服务暂时没有完成处理。请稍后重新提交，页面会保留你已经选择的内容。',
    retry: true,
  };
}

function freshQuestions() {
  return ASSESSMENT_QUESTIONS.map((question, index) => ({
    ...question,
    number: index + 1,
    selected: '',
    options: question.options.map((option) => ({ ...option, active: false })),
  }));
}

Page({
  data: {
    pairId: null,
    elders: [],
    elderNames: [],
    elderIndex: 0,
    questions: freshQuestions(),
    answers: {},
    completedCount: 0,
    latest: null,
    latestLoading: false,
    latestError: '',
    submitError: null,
    contextReady: false,
    loadError: '',
    loading: true,
    busy: false,
  },

  async onLoad(options) {
    this._unloaded = false;
    this._hidden = false;
    this._latestRequestToken = 0;
    this._pageRequestToken = 0;
    this._submitRequestToken = 0;
    this.requestedPairId = Number(options.pair_id || 0) || null;
    if (!requireToken()) return;
    await guardHealthSensitivePage(this, () => this.loadPage());
  },

  async onShow() {
    this._hidden = false;
    if (!requireToken()) return;
    const resumed = await resumeHealthMutation(this);
    if (this._unloaded || this._hidden) return;
    const resumedSubmit = resumed.resumed && resumed.kind === 'assessment-submit';
    if (resumed.resumed) {
      const nextData = { busy: false, loading: true };
      if (resumedSubmit && resumed.ok) {
        Object.assign(nextData, {
          latest: normalizeAssessment(resumed.value) || this.data.latest,
          submitError: null,
          questions: freshQuestions(),
          answers: {},
          completedCount: 0,
        });
      } else if (resumedSubmit) {
        Object.assign(nextData, { submitError: submitErrorView(resumed.error) });
      }
      this.setData(nextData);
    }
    if (resumed.resumed && !requireToken()) return;
    await guardHealthSensitivePage(this, () => this.loadPage());
    if (this._unloaded || this._hidden || !resumedSubmit) return;
    wx.showToast({
      title: resumed.ok ? '筛查已保存并重新核对' : '本次未保存，请查看提示',
      icon: resumed.ok ? 'success' : 'none',
    });
  },

  onHide() {
    suspendHealthMutation(this);
    this._hidden = true;
    this._latestRequestToken = (this._latestRequestToken || 0) + 1;
    this._pageRequestToken = (this._pageRequestToken || 0) + 1;
    this._submitRequestToken = (this._submitRequestToken || 0) + 1;
  },

  onSessionInvalidated() {
    const submitWasPending = this.data.busy === true
      || this._healthMutationKind === 'assessment-submit'
      || this._healthMutationResumeKind === 'assessment-submit';
    this._healthConsentLoadedOnce = false;
    this._healthConsentLoadedToken = '';
    this._latestRequestToken = (this._latestRequestToken || 0) + 1;
    this._pageRequestToken = (this._pageRequestToken || 0) + 1;
    this._submitRequestToken = (this._submitRequestToken || 0) + 1;
    this.requestedPairId = null;
    this.setData({
      pairId: null,
      elders: [],
      elderNames: [],
      elderIndex: 0,
      questions: freshQuestions(),
      answers: {},
      completedCount: 0,
      latest: null,
      latestLoading: false,
      latestError: '',
      submitError: submitWasPending
        ? submitErrorView({ statusCode: 401, code: 'session_expired' })
        : null,
      contextReady: false,
      loadError: '',
      loading: false,
      busy: false,
    });
  },

  onHealthConsentRequired() {
    const submitWasPending = this.data.busy === true
      || this._healthMutationKind === 'assessment-submit'
      || this._healthMutationResumeKind === 'assessment-submit';
    this._healthConsentReloadPending = true;
    this._latestRequestToken = (this._latestRequestToken || 0) + 1;
    this._pageRequestToken = (this._pageRequestToken || 0) + 1;
    this._submitRequestToken = (this._submitRequestToken || 0) + 1;
    if (this._unloaded) return;
    this.setData({
      pairId: null,
      elders: [],
      elderNames: [],
      elderIndex: 0,
      questions: freshQuestions(),
      answers: {},
      completedCount: 0,
      latest: null,
      latestLoading: false,
      latestError: '',
      submitError: submitWasPending
        ? submitErrorView({ statusCode: 428, code: 'health_sensitive_consent_required' })
        : null,
      contextReady: false,
      loadError: '',
      loading: true,
      busy: false,
    });
  },

  onUnload() {
    this._unloaded = true;
    this._latestRequestToken += 1;
    this._pageRequestToken += 1;
    this._submitRequestToken += 1;
  },

  async loadPage() {
    if (this._unloaded || this._hidden) return;
    const pageToken = (this._pageRequestToken || 0) + 1;
    this._pageRequestToken = pageToken;
    const requestedPairId = Number(this.requestedPairId || 0);
    const latestToken = requestedPairId
      ? (this._latestRequestToken || 0) + 1
      : 0;
    if (latestToken) this._latestRequestToken = latestToken;
    this.setData(Object.assign(
      { loading: true, contextReady: false, loadError: '' },
      latestToken ? { latestLoading: true, latestError: '' } : {}
    ));
    try {
      const latestRequest = requestedPairId
        ? authApi({
          method: 'GET',
          path: `/mp/api/v1/health/assessment?pair_id=${requestedPairId}`,
        }).then(
          (data) => ({ ok: true, data }),
          (error) => ({ ok: false, error })
        )
        : Promise.resolve(null);
      // 路由已携带家人 ID 时，家人列表和最近筛查互不依赖，同步发起可节省一次 RTT。
      const [elderData, prefetchedLatest] = await Promise.all([
        authApi({ method: 'GET', path: '/mp/api/v1/elders' }),
        latestRequest,
      ]);
      if (this._unloaded || this._hidden || pageToken !== this._pageRequestToken) return;
      const elders = normalizeList(elderData, ['items', 'elders']);
      if (!elders.length) {
        if (latestToken === this._latestRequestToken) {
          this._latestRequestToken += 1;
          this.setData({ latestLoading: false, latestError: '' });
        }
        this.setData({
          contextReady: false,
          loadError: '请先添加一位家人后再进行健康筛查。',
        });
        wx.showModal({
          title: '请先添加家人',
          content: '健康筛查需要关联一位家中老人。',
          showCancel: false,
          success: () => {
            if (!this._unloaded && !this._hidden && pageToken === this._pageRequestToken) {
              wx.redirectTo({ url: '/pages/elder-edit/index?mode=create' });
            }
          },
        });
        return;
      }
      let elderIndex = elders.findIndex((item) => Number(item.pair_id) === requestedPairId);
      if (elderIndex < 0) elderIndex = 0;
      const pairId = Number(elders[elderIndex].pair_id);
      const prefetchedPairMatches = Boolean(
        requestedPairId
        && pairId === requestedPairId
        && latestToken === this._latestRequestToken
      );
      const nextData = {
        elders,
        elderNames: elders.map((item) => (item.member && item.member.name) || '家中老人'),
        elderIndex,
        pairId,
        contextReady: true,
        loadError: '',
      };
      if (prefetchedPairMatches) {
        Object.assign(nextData, {
          latest: prefetchedLatest && prefetchedLatest.ok
            ? normalizeAssessment(prefetchedLatest.data && prefetchedLatest.data.latest)
            : this.data.latest,
          latestLoading: false,
          latestError: prefetchedLatest && prefetchedLatest.ok
            ? ''
            : '最近一次筛查结果暂时无法读取，本次筛查仍可继续。',
        });
      }
      this.setData(nextData);
      if (!prefetchedPairMatches) {
        if (latestToken === this._latestRequestToken) {
          this._latestRequestToken += 1;
          this.setData({ latestLoading: false, latestError: '' });
        }
        await this.loadLatest();
      }
    } catch (error) {
      if (!this._unloaded && !this._hidden && pageToken === this._pageRequestToken) {
        if (latestToken === this._latestRequestToken) {
          this._latestRequestToken += 1;
          this.setData({ latestLoading: false });
        }
        this.setData({
          contextReady: false,
          loadError: '筛查页面暂时没有加载出来，请检查网络后重试。',
        });
      }
    } finally {
      if (!this._unloaded && !this._hidden && pageToken === this._pageRequestToken) this.setData({ loading: false });
    }
  },

  async loadLatest() {
    const requestedPairId = Number(this.data.pairId);
    if (!requestedPairId) {
      this.setData({
        latestLoading: false,
        latestError: '缺少家人信息，暂时无法读取最近一次筛查结果。',
      });
      return;
    }
    const requestToken = (this._latestRequestToken || 0) + 1;
    this._latestRequestToken = requestToken;
    this.setData({ latestLoading: true, latestError: '' });
    try {
      const data = await authApi({
        method: 'GET',
        path: `/mp/api/v1/health/assessment?pair_id=${requestedPairId}`,
      });
      if (
        this._unloaded
        || this._hidden
        || requestToken !== this._latestRequestToken
        || Number(this.data.pairId) !== requestedPairId
      ) return;
      this.setData({
        latest: normalizeAssessment(data && data.latest),
        latestError: '',
      });
    } catch (error) {
      if (
        !this._unloaded
        && !this._hidden
        && requestToken === this._latestRequestToken
        && Number(this.data.pairId) === requestedPairId
      ) {
        // 历史结果读取失败时保留已经显示的结果，并明确本次筛查仍可继续。
        this.setData({
          latestError: '最近一次筛查结果暂时无法读取，本次筛查仍可继续。',
        });
      }
    } finally {
      if (
        !this._unloaded
        && !this._hidden
        && requestToken === this._latestRequestToken
        && Number(this.data.pairId) === requestedPairId
      ) this.setData({ latestLoading: false });
    }
  },

  async onElderChange(event) {
    if (this.data.busy || this.data.loading) return;
    const elderIndex = Number(event.detail.value || 0);
    const elder = this.data.elders[elderIndex];
    if (!elder) return;
    const pairId = Number(elder.pair_id);
    if (!pairId || pairId === Number(this.data.pairId)) return;
    this.requestedPairId = pairId;
    this._latestRequestToken = (this._latestRequestToken || 0) + 1;
    this.setData({
      elderIndex,
      pairId,
      latest: null,
      latestLoading: false,
      latestError: '',
      submitError: null,
      questions: freshQuestions(),
      answers: {},
      completedCount: 0,
    });
    await this.loadLatest();
  },

  onSelect(event) {
    if (!this.data.contextReady || this.data.busy || this.data.loading) return;
    const id = event.currentTarget.dataset.id;
    const value = event.currentTarget.dataset.value;
    const answers = Object.assign({}, this.data.answers, { [id]: value });
    const questions = this.data.questions.map((question) => {
      if (question.id !== id) return question;
      return Object.assign({}, question, {
        selected: value,
        options: question.options.map((option) => Object.assign({}, option, { active: option.value === value })),
      });
    });
    this.setData({ answers, questions, completedCount: Object.keys(answers).length });
    if (id === 'symptom_level' && value === 'severe') {
      wx.showModal({
        title: '请优先关注身体安全',
        content: '这项筛查不作诊断。若有胸痛、呼吸困难、意识异常、持续高热等严重症状，请立即联系家人并及时就医或求助。',
        showCancel: false,
        confirmText: '我知道了',
      });
    }
  },

  async submitAssessment() {
    if (this.data.busy) return;
    if (!this.data.contextReady) {
      wx.showToast({ title: '请先重新加载筛查页面', icon: 'none' });
      return;
    }
    const submittedPairId = Number(this.data.pairId);
    if (!submittedPairId) {
      wx.showToast({ title: '请先选择家人', icon: 'none' });
      return;
    }
    const validation = validateAssessment(this.data.answers);
    if (!validation.valid) {
      wx.showToast({ title: validation.error, icon: 'none' });
      return;
    }
    const submitToken = (this._submitRequestToken || 0) + 1;
    this._submitRequestToken = submitToken;
    this.setData({ busy: true });
    let mutation = null;
    try {
      mutation = trackHealthMutation(
        this,
        authApi({
          method: 'POST',
          path: '/mp/api/v1/health/assessment',
          data: Object.assign({ pair_id: submittedPairId }, validation.payload),
        }),
        'assessment-submit'
      );
      const data = await mutation;
      if (
        this._unloaded
        || this._hidden
        || submitToken !== this._submitRequestToken
        || Number(this.data.pairId) !== submittedPairId
      ) return;
      this.setData({
        latest: normalizeAssessment(data),
        latestLoading: false,
        latestError: '',
        submitError: null,
        questions: freshQuestions(),
        answers: {},
        completedCount: 0,
      });
      wx.showToast({ title: '筛查已保存', icon: 'success' });
    } catch (error) {
      if (!this._unloaded && !this._hidden && submitToken === this._submitRequestToken) {
        this.setData({ submitError: submitErrorView(error) });
        wx.showToast({ title: '本次未保存，请查看提示', icon: 'none' });
      }
    } finally {
      finishHealthMutation(this, mutation);
      if (!this._unloaded && !this._hidden && submitToken === this._submitRequestToken) this.setData({ busy: false });
    }
  },

  backToCare() {
    wx.switchTab({ url: '/pages/elders/index' });
  },
});
