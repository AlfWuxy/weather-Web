const {
  extractAuthToken,
  extractRequiredPrivacyVersion,
  getSnapshot,
  getToken,
  publicApi,
  saveToken,
} = require('../elders/care-session');
const { PRIVACY_CONSENT_VERSION } = require('../../config');
const { clearAcquisitionContext, readAcquisitionSource } = require('../../utils/share');

function requiresPrivacyRefresh(error) {
  const statusCode = Number(error && (error.statusCode || error.status_code || error.status));
  const message = String(error && (error.code || error.message || error.error) || '').toLowerCase();
  return statusCode === 428 || message.includes('privacy_consent') || message.includes('consent_version');
}

function wxLogin() {
  return new Promise((resolve, reject) => {
    wx.login({ success: resolve, fail: reject });
  });
}

Page({
  data: {
    privacyAgreed: false,
    busy: false,
    loginFailed: false,
    loginHint: '',
    requiredPrivacyVersion: PRIVACY_CONSENT_VERSION,
  },

  async onLoad() {
    this._unloaded = false;
    if (getToken()) {
      wx.switchTab({ url: '/pages/elders/index' });
      return;
    }
    await this.loadRequiredPrivacyVersion();
  },

  onUnload() {
    this._unloaded = true;
  },

  async loadRequiredPrivacyVersion(options) {
    try {
      const snapshot = await getSnapshot(options);
      if (this._unloaded) return this.data.requiredPrivacyVersion || PRIVACY_CONSENT_VERSION;
      const version = extractRequiredPrivacyVersion(snapshot, this.data.requiredPrivacyVersion || PRIVACY_CONSENT_VERSION);
      if (version && version !== this.data.requiredPrivacyVersion) {
        this.setData({ requiredPrivacyVersion: version });
      }
      return version;
    } catch (error) {
      // 公共快照暂时失败时保留随包版本，428 响应仍可在登录时纠正版本。
      return this.data.requiredPrivacyVersion || PRIVACY_CONSENT_VERSION;
    }
  },

  onConsentChange(event) {
    const values = event.detail.value || [];
    const privacyAgreed = values.includes('agreed');
    this.setData({
      privacyAgreed,
      loginHint: privacyAgreed && /隐私说明和用户协议/.test(this.data.loginHint) ? '' : this.data.loginHint,
    });
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

  requireConsent() {
    if (this.data.privacyAgreed) return true;
    this.setData({ loginHint: '请先阅读并勾选同意《隐私说明》和《用户协议》，再继续登录。' });
    wx.showToast({ title: '请先同意协议', icon: 'none' });
    return false;
  },

  async onWechatLogin() {
    if (this.data.busy || !this.requireConsent()) return;
    this.setData({ busy: true, loginFailed: false, loginHint: '' });
    try {
      const consentVersion = extractRequiredPrivacyVersion(
        { required_privacy_consent_version: this.data.requiredPrivacyVersion },
        PRIVACY_CONSENT_VERSION
      );
      if (!consentVersion) throw new Error('privacy_consent_version_missing');
      const loginResult = await wxLogin();
      if (this._unloaded) return;
      if (!loginResult.code) throw new Error('missing_wechat_code');
      const loginPayload = {
        code: loginResult.code,
        privacy_consent_version: consentVersion,
      };
      const acquisitionSource = readAcquisitionSource();
      if (acquisitionSource) loginPayload.acquisition_source = acquisitionSource;
      const data = await publicApi({
        method: 'POST',
        path: '/mp/api/v1/auth/wechat',
        data: loginPayload,
      });
      if (this._unloaded) return;
      const token = extractAuthToken(data);
      if (!token) throw new Error('missing_session_token');
      saveToken(token, {
        login_method: 'wechat',
        privacy_consent_version: consentVersion,
        expires_at: data.expires_at || '',
        expires_in: data.expires_in || null,
      });
      // 登录成功后消费来源标记，避免共享设备上的下一位用户继承归因。
      clearAcquisitionContext();
      wx.showToast({ title: '登录成功', icon: 'success' });
      wx.switchTab({ url: '/pages/elders/index' });
    } catch (error) {
      if (this._unloaded) return;
      if (requiresPrivacyRefresh(error)) {
        let requiredPrivacyVersion = extractRequiredPrivacyVersion(
          error && error.data,
          this.data.requiredPrivacyVersion || PRIVACY_CONSENT_VERSION
        );
        if (!requiredPrivacyVersion || requiredPrivacyVersion === this.data.requiredPrivacyVersion) {
          const refreshedVersion = await this.loadRequiredPrivacyVersion({ revalidate: true });
          if (this._unloaded) return;
          requiredPrivacyVersion = refreshedVersion || requiredPrivacyVersion;
        }
        if (this._unloaded) return;
        this.setData({
          privacyAgreed: false,
          loginFailed: false,
          requiredPrivacyVersion,
          loginHint: '隐私说明版本已更新，请重新阅读并主动勾选同意。',
        });
        wx.showModal({
          title: '隐私说明已更新',
          content: '请重新阅读最新隐私说明，并在返回后主动勾选同意。系统不会替你自动同意。',
          showCancel: false,
          confirmText: '去阅读',
          success: () => {
            if (!this._unloaded) this.openPrivacy();
          },
        });
        return;
      }
      if (this._unloaded) return;
      this.setData({
        loginFailed: true,
        loginHint: '微信登录暂时没有成功，请检查网络后重试；你也可以先查看公共天气和预警。',
      });
      wx.showToast({ title: '登录失败，请重试', icon: 'none' });
    } finally {
      if (!this._unloaded) this.setData({ busy: false });
    }
  },

  goPublicHome() {
    wx.switchTab({ url: '/pages/home/index' });
  },
});
