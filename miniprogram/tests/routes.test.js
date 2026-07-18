const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const miniRoot = path.resolve(__dirname, '..');
const appConfig = JSON.parse(fs.readFileSync(path.join(miniRoot, 'app.json'), 'utf8'));

function collectFiles(directory, suffix, result) {
  fs.readdirSync(directory, { withFileTypes: true }).forEach((entry) => {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory()) collectFiles(target, suffix, result);
    if (entry.isFile() && entry.name.endsWith(suffix)) result.push(target);
  });
}

test('app 首屏、隐私检查与 tabBar 完整', () => {
  assert.equal(appConfig.pages[0], 'pages/home/index');
  assert.equal(appConfig.__usePrivacyCheck__, true);
  assert.deepEqual(appConfig.tabBar.list.map((item) => item.pagePath), [
    'pages/home/index',
    'pages/forecast/index',
    'pages/elders/index',
    'pages/community/index',
    'pages/settings/index',
  ]);
});

test('tabBar 默认与选中图标均存在且视觉资源不同', () => {
  appConfig.tabBar.list.forEach((item) => {
    assert.notEqual(item.iconPath, item.selectedIconPath, `${item.text} 必须配置独立的选中图标`);
    const iconFile = path.join(miniRoot, item.iconPath);
    const selectedIconFile = path.join(miniRoot, item.selectedIconPath);
    assert.equal(fs.existsSync(iconFile), true, `${item.text} 默认图标必须存在`);
    assert.equal(fs.existsSync(selectedIconFile), true, `${item.text} 选中图标必须存在`);
    assert.equal(
      fs.readFileSync(iconFile).equals(fs.readFileSync(selectedIconFile)),
      false,
      `${item.text} 默认与选中图标内容必须不同`,
    );
  });
});

test('自定义组件只在实际使用的页面按需注册', () => {
  assert.equal(appConfig.usingComponents, undefined);
  const componentPages = ['home', 'forecast', 'community', 'actions', 'alerts', 'cooling', 'gis', 'transparency'];
  componentPages.forEach((name) => {
    const config = JSON.parse(fs.readFileSync(path.join(miniRoot, 'pages', name, 'index.json'), 'utf8'));
    assert.deepEqual(config.usingComponents, {
      'status-card': '/components/status-card/index',
      'freshness-bar': '/components/freshness-bar/index',
    }, `${name} 应按需注册公共组件`);
  });
});

test('根目录工程配置使用安全占位并开启域名校验', () => {
  const repoRoot = path.resolve(miniRoot, '..');
  const projectText = fs.readFileSync(path.join(repoRoot, 'project.config.json'), 'utf8');
  const project = JSON.parse(projectText);
  const gitignore = fs.readFileSync(path.join(repoRoot, '.gitignore'), 'utf8');
  assert.equal(project.miniprogramRoot, 'miniprogram/');
  assert.equal(project.appid, 'touristappid');
  assert.doesNotMatch(projectText, /wx[a-f0-9]{16}/);
  assert.match(gitignore, /^\/project\.private\.config\.json$/m);
  assert.equal(project.setting.urlCheck, true);
  assert.equal(project.setting.es6, true);
  assert.equal(project.setting.enhance, true);
});

