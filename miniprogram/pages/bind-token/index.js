const {
  extractAuthToken,
  extractRequiredPrivacyVersion,
  getSnapshot,
  getToken,
  publicApi,
  saveToken,
  tokenApi,
} = require('../elders/care-session');
const { PRIVACY_CONSENT_VERSION } = require('../../config');

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
    showTokenFallback: false,
    tokenInput: '',
    busy: false,
    loginHint: '',
    requiredPrivacyVersion: PRIVACY_CONSENT_VERSION,
  },

  async onLoad() {
    if (getToken()) {
      wx.switchTab({ url: '/pages/elders/index' });
      return;
    }
    await this.loadRequiredPrivacyVersion();
  },

  async loadRequiredPrivacyVersion(options) {
    try {
      const snapshot = await getSnapshot(options);
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
    this.setData({ privacyAgreed: values.includes('agreed') });
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
      title: '隐私说明',
      content: '登录后仅保存完成天气提醒和照护功能所需的账号标识、老人资料与主动填写的健康记录。健康信息不会用于医疗诊断。你可以在“账号与隐私”中退出并清理本机登录状态。',
      showCancel: false,
      confirmText: '我知道了',
    });
  },

  requireConsent() {
    if (this.data.privacyAgreed) return true;
    wx.showToast({ title: '请先阅读并同意隐私说明', icon: 'none' });
    return false;
  },

  async onWechatLogin() {
    if (this.data.busy || !this.requireConsent()) return;
    this.setData({ busy: true, loginHint: '' });
    try {
      const consentVersion = extractRequiredPrivacyVersion(
        { required_privacy_consent_version: this.data.requiredPrivacyVersion },
        PRIVACY_CONSENT_VERSION
      );
      if (!consentVersion) throw new Error('privacy_consent_version_missing');
      const loginResult = await wxLogin();
      if (!loginResult.code) throw new Error('missing_wechat_code');
      const data = await publicApi({
        method: 'POST',
        path: '/mp/api/v1/auth/wechat',
        data: {
          code: loginResult.code,
          privacy_consent_version: consentVersion,
        },
      });
      const token = extractAuthToken(data);
      if (!token) throw new Error('missing_session_token');
      saveToken(token, {
        login_method: 'wechat',
        privacy_consent_version: consentVersion,
        expires_at: data.expires_at || '',
        expires_in: data.expires_in || null,
      });
      wx.showToast({ title: '登录成功', icon: 'success' });
      wx.switchTab({ url: '/pages/elders/index' });
    } catch (error) {
      if (requiresPrivacyRefresh(error)) {
        let requiredPrivacyVersion = extractRequiredPrivacyVersion(
          error && error.data,
          this.data.requiredPrivacyVersion || PRIVACY_CONSENT_VERSION
        );
        if (!requiredPrivacyVersion || requiredPrivacyVersion === this.data.requiredPrivacyVersion) {
          const refreshedVersion = await this.loadRequiredPrivacyVersion({ force: true });
          requiredPrivacyVersion = refreshedVersion || requiredPrivacyVersion;
        }
        this.setData({
          privacyAgreed: false,
          requiredPrivacyVersion,
          loginHint: '隐私说明版本已更新，请重新阅读并主动勾选同意。',
        });
        wx.showModal({
          title: '隐私说明已更新',
          content: '请重新阅读最新隐私说明，并在返回后主动勾选同意。系统不会替你自动同意。',
          showCancel: false,
          confirmText: '去阅读',
          success: () => this.openPrivacy(),
        });
        return;
      }
      this.setData({
        showTokenFallback: true,
        loginHint: '微信登录暂未可用，可以先使用网页端生成的旧版 Token 绑定。',
      });
      wx.showToast({ title: '微信登录暂不可用', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  toggleTokenFallback() {
    this.setData({ showTokenFallback: !this.data.showTokenFallback, loginHint: '' });
  },

  onTokenInput(event) {
    this.setData({ tokenInput: event.detail.value || '' });
  },

  onClear() {
    this.setData({ tokenInput: '' });
  },

  goPublicHome() {
    wx.switchTab({ url: '/pages/home/index' });
  },

  async onBind() {
    if (this.data.busy || !this.requireConsent()) return;
    const token = String(this.data.tokenInput || '').trim();
    if (!token) {
      wx.showToast({ title: '请粘贴完整 Token', icon: 'none' });
      return;
    }
    this.setData({ busy: true });
    try {
      await tokenApi(token, { method: 'GET', path: '/mp/api/v1/me' });
      const consentVersion = extractRequiredPrivacyVersion(
        { required_privacy_consent_version: this.data.requiredPrivacyVersion },
        PRIVACY_CONSENT_VERSION
      );
      saveToken(token, { login_method: 'legacy_token', privacy_consent_version: consentVersion });
      this.setData({ tokenInput: '' });
      wx.showToast({ title: '绑定成功', icon: 'success' });
      wx.switchTab({ url: '/pages/elders/index' });
    } catch (error) {
      wx.showToast({ title: '绑定失败，请检查 Token', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },
});
