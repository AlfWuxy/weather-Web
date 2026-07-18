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
  settingsPage: 'miniprogram/pages/settings/index.wxml',
  actionCheckinPage: 'miniprogram/pages/action-checkin/index.wxml',
  webActionCheckinPage: 'templates/action_checkin.html',
  careLogic: 'miniprogram/pages/elders/care-logic.js',
  mpApi: 'blueprints/mp_api.py',
  publicRoutes: 'blueprints/public.py',
  envExample: '.env.example',
  staticConfig: 'config.py',
  runtimeConfig: 'core/config.py',
  usageCore: 'core/usage.py',
  usageCleanup: 'services/pipelines/cleanup_usage_events.py',
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

test('未登录家庭行动安全入口只开放有限主动写入', () => {
  for (const file of [
    FILES.webActionCheckinPage,
    FILES.privacyDoc,
    FILES.agreementDoc,
    FILES.privacyPage,
    FILES.agreementPage,
  ]) {
    const text = read(file);
    assert.match(text, /未登录.*有时效家庭行动安全链接.*主动提交.*写入对应照护账号/s, `${file} 缺少未登录主动写入边界`);
    assert.match(text, /入口不等于登录.*不能查看账号内其他/s, `${file} 缺少安全入口权限边界`);
    assert.match(text, /(请勿转发给无关人员|不能查看账号内其他资料)/s, `${file} 缺少入口安全提示`);
  }
});

