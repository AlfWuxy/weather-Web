// Backend base URL (must be HTTPS for real MiniProgram requests).
// During local dev you can temporarily use a LAN IP + HTTPS tunnel.
module.exports = {
  // Dev/pilot default (WeChat DevTools can disable domain validation).
  // Production: must be HTTPS and whitelisted in WeChat MiniProgram console.
  API_BASE_URL: 'http://172.245.126.42:5000',
};
