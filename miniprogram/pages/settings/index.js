const { authApi, clear, getToken } = require('../elders/care-session');

Page({
  data: {
    loading: false,
    busy: false,
    loggedIn: false,
    settingsVerified: false,
    wxpusherUid: '',
    pushEnabled: false,
    wxpusherAvailable: false,
    wxpusherConsent: false,
  },

  clearPrivateSettings(overrides) {
    this._settingsVerifiedToken = '';
    this.setData(Object.assign({
      settingsVerified: false,
      wxpusherUid: '',
      pushEnabled: false,
      wxpusherAvailable: false,
      wxpusherConsent: false,
    }, overrides || {}));
  },

  async onShow() {
    const sessionToken = getToken();
    const loggedIn = !!sessionToken;
    this._settingsLoadId = (this._settingsLoadId || 0) + 1;
    this.clearPrivateSettings({ loggedIn, loading: false });
    if (!loggedIn) {
      return;
    }
    await this.loadSettings(sessionToken);
  },

  onSessionInvalidated() {
    this._settingsLoadId = (this._settingsLoadId || 0) + 1;
    this.clearPrivateSettings({ loggedIn: false, loading: false, busy: false });
  },

  async loadSettings(expectedToken) {
    const sessionToken = String(expectedToken || getToken() || '').trim();
    const loadId = (this._settingsLoadId || 0) + 1;
    this._settingsLoadId = loadId;
    this.clearPrivateSettings({ loggedIn: !!sessionToken, loading: !!sessionToken });
    if (!sessionToken) return;
    try {
      const me = await authApi({ method: 'GET', path: '/mp/api/v1/me' });
      if (loadId !== this._settingsLoadId || getToken() !== sessionToken) return;
      if (!me || typeof me !== 'object') throw new Error('invalid_settings_response');
      this._settingsVerifiedToken = sessionToken;
      this.setData({
        wxpusherUid: me.wxpusher_uid || '',
        pushEnabled: !!me.push_enabled,
        wxpusherAvailable: me.wxpusher_available === true,
        wxpusherConsent: false,
        settingsVerified: true,
      });
    } catch (error) {
      if (loadId === this._settingsLoadId) {
        const currentToken = getToken();
        this.clearPrivateSettings({ loggedIn: !!currentToken });
        wx.showToast({ title: '设置加载失败', icon: 'none' });
      }
    } finally {
      if (loadId === this._settingsLoadId) this.setData({ loading: false });
    }
  },

  onUid(event) { this.setData({ wxpusherUid: String(event.detail.value || '').trim() }); },
  onToggle(event) {
    const pushEnabled = !!event.detail.value;
    if (pushEnabled && !this.data.wxpusherAvailable) {
      this.setData({ pushEnabled: false, wxpusherConsent: false });
      wx.showToast({ title: '推送服务暂不可用', icon: 'none' });
      return;
    }
    this.setData({ pushEnabled, wxpusherConsent: pushEnabled ? this.data.wxpusherConsent : false });
  },
  onWxPusherConsent(event) {
    const values = event.detail.value || [];
    this.setData({ wxpusherConsent: values.includes('agreed') });
  },

  async saveSettings() {
    if (this.data.busy) return;
    const sessionToken = getToken();
    if (
      !sessionToken
      || !this.data.settingsVerified
      || this._settingsVerifiedToken !== sessionToken
    ) {
      this.clearPrivateSettings({ loggedIn: !!sessionToken, loading: false });
      wx.showToast({ title: '请先重新加载并验证设置', icon: 'none' });
      return;
    }
    if (this.data.pushEnabled && !this.data.wxpusherAvailable) {
      wx.showToast({ title: '推送服务暂不可用', icon: 'none' });
      return;
    }
    if (this.data.pushEnabled && !this.data.wxpusherUid) {
      wx.showToast({ title: '请先填写 WxPusher UID', icon: 'none' });
      return;
    }
    if (this.data.pushEnabled && !this.data.wxpusherConsent) {
      wx.showToast({ title: '请先确认第三方传输说明', icon: 'none' });
      return;
    }
    this.setData({ busy: true });
    try {
      await authApi({
        method: 'PATCH',
        path: '/mp/api/v1/me',
        data: {
          wxpusher_uid: this.data.wxpusherUid,
          push_enabled: this.data.pushEnabled,
          wxpusher_consent: this.data.pushEnabled && this.data.wxpusherConsent,
        },
      });
      wx.showToast({ title: '设置已保存', icon: 'success' });
    } catch (error) {
      wx.showToast({ title: '保存失败，请稍后再试', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  openSystemSettings() {
    wx.openSetting({ fail: () => wx.showToast({ title: '暂时无法打开系统设置', icon: 'none' }) });
  },

  goAccount() {
    wx.navigateTo({ url: '/pages/account/index' });
  },

  goLogin() {
    wx.navigateTo({ url: '/pages/bind-token/index' });
  },

  goPrivacy() {
    wx.navigateTo({ url: '/pages/privacy/index' });
  },

  goAgreement() {
    wx.navigateTo({ url: '/pages/agreement/index' });
  },

  goAbout() {
    wx.navigateTo({ url: '/pages/about/index' });
  },

  goTransparency() {
    wx.navigateTo({ url: '/pages/transparency/index' });
  },

  logout() {
    if (this.data.busy) return;
    wx.showModal({
      title: '退出登录？',
      content: '会清理本机登录状态，公共天气仍可继续查看。',
      confirmText: '退出登录',
      success: async (result) => {
        if (!result.confirm) return;
        this.setData({ busy: true });
        try {
          await authApi({ method: 'POST', path: '/mp/api/v1/auth/logout' });
        } catch (error) {
          // 网络异常时仍优先保护共享设备上的本机登录状态。
        } finally {
          clear();
          this.setData({ busy: false });
          wx.reLaunch({ url: '/pages/home/index' });
        }
      },
    });
  },
});
