// Backend base URL (must be HTTPS for real MiniProgram requests).
// During local dev you can temporarily use a LAN IP + HTTPS tunnel.
module.exports = {
  // 公开仓库只保留占位值；真实地址请在私有环境或构建流程中注入。
  // Production: must be HTTPS and whitelisted in WeChat MiniProgram console.
  API_BASE_URL: 'https://your-miniapp-api.example.com',
};
