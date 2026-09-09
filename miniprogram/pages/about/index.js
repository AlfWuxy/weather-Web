const { createPageShare, createTimelineShare, showPublicShareMenu } = require('../../utils/share');

Page({
  onLoad() {
    showPublicShareMenu();
  },

  onShareAppMessage() {
    return createPageShare({
      title: '宜老天气通：把预警变成可信行动',
      route: '/pages/about/index',
    });
  },

  onShareTimeline() {
    return createTimelineShare({ title: '宜老天气通：把预警变成可信行动' });
  },
});
