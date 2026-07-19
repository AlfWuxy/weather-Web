const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', '..');

function read(relativePath) {
  return fs.readFileSync(path.join(ROOT, relativePath), 'utf8');
}

const FILES = {
  privacyDoc: 'docs/miniprogram/PRIVACY_NOTICE_TEMPLATE.md',
  agreementDoc: 'docs/miniprogram/USER_AGREEMENT_TEMPLATE.md',
  listing: 'docs/miniprogram/LISTING_COPY.md',
  handoff: 'docs/miniprogram/WECHAT_RELEASE_HANDOFF.md',
  manifest: 'docs/miniprogram/REVIEW_SCREENSHOT_MANIFEST_TEMPLATE.md',
  privacyPage: 'miniprogram/pages/privacy/index.wxml',
  agreementPage: 'miniprogram/pages/agreement/index.wxml',
  healthConsentPage: 'miniprogram/pages/health-consent/index.wxml',
  actionCheckinPage: 'miniprogram/pages/action-checkin/index.wxml',
  webActionCheckinPage: 'templates/action_checkin.html',
  careLogic: 'miniprogram/pages/elders/care-logic.js',
  mpApi: 'blueprints/mp_api.py',
  releaseValidator: 'scripts/validate_release_env.py',
};

function frontendDiaryLimit(source, field) {
  const match = source.match(new RegExp(`const ${field} = cleanText\\(input && input\\.${field}, (\\d+)\\)`));
  assert.ok(match, `前端未找到 ${field} 长度边界`);
  return Number(match[1]);
}

function serverDiaryLimit(source, field) {
  const start = source.indexOf('@bp.route("/health/diary"');
  const end = source.indexOf('@bp.route("/medications"', start);
  assert.ok(start >= 0 && end > start, '服务端未找到健康日记 API 区段');
  const diaryBlock = source.slice(start, end);
  const match = diaryBlock.match(new RegExp(`${field} = _strict_text\\(payload, "${field}", (\\d+)`));
  assert.ok(match, `服务端未找到 ${field} 长度边界`);
  return Number(match[1]);
}

test('隐私候选文案完整披露健康筛查与日记字段', () => {
  for (const file of [FILES.privacyDoc, FILES.privacyPage]) {
    const text = read(file);
    for (const field of ['户外暴露', '不适程度', '饮水', '服药规律', '睡眠']) {
      assert.match(text, new RegExp(field), `${file} 缺少 ${field}`);
    }
    assert.match(text, /200 字/);
    assert.match(text, /300 字/);
    assert.match(text, /500 字/);
    assert.match(text, /评估时间.*天气.*快照.*模型.*规则.*结果/s);
    assert.match(text, /用于.*(风险|家庭|回看|行动)/s);
    assert.match(text, /账号注销时删除/);
    assert.match(text, /用药记录可逐条删除/);
  }
});

test('隐私模板与小程序页面统一披露微信登录标识处理', () => {
  for (const file of [FILES.privacyDoc, FILES.privacyPage]) {
    const text = read(file);
    assert.match(text, /微信登录临时代码/, `${file} 缺少微信临时代码用途`);
    assert.match(text, /OpenID.*pepper.*哈希/s, `${file} 缺少 OpenID 哈希方式`);
    assert.match(text, /不保存明文 OpenID/, `${file} 缺少明文排除说明`);
  }
});

test('法律与上架材料统一披露健康敏感信息单独同意和成人边界', () => {
  for (const file of [
    FILES.privacyDoc,
    FILES.agreementDoc,
    FILES.listing,
    FILES.privacyPage,
    FILES.agreementPage,
  ]) {
    const text = read(file);
    assert.match(text, /(健康敏感个人信息|健康敏感信息).*单独同意/s, `${file} 缺少单独同意`);
    assert.match(text, /(18 至 120 岁|18 至 120)/, `${file} 缺少成年家人年龄边界`);
    assert.match(text, /拒绝或撤回.*公开天气.*(仍可|继续)/s, `${file} 缺少拒绝后的公开功能边界`);
  }

  const consentPage = read(FILES.healthConsentPage);
  assert.match(consentPage, /默认不勾选|checked="\{\{agreed\}\}"/);
  assert.match(consentPage, /确认我有权.*年满 18 岁/s);
  assert.match(consentPage, /撤回单独同意/);
});

