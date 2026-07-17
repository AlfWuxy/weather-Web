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
    loading: false,
    busy: false,
  },

  async onLoad(options) {
    if (!requireToken()) return;
    this.requestedPairId = Number(options.pair_id || 0) || null;
    await this.loadPage();
  },

  async loadPage() {
    this.setData({ loading: true });
    try {
      const elderData = await authApi({ method: 'GET', path: '/mp/api/v1/elders' });
      const elders = normalizeList(elderData, ['items', 'elders']);
      if (!elders.length) {
        wx.showModal({
          title: '请先添加家人',
          content: '健康筛查需要关联一位家中老人。',
          showCancel: false,
          success: () => wx.redirectTo({ url: '/pages/elder-edit/index?mode=create' }),
        });
        return;
      }
      let elderIndex = elders.findIndex((item) => Number(item.pair_id) === this.requestedPairId);
      if (elderIndex < 0) elderIndex = 0;
      const pairId = Number(elders[elderIndex].pair_id);
      this.setData({
        elders,
        elderNames: elders.map((item) => (item.member && item.member.name) || '家中老人'),
        elderIndex,
        pairId,
      });
      await this.loadLatest();
    } catch (error) {
      wx.showToast({ title: '筛查页面加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  async loadLatest() {
    if (!this.data.pairId) return;
    try {
      const data = await authApi({
        method: 'GET',
        path: `/mp/api/v1/health/assessment?pair_id=${this.data.pairId}`,
      });
      this.setData({ latest: normalizeAssessment(data && data.latest) });
    } catch (error) {
      this.setData({ latest: null });
    }
  },

  async onElderChange(event) {
    const elderIndex = Number(event.detail.value || 0);
    const pairId = Number(this.data.elders[elderIndex].pair_id);
    this.setData({ elderIndex, pairId, latest: null });
    await this.loadLatest();
  },

  onSelect(event) {
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
    const validation = validateAssessment(this.data.answers);
    if (!validation.valid) {
      wx.showToast({ title: validation.error, icon: 'none' });
      return;
    }
    this.setData({ busy: true });
    try {
      const data = await authApi({
        method: 'POST',
        path: '/mp/api/v1/health/assessment',
        data: Object.assign({ pair_id: this.data.pairId }, validation.payload),
      });
      this.setData({ latest: normalizeAssessment(data), questions: freshQuestions(), answers: {}, completedCount: 0 });
      wx.showToast({ title: '筛查已保存', icon: 'success' });
    } catch (error) {
      wx.showToast({ title: '提交失败，请稍后再试', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },
});