test('未登录安全入口的产品事件保持账号级匿名聚合', () => {
  for (const file of [FILES.privacyDoc, FILES.privacyPage]) {
    const text = read(file);
    assert.match(text, /未登录访问者.*有效家庭行动安全入口.*主动提交.*固定枚举/s, `${file} 缺少未登录入口事件口径`);
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

test('隐私与协议统一 30 分钟新鲜度和 WxPusher 单独同意字段', () => {
  for (const file of [FILES.privacyDoc, FILES.agreementDoc, FILES.privacyPage, FILES.agreementPage]) {
    const text = read(file);
    assert.match(text, /30 分钟.*新鲜度/s, `${file} 缺少 30 分钟新鲜度`);
    assert.match(text, /30 分钟.*(不表示|不是)个人数据保存期限/s, `${file} 混淆天气新鲜度和个人数据保存期限`);
    assert.match(text, /WxPusher/);
    assert.match(text, /单独勾选/);
    assert.match(text, /WxPusher UID/);
    assert.match(text, /都昌县级预警标题与正文/);
    assert.match(text, /7\s*天内有效的(?:随机持有者)?(?:点击)?链接/);
  }
});

test('WxPusher 文案与配置锁定七天点击期限和健康筛查排除', () => {
  const disclosureFiles = [
    FILES.privacyDoc,
    FILES.agreementDoc,
    FILES.handoff,
    FILES.manifest,
    FILES.privacyPage,
    FILES.agreementPage,
    FILES.settingsPage,
  ];
  for (const file of disclosureFiles) {
    const text = read(file);
    assert.match(text, /7\s*天(?:内有效的)?[^。\n]{0,12}链接/, `${file} 缺少七天链接期限`);
    assert.doesNotMatch(text, /一次性点击链接/, `${file} 仍宣称链接一次性`);
  }

  const settings = read(FILES.settingsPage);
  assert.match(settings, /不会发送家人姓名、健康筛查、健康日记、用药记录或家庭地址/);
  assert.match(settings, /打开或预览链接不会记为送达确认/);
  assert.match(settings, /必要的访问安全日志/);
  assert.match(settings, /页面无法核验实际点击者身份/);
  assert.match(settings, /持有链接的人主动点击“我已看到这条提醒”后.*首次记录一次送达确认/);
  assert.match(settings, /确认时间和自动确认标记满 30 天后由每日清理任务清空/);
  assert.match(settings, /防重复投递状态和人工复核记录保留至账号注销/);
  assert.match(settings, /说明版本和 UTC 同意时间在关闭推送后继续保留至账号注销/);
  assert.match(settings, /当前说明版本/);

  for (const file of [FILES.privacyDoc, FILES.privacyPage]) {
    const text = read(file);
    assert.match(text, /确认时间和.*自动确认标记.*满 30 天后.*清空/s, `${file} 缺少主动确认记录清理口径`);
    assert.match(text, /防重复投递状态和人工复核记录.*账号注销/s, `${file} 缺少投递记录生命周期`);
    assert.match(text, /同意.*版本.*UTC.*时间.*账号注销/s, `${file} 缺少同意回执生命周期`);
    assert.match(text, /消息.*状态.*7 天.*删除.*详情.*无法删除.*已经推送/s, `${file} 缺少服务商保存与删除边界`);
    assert.match(text, /隐私政策或数据处理条款.*缺少.*通道.*关闭/s, `${file} 缺少服务商政策 URL 门禁`);
  }

  assert.match(read(FILES.usageCore), /ALERT_DELIVERY_CLICK_RETENTION_DAYS\s*=\s*30/);
  assert.match(read(FILES.usageCleanup), /clear_expired_alert_delivery_clicks/);

  const envExample = read(FILES.envExample);
  const staticConfig = read(FILES.staticConfig);
  const runtimeConfig = read(FILES.runtimeConfig);
  const publicRoutes = read(FILES.publicRoutes);
  assert.match(envExample, /^PUSH_TRACKING_LINK_TTL_DAYS=7$/m);
  assert.match(staticConfig, /PUSH_TRACKING_LINK_TTL_DAYS_DEFAULT\s*=\s*7/);
  assert.match(staticConfig, /PUSH_TRACKING_LINK_TTL_DAYS_MAX\s*=\s*7/);
  assert.match(runtimeConfig, /app\.config\['PUSH_TRACKING_LINK_TTL_DAYS'\]/);
  assert.match(publicRoutes, /PUSH_TRACKING_LINK_TTL_DAYS/);
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
    'WxPusher',
  ]) {
    assert.match(listing, new RegExp(feature), `类目材料缺少 ${feature}`);
  }
  assert.match(listing, /禁止通过缩窄描述、隐藏功能/);
  assert.match(listing, /个人主体后台.*无法核验.*保持通道关闭.*停止全功能包提交/s);
});

test('发布材料精确锁定平台数据声明与 WxPusher 回执门禁', () => {
  const handoff = read(FILES.handoff);
  for (const field of ['wx.login', 'OpenID 哈希', '家人档案与健康字段', 'WxPusher UID', '固定枚举产品事件', '必要安全限流']) {
    assert.match(handoff, new RegExp(field.replace('.', '\\.')), `交接缺少平台声明 ${field}`);
  }
  for (const excluded of ['个人定位', '昵称头像', '手机号', '订阅消息']) {
    assert.match(handoff, new RegExp(excluded), `交接缺少未调用声明 ${excluded}`);
  }
  assert.match(handoff, /缺失、过期或无时间回执.*fail closed/s);
  assert.match(handoff, /服务商当前有效的隐私政策或数据处理条款 URL/);
});

test('法律与上架文件具备正式冻结说明且门禁拒绝候选占位', () => {
  for (const file of [
    FILES.privacyDoc,
    FILES.agreementDoc,
    FILES.listing,
    FILES.handoff,
    FILES.privacyPage,
    FILES.agreementPage,
  ]) {
    const text = read(file);
    assert.doesNotMatch(text, /初稿/, `${file} 仍包含初稿标记`);
    assert.match(text, /正式提交审核时.*冻结/s, `${file} 缺少正式冻结步骤`);
    assert.match(text, /生效日期/);
    assert.match(text, /commit hash/);
    assert.match(text, /内容 hash/);
  }

  const validator = read(FILES.releaseValidator);
  assert.match(validator, /WECHAT_RELEASE_CANDIDATE_MARKER\s*=\s*["']候选["']/);
  assert.match(validator, /WECHAT_RELEASE_FINAL_STATUS_MARKER/);
  assert.match(validator, /WECHAT_RELEASE_STATUS: final/);
  assert.match(validator, /WECHAT_MINIPROGRAM_NAME_MARKER_FORMAT/);
  assert.match(validator, /WECHAT_MINIPROGRAM_NAME_MARKER_PATTERN/);
  assert.match(read(FILES.handoff), /WECHAT_MINIPROGRAM_NAME: 后台批准名称/);
  assert.match(validator, /WECHAT_VISIBLE_EFFECTIVE_DATE_PATTERN/);
  assert.match(validator, /WECHAT_VISIBLE_PRIVACY_VERSION_PATTERN/);
  assert.match(validator, /候选占位/);
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
    'WxPusher 单独同意',
    '旧数据状态',
    '数据来源',
    '医疗边界',
  ]) {
    assert.match(manifest, new RegExp(evidence), `截图清单缺少 ${evidence}`);
  }
  assert.match(manifest, /待拍摄/);
  assert.match(manifest, /目标 commit hash/);
});
