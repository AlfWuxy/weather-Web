const { getBootstrap } = require('../../utils/public-data');
const { freshnessView, normalizeBootstrap } = require('../../utils/format');

const GENERAL_ACTIONS = [
  { id: 'general-water', title: '少量多次补水', detail: '不要等到明显口渴才喝水。心肾疾病患者按医生要求控制饮水。' },
  { id: 'general-room', title: '检查室内温度和通风', detail: '午后高温时拉上遮光帘，合理使用风扇或空调。' },
  { id: 'general-contact', title: '和家人确认一次状态', detail: '问清是否头晕、胸闷、乏力，并确认电话保持畅通。' },
  { id: 'general-outdoor', title: '避开最热时段外出', detail: '需要外出时带水、遮阳用品和常用药。' },
];

function todayKey() {
  const date = new Date();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

function readChecked() {
  try {
    const record = wx.getStorageSync(`yl_actions_${todayKey()}`);
    return record && typeof record === 'object' ? record : {};
  } catch (error) {
    return {};
  }
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
  },

  onLoad() {
    this.loadData();
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
      const result = await getBootstrap(options);
      const snapshot = normalizeBootstrap(result.data);
      const generalMode = !snapshot.available || !snapshot.actions.length;
      const sourceActions = generalMode ? GENERAL_ACTIONS : snapshot.actions;
      const actions = this.mergeChecked(sourceActions);
      this.setData({
        loading: false,
        error: '',
        actions,
        completedCount: actions.filter((item) => item.checked).length,
        progressPercent: actions.length ? Math.round(actions.filter((item) => item.checked).length / actions.length * 100) : 0,
        generalMode,
        locationName: snapshot.location.name,
        freshness: freshnessView(result.meta, snapshot),
      });
    } catch (error) {
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

  toggleAction(event) {
    const id = event.currentTarget.dataset.id;
    const actions = this.data.actions.map((item) => item.id === id
      ? Object.assign({}, item, { checked: !item.checked })
      : item);
    const record = {};
    actions.forEach((item) => { if (item.checked) record[item.id] = true; });
    try {
      wx.setStorageSync(`yl_actions_${todayKey()}`, record);
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

  onShareAppMessage() {
    return { title: `${this.data.locationName}今日防护清单`, path: '/pages/actions/index' };
  },
});
