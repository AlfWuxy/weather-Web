const { authApi, clear, getMeta, requireToken } = require('../elders/care-session');

function publicHome() {
  wx.reLaunch({ url: '/pages/home/index' });
}

Page({
  data: {
    me: {},
    canDeleteAccount: false,
    loading: false,
    busy: false,
  },

  async onShow() {
    if (!requireToken()) return;
    const meta = getMeta();
    this.setData({ canDeleteAccount: meta.login_method === 'wechat' });
    await this.loadAccount();
  },

  async loadAccount() {
    this.setData({ loading: true });
    try {
      const me = await authApi({ method: 'GET', path: '/mp/api/v1/me' });
      this.setData({ me: me || {} });
    } catch (error) {
      wx.showToast({ title: '账号信息加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  openPrivacy() {
    if (typeof wx.openPrivacyContract === 'function') {
      wx.openPrivacyContract({
        fail: () => this.showLocalPrivacy(),
      });
      return;
    }
    this.showLocalPrivacy();
  },

  showLocalPrivacy() {
    wx.showModal({
      title: '账号与健康信息说明',
      content: '系统仅保存提供天气提醒、家庭照护和健康记录所需的信息。健康筛查不作医疗诊断。退出登录会清理本机登录状态，服务端记录按隐私规则处理。',
      showCancel: false,
      confirmText: '我知道了',
    });
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
          this.setData({ busy: false });
          publicHome();
        }
      },
    });
  },

  requestAccountDeletion() {
    if (this.data.busy) return;
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
