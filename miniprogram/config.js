// Backend base URL (must be HTTPS for real MiniProgram requests).
// During local dev you can temporarily use a LAN IP + HTTPS tunnel.
module.exports = {
  // 公开仓库默认留空。调试或联调前，必须先改成真实 HTTPS API 地址。
  // Production: must be HTTPS and whitelisted in WeChat MiniProgram console.
  API_BASE_URL: '',
};
