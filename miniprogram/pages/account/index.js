const { authApi, clear, getMeta, requireToken } = require('../elders/care-session');
const { clearAcquisitionContext } = require('../../utils/share');

function publicHome() {
  wx.reLaunch({ url: '/pages/home/index' });
}

Page({
  data: {
    me: null,
    accountVerified: false,
    loadError: '',
    canDeleteAccount: false,
    loading: true,
    busy: false,
  },

  async onShow() {
    if (!requireToken()) return;
    this._unloaded = false;
    const meta = getMeta();
    this.setData({ canDeleteAccount: meta.login_method === 'wechat' });
    await this.loadAccount();
  },

  onSessionInvalidated() {
    this.setData({
      me: null,
      accountVerified: false,
      loadError: '',
      canDeleteAccount: false,
      loading: false,
      busy: false,
    });
  },

  onUnload() {
    this._unloaded = true;
  },

  async loadAccount() {
    this.setData({ loading: true, loadError: '', accountVerified: false, me: null });
    try {
      const me = await authApi({ method: 'GET', path: '/mp/api/v1/me' });
      if (this._unloaded) return;
      if (!me || typeof me !== 'object') throw new Error('invalid_account_response');
      this.setData({ me, accountVerified: true });
    } catch (error) {
      if (this._unloaded) return;
      this.setData({
        me: null,
        accountVerified: false,
        loadError: '账号信息没有验证成功，请检查网络后重试。',
      });
    } finally {
      if (!this._unloaded) this.setData({ loading: false });
    }
  },

  openPrivacy() {
    if (typeof wx.openPrivacyContract === 'function') {
      wx.openPrivacyContract({
        fail: () => {
          if (!this._unloaded) this.showLocalPrivacy();
        },
      });
      return;
    }
    this.showLocalPrivacy();
  },

  showLocalPrivacy() {
    wx.navigateTo({ url: '/pages/privacy/index' });
  },

  openAgreement() {
    wx.navigateTo({ url: '/pages/agreement/index' });
  },

  logout() {
    if (this.data.busy) return;
    wx.showModal({
      title: '退出登录？',
      content: '退出后会清理本机登录状态，重新使用照护功能时需要再次登录。',
      confirmText: '退出登录',
      success: async (result) => {
        if (!result.confirm) return;
        this.setData({ busy: true });
        try {
          await authApi({ method: 'POST', path: '/mp/api/v1/auth/logout' });
        } catch (error) {
          // 网络异常时仍清理本机状态，避免共享设备继续显示账号资料。
        } finally {
          clear();
          clearAcquisitionContext();
          this.setData({ busy: false });
          publicHome();
        }
      },
    });
  },

  requestAccountDeletion() {
    if (this.data.busy) return;
    if (!this.data.accountVerified) {
      wx.showToast({ title: '请先重新验证账号信息', icon: 'none' });
      return;
    }
    if (!this.data.canDeleteAccount) {
      wx.showModal({
        title: '请先使用微信登录',
        content: '旧版 Web Token 只能退出本机绑定。账号注销需要先退出，再使用微信快捷登录进入。',
        showCancel: false,
      });
      return;
    }
    wx.showModal({
      title: '申请注销账号',
      content: '注销会影响老人资料、健康记录和提醒功能。服务端将按隐私规则和保留期限处理相关数据。是否继续查看最终确认？',
      confirmText: '继续',
      confirmColor: '#b42318',
      success: (first) => {
        if (!first.confirm) return;
        wx.showModal({
          title: '再次确认注销申请',
          content: '提交后当前设备会立即退出登录。数据处理进度与范围以服务端返回说明为准。',
          confirmText: '提交申请',
          confirmColor: '#b42318',
          success: async (second) => {
            if (!second.confirm) return;
            this.setData({ busy: true });
            try {
              await authApi({
                method: 'DELETE',
                path: '/mp/api/v1/me',
                data: { confirm: true },
              });
              const message = '注销处理已完成，账号身份已匿名化，当前会话已退出。关联照护数据已按服务端规则处理。';
              clear();
              clearAcquisitionContext();
              wx.showModal({
                title: '服务端处理结果',
                content: message,
                showCancel: false,
                success: publicHome,
              });
            } catch (error) {
              wx.showToast({ title: '申请未提交成功，请稍后再试', icon: 'none' });
            } finally {
              this.setData({ busy: false });
            }
          },
        });
      },
    });
  },
});
