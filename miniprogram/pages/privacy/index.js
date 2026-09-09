const { createPageShare, createTimelineShare, showPublicShareMenu } = require('../../utils/share');

Page({
  onLoad() {
    showPublicShareMenu();
  },

  onShareAppMessage() {
    return createPageShare({ title: '宜老天气通隐私说明', route: '/pages/privacy/index' });
  },

  onShareTimeline() {
    return createTimelineShare({ title: '宜老天气通隐私与数据边界' });
  },
});
