const { getBootstrap } = require('../../utils/public-data');
const { duchangDateKey, freshnessView, normalizeBootstrap } = require('../../utils/format');
const { allowsJsMotion } = require('../../utils/motion');
const { loadActionChecked, saveActionChecked } = require('../../utils/action-storage');
const {
  beginPublicPage,
  hidePublicPage,
  pageCanRender,
  schedulePublicRefresh,
  showPublicPage,
  unloadPublicPage,
} = require('../../utils/public-page-lifecycle');
const {
  createPageShare,
  createTimelineShare,
  showPublicShareMenu,
  sourceFromShareEvent,
} = require('../../utils/share');

const GENERAL_ACTIONS = [
  { id: 'general-water', title: '少量多次补水', detail: '不要等到明显口渴才喝水。心肾疾病患者按医生要求控制饮水。' },
  { id: 'general-room', title: '检查室内温度和通风', detail: '午后高温时拉上遮光帘，合理使用风扇或空调。' },
  { id: 'general-contact', title: '和家人确认一次状态', detail: '问清是否头晕、胸闷、乏力，并确认电话保持畅通。' },
  { id: 'general-outdoor', title: '避开最热时段外出', detail: '需要外出时带水、遮阳用品和常用药。' },
];

function todayKey() {
  return duchangDateKey();
}

function readChecked() {
  try {
    return loadActionChecked(wx, todayKey());
  } catch (error) {
    return {};
  }
}

function isCompletionReceiptShare(options) {
  const event = options || {};
  const dataset = event.target && event.target.dataset;
  return Boolean(event.from === 'button' && dataset && dataset.shareKind === 'completion_receipt');
}

Page({
  data: {
    loading: true,
    error: '',
    actions: [],
    completedCount: 0,
    progressPercent: 0,
    generalMode: false,
    freshness: {},
    locationName: '都昌县',
    reduceMotion: !allowsJsMotion(),
  },

  onLoad() {
    // 页面启动即执行旧键迁移和跨日清理，不等待天气请求。
    readChecked();
    beginPublicPage(this);
    showPublicShareMenu();
  },

  onShow() {
    showPublicPage(this, () => this.loadData());
  },

  onHide() {
    hidePublicPage(this);
  },

  onUnload() {
    unloadPublicPage(this);
  },

  async onPullDownRefresh() {
    await this.loadData({ force: true });
    wx.stopPullDownRefresh();
  },

  mergeChecked(actions) {
    const checked = readChecked();
    return actions.map((item) => Object.assign({}, item, { checked: Boolean(checked[item.id]) }));
  },

  async loadData(options) {
    if (!this.data.actions.length) this.setData({ loading: true, error: '' });
    try {
      const requestOptions = Object.assign({}, options, {
        onRevalidated: (freshResult) => {
          if (pageCanRender(this)) this.renderActions(freshResult);
        },
      });
      const result = await getBootstrap(requestOptions);
      if (pageCanRender(this)) this.renderActions(result);
    } catch (error) {
      if (!pageCanRender(this)) return;
      const actions = this.mergeChecked(GENERAL_ACTIONS);
      this.setData({
        loading: false,
        error: '天气暂时无法更新，当前显示通用安全清单。',
        actions,
        completedCount: actions.filter((item) => item.checked).length,
        progressPercent: actions.length ? Math.round(actions.filter((item) => item.checked).length / actions.length * 100) : 0,
        generalMode: true,
      });
    }
  },

  renderActions(result) {
    const snapshot = normalizeBootstrap(result.data);
    const freshness = freshnessView(result.meta, snapshot);
    const generalMode = freshness.stale || !snapshot.available || !snapshot.actions.length;
    const sourceActions = generalMode ? GENERAL_ACTIONS : snapshot.actions;
    const actions = this.mergeChecked(sourceActions);
    const completedCount = actions.filter((item) => item.checked).length;
    this.setData({
      loading: false,
      error: freshness.stale ? '正在显示较早天气，当前已切换为通用安全清单。刷新后再判断今日风险。' : '',
      actions,
      completedCount,
      progressPercent: actions.length ? Math.round(completedCount / actions.length * 100) : 0,
      generalMode,
      locationName: snapshot.location.name,
      freshness,
    });
    schedulePublicRefresh(this, result.meta, () => this.loadData());
  },

  toggleAction(event) {
    const id = event.currentTarget.dataset.id;
    const actions = this.data.actions.map((item) => item.id === id
      ? Object.assign({}, item, { checked: !item.checked })
      : item);
    const record = {};
    actions.forEach((item) => { if (item.checked) record[item.id] = true; });
    try {
      saveActionChecked(wx, todayKey(), record);
    } catch (error) {
      wx.showToast({ title: '记录保存失败', icon: 'none' });
    }
    const completedCount = actions.filter((item) => item.checked).length;
    this.setData({
      actions,
      completedCount,
      progressPercent: actions.length ? Math.round(completedCount / actions.length * 100) : 0,
    });
  },

  copyReminder() {
    const remaining = this.data.actions.filter((item) => !item.checked).map((item) => `• ${item.title}`);
    const lines = remaining.length ? remaining : ['• 今日行动已全部确认'];
    const text = `宜老天气通提醒：\n${lines.join('\n')}\n如明显不适，请及时就医。`;
    wx.setClipboardData({ data: text });
  },

  onShareAppMessage(options) {
    const completionReceipt = this.data.completedCount > 0 && isCompletionReceiptShare(options);
    return createPageShare({
      title: completionReceipt
        ? '我已看到，并完成一项防护准备'
        : `${this.data.locationName}今日防护清单`,
      route: '/pages/actions/index',
      source: sourceFromShareEvent(options),
    });
  },

  onShareTimeline() {
    return createTimelineShare({ title: `${this.data.locationName}今日防护清单` });
  },
});
