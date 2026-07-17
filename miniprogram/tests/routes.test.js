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

test('根目录工程配置可按游客模式导入并开启域名校验', () => {
  const project = JSON.parse(fs.readFileSync(path.resolve(miniRoot, '..', 'project.config.json'), 'utf8'));
  assert.equal(project.miniprogramRoot, 'miniprogram/');
  assert.equal(project.appid, 'touristappid');
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

test('公开配置不包含真实生产域名', () => {
  const files = ['config.js', 'config.runtime.js', 'config.example.js'];
  const text = files.map((file) => fs.readFileSync(path.join(miniRoot, file), 'utf8')).join('\n');
  assert.match(text, /https:\/\/api\.example\.com/);
  const runtimeConfig = require('../config.runtime');
  assert.equal(runtimeConfig.API_BASE_URL, '');
});

test('sitemap 只允许公共页面并排除照护页面', () => {
  const sitemap = JSON.parse(fs.readFileSync(path.join(miniRoot, 'sitemap.json'), 'utf8'));
  const allowed = sitemap.rules.filter((rule) => rule.action === 'allow').map((rule) => rule.page).sort();
  assert.deepEqual(allowed, [
    'pages/about/index',
    'pages/actions/index',
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
