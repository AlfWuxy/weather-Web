const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const miniRoot = path.resolve(__dirname, '..');

function collectFiles(directory, suffix, result = []) {
  fs.readdirSync(directory, { withFileTypes: true }).forEach((entry) => {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory()) collectFiles(target, suffix, result);
    if (entry.isFile() && entry.name.endsWith(suffix)) result.push(target);
  });
  return result;
}

function parseRules(style) {
  return Array.from(style.matchAll(/([^{}]+)\{([^{}]*)\}/g), (match) => ({
    selectors: match[1].split(',').map((selector) => selector.trim()),
    body: match[2],
  }));
}

function minHeightsForSelector(relativePath, selector) {
  const style = fs.readFileSync(path.join(miniRoot, relativePath), 'utf8');
  return parseRules(style)
    .filter((rule) => rule.selectors.includes(selector))
    .flatMap((rule) => Array.from(rule.body.matchAll(/min-height\s*:\s*(\d+(?:\.\d+)?)rpx/g), (match) => Number(match[1])));
}

function pixelFontSizesForSelector(relativePath, selector) {
  const style = fs.readFileSync(path.join(miniRoot, relativePath), 'utf8');
  return parseRules(style)
    .filter((rule) => rule.selectors.includes(selector))
    .flatMap((rule) => Array.from(
      rule.body.matchAll(/font-size\s*:\s*(\d+(?:\.\d+)?)px/g),
      (match) => Number(match[1])
    ));
}

test('所有小程序样式在 320px 设备上的阅读字号不小于 14px', () => {
  const styleFiles = collectFiles(miniRoot, '.wxss').sort();
  const violations = [];

  styleFiles.forEach((file) => {
    const style = fs.readFileSync(file, 'utf8');
    Array.from(style.matchAll(/font-size\s*:\s*([^;}]+)/g)).forEach((match) => {
      const value = match[1].trim();
      const rpx = value.match(/^(\d+(?:\.\d+)?)rpx$/);
      const px = value.match(/^(\d+(?:\.\d+)?)px$/);
      const renderedPx = px ? Number(px[1]) : (rpx ? Number(rpx[1]) * 320 / 750 : 0);
      if (renderedPx < 14) {
        const line = style.slice(0, match.index).split('\n').length;
        violations.push(`${path.relative(miniRoot, file)}:${line} font-size: ${value}`);
      }
    });
  });

  assert.deepEqual(violations, []);
});