test('隐私与发布材料精确锁定首发安全限流和审计日志边界', () => {
  for (const file of [FILES.privacyDoc, FILES.privacyPage]) {
    const text = read(file);
    assert.match(text, /限流.*IP.*不可逆哈希.*窗口.*(到期|失效)/s, `${file} 缺少临时 IP 哈希边界`);
    assert.match(text, /1\.0\.0.*关闭数据库安全审计日志/s, `${file} 缺少审计关闭说明`);
    assert.match(text, /不把 IP 哈希或 User-Agent 写入应用审计表/);
  }

  for (const file of [FILES.handoff, 'docs/miniprogram/RELEASE_CHECKLIST.md']) {
    const text = read(file);
    assert.match(text, /FEATURE_AUDIT_LOGS=0/, `${file} 缺少审计发布门禁`);
    assert.match(text, /IP.*哈希.*限流/s, `${file} 缺少限流披露`);
  }

  const validator = read(FILES.releaseValidator);
  assert.match(validator, /FEATURE_AUDIT_LOGS/);
  assert.match(validator, /正式.*FEATURE_AUDIT_LOGS=0/);
});

test('健康日记前端与服务端共享 200 字症状和 500 字备注边界', () => {
  const frontend = read(FILES.careLogic);
  const server = read(FILES.mpApi);
  const expected = { symptoms: 200, notes: 500 };

  for (const [field, limit] of Object.entries(expected)) {
    assert.equal(frontendDiaryLimit(frontend, field), limit);
    assert.equal(serverDiaryLimit(server, field), limit);
  }
});

test('部署事务副本披露 30 天人工保留目标与未解决例外', () => {
  for (const file of [FILES.privacyDoc, FILES.privacyPage]) {
    const privacy = read(file);
    assert.match(privacy, /正常完成且已经解决的事务副本以 30 天为人工保留目标.*root 管理员.*单独.*清理/s);
    assert.match(privacy, /超过 30 天仍未解决或处于中断状态的(?:事务)?副本.*故障恢复与安全审计.*人工处置完成/s);
    assert.match(privacy, /(服务器受限目录内|保存在受限目录).*仅服务器管理员可访问/s);
    assert.match(privacy, /运行服务不具备读取或清理该目录的权限/);
    assert.match(privacy, /处置完成后由 root 管理员.*单独清理/);
    assert.doesNotMatch(privacy, /部署(?:事务)?副本[^。]*最多保留 30 天/);
  }
});

