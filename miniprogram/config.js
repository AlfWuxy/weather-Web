// 生产域名属于公开网络配置，第三方密钥始终只保存在服务端。
const defaults = {
  API_BASE_URL: '',
  PUBLIC_CACHE_TTL_MS: 30 * 60 * 1000,
  REQUEST_TIMEOUT_MS: 12000,
  GIS_REQUEST_TIMEOUT_MS: 30000,
  PRIVACY_CONSENT_VERSION: '2026-07-21',
};

// 该模块始终存在，确保正式提交可复现并可直接编译。
const runtimeConfig = require('./config.runtime');

module.exports = Object.assign({}, defaults, runtimeConfig || {});
