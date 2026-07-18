const { authApi, requireToken } = require('../elders/care-session');
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
    loading: false,
    busy: false,
  },

  async onLoad(options) {
    if (!requireToken()) return;
    this._unloaded = false;
    this._latestRequestToken = 0;
    this._pageRequestToken = 0;
    this._submitRequestToken = 0;
    this.requestedPairId = Number(options.pair_id || 0) || null;
    await this.loadPage();
  },

  onShow() {
    requireToken();
  },

  onSessionInvalidated() {
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
      loading: false,
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
    const pageToken = (this._pageRequestToken || 0) + 1;
    this._pageRequestToken = pageToken;
    const requestedPairId = Number(this.requestedPairId || 0);
    const latestToken = requestedPairId
      ? (this._latestRequestToken || 0) + 1
      : 0;
    if (latestToken) this._latestRequestToken = latestToken;
    this.setData(Object.assign(
      { loading: true },
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
      if (this._unloaded || pageToken !== this._pageRequestToken) return;
      const elders = normalizeList(elderData, ['items', 'elders']);
      if (!elders.length) {
        if (latestToken === this._latestRequestToken) {
          this._latestRequestToken += 1;
          this.setData({ latestLoading: false, latestError: '' });
        }
        wx.showModal({
          title: '请先添加家人',
          content: '健康筛查需要关联一位家中老人。',
          showCancel: false,
          success: () => wx.redirectTo({ url: '/pages/elder-edit/index?mode=create' }),
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
      if (!this._unloaded && pageToken === this._pageRequestToken) {
        if (latestToken === this._latestRequestToken) {
          this._latestRequestToken += 1;
          this.setData({ latestLoading: false });
        }
        wx.showToast({ title: '筛查页面加载失败', icon: 'none' });
      }
    } finally {
      if (!this._unloaded && pageToken === this._pageRequestToken) this.setData({ loading: false });
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
    this._latestRequestToken = (this._latestRequestToken || 0) + 1;
    this.setData({
      elderIndex,
      pairId,
      latest: null,
      latestLoading: false,
      latestError: '',
      questions: freshQuestions(),
      answers: {},
      completedCount: 0,
    });
    await this.loadLatest();
  },

  onSelect(event) {
    if (this.data.busy || this.data.loading) return;
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
    try {
      const data = await authApi({
        method: 'POST',
        path: '/mp/api/v1/health/assessment',
        data: Object.assign({ pair_id: submittedPairId }, validation.payload),
      });
      if (
        this._unloaded
        || submitToken !== this._submitRequestToken
        || Number(this.data.pairId) !== submittedPairId
      ) return;
      this.setData({
        latest: normalizeAssessment(data),
        latestLoading: false,
        latestError: '',
        questions: freshQuestions(),
        answers: {},
        completedCount: 0,
      });
      wx.showToast({ title: '筛查已保存', icon: 'success' });
    } catch (error) {
      if (!this._unloaded && submitToken === this._submitRequestToken) {
        wx.showToast({ title: '提交失败，请稍后再试', icon: 'none' });
      }
    } finally {
      if (!this._unloaded && submitToken === this._submitRequestToken) this.setData({ busy: false });
    }
  },
});
