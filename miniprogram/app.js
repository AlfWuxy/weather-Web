App({
  globalData: {
    apiToken: null,
  },

  onLaunch() {
    const token = (wx.getStorageSync('api_token') || '').trim();
    this.globalData.apiToken = token || null;
  },
});
