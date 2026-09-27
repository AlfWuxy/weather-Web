const { refreshPendingBadge } = require('./utils/pendingBadge');

App({
  globalData: {
    apiToken: null,
  },
  onShow() {
    refreshPendingBadge();
  },
});
