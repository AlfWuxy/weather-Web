const {
  authApi,
  extractRequiredHealthConsentVersion,
  finishHealthMutation,
  invalidateHealthConsent,
  markHealthConsentCurrent,
  releaseHealthConsentNavigation,
  requireToken,
  resumeHealthMutation,
  suspendHealthMutation,
  trackHealthMutation,
} = require('../elders/care-session');

function lifecycleIsActive(page, lifecycle) {
  return page._unloaded !== true
    && page._hidden !== true
    && Number(page._lifecycleGeneration || 0) === lifecycle;
}

function formatConsentTimeUtc(value) {
  const timestamp = Date.parse(String(value || '').trim());
  if (!Number.isFinite(timestamp)) return '';
  return new Date(timestamp).toISOString().replace('.000Z', 'Z');
}

Page({
  data: {
    agreed: false,
    manageMode: false,
    consentCurrent: false,
    requiredVersion: '',
    consentTimeUtc: '',
    loading: true,
    busy: false,
    loadError: '',
    statusHint: '',
  },

  async onLoad(options) {
    this._unloaded = false;
    this._hidden = false;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    this._fallbackVersion = extractRequiredHealthConsentVersion({
      required_health_consent_version: options && options.required_version,
    }, '');
    this.setData({ manageMode: Boolean(options && String(options.manage) === '1') });
    if (!requireToken()) return;
    await this.loadConsentStatus();
  },

  async onShow() {
    this._hidden = false;
    if (!requireToken()) return;
    const shouldRecover = this._resumeStatusPending === true || Boolean(this._healthMutationResumePromise);
    if (!shouldRecover) return;
    await resumeHealthMutation(this);
    if (this._unloaded || this._hidden) return;
    if (!requireToken()) return;
    this._resumeStatusPending = false;
    this.setData({ busy: false, loading: true });
    await this.loadConsentStatus();
  },

  onHide() {
    if (this.data.loading || this.data.busy) {
      this._resumeStatusPending = true;
      this._resumeConsentOperation = this._activeConsentOperation || 'load';
    }
    suspendHealthMutation(this);
    this._hidden = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
  },

  onUnload() {
    this._unloaded = true;
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    releaseHealthConsentNavigation();
  },

  onSessionInvalidated() {
    this._lifecycleGeneration = Number(this._lifecycleGeneration || 0) + 1;
    if (this._unloaded) return;
    this._resumeStatusPending = false;
    this._resumeConsentOperation = '';
    this._activeConsentOperation = '';
    this.setData({
      agreed: false,
      consentCurrent: false,
      requiredVersion: '',
      consentTimeUtc: '',
      loading: false,
      busy: false,
      loadError: '',
      statusHint: '',
    });
  },

  loadConsentStatus() {
    const lifecycle = Number(this._lifecycleGeneration || 0);
    if (this._statusLoadPromise && this._statusLoadLifecycle === lifecycle) {
      return this._statusLoadPromise;
    }
    const pending = this._loadConsentStatus(lifecycle);
    this._statusLoadPromise = pending;
    this._statusLoadLifecycle = lifecycle;
    pending.then(
      () => {
        if (this._statusLoadPromise === pending) this._statusLoadPromise = null;
      },
      () => {
        if (this._statusLoadPromise === pending) this._statusLoadPromise = null;
      }
    );
    return pending;
  },

  async _loadConsentStatus(lifecycle) {
    this.setData({ loading: true, loadError: '', statusHint: '' });
    try {
      const data = await authApi({ method: 'GET', path: '/mp/api/v1/health-consent' });
      if (!lifecycleIsActive(this, lifecycle)) return;
      const requiredVersion = extractRequiredHealthConsentVersion(data, this._fallbackVersion);
      if (!requiredVersion) throw new Error('health_consent_version_missing');
      const recoveryOperation = this._resumeConsentOperation || '';
      if (data && data.health_consent_current === true) {
        markHealthConsentCurrent(requiredVersion);
        this._resumeConsentOperation = '';
        this.setData({
          requiredVersion,
          consentCurrent: true,
          consentTimeUtc: formatConsentTimeUtc(data.health_consented_at),
          loading: false,
          statusHint: recoveryOperation === 'withdraw'
            ? '撤回尚未生效，当前仍保持单独同意。你可以稍后重试。'
            : '当前版本已经完成单独同意。',
        });
        if (!this.data.manageMode) this.returnToPrivatePage();
        return;
      }
      this._resumeConsentOperation = '';
      if (recoveryOperation === 'withdraw') {
        invalidateHealthConsent();
        wx.showToast({ title: '已撤回单独同意', icon: 'success' });
        wx.reLaunch({ url: '/pages/home/index' });
        return;
      }
      this.setData({
        agreed: recoveryOperation === 'submit' ? false : this.data.agreed,
        requiredVersion,
        consentCurrent: false,
        consentTimeUtc: '',
        loading: false,
        statusHint: recoveryOperation === 'submit'
          ? '本次单独同意尚未生效，请重新阅读并勾选。'
          : '',
      });
    } catch (error) {
      if (!lifecycleIsActive(this, lifecycle)) return;
      this.setData({
        loading: false,
        loadError: '授权说明暂时没有加载出来。请检查网络后重试，当前不会显示或提交私密健康资料。',
      });
    }
  },

  onAgreementChange(event) {
    const values = event && event.detail && Array.isArray(event.detail.value)
      ? event.detail.value
      : [];
    this.setData({
      agreed: values.includes('health-consent'),
      statusHint: '',
    });
  },

  async submitConsent() {
    if (this.data.busy || this.data.loading || this._unloaded) return;
    if (!this.data.agreed) {
      this.setData({ statusHint: '请先勾选单独同意，并确认你有权管理这位成年家人的资料。' });
      wx.showToast({ title: '请先勾选单独同意', icon: 'none' });
      return;
    }
    const version = extractRequiredHealthConsentVersion({
      required_health_consent_version: this.data.requiredVersion,
    }, '');
    if (!version) {
      this.setData({ statusHint: '缺少服务端要求的同意版本，请重新加载后再试。' });
      return;
    }
    const lifecycle = Number(this._lifecycleGeneration || 0);
    this.setData({ busy: true, statusHint: '' });
    this._activeConsentOperation = 'submit';
    let mutation = null;
    try {
      mutation = trackHealthMutation(this, authApi({
        method: 'POST',
        path: '/mp/api/v1/health-consent',
        data: {
          consent: true,
          health_consent_version: version,
        },
      }));
      const result = await mutation;
      if (!lifecycleIsActive(this, lifecycle)) return;
      markHealthConsentCurrent(version);
      wx.showToast({ title: '已完成单独同意', icon: 'success' });
      if (this.data.manageMode) {
        this.setData({
          agreed: false,
          consentCurrent: true,
          consentTimeUtc: formatConsentTimeUtc(result && result.health_consented_at),
          busy: false,
          statusHint: '当前版本已经完成单独同意。',
        });
      } else {
        this.returnToPrivatePage();
      }
    } catch (error) {
      if (!lifecycleIsActive(this, lifecycle)) return;
      const requiredVersion = extractRequiredHealthConsentVersion(error && error.data, version);
      this.setData({
        agreed: false,
        requiredVersion,
        statusHint: requiredVersion !== version
          ? '授权说明版本已更新，请重新阅读并勾选。'
          : '提交失败，请检查网络后重试。',
      });
    } finally {
      finishHealthMutation(this, mutation);
      if (this._activeConsentOperation === 'submit') this._activeConsentOperation = '';
      if (lifecycleIsActive(this, lifecycle)) this.setData({ busy: false });
    }
  },

  withdrawConsent() {
    if (this.data.busy || this.data.loading || this._unloaded) return;
    const lifecycle = Number(this._lifecycleGeneration || 0);
    wx.showModal({
      title: '撤回健康资料单独同意？',
      content: '撤回后，家人档案、筛查、日记、用药和家庭行动等私密功能会立即关闭。公开天气仍可正常使用。如需逐条删除，请先取消本次撤回并到对应功能删除；撤回后仍可通过账号注销删除账号关联资料，重新单独同意后也可进入各功能管理。',
      confirmText: '确认撤回',
      confirmColor: '#b42318',
      success: async (result) => {
        if (!lifecycleIsActive(this, lifecycle) || !result.confirm) return;
        this.setData({ busy: true, statusHint: '' });
        this._activeConsentOperation = 'withdraw';
        let mutation = null;
        try {
          mutation = trackHealthMutation(
            this,
            authApi({ method: 'DELETE', path: '/mp/api/v1/health-consent' })
          );
          await mutation;
          if (!lifecycleIsActive(this, lifecycle)) return;
          invalidateHealthConsent();
          wx.showToast({ title: '已撤回单独同意', icon: 'success' });
          wx.reLaunch({ url: '/pages/home/index' });
        } catch (error) {
          if (lifecycleIsActive(this, lifecycle)) {
            this.setData({ statusHint: '撤回失败，请检查网络后重试。' });
          }
        } finally {
          finishHealthMutation(this, mutation);
          if (this._activeConsentOperation === 'withdraw') this._activeConsentOperation = '';
          if (lifecycleIsActive(this, lifecycle)) this.setData({ busy: false });
        }
      },
    });
  },

  returnToPrivatePage() {
    if (this._unloaded) return;
    const pages = typeof getCurrentPages === 'function' ? getCurrentPages() : [];
    if (pages.length > 1) {
      wx.navigateBack({ fail: () => this.goPublicHome() });
      return;
    }
    this.goPublicHome();
  },

  goPublicHome() {
    if (this._unloaded) return;
    // 拒绝或无法核验时销毁私人页面栈，只保留公开天气入口。
    wx.reLaunch({ url: '/pages/home/index' });
  },

  retry() {
    if (!this.data.loading && !this.data.busy) this.loadConsentStatus();
  },

  openPrivacy() {
    if (!this._unloaded) wx.navigateTo({ url: '/pages/privacy/index' });
  },
});