test('公共行动本机勾选与家人行动账号保存保持清晰分离', () => {
  for (const file of [FILES.privacyDoc, FILES.agreementDoc, FILES.privacyPage, FILES.agreementPage]) {
    const text = read(file);
    assert.match(text, /公共[“\"]?今日行动[”\"]?.*(当前微信设备|当前设备|本机)/s, `${file} 缺少本机保存边界`);
    assert.match(text, /(一份.*当前都昌县日期|当前都昌县日期.*一份)/s, `${file} 缺少单一当前日期记录边界`);
    assert.match(text, /(新日期|再次打开|再次进入).*清理旧日期键/s, `${file} 缺少旧日期清理边界`);
    assert.match(text, /(指定家人|某位家人|家人).*行动确认.*保存到账号/s, `${file} 缺少账号保存边界`);
  }
});

test('正式微信首发未登录家庭行动入口在家庭解析前零读写', () => {
  for (const file of [FILES.privacyDoc, FILES.privacyPage]) {
    const text = read(file);
    assert.match(text, /1\.0\.0 首发不开放未登录家庭行动.*读取或写入/s, `${file} 缺少首发零读写边界`);
    assert.match(text, /不会读取短码.*兑换链接.*解析家庭资料.*行动确认.*求助.*复盘/s, `${file} 缺少家庭解析前停止说明`);
  }
  for (const file of [FILES.agreementDoc, FILES.agreementPage]) {
    const text = read(file);
    assert.match(text, /1\.0\.0 首发.*未登录家庭行动安全链接.*只显示停用说明.*不读取或写入家庭资料/s, `${file} 缺少首发停用边界`);
  }
  const webPage = read(FILES.webActionCheckinPage);
  assert.match(webPage, /微信正式版不会在网页读取短码或家庭安全链接/);
  assert.match(webPage, /不会读取短码、兑换安全链接或写入家庭记录/);
  assert.match(webPage, /web_actions_read_only/);
});

test('首发产品事件只来自完成单独同意的登录账号', () => {
  for (const file of [FILES.privacyDoc, FILES.privacyPage]) {
    const text = read(file);
    assert.match(text, /登录用户.*健康敏感个人信息单独同意.*固定枚举/s, `${file} 缺少登录事件边界`);
    assert.match(text, /首发未登录家庭行动页.*不产生.*提交事件/s, `${file} 缺少未登录零事件边界`);
    assert.match(text, /对应照护账号.*内部 ID.*不保存.*家庭成员.*配对标识/s, `${file} 缺少账号级匿名聚合边界`);
  }
});

test('复盘关闭家人关联后仍由账号持有且社区只展示数量', () => {
  const actionCheckin = read(FILES.actionCheckinPage);
  assert.match(actionCheckin, /关闭后不会显示在这位家人的今日记录中/);

  for (const file of [
    FILES.actionCheckinPage,
    FILES.webActionCheckinPage,
    FILES.privacyDoc,
    FILES.agreementDoc,
    FILES.privacyPage,
    FILES.agreementPage,
  ]) {
    const text = read(file);
    assert.match(text, /复盘.*账号.*保存.*账号注销时删除/s, `${file} 缺少账号生命周期说明`);
    assert.match(text, /无论是否关联.*当天.*社区.*提交数量/s, `${file} 缺少社区计数口径`);
    assert.match(text, /社区.*只.*展示.*数量.*不展示复盘原文/s, `${file} 缺少社区原文隐私边界`);
  }
});

test('求助入口明确只记录并要求直接联系照护人', () => {
  for (const file of [
    FILES.actionCheckinPage,
    FILES.webActionCheckinPage,
    FILES.privacyDoc,
    FILES.agreementDoc,
    FILES.privacyPage,
    FILES.agreementPage,
  ]) {
    const text = read(file);
    assert.match(
      text,
      /(?:求助[^。]*(?:只记录|仅保存)|(?:只记录|仅保存)[^。]*求助)[^。]*不会自动通知照护人.*直接联系(?:家人|照护人)/s,
      `${file} 缺少求助通知边界`,
    );
  }
});

test('隐私与协议统一 30 分钟新鲜度和首发第三方消息边界', () => {
  for (const file of [FILES.privacyDoc, FILES.agreementDoc, FILES.privacyPage, FILES.agreementPage]) {
    const text = read(file);
    assert.match(text, /30 分钟.*新鲜度/s, `${file} 缺少 30 分钟新鲜度`);
    assert.match(text, /30 分钟.*(不表示|不是)个人数据保存期限/s, `${file} 混淆天气新鲜度和个人数据保存期限`);
    assert.match(text, /当前版本不提供第三方消息推送/);
    assert.match(text, /不收集第三方推送接收标识/);
  }
});

test('首发材料排除第三方消息功能、共享数据和旧推送截图', () => {
  const publicFiles = [
    FILES.privacyDoc,
    FILES.agreementDoc,
    FILES.listing,
    FILES.privacyPage,
    FILES.agreementPage,
  ];
  for (const file of publicFiles) {
    const text = read(file);
    assert.match(text, /当前版本不提供第三方消息推送/, `${file} 缺少首发关闭说明`);
    assert.match(text, /不收集第三方推送接收标识/, `${file} 缺少接收标识排除说明`);
    assert.doesNotMatch(text, /WxPusher|WXPUSHER|第三方传输同意/, `${file} 仍将第三方推送写成可用功能`);
  }

  for (const file of [FILES.handoff, 'docs/miniprogram/RELEASE_CHECKLIST.md']) {
    const text = read(file);
    assert.match(text, /当前版本不提供第三方消息推送/, `${file} 缺少首发关闭门禁`);
    assert.match(text, /不收集第三方推送接收标识/, `${file} 缺少平台数据排除门禁`);
    assert.match(text, /WXPUSHER_APP_TOKEN.*为空/s, `${file} 缺少服务端空凭据门禁`);
  }

  const manifest = read(FILES.manifest);
  assert.match(manifest, /\| P02 \| `02_stale_snapshot_state\.png`/);
  assert.doesNotMatch(manifest, /wxpusher|WxPusher|02_wxpusher|单独同意/);
  assert.match(manifest, /截图清单不要求第三方推送同意、接收标识或投递证据/);
});

test('上架文案使用高温行动并完整披露候选包功能', () => {
  const listing = read(FILES.listing);
  assert.match(listing, /搜索关键词：[^\n]*`高温行动`/);
  assert.doesNotMatch(listing, /搜索关键词：[^\n]*`高温提醒`/);
  for (const feature of [
    '社区脆弱性',
    '避暑资源',
    '热暴露 GIS',
    '本机公共行动清单',
    '家人档案',
    '五项健康筛查',
    '200 字症状',
    '300 字求助',
    '500 字备注',
    '用药记录',
    '家人行动确认',
    '求助',
    '复盘',
  ]) {
    assert.match(listing, new RegExp(feature), `类目材料缺少 ${feature}`);
  }
  assert.match(listing, /禁止通过缩窄描述、隐藏功能/);
  assert.match(listing, /当前版本不提供第三方消息推送.*不得把第三方消息推送.*填写为首发功能/s);
});

test('发布材料精确锁定平台数据声明与第三方推送排除项', () => {
  const handoff = read(FILES.handoff);
  for (const field of ['wx.login', 'OpenID 哈希', '家人档案与健康字段', '固定枚举产品事件', '必要安全限流']) {
    assert.match(handoff, new RegExp(field.replace('.', '\\.')), `交接缺少平台声明 ${field}`);
  }
  for (const excluded of ['第三方推送接收标识', '个人定位', '昵称头像', '手机号', '订阅消息']) {
    assert.match(handoff, new RegExp(excluded), `交接缺少未调用声明 ${excluded}`);
  }
  assert.match(handoff, /不向第三方消息服务发送预警或用户数据/);
  assert.match(handoff, /平台隐私保护指引和审核材料均不声明对应第三方共享项/);
});

test('六份发布材料只允许处于完整候选态或完整正式态', () => {
  const files = [
    FILES.privacyDoc,
    FILES.agreementDoc,
    FILES.listing,
    FILES.privacyPage,
    FILES.agreementPage,
    FILES.healthConsentPage,
  ];
  const contents = files.map((file) => [file, read(file)]);
  const publicContents = contents.slice(0, -1);
  const healthConsentText = contents.at(-1)[1];
  const isCandidate = publicContents.every(([, text]) => text.includes('候选') && !text.includes('<!-- WECHAT_RELEASE_STATUS: final -->'))
    && !healthConsentText.includes('<!-- WECHAT_RELEASE_STATUS: final -->');
  const isFinal = contents.every(([, text]) => !text.includes('候选') && text.includes('<!-- WECHAT_RELEASE_STATUS: final -->'));

  assert.ok(isCandidate || isFinal, '六份发布材料出现候选态与正式态混用');
  for (const [file, text] of contents) {
    if (isCandidate) {
      assert.doesNotMatch(text, /<!-- WECHAT_EFFECTIVE_DATE:/, `${file} 不应提前冻结日期`);
    } else {
      assert.match(text, /<!-- WECHAT_MINIPROGRAM_NAME: 宜老天气通 -->/, `${file} 缺少正式名称 marker`);
    }
  }
});

test('候选文件保留冻结说明，正式文件具备完整发布 marker', () => {
  for (const file of [
    FILES.privacyDoc,
    FILES.agreementDoc,
    FILES.listing,
    FILES.privacyPage,
    FILES.agreementPage,
  ]) {
    const text = read(file);
    assert.doesNotMatch(text, /初稿/, `${file} 仍包含初稿标记`);
    if (text.includes('<!-- WECHAT_RELEASE_STATUS: final -->')) {
      assert.match(text, /<!-- WECHAT_MINIPROGRAM_NAME: 宜老天气通 -->/, `${file} 缺少正式名称 marker`);
      assert.doesNotMatch(text, /候选/, `${file} 正式态仍包含候选占位`);
      if (file !== FILES.listing) {
        assert.match(text, /<!-- WECHAT_EFFECTIVE_DATE: \d{4}-\d{2}-\d{2} -->/, `${file} 缺少正式日期 marker`);
      }
    } else {
      assert.match(text, /正式提交审核时.*冻结/s, `${file} 缺少正式冻结步骤`);
      assert.match(text, /生效日期/);
      assert.match(text, /commit hash/);
      assert.match(text, /内容 hash/);
    }
  }

  const validator = read(FILES.releaseValidator);
  assert.match(validator, /WECHAT_RELEASE_CANDIDATE_MARKER\s*=\s*["']候选["']/);
  assert.match(validator, /WECHAT_RELEASE_FINAL_STATUS_MARKER/);
  assert.match(validator, /WECHAT_RELEASE_STATUS: final/);
  assert.match(validator, /WECHAT_MINIPROGRAM_NAME_MARKER_FORMAT/);
  assert.match(validator, /WECHAT_MINIPROGRAM_NAME_MARKER_PATTERN/);
  assert.match(validator, /WECHAT_VISIBLE_EFFECTIVE_DATE_PATTERN/);
  assert.match(validator, /WECHAT_VISIBLE_PRIVACY_VERSION_PATTERN/);
  assert.match(validator, /候选占位/);

  const handoff = read(FILES.handoff);
  assert.match(handoff, /正式提交审核时.*冻结/s, '交接文档缺少正式冻结步骤');
  assert.match(handoff, /生效日期/);
  assert.match(handoff, /commit hash/);
  assert.match(handoff, /内容 hash/);
  const finalizeCommand = `python3 scripts/finalize_wechat_release.py finalize-content \\
  --wechat-form .env.wechat-release \\
  --repo-root .`;
  const recordCommand = `python3 scripts/finalize_wechat_release.py record-freeze \\
  --wechat-form .env.wechat-release \\
  --repo-root .`;
  assert.ok(handoff.includes(finalizeCommand), '交接文档缺少完整 finalize-content 命令');
  assert.ok(handoff.includes(recordCommand), '交接文档缺少完整 record-freeze 命令');
  assert.doesNotMatch(handoff, /WECHAT_MINIPROGRAM_NAME: 后台批准名称/);
});

test('审核截图清单登记设备证据并覆盖五项关键审核状态', () => {
  const manifest = read(FILES.manifest);
  for (const field of [
    '文件名',
    '系统与版本',
    '设备机型',
    '系统字号',
    '截图时间与时区',
    '目标 commit',
    '审核用途',
    '完成状态',
  ]) {
    assert.match(manifest, new RegExp(field), `截图清单缺少 ${field}`);
  }
  for (const evidence of [
    '隐私同意',
    '旧数据状态',
    '数据来源',
    '医疗边界',
  ]) {
    assert.match(manifest, new RegExp(evidence), `截图清单缺少 ${evidence}`);
  }
  assert.match(manifest, /待拍摄/);
  assert.match(manifest, /目标 commit hash/);
});