test('核心适老正文、新鲜度和风险说明使用 16px 大字令牌', () => {
  const privacyStyle = fs.readFileSync(path.join(miniRoot, 'pages/privacy/index.wxss'), 'utf8');
  const agreementStyle = fs.readFileSync(path.join(miniRoot, 'pages/agreement/index.wxss'), 'utf8');
  const freshnessStyle = fs.readFileSync(path.join(miniRoot, 'components/freshness-bar/index.wxss'), 'utf8');
  const homeStyle = fs.readFileSync(path.join(miniRoot, 'pages/home/index.wxss'), 'utf8');
  const settingsView = fs.readFileSync(path.join(miniRoot, 'pages/settings/index.wxml'), 'utf8');

  for (const selector of ['.privacy-text', '.list-card view', '.privacy-version']) {
    assert.match(privacyStyle, new RegExp(`${selector.replace('.', '\\.')}\\s*\\{[^}]*font-size:\\s*16px`));
  }
  assert.match(agreementStyle, /\.agreement-text\s*\{[^}]*font-size:\s*16px/s);
  assert.match(agreementStyle, /\.effective-date\s*\{[^}]*font-size:\s*16px/s);
  assert.match(freshnessStyle, /\.freshness\s*\{[^}]*font-size:\s*16px/s);
  assert.match(homeStyle, /\.family-share-copy\s*\{[^}]*font-size:\s*16px/);
  assert.match(homeStyle, /\.risk-label\s*\{[^}]*font-size:\s*16px/);
  assert.match(homeStyle, /\.risk-summary\s*\{[^}]*font-size:\s*16px/);
  assert.match(privacyStyle, /@media screen and \(max-width: 340px\)[\s\S]*\.privacy-card\s*\{\s*grid-template-columns:\s*1fr/);
  assert.match(homeStyle, /@media screen and \(max-width: 340px\)[\s\S]*\.risk-bubble\s*\{\s*width:\s*100%/);
  assert.match(settingsView, /生成可手动发送的提醒话术/);
  assert.doesNotMatch(settingsView, /设置提醒/);

  const largeTextContracts = [
    ['app.wxss', '.hero-subtitle'],
    ['app.wxss', '.section-subtitle'],
    ['app.wxss', '.safe-note'],
    ['components/status-card/index.wxss', '.state-detail'],
    ['components/status-card/index.wxss', '.state-action'],
    ['pages/elders/index.wxss', '.page-subtitle'],
    ['pages/elders/index.wxss', '.header-button'],
    ['pages/elders/index.wxss', '.edit-button'],
    ['pages/elders/index.wxss', '.weather-label'],
    ['pages/elders/index.wxss', '.weather-range'],
    ['pages/elders/index.wxss', '.weather-status-copy'],
    ['pages/elders/index.wxss', '.risk-badge'],
    ['pages/elders/index.wxss', '.add-button'],
    ['pages/elders/index.wxss', '.inline-refresh'],
    ['pages/elders/index.wxss', '.inline-warning'],
    ['pages/elders/index.wxss', '.retry-button'],
    ['pages/elders/index.wxss', '.empty-copy'],
    ['pages/elders/index.wxss', '.primary-action'],
    ['pages/elders/index.wxss', '.tool-button'],
    ['pages/elders/index.wxss', '.delete-link'],
    ['pages/elders/index.wxss', '.medical-note'],
    ['pages/settings/index.wxss', '.page-copy'],
    ['pages/settings/index.wxss', '.guest-copy'],
    ['pages/settings/index.wxss', '.login-button'],
    ['pages/settings/index.wxss', '.fallback-note'],
    ['pages/settings/index.wxss', '.policy-copy'],
    ['pages/settings/index.wxss', '.section-copy'],
    ['pages/settings/index.wxss', '.menu-copy'],
    ['pages/settings/index.wxss', '.switch-copy'],
    ['pages/settings/index.wxss', '.field-label'],
    ['pages/settings/index.wxss', '.text-input'],
    ['pages/settings/index.wxss', '.consent-copy'],
    ['pages/settings/index.wxss', '.switch-title'],
    ['pages/settings/index.wxss', '.menu-title'],
    ['pages/settings/index.wxss', '.save-button'],
    ['pages/settings/index.wxss', '.logout-button'],
    ['pages/actions/index.wxss', '.check-detail'],
    ['pages/actions/index.wxss', '.check-state'],
    ['pages/actions/index.wxss', '.completion-share-copy'],
  ];
  const undersized = largeTextContracts.filter(([file, selector]) => (
    !pixelFontSizesForSelector(file, selector).some((size) => size >= 16)
  )).map(([file, selector]) => `${file} ${selector}`);
  assert.deepEqual(undersized, []);
});

test('374px 及以下设备为原生交互控件提供 44px 触控基线并降低公共双列布局', () => {
  const style = fs.readFileSync(path.join(miniRoot, 'app.wxss'), 'utf8');
  assert.match(style, /@media screen and \(max-width: 374px\)[\s\S]*min-height:\s*44px\s*!important/);
  assert.match(style, /@media screen and \(max-width: 374px\)[\s\S]*min-width:\s*44px\s*!important/);
  assert.match(style, /@media screen and \(max-width: 374px\)[\s\S]*\.grid-two\s*\{\s*grid-template-columns:\s*1fr/);
});

function relativeLuminance(hex) {
  const value = hex.replace('#', '');
  const channels = [0, 2, 4].map((offset) => parseInt(value.slice(offset, offset + 2), 16) / 255);
  return channels.map((channel) => (
    channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  )).reduce((sum, channel, index) => sum + channel * [0.2126, 0.7152, 0.0722][index], 0);
}

function contrastRatio(foreground, background) {
  const light = Math.max(relativeLuminance(foreground), relativeLuminance(background));
  const dark = Math.min(relativeLuminance(foreground), relativeLuminance(background));
  return (light + 0.05) / (dark + 0.05);
}

function expandHex(value) {
  const color = String(value || '').toLowerCase();
  if (/^#[0-9a-f]{6}$/.test(color)) return color;
  if (/^#[0-9a-f]{3}$/.test(color)) {
    return `#${color.slice(1).split('').map((part) => part + part).join('')}`;
  }
  return '';
}

test('深色和浅色 hero 副标题令牌均达到 4.5:1', () => {
  const globalStyle = fs.readFileSync(path.join(miniRoot, 'app.wxss'), 'utf8');
  const agreementStyle = fs.readFileSync(path.join(miniRoot, 'pages/agreement/index.wxss'), 'utf8');
  assert.match(globalStyle, /\.hero-subtitle\s*\{[\s\S]*color:\s*#fff;[\s\S]*background:\s*#6f290d;/);
  assert.match(agreementStyle, /\.agreement-hero \.hero-subtitle\s*\{[\s\S]*color:\s*#34483c;[\s\S]*background:\s*#e3eee7;/);
  assert.ok(contrastRatio('#ffffff', '#6f290d') >= 4.5);
  assert.ok(contrastRatio('#34483c', '#e3eee7') >= 4.5);
});

test('橙色 hero 渐变的所有端点与白色标题均达到 4.5:1', () => {
  const styleFiles = collectFiles(miniRoot, '.wxss')
    .filter((file) => !file.endsWith(path.join('pages', 'agreement', 'index.wxss')));
  const gradients = [];
  styleFiles.forEach((file) => {
    const style = fs.readFileSync(file, 'utf8');
    Array.from(style.matchAll(/linear-gradient\(145deg,\s*(#[0-9a-f]{6})\s+0%,\s*(#[0-9a-f]{6})\s+100%\)/gi))
      .forEach((match) => gradients.push({ file, colors: [match[1], match[2]] }));
  });

  assert.ok(gradients.length >= 10, '应覆盖全局及所有橙色 hero 渐变');
  const violations = gradients.flatMap(({ file, colors }) => colors
    .filter((color) => contrastRatio('#ffffff', color) < 4.5)
    .map((color) => `${path.relative(miniRoot, file)} ${color}`));
  assert.deepEqual(violations, []);
});

test('所有同规则小字颜色与纯色背景达到 4.5:1', () => {
  const styleFiles = collectFiles(miniRoot, '.wxss').sort();
  const violations = [];
  let checkedRules = 0;
  styleFiles.forEach((file) => {
    const style = fs.readFileSync(file, 'utf8');
    parseRules(style).forEach((rule) => {
      const selectorText = rule.selectors.join(',');
      // WCAG 允许已禁用控件不遵循普通文字对比度阈值。
      if (/\[disabled\]|\.unavailable|\.disabled/.test(selectorText)) return;
      const colorMatch = rule.body.match(/(?:^|;)\s*color\s*:\s*(#[0-9a-f]{3,6})(?:\s*!important)?\s*(?:;|$)/i);
      const backgroundMatch = rule.body.match(/(?:^|;)\s*background\s*:\s*(#[0-9a-f]{3,6})(?:\s*!important)?\s*(?:;|$)/i);
      if (!colorMatch || !backgroundMatch) return;
      const color = expandHex(colorMatch[1]);
      const background = expandHex(backgroundMatch[1]);
      if (!color || !background) return;
      checkedRules += 1;
      if (contrastRatio(color, background) < 4.5) {
        violations.push(`${path.relative(miniRoot, file)} ${selectorText} ${color}/${background}`);
      }
    });
  });
  assert.ok(checkedRules >= 20, '应覆盖主要纯色按钮和文字卡片');
  assert.deepEqual(violations, []);
});

test('继承父卡片背景的小字颜色也达到 4.5:1', () => {
  const coolingStyle = fs.readFileSync(path.join(miniRoot, 'pages/cooling/index.wxss'), 'utf8');
  const elderStyle = fs.readFileSync(path.join(miniRoot, 'pages/elder-edit/index.wxss'), 'utf8');
  assert.match(coolingStyle, /\.line-label\s*\{\s*color:\s*#66594f/);
  assert.match(elderStyle, /\.fixed-field text\s*\{[^}]*color:\s*#586d63/);
  assert.match(elderStyle, /\.field-hint\s*\{[^}]*color:\s*#5f6f67/);
  assert.ok(contrastRatio('#66594f', '#fffdf9') >= 4.5);
  assert.ok(contrastRatio('#586d63', '#f2f8f4') >= 4.5);
  assert.ok(contrastRatio('#5f6f67', '#ffffff') >= 4.5);
});

test('原生按钮、导航、选择和输入控件共享 88rpx 触控基线', () => {
  const style = fs.readFileSync(path.join(miniRoot, 'app.wxss'), 'utf8');
  const rules = parseRules(style);
  const controlTags = ['button', 'navigator', 'picker', 'input', 'textarea', 'label'];
  const baseline = rules.find((rule) => (
    controlTags.every((tag) => rule.selectors.includes(tag))
    && /min-height\s*:\s*88rpx/.test(rule.body)
  ));

  assert.ok(baseline, 'app.wxss 必须声明完整的原生交互控件基线');
  assert.match(baseline.body, /min-height\s*:\s*88rpx/);
});

test('紧凑按钮、页签、复选与开关包装区仍保持 88rpx', () => {
  const contracts = [
    ['components/status-card/index.wxss', '.state-action'],
    ['pages/account/index.wxss', '.delete-button'],
    ['pages/action-checkin/index.wxss', '.action-row'],
    ['pages/action-checkin/index.wxss', '.help-button'],
    ['pages/action-checkin/index.wxss', '.emergency-button'],
    ['pages/action-checkin/index.wxss', '.picker-field'],
    ['pages/action-checkin/index.wxss', '.text-input'],
    ['pages/action-checkin/index.wxss', '.optin-row'],
    ['pages/bind-token/index.wxss', '.consent-row'],
    ['pages/bind-token/index.wxss', '.privacy-link'],
    ['pages/bind-token/index.wxss', '.row-button'],
    ['pages/community/index.wxss', '.filter-pill'],
    ['pages/cooling/index.wxss', '.filter-pill'],
    ['pages/cooling/index.wxss', '.small-button'],
    ['pages/diary/index.wxss', '.picker-field'],
    ['pages/elder-edit/index.wxss', '.text-input'],
    ['pages/elder-edit/index.wxss', '.picker-field'],
    ['pages/elders/index.wxss', '.primary-action'],
    ['pages/elders/index.wxss', '.tool-button'],
    ['pages/forecast/index.wxss', '.compact-button'],
    ['pages/gis/index.wxss', '.mode-button'],
    ['pages/gis/index.wxss', '.layer-button'],
    ['pages/health-assessment/index.wxss', '.person-picker'],
    ['pages/health-assessment/index.wxss', '.option-button'],
    ['pages/medications/index.wxss', '.text-input'],
    ['pages/medications/index.wxss', '.picker-field'],
    ['pages/medications/index.wxss', '.small-input'],
    ['pages/settings/index.wxss', '.text-input'],
    ['pages/settings/index.wxss', '.switch-row'],
    ['pages/settings/index.wxss', '.third-party-consent'],
  ];
  const violations = contracts.filter(([file, selector]) => (
    !minHeightsForSelector(file, selector).some((height) => height >= 88)
  )).map(([file, selector]) => `${file} ${selector}`);

  assert.deepEqual(violations, []);
});

test('登录协议复选标签自身具有完整触控面积', () => {
  const style = fs.readFileSync(path.join(miniRoot, 'pages/bind-token/index.wxss'), 'utf8');
  const view = fs.readFileSync(path.join(miniRoot, 'pages/bind-token/index.wxml'), 'utf8');
  assert.match(style, /\.consent-check\s*\{[^}]*min-width:\s*88rpx;[^}]*min-height:\s*88rpx;/);
  assert.match(style, /\.consent-check-group\s*\{[^}]*flex:\s*0 0 88rpx;[^}]*min-height:\s*88rpx;/);
  assert.match(view, /<label class="consent-check" for="privacyAgreementCheckbox"[\s\S]*我已阅读并同意[\s\S]*<\/label>/);
  assert.match(view, /<checkbox id="privacyAgreementCheckbox"/);
  assert.match(style, /\.consent-tail\s*\{[^}]*display:\s*inline-flex;[^}]*flex:\s*0 0 auto;[^}]*min-height:\s*88rpx;/);
  assert.match(view, /<view class="consent-tail">[\s\S]*>和<[\s\S]*aria-label="阅读用户协议"[\s\S]*<\/view>/);
});

test('开关的文案与控件共用整行 label 触控区', () => {
  const checkinView = fs.readFileSync(path.join(miniRoot, 'pages/action-checkin/index.wxml'), 'utf8');
  const settingsView = fs.readFileSync(path.join(miniRoot, 'pages/settings/index.wxml'), 'utf8');
  assert.match(checkinView, /<label class="optin-row" for="debriefOptinSwitch"[\s\S]*<switch id="debriefOptinSwitch"[\s\S]*<\/label>/);
  assert.match(settingsView, /<label class="switch-row" for="wxpusherSwitch"[\s\S]*<switch id="wxpusherSwitch"[\s\S]*<\/label>/);
  assert.match(settingsView, /wx:if="\{\{wxpusherFeatureEnabled\}\}" class="settings-card"/);
});

test('所有 WXML role 同时声明小程序 aria-role', () => {
  const viewFiles = collectFiles(miniRoot, '.wxml').sort();
  const violations = [];
  viewFiles.forEach((file) => {
    const view = fs.readFileSync(file, 'utf8');
    Array.from(view.matchAll(/<[^>]*\srole="[^"]+"[^>]*>/g)).forEach((match) => {
      if (/\saria-role="[^"]+"/.test(match[0])) return;
      const line = view.slice(0, match.index).split('\n').length;
      violations.push(`${path.relative(miniRoot, file)}:${line}`);
    });
  });
  assert.deepEqual(violations, []);
});

test('所有表单文本控件和图片都有程序化可读名称', () => {
  const viewFiles = collectFiles(miniRoot, '.wxml').sort();
  const violations = [];
  viewFiles.forEach((file) => {
    const view = fs.readFileSync(file, 'utf8');
    Array.from(view.matchAll(/<(?:input|textarea|picker)\b[^>]*>/g)).forEach((match) => {
      if (/\saria-label="[^"]+"/.test(match[0])) return;
      const line = view.slice(0, match.index).split('\n').length;
      violations.push(`${path.relative(miniRoot, file)}:${line} 表单控件缺少 aria-label`);
    });
    Array.from(view.matchAll(/<image\b[^>]*>/g)).forEach((match) => {
      if (/\saria-(?:hidden|label)="[^"]+"/.test(match[0])) return;
      const line = view.slice(0, match.index).split('\n').length;
      violations.push(`${path.relative(miniRoot, file)}:${line} 图片缺少隐藏或名称`);
    });
  });
  assert.deepEqual(violations, []);
});

test('组件 WXSS 状态图标只使用 class 选择器', () => {
  const style = fs.readFileSync(path.join(miniRoot, 'components/status-card/index.wxss'), 'utf8');
  const view = fs.readFileSync(path.join(miniRoot, 'components/status-card/index.wxml'), 'utf8');
  assert.doesNotMatch(style, /\.state-icon\s+image\b/);
  assert.match(style, /\.state-icon-image\s*\{/);
  assert.match(view, /class="state-icon-image loading-icon"/);
  assert.match(view, /role="\{\{state === 'error' \? 'alert' : 'status'\}\}"/);
  assert.match(view, /aria-role="\{\{state === 'error' \? 'alert' : 'status'\}\}"/);
  assert.match(view, /aria-live="\{\{state === 'error' \? 'assertive' : 'polite'\}\}"/);
});

test('可见操作图标使用真实图片资源而非文本符号', () => {
  const viewFiles = collectFiles(path.join(miniRoot, 'pages'), '.wxml').sort();
  const violations = [];
  viewFiles.forEach((file) => {
    const view = fs.readFileSync(file, 'utf8');
    if (/[＋✓○]/.test(view)) violations.push(path.relative(miniRoot, file));
  });

  assert.deepEqual(violations, []);
  assert.equal(fs.existsSync(path.join(miniRoot, 'assets/icons/add.png')), true);
  assert.equal(fs.existsSync(path.join(miniRoot, 'assets/icons/add-white.png')), true);
});
