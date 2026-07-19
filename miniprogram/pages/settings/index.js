const {
  clear,
  getToken,
  tokenApi,
} = require('../elders/care-session');
const { clearAcquisitionContext } = require('../../utils/share');

Page({
  data: {
    busy: false,
    loggedIn: false,
  },

  onShow() {
    this.setData({ loggedIn: !!getToken(), busy: false });
  },

  onSessionInvalidated() {
    this.setData({ loggedIn: false, busy: false });
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

  goHealthConsent() {
    if (!getToken()) {
      this.goLogin();
      return;
    }
    wx.navigateTo({ url: '/pages/health-consent/index?manage=1' });
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
        const sessionToken = getToken();
        let logoutRequest = null;
        try {
          if (sessionToken) {
            logoutRequest = tokenApi(sessionToken, {
              method: 'POST',
              path: '/mp/api/v1/auth/logout',
            });
          }
        } catch (error) {
          logoutRequest = null;
        }
        clear();
        clearAcquisitionContext();
        this.setData({ loggedIn: false, busy: false });
        wx.reLaunch({ url: '/pages/home/index' });
        try {
          if (logoutRequest) await logoutRequest;
        } catch (error) {
          // 远端注销失败时，本机私人数据仍已立即清除。
        }
      },
    });
  },
});
