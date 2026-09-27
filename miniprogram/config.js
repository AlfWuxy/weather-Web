// Backend base URL (must be HTTPS for real MiniProgram requests).
// During local dev you can temporarily use a LAN IP + HTTPS tunnel.
const PROD_API_BASE_URL = 'https://yilaoweather.org';
// 开发者工具可改成局域网隧道地址；留空则 develop 也走正式域名。
const DEV_API_BASE_URL = '';

function getMiniProgramEnvVersion() {
  try {
    if (typeof wx === 'undefined' || typeof wx.getAccountInfoSync !== 'function') {
      return '';
    }
    const account = wx.getAccountInfoSync();
    return (account && account.miniProgram && account.miniProgram.envVersion) || '';
  } catch (e) {
    return '';
  }
}

function getApiBaseUrl() {
  const envVersion = getMiniProgramEnvVersion();
  if (envVersion === 'develop' && DEV_API_BASE_URL) {
    return String(DEV_API_BASE_URL).replace(/\/$/, '');
  }
  return PROD_API_BASE_URL;
}

module.exports = {
  API_BASE_URL: getApiBaseUrl(),
  DEV_API_BASE_URL,
  getApiBaseUrl,
};