test('指向 tabBar 的 navigator 使用 switchTab', () => {
  const tabUrls = new Set(appConfig.tabBar.list.map((item) => `/${item.pagePath}`));
  const files = [];
  collectFiles(path.join(miniRoot, 'pages'), '.wxml', files);
  const violations = [];
  files.forEach((file) => {
    const content = fs.readFileSync(file, 'utf8');
    const tags = content.match(/<navigator\b[^>]*>/g) || [];
    tags.forEach((tag) => {
      const urlMatch = tag.match(/\burl=["']([^"']+)["']/);
      if (urlMatch && tabUrls.has(urlMatch[1]) && !/\bopen-type=["']switchTab["']/.test(tag)) {
        violations.push(`${path.relative(miniRoot, file)}: ${tag}`);
      }
    });
  });
  assert.deepEqual(violations, []);
});

test('正式分支固定公开生产域名且保留无密钥示例', () => {
  const files = ['config.js', 'config.runtime.js', 'config.example.js'];
  const text = files.map((file) => fs.readFileSync(path.join(miniRoot, file), 'utf8')).join('\n');
  assert.match(text, /https:\/\/api\.example\.com/);
  const runtimeConfig = require('../config.runtime');
  assert.equal(runtimeConfig.API_BASE_URL, 'https://yilaoweather.org');
  assert.doesNotMatch(text, /(AppSecret|QWEATHER_KEY)\s*[:=]\s*['"][^'"]+['"]/);
});

test('sitemap 只允许公共页面并排除照护页面', () => {
  const sitemap = JSON.parse(fs.readFileSync(path.join(miniRoot, 'sitemap.json'), 'utf8'));
  const allowed = sitemap.rules.filter((rule) => rule.action === 'allow').map((rule) => rule.page).sort();
  assert.deepEqual(allowed, [
    'pages/about/index',
    'pages/actions/index',
    'pages/agreement/index',
    'pages/alerts/index',
    'pages/community/index',
    'pages/cooling/index',
    'pages/forecast/index',
    'pages/gis/index',
    'pages/home/index',
    'pages/privacy/index',
    'pages/transparency/index',
  ]);
  const disallowed = new Set(sitemap.rules.filter((rule) => rule.action === 'disallow').map((rule) => rule.page));
  ['pages/elders/index', 'pages/health-assessment/index', 'pages/diary/index', 'pages/medications/index', 'pages/account/index', 'pages/settings/index', '*'].forEach((page) => {
    assert.equal(disallowed.has(page), true, `${page} 必须禁止索引`);
  });
});

test('登录页提供返回公共首页的明确入口', () => {
  const loginView = fs.readFileSync(path.join(miniRoot, 'pages/bind-token/index.wxml'), 'utf8');
  assert.match(loginView, /bindtap="goPublicHome"/);
  assert.match(loginView, /先查看公共天气/);
  assert.match(loginView, /《隐私说明》/);
  assert.match(loginView, /《用户协议》/);
  assert.equal(appConfig.pages.includes('pages/agreement/index'), true);
});

test('WxPusher 开启前展示完整第三方传输范围', () => {
  const settingsView = fs.readFileSync(path.join(miniRoot, 'pages/settings/index.wxml'), 'utf8');
  const settingsScript = fs.readFileSync(path.join(miniRoot, 'pages/settings/index.js'), 'utf8');
  assert.match(settingsView, /wx:if="\{\{wxpusherFeatureEnabled\}\}" class="settings-card"/);
  assert.match(settingsView, /发送 UID、都昌县级预警标题与正文及 7\s*天内有效的点击链接/);
  assert.match(settingsView, /不会发送家人姓名、健康筛查、健康日记、用药记录或家庭地址/);
  assert.match(settingsView, /打开或预览链接不会记为送达确认/);
  assert.match(settingsView, /必要的访问安全日志/);
  assert.match(settingsView, /页面无法核验实际点击者身份/);
  assert.match(settingsView, /持有链接的人主动点击“我已看到这条提醒”/);
  assert.match(settingsView, /当前说明版本/);
  assert.doesNotMatch(settingsView, /一次性点击链接/);
  assert.match(settingsView, /bindchange="onWxPusherConsent"/);
  assert.match(settingsScript, /wxpusher_consent/);
  assert.match(settingsScript, /required_wxpusher_consent_version/);
  assert.match(settingsScript, /wxpusher_consent_version:\s*this\.data\.requiredWxpusherConsentVersion/);
  assert.match(settingsScript, /me\.wxpusher_feature_enabled === true/);
  assert.doesNotMatch(settingsView, /微信通知权限/);
  assert.doesNotMatch(settingsScript, /wx\.openSetting|openSystemSettings/);
});

test('用药与求助文案明确为仅记录能力', () => {
  const medicationView = fs.readFileSync(path.join(miniRoot, 'pages/medications/index.wxml'), 'utf8');
  const medicationScript = fs.readFileSync(path.join(miniRoot, 'pages/medications/index.js'), 'utf8');
  const helpView = fs.readFileSync(path.join(miniRoot, 'pages/action-checkin/index.wxml'), 'utf8');
  const medicationEntries = [
    'pages/settings/index.wxml',
    'pages/account/index.wxml',
    'pages/elders/index.wxml',
  ].map((file) => fs.readFileSync(path.join(miniRoot, file), 'utf8')).join('\n');
  assert.match(medicationView, /不会定时提醒/);
  assert.match(medicationView, /不会发送订阅消息/);
  assert.match(medicationView, /不会自动通知家人/);
  assert.doesNotMatch(`${medicationView}\n${medicationScript}`, /提醒已添加|满足任一条件时加强提醒|删除这条提醒/);
  assert.match(medicationEntries, /用药记录/);
  assert.doesNotMatch(medicationEntries, /用药提醒/);
  assert.match(helpView, /仅保存求助需求/);
  assert.match(helpView, /不会自动通知照护人/);
});

test('微信发布交接固定个人主体并隔离敏感材料', () => {
  const handoff = fs.readFileSync(path.resolve(miniRoot, '..', 'docs/miniprogram/WECHAT_RELEASE_HANDOFF.md'), 'utf8');
  assert.match(handoff, /已选个人主体/);
  assert.match(handoff, /个人主体无需提供/);
  ['营业执照', '统一社会信用代码', '法人身份资料'].forEach((field) => {
    assert.match(handoff, new RegExp(field));
  });
  ['实名认证', '刷脸验证', '验证码', '页面实际显示缴费项目'].forEach((step) => {
    assert.match(handoff, new RegExp(step));
  });
  assert.match(handoff, /本文档不预设费用/);
  assert.match(handoff, /后台当时实际可选范围/);
  assert.match(handoff, /\.env\.wechat-release/);
  assert.match(handoff, /WECHAT_FORM_READY/);
  assert.match(handoff, /AppID 和 AppSecret/);
  assert.match(handoff, /权限保持 `0600`/);
  ['身份证号码', '人脸信息', '验证码', '银行卡信息', '付款凭证'].forEach((field) => {
    assert.match(handoff, new RegExp(field));
  });
  assert.doesNotMatch(handoff, /\b\d+(?:\.\d+)?\s*元\b/);
});

test('社区页不把静态脆弱性称为当前天气风险', () => {
  const communityView = fs.readFileSync(path.join(miniRoot, 'pages/community/index.wxml'), 'utf8');
  assert.match(communityView, /高脆弱性社区/);
  assert.match(communityView, /不代表当前天气风险/);
  assert.doesNotMatch(communityView, /当前高风险/);
});

test('GIS 只构建一次 Canvas 模型并在离页时中止下载', () => {
  const gisScript = fs.readFileSync(path.join(miniRoot, 'pages/gis/index.js'), 'utf8');
  assert.match(gisScript, /this\._mapRequest\.abort\(\)/);
  assert.match(gisScript, /this\._unloaded/);
  assert.equal((gisScript.match(/makeCanvasModel\(/g) || []).length, 1);
});
