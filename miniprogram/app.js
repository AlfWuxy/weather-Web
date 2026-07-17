App({
  globalData: {
    apiToken: null,
    isOnline: true,
    networkType: 'unknown',
  },

  onLaunch() {
    this._networkListeners = [];
    wx.getNetworkType({
      success: (result) => {
        this._setNetwork(result.networkType !== 'none', result.networkType);
      },
    });
    wx.onNetworkStatusChange((result) => {
      this._setNetwork(result.isConnected, result.networkType);
    });
  },

  _setNetwork(isOnline, networkType) {
    this.globalData.isOnline = Boolean(isOnline);
    this.globalData.networkType = networkType || 'unknown';
    (this._networkListeners || []).slice().forEach((listener) => {
      try {
        listener(this.globalData.isOnline, this.globalData.networkType);
      } catch (error) {
        console.warn('网络状态监听执行失败', error);
      }
    });
  },

  watchNetwork(listener) {
    if (typeof listener !== 'function') return function noop() {};
    this._networkListeners = this._networkListeners || [];
    this._networkListeners.push(listener);
    listener(this.globalData.isOnline, this.globalData.networkType);
    return () => {
      this._networkListeners = (this._networkListeners || []).filter((item) => item !== listener);
    };
  },
});
