const { getBootstrap, PUBLIC_RETRY_DELAY_MS } = require('../../utils/public-data');
const { freshnessView, normalizeBootstrap } = require('../../utils/format');
const {
  beginPublicPage,
  hidePublicPage,
  pageCanRender,
  schedulePublicRefresh,
  showPublicPage,
  staleRetryMeta,
  unloadPublicPage,
} = require('../../utils/public-page-lifecycle');
const {
  createPageShare,
  createTimelineShare,
  readFamilyShareEntryRecord,
  showPublicShareMenu,
  sourceFromShareEvent,
} = require('../../utils/share');

function homeSnapshotView(snapshot) {
  const source = snapshot || {};
  // 首页只跨逻辑层与视图层传递实际渲染字段，避免复制预报、来源等整份快照。
  return {
    available: source.available,
    location: source.location,
    current: source.current,
    warnings: Array.isArray(source.warnings) ? source.warnings : [],
    warningsSourceAvailable: source.warningsSourceAvailable === true,
    warningsStatusText: source.warningsStatusText,
    risk: source.risk,
  };
}

function staleHomeSnapshot(snapshot) {
  if (!snapshot) return null;
  return Object.assign({}, snapshot, {
    warnings: [],
    warningsSourceAvailable: false,
    warningsStatusText: '官方预警待刷新',
    risk: Object.assign({}, snapshot.risk || {}, {
      available: false,
      score: null,
      scoreText: '待刷新',
      level: '',
      label: '风险待刷新',
      tone: 'unknown',
      summary: '',
    }),
  });
}

Page({
  data: {
    loading: true,
    error: '',
    snapshot: null,
    freshness: {},
    topActions: [],
    familyShareEntry: false,
    entryContextReady: false,
  },

  onLoad() {
    beginPublicPage(this);
    showPublicShareMenu();
    this.updateEntryContext();
  },

  onUnload() {
    this.clearEntryContextTimer();
    unloadPublicPage(this);
  },

  onShow() {
    showPublicPage(this, () => this.loadData());
    this.updateEntryContext();
  },

  onHide() {
    this.clearEntryContextTimer();
    hidePublicPage(this);
  },

  updateEntryContext() {
    this.clearEntryContextTimer();
    const entryRecord = readFamilyShareEntryRecord();
    const familyShareEntry = Boolean(entryRecord && entryRecord.source === 'family_share');
    if (familyShareEntry !== this.data.familyShareEntry || !this.data.entryContextReady) {
      this.setData({ familyShareEntry, entryContextReady: true });
    }
    if (!familyShareEntry || !pageCanRender(this)) return;
    const delay = Math.min(0x7fffffff, Math.max(0, entryRecord.expiresAt - Date.now()));
    this._familyEntryTimer = setTimeout(() => {
      this._familyEntryTimer = null;
      if (pageCanRender(this)) this.updateEntryContext();
    }, delay);
  },

  clearEntryContextTimer() {
    if (!this._familyEntryTimer) return;
    clearTimeout(this._familyEntryTimer);
    this._familyEntryTimer = null;
  },

  startFamilyCare() {
    wx.navigateTo({ url: '/pages/bind-token/index' });
  },

  openTodayActions() {
    wx.navigateTo({ url: '/pages/actions/index' });
  },

  async onPullDownRefresh() {
    await this.loadData({ force: true });
    wx.stopPullDownRefresh();
  },

  async loadData(options) {
    if (!this.data.snapshot) this.setData({ loading: true, error: '' });
    try {
      const requestOptions = Object.assign({}, options, {
        onRevalidated: (freshResult) => {
          if (pageCanRender(this)) this.renderSnapshot(freshResult);
        },
      });
      const result = await getBootstrap(requestOptions);
      if (pageCanRender(this)) this.renderSnapshot(result);
    } catch (error) {
      if (!pageCanRender(this)) return;
      const hasSnapshot = Boolean(this.data.snapshot);
      const freshness = staleRetryMeta(this.data.freshness, PUBLIC_RETRY_DELAY_MS);
      this.setData({
        loading: false,
        error: hasSnapshot
          ? '天气更新失败，正在显示较早观测；风险、预警和定制行动已暂停。稍后会自动重试。'
          : '天气数据暂时无法获取。请检查网络，稍后再试。',
        snapshot: hasSnapshot ? staleHomeSnapshot(this.data.snapshot) : null,
        topActions: [],
        freshness,
      });
      schedulePublicRefresh(this, freshness, () => this.loadData());
    }
  },

  renderSnapshot(result) {
    const snapshot = normalizeBootstrap(result.data);
    const freshness = freshnessView(result.meta, snapshot);
    // 较早天气可以继续展示观测值，风险分数和定制行动必须等刷新后再启用。
    const displaySnapshot = freshness.stale ? staleHomeSnapshot(snapshot) : snapshot;
    this.setData({
      loading: false,
      error: result.meta && result.meta.networkError
        ? '天气更新失败，正在显示较早观测；风险、预警和定制行动已暂停。稍后会自动重试。'
        : '',
      snapshot: homeSnapshotView(displaySnapshot),
      topActions: freshness.stale ? [] : snapshot.actions.slice(0, 3),
      freshness,
    });
    schedulePublicRefresh(this, result.meta, () => this.loadData());
  },

  retry() {
    this.loadData({ force: true });
  },

  onShareAppMessage(options) {
    return createPageShare({
      title: '宜老天气通：把天气预警变成今天能做的事',
      route: '/pages/home/index',
      source: sourceFromShareEvent(options),
    });
  },

  onShareTimeline() {
    return createTimelineShare({
      title: '宜老天气通：都昌县天气与今日行动',
    });
  },
});
