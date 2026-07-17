Component({
  properties: {
    updatedText: { type: String, value: '' },
    stale: { type: Boolean, value: false },
    source: { type: String, value: '' },
    refreshDeferred: { type: Boolean, value: false },
    refreshStarted: { type: Boolean, value: false },
  },
  data: {
    isOnline: true,
  },
  lifetimes: {
    attached() {
      const app = getApp();
      if (app && typeof app.watchNetwork === 'function') {
        this._unwatch = app.watchNetwork((isOnline) => this.setData({ isOnline }));
      }
    },
    detached() {
      if (this._unwatch) this._unwatch();
    },
  },
});
