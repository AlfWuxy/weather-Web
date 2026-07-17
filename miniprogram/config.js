// 公开仓库不保存生产域名和第三方密钥。
const defaults = {
  API_BASE_URL: '',
  PUBLIC_CACHE_TTL_MS: 30 * 60 * 1000,
  REQUEST_TIMEOUT_MS: 12000,
  GIS_REQUEST_TIMEOUT_MS: 30000,
  PRIVACY_CONSENT_VERSION: '2026-07-17',
};

// 该模块始终存在，避免微信编译器因缺失模块停止构建。
// 发布者在本机临时填写后应通过 git diff 确认域名未进入提交。
const runtimeConfig = require('./config.runtime');

module.exports = Object.assign({}, defaults, runtimeConfig || {});
