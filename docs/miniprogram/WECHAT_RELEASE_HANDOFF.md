# 微信正式上架交接（发布候选版）：已选个人主体

本次注册路线已经确定为个人主体。后续操作始终保持“个人”主体类型，发布执行方负责账号设置、工程配置、测试、上传和审核资料，用户只处理必须由本人完成的官方验证与页面实际缴费。

微信平台官方小程序名称固定为“宜老平安”，小程序内的适老天气风险与行动提醒服务名称固定为“宜老天气通”。这组双名称分别承担平台身份与服务识别，现有功能页、分享标题和服务说明可以继续在服务语境中使用“宜老天气通”，不触发项目全局改名。所有平台资料与正式材料都要把两者的关系写清楚，避免把服务名误填为官方小程序名称。

## 先建立本机私密表单

在仓库根目录手动执行：

```bash
# 从可提交模板创建本机私密表单。
cp .env.wechat-release.example .env.wechat-release
# 只允许当前用户读写。
chmod 600 .env.wechat-release
# 确认真实表单已被 Git 忽略。
git check-ignore .env.wechat-release
```

`.env.wechat-release.example` 只保留字段和安全默认值，真实资料只写入 `.env.wechat-release`。若最后一条命令没有输出 `.env.wechat-release`，立即停止填写并检查 `.gitignore`。不得把真实 AppID、AppSecret、运营者资料或官方后台截图复制回模板、普通 `.env`、提交记录或公开聊天。

受版本控制的 `project.config.json` 固定使用 `touristappid`。微信开发者工具会把该公开配置与根目录 `project.private.config.json` 合并；本机私有配置在保留开发者工具偏好的同时配置正式 AppID，并保持权限 `0600`，`git check-ignore project.private.config.json` 必须命中。AppSecret 不参与开发者工具工程合并，绝不写入 `project.private.config.json`。

## 个人主体材料边界

个人主体无需提供以下材料：

- 营业执照
- 统一社会信用代码
- 法人身份资料
- 法人授权或企业管理员资料

如果官方页面出现上述企业材料字段，先停止填写并检查主体类型是否仍为“个人”。禁止编造信息或临时切换到其他主体。

“职业信息”属于选填展示项，本次选择“无”或直接跳过。科学科普、公益、健康医疗和 IT 通信等职业身份只有在后台要求的任职、资质或机构材料能够真实核验时再申请，当前不纳入上架门禁，也不影响天气类目和功能审核。徐医生未来可以在真实参与的前提下担任内容审阅者，并保留审阅范围、时间和修改记录；该角色不改变个人主体，也不能替代平台要求的机构资质或被用于借用身份。

## 用户只需亲自完成

用户操作以 `docs/miniprogram/PERSONAL_SUBJECT_ACTION_SHEET.md` 为唯一入口。本工程手册只保留发布边界，避免两份用户指令发生漂移。以下操作只在微信官方页面内完成：

1. 按页面提示完成本人实名认证。
2. 页面要求时，由本人完成刷脸验证。
3. 页面要求时，由本人接收并填写验证码。
4. 页面实际显示缴费项目时，由本人核对页面显示的项目和金额后决定并完成支付。
5. 页面生成或重置 AppSecret、上传、提交审核或发布时若要求管理员核验，由本人完成扫码、人脸、短信或微信确认。正式 AppID 已由发布执行方保存；AppSecret、运营者姓名和联系邮箱由发布执行方现场读取官方页面，或由本人粘贴到当前电脑的无回显输入，用户无需编辑任何本机配置。OpenID pepper 和会话密钥由服务器自动生成，无需用户填写。

本文档不预设费用。页面没有显示缴费项目时，无需主动寻找付款入口。身份证号码、人脸信息、验证码、银行卡信息和付款信息均留在官方页面内。

## 发布执行方负责

- 引导注册页面保持个人主体，并核对账号状态。
- 以“宜老平安”填写微信平台名称，并在简介、隐私说明、版本说明和审核路径中明确小程序内提供“宜老天气通”服务；同步完成头像和其他上架资料。
- 依据后台当时实际可选范围和产品现有功能填写服务类目；遇到额外资质要求时暂停，不猜测、不代填。
- 配置正式工程、合法域名和服务器环境。当前小程序只使用 `wx.request`，后台只填写 request 合法域名 `https://yilaoweather.org`；socket、uploadFile、downloadFile、UDP、TCP、DNS 预解析和预连接字段保持空白。
- 完成编译、离线回归、真机检查、截图、上传和审核资料整理。
- 在既有授权范围内推进提交；出现新的对外发布确认时暂停并请求用户决定。

## 个人主体类目证据与门禁

类目以发布当天微信公众平台正式小程序后台实际显示为准，仓库不预填可能变化的类目名称。发布执行方按以下顺序处理：

1. 在官方后台确认主体类型仍为“个人”，并确认账号状态允许提交审核。
2. 从个人主体当时可选的正式类目中，选择能够覆盖候选包完整真实功能的全部必要类目。核对范围必须包含公开天气、七日预报、官方预警、社区脆弱性、避暑资源、热暴露 GIS、本机公共行动清单，以及登录后的家人档案、五项健康筛查、200 字症状与 500 字备注健康日记、用药记录、家人行动确认、求助和复盘。当前版本不提供第三方消息推送，也不收集第三方推送接收标识，不得把第三方消息推送、第三方推送接收标识或对应共享项写入首发类目与平台声明。禁止为了使用更窄类目而省略、改名或隐藏已存在的功能。页面出现额外许可证、机构证明或企业材料时停止，保持 `WECHAT_CATEGORY_CONFIRMED=0`。
3. 保存一组发布证据截图，至少能看清“主体类型：个人”、实际选中类目、页面显示的资质要求或无需额外资质状态，以及带时区的确认时间。账号标识、证件、联系方式和其他敏感字段先遮盖。截图只放本机私有发布资料或私有 ops，不进入仓库。
4. 在 `.env.wechat-release` 中完整填写类目路径、资质状态、仓库外证据根目录、相对引用、证据摘要和确认时间。人工复核截图与最终提交页一致后仍保持 `WECHAT_CATEGORY_CONFIRMED=0`，先完成公开材料冻结、测试、提交与摘要记录。最终校验前再次确认截图未超过 24 小时且提交页未变化，才把该门禁改为 `1`。类目或主体状态之后发生变化时立即改回 `0` 并重新取证。

结构化类目证据使用以下六个字段：

- `WECHAT_CATEGORY_PATHS_JSON`：JSON 字符串数组，逐条抄录后台全部必要类目的完整路径，例如 `["一级类目/二级类目"]`；一个类目无法覆盖完整功能时记录全部实际选中路径。
- `WECHAT_CATEGORY_QUALIFICATION_STATUS`：个人主体首发只允许填写固定状态 `no_extra_institutional_qualification`。后台显示需要许可证、机构证明、企业材料或其他机构资质时停止发布，保持两个门禁为 `0`。
- `WECHAT_CATEGORY_EVIDENCE_ROOT`：仓库外私有证据目录的绝对路径。根目录和相对引用经过的中间目录只能由当前用户访问，禁止使用符号链接。
- `WECHAT_CATEGORY_EVIDENCE_REF`：证据根目录内的非敏感相对文件引用，例如 `wechat-category/2026-07-18/category.png`。引用不得包含姓名、账号标识、绝对路径、反斜杠或 `..` 目录跳转；最终文件必须留在证据根目录内，且不得进入仓库。
- `WECHAT_CATEGORY_EVIDENCE_SHA256`：对证据文件逐字节计算的 64 位小写 SHA-256。正式门禁要求文件为非空普通文件、权限为 `0600`、大小不超过 20 MiB，且摘要完全一致。
- `WECHAT_CATEGORY_CONFIRMED_AT`：带显式时区的 ISO 8601 时间，例如 `2026-07-18T15:30:00+08:00`。无时区、晚于校验时刻或已经超过 24 小时的记录不能作为正式发布证据。超过 24 小时后必须重新截图，并再次核对主体、类目和资质状态。

机器校验确认文件存在、路径边界、权限、大小和摘要。截图是否真实显示个人主体、完整类目和无需额外资质仍由发布执行方人工核对。错误不回显证据根目录、相对引用、确认时间、文件内容或任何凭据值。

`WECHAT_FORM_READY` 是最后一道完整性门禁。只有以下项目全部完成后才能改为 `1`：

- `WECHAT_SUBJECT_TYPE=personal`
- `WECHAT_MINIPROGRAM_NAME=宜老平安`，与官方后台小程序名称逐字一致
- `WECHAT_SERVICE_NAME=宜老天气通`，与小程序内适老天气风险与行动提醒服务名称逐字一致
- 运营者姓名、专用联系邮箱、生效日期已经核对
- 正式 AppID、AppSecret 已从当前账号后台取得
- `WECHAT_APPSECRET_PRODUCTION_SAFE_CONFIRMED=1`；AppSecret 若曾进入聊天、日志、截图或其他外部系统，已先在微信后台重置并通过本机无回显输入重新录入
- `WECHAT_FORMAL_RUNTIME=1` 且 `DEBUG=false`；正式服务器的 `WX_MINIPROGRAM_APPID`、`WX_MINIPROGRAM_SECRET`、`WX_MINIPROGRAM_OPENID_PEPPER` 与 `WX_MINIPROGRAM_SESSION_SECRET` 四项完整。`WECHAT_FORMAL_RUNTIME=0` 只用于 Web-only 运行态，并要求清空上述四项微信服务端配置
- `WX_MINIPROGRAM_PRIVACY_VERSION` 与本次提交的隐私说明版本一致，生效日期、目标 commit hash 和页面内容 hash 已同时冻结
- `FEATURE_AUDIT_LOGS=0`，首发不持久化应用数据库安全审计日志；`FEATURE_STRUCTURED_LOGS=1`，保留经过正式运行态全局白名单处理的排障日志
- `SENTRY_DSN` 为空、`SENTRY_TRACES_SAMPLE_RATE=0`、`SENTRY_SEND_PII=0`；正式微信运行态不接入第三方错误监控或性能追踪服务
- 六项结构化类目证据均已填写且通过文件完整性校验；资质状态明确为无需额外机构资质
- `WECHAT_CATEGORY_CONFIRMED=1`，且非敏感证据引用可回溯到私有截图

正式部署同时设置 `DEPLOY_REQUIRE_WECHAT_READY=1`。校验会要求私密表单为普通文件、权限为 `0600`、`WECHAT_FORM_READY` 与 `WECHAT_CATEGORY_CONFIRMED` 均为 `1`、生产 AppSecret 安全确认为 `1`，并检查必填字段格式、当前 Git 工作树、目标提交和六份发布材料。任一项不满足时只允许在本地微信开发者工具中预览，不得标记为正式可上架版本。远程发布脚本仅接受 `DEPLOY_REQUIRE_WECHAT_READY=1`，其他值会在 SSH、上传和服务器变更前终止。

`WECHAT_FORM_READY=0` 时的表单校验只用于本地预览，不会读取 AppID 或 AppSecret，也不会生成微信 OpenID pepper 或会话密钥。只有同一次 `0600` 验证快照同时满足 `DEPLOY_REQUIRE_WECHAT_READY=1` 与 `WECHAT_FORM_READY=1`，发布脚本才允许下发这些正式配置。

## 最终冻结的机器校验

本分支候选版本固定为 `1.1.0`。开启 `WECHAT_FORM_READY=1` 前，按以下顺序完成人工复核和机器冻结：

正式提交审核时，必须先冻结生效日期、首发版本、目标 commit 与六份材料摘要，再进入部署和上传步骤。

1. 在私密表单分别填写 `WECHAT_MINIPROGRAM_NAME=宜老平安`、`WECHAT_SERVICE_NAME=宜老天气通`、正式生效日期、重新核对后的隐私版本和候选版本 `1.1.0`，继续保持 `WECHAT_FORM_READY=0` 与 `WECHAT_CATEGORY_CONFIRMED=0`。冻结工具会分别校验平台名和服务名；任一名称不一致时安全停止，先核对双名称关系、重新编译和真机验收。执行下一步前确认 `git status --short --untracked-files=all` 没有输出；已被 `.gitignore` 忽略的本机私密表单和私有配置可以保留，任何 tracked 修改、暂存修改或未忽略的 untracked 文件都会阻断冻结工具。
2. 执行 `finalize-content`。工具只使用平台名、服务名、生效日期、隐私版本和首发版本生成公开内容，并额外读取两个门禁确认它们都保持为 `0`；绝不把运营者姓名、邮箱、AppID、AppSecret、类目证据或 QWeather 字段写入受版本控制文件。它会先用内置 SHA-256 清单确认七份 `HEAD` 候选 blob 与人工复核基线完全一致，再确定性更新六份材料与 `miniprogram/config.js`，清除全部候选文字。六份正式材料各生成唯一状态 marker、平台名 marker 和服务名 marker，可见正文同时说明“宜老平安”与“宜老天气通”的关系；加上 5 个生效日期 marker 和 3 个隐私版本 marker，正式 marker 总数固定为 26 个。工具使用单维护者协作锁，临时文件与目标文件位于同一目录并以原子替换落盘；中断后只接受每份文件处于可信候选态或本次精确正式输出态的已知部分状态，重跑时继续向完整正式态收敛。未知字节、暂存修改、候选 SHA 漂移或无关仓库变化都会安全停止，工具不会创建包含发布表单或密钥的事务 journal。

```bash
python3 scripts/finalize_wechat_release.py finalize-content \
  --wechat-form .env.wechat-release \
  --repo-root .
```

3. 人工复核 `git diff`，运行全量测试、开发者工具和真机验收，再提交最终代码与六份材料。双名称会改变三份小程序 WXML 正式材料，因此先前上传包不再对应当前材料；本轮必须重新编译并准备重新上传。确认 `git status --short --untracked-files=all` 没有输出。已被 `.gitignore` 忽略的本机私密表单和私有配置可以保留；任何 tracked 修改、暂存修改或未忽略的 untracked 文件都会阻断正式门禁。
4. 在干净最终提交上重新执行 `record-freeze`。工具从 Git HEAD blob 计算六项 SHA-256，只更新私密表单中的首发版本、40 位目标提交和六项摘要，保持权限 `0600`，不会开启 `WECHAT_FORM_READY` 或 `WECHAT_CATEGORY_CONFIRMED`。每次双名称正文、marker 或其他冻结材料发生字节变化，都要再次提交并重新执行本步骤。

```bash
python3 scripts/finalize_wechat_release.py record-freeze \
  --wechat-form .env.wechat-release \
  --repo-root .
```

以下命令只用于人工复核工具结果。换行、空格或单个字节发生变化都需要重新提交并再次执行 `record-freeze`。

```bash
shasum -a 256 docs/miniprogram/PRIVACY_NOTICE_TEMPLATE.md
shasum -a 256 docs/miniprogram/USER_AGREEMENT_TEMPLATE.md
shasum -a 256 docs/miniprogram/LISTING_COPY.md
shasum -a 256 miniprogram/pages/privacy/index.wxml
shasum -a 256 miniprogram/pages/agreement/index.wxml
shasum -a 256 miniprogram/pages/health-consent/index.wxml
```

5. 再次核对类目证据仍处于 24 小时有效期内、截图与最终提交页一致且无需额外机构资质。运营者私密资料、已通过安全捕获或必要轮换的 AppSecret 与 QWeather 一次性控制台字段均完成后，依次设置 `WECHAT_CATEGORY_CONFIRMED=1`、`WECHAT_APPSECRET_PRODUCTION_SAFE_CONFIRMED=1`，最后设置 `WECHAT_FORM_READY=1`，并在仓库根目录执行最终校验：

```bash
python3 scripts/validate_release_env.py \
  --wechat-form .env.wechat-release \
  --form-only \
  --require-wechat 1 \
  --repo-root .
```

校验器会确认生产 AppSecret 安全确认为 `1`、工作树干净、HEAD 与 `WECHAT_TARGET_COMMIT_SHA` 完全一致，并从该 HEAD 读取六个 Git blob 逐字节核对 SHA-256。六份材料的唯一平台名 marker 与可见平台名必须逐字等于 `WECHAT_MINIPROGRAM_NAME`，唯一服务名 marker 与可见服务名必须逐字等于 `WECHAT_SERVICE_NAME`；正式材料的 26 个 marker 必须完整且唯一。同时要求受版本控制的 `project.config.json` 使用 `touristappid`，被 Git 忽略且权限为 `0600` 的根目录 `project.private.config.json` 的 AppID 字段与表单一致且文件不含 AppSecret；`miniprogram/config.js` 的唯一隐私同意版本精确等于表单隐私版本，`miniprogram/config.runtime.js` 的唯一 `API_BASE_URL` 是无路径、查询和片段的 HTTPS origin，并精确等于 `WECHAT_REQUEST_DOMAIN=https://yilaoweather.org`。首发完整功能要求 `FEATURE_HEAT_EXPOSURE_GIS=1`，同时要求 `WXPUSHER_APP_TOKEN` 为空、`FEATURE_WXPUSHER=0`、`FEATURE_AUDIT_LOGS=0`。这意味着第三方消息发送与应用数据库安全审计持久化均关闭；必要限流仍只在限制窗口内处理不可逆 IP 哈希键。正式服务器还要求 `FEATURE_WEB_AI=0`、空的 `SILICONFLOW_API_KEY` 与官方 API Base。类目证据时间必须处于校验时刻之前且不超过 24 小时。错误只返回固定字段名和状态，不回显字段原值、待提交文件名或本机绝对路径。`WECHAT_FORM_READY=0` 的本地预览校验跳过 Git、材料和证据时效门禁，生产 AppSecret 安全确认仍保持关闭并给出固定预览警告。

## 已准备的发布基础

- 微信平台官方小程序名称：`宜老平安`
- 小程序内服务名称：`宜老天气通`
- 名称使用规则：平台账号、审核资料中的“小程序名称”填写“宜老平安”；功能页、分享标题和服务说明可使用“宜老天气通”，同时在隐私、协议、健康同意与上架资料中展示两者关系。该映射不要求全局替换服务品牌。
- 服务范围：江西省九江市都昌县
- request 合法域名：`https://yilaoweather.org`
- 候选版本：`1.1.0`
- 产品介绍、版本说明、隐私模板、测试计划和上架截图清单
- 30 分钟服务端同步、客户端缓存、预算上限和失败保留旧快照策略
- 微信快捷登录、不同 OpenID 私有资料隔离、健康敏感信息单独同意与撤回、账户删除和隐私版本机制
- 逐次确认的单次位置、端内距离排序、社区手选回退与资源点原生地图查看

## 正式资料一致性

- 正式 AppID 需要在微信公众平台、开发者工具合并后的本机工程、`.env.wechat-release` 和服务器环境四处一致。受版本控制的工程只保留 `touristappid`；AppSecret 只进入 `.env.wechat-release` 和受控服务器环境，绝不进入 `project.private.config.json`。
- `WECHAT_REQUEST_DOMAIN` 固定记录微信后台 request 合法域名 `https://yilaoweather.org`，必须与冻结 HEAD 的公开 `API_BASE_URL` 完全一致。
- `WX_MINIPROGRAM_PRIVACY_VERSION` 需要与小程序包内显示的隐私版本、服务器要求版本和平台隐私保护指引本次生效内容一致。隐私内容更新时先递增版本，再重新取得主动同意。
- 登录的一般隐私同意与健康敏感个人信息单独同意分开记录。首次进入家人档案、筛查、日记、用药或家庭行动前必须显示默认不勾选的独立说明；服务器保存独立版本和 UTC 时间，拒绝、撤回或版本过期时私密路由返回 428，公开天气继续可用。
- 正式微信运行态的 Web 家庭行动安全链接在 1.1.0 只显示停用说明；短码读取、链接兑换、家庭解析、天气构建以及确认行动、求助和复盘写入都会在触碰家庭资料前停止。查看和保存家庭行动只允许在小程序登录并完成健康敏感个人信息单独同意后执行。
- 家人档案只接受 18 至 120 岁成年人，用户在单独同意页确认已取得对方同意或具备其他合法管理权限。账号使用者年龄不采集、不推断。撤回入口位于“我的 → 健康资料授权管理”；撤回只清回执并关闭私密功能，已有资料继续按逐条删除或账号注销路径处理。
- `WECHAT_OPERATOR_NAME` 填认证账号对应的实际运营者姓名，`WECHAT_CONTACT_EMAIL` 使用可持续接收审核通知的专用邮箱，`WECHAT_EFFECTIVE_DATE` 使用 `YYYY-MM-DD`。
- 平台隐私保护指引使用认证运营者姓名、专用联系邮箱和生效日期。运营者姓名、证件及其他认证资料只保存在微信平台、权限为 `0600` 的忽略表单和仓库外私有 ops；经运营者批准的专用支持邮箱可在 Web 透明度页和微信平台公开联系入口展示。受版本控制的隐私说明、用户协议、上架文案和小程序页面同时使用批准的平台名“宜老平安”和服务名“宜老天气通”，并通过微信“反馈与投诉”提供联系入口，禁止写入运营者姓名或其他认证隐私材料。
- 平台数据类型按代码实际处理逐项声明：用户点击后由 `wx.setClipboardData` 写入提醒或公共纳凉点信息（只写不读，复制内容不上传）；`wx.login` 产生的 OpenID 哈希、账号与会话信息；18 至 120 岁成年家人档案与健康字段；老人码、短码明文及不可逆哈希等家庭照护关系技术标识；独立健康同意版本和 UTC 时间；固定枚举产品事件；受限结构化运行日志；必要安全限流中的临时 IP 哈希键；以及用户每次主动确认后由 `wx.getLocation` 读取的一次位置。用户坐标只在避暑资源页面实例内按 GCJ-02 资源点计算直线距离，不发送到项目服务器，不进入持久存储、产品事件、日志、分享参数或公共缓存，页面隐藏或离开后清除，拒绝或失败后仍可手选社区。结构化请求日志只记录服务端随机请求编号、方法、经过凭据替换的路径、接口、状态和耗时，其他正式应用日志只保留事件类型、模块、级别、函数和代码行；全局过滤器会丢弃原始消息参数、异常正文和 traceback。运行日志不记录请求或响应正文、查询参数、IP、User-Agent、请求头、SQL 参数、会话 token 或用户坐标。正式站点的 Nginx server 块必须同时声明 `access_log off` 和 `error_log /dev/null crit`，部署器会在候选激活前安全停止任何不一致配置。应用日志进入 systemd journal；2026-07-21 核验时 systemd 252 没有 journald 显式容量或时间覆盖，`MaxRetentionSec=0`，所以 systemd journal 当前没有固定保存天数，按磁盘容量边界和日志量自动轮转覆盖。`MaxFileSec` 默认 1 个月只控制单个文件轮转；`SystemMaxUse` 默认文件系统 10% 且上限 4 GiB，`SystemKeepFree` 默认 15% 且上限 4 GiB，核验时实际占用约 2.9 GiB。只有归档文件会在容量压力下删除。1.1.0 固定 `FEATURE_AUDIT_LOGS=0`，不把 IP 哈希或 User-Agent 写入数据库应用审计表。当前版本不收集第三方推送接收标识、昵称头像、手机号或订阅消息，不得误勾这些数据类型。完整证据边界见 [运行日志边界](./RUNTIME_LOG_BOUNDARY.md)。
- 正式微信运行态不接入第三方错误监控或性能追踪服务。正式表单和候选服务器必须保持 `SENTRY_DSN` 为空，并显式固定 `SENTRY_TRACES_SAMPLE_RATE=0`、`SENTRY_SEND_PII=0`；任一项不一致时发布校验和应用启动都会停止，应用日志、异常上下文、请求信息或用户资料不会发送到第三方监控服务。
- 当前版本不提供第三方消息推送，不配置第三方消息服务凭据，不向第三方消息服务发送预警或用户数据，平台隐私保护指引和审核材料均不声明对应第三方共享项。未来若新增该能力，需要先形成新的隐私候选版本、重新完成类目和平台数据声明核对，再取得用户主动同意。
- 运行 `finalize-content` 前，仓库文本应处于“发布候选版”整理阶段；运行后，六份材料必须全部带平台名与服务名双 marker、可见双名称关系和其余正式 marker，合计 26 个 marker，且不再含候选文字。随后冻结 `WECHAT_EFFECTIVE_DATE`、小程序版本、`WX_MINIPROGRAM_PRIVACY_VERSION`、目标 commit hash，以及隐私说明、用户协议、上架文案、隐私页、协议页与健康资料单独同意页的内容 hash；任何一项变更都要把 `WECHAT_FORM_READY` 与 `WECHAT_CATEGORY_CONFIRMED` 恢复为 `0`，重新运行 `record-freeze`、重新编译上传并重新取证。

## 首发人工验收

### 健康敏感信息单独同意

1. 完成一般隐私同意与登录后首次进入任一私密健康页面，确认独立复选框默认不勾选，页面完整展示用途、字段、18 至 120 岁成年家人边界、拒绝影响、保存与删除方式。
2. 未勾选时确认请求数为 0；勾选后确认六个私密页面只加载当前账号资料，服务端回执版本与 UTC 时间可核对。
3. 在请求途中切到后台再返回，健康同意页和五个私密写页面均应恢复可操作状态并重新读取服务端权威结果，不得永久 loading/busy 或重复提交。
4. 在“我的 → 健康资料授权管理”撤回，确认登录会话保留、私密页面立即清空、11 组私密路由重新返回 428，公开天气仍可使用，账号注销仍可执行。
5. 验证年龄边界：空值、17、121、`1200` 和 `120.0` 拒绝，18 与 120 接受；历史不完整档案只能补正或停止管理，补正前不能新增健康或行动记录。

### 30 分钟刷新

1. 确认 bootstrap timer 在部署或开机后完整等待 30 分钟并直接触发缓存服务；首次同步无论成功或失败都通过缓存服务的 `OnSuccess`/`OnFailure` 启动 recurring cache timer，客户端公共快照缓存也为 30 分钟。
2. 确认正式服务启动前已设置 30 分钟 QWeather 网络闸门；窗口内任何意外入口都会在预算计数前被阻断，过期后自动恢复。
3. 确认正式环境固定 `QWEATHER_REQUIRE_PERSISTENT_BUDGET=1`，QWeather 已使用 Redis 持久化预算；候选发布在停止生产服务前已对该 Redis 完成短超时 PING。
4. 确认 Redis 已启用 AOF，`appendfsync` 为 `everysec` 或 `always`，持久化探测没有权限错误、加载状态或最近写入错误。
5. 为本项目使用独立 QWeather 凭据，并在正式发布当天从控制台抄录一次北京时间当月已用量。月份和整数基线写入私密发布表单，发布脚本只会把 Redis 计数抬高到该基线。
6. 确认基线加上“距北京时间下月起点的剩余 30 分钟周期数 × 3”及最多 3 次正式烟测仍不超过月上限。
7. 正式 AppID、AppSecret 与天气认证全部就绪后，由发布执行方完成唯一一次受控真实联调，并记录调用前后预算计数、快照时间和数据来源。
8. 连续打开首页、预报、预警和社区页面，确认普通访问、风险预计算与 `/healthz` 不会触发额外上游天气请求。网络异常时保留旧快照并明确显示 stale。
9. 确认 timer、手工普通同步与发布烟测在任何上游访问前共用 `$STATE_DIR/run/case-weather-sync.lock` 非阻塞 flock；普通周期还必须取得 Redis `SET NX EX 1800` 租约。锁被占用或 Redis 异常时在访问上游前退出。
10. 确认每次 QWeather 额度预占由单个 Redis Lua 脚本原子完成月总量检查、总量递增、endpoint 递增与两类键的 TTL 设置；脚本返回异常或执行结果未知时 fail-closed，正式环境不得回退到进程内计数。
11. 确认实况、七日预报和成功取得的预警状态分别保存真实 `fetched_at` 与 `expires_at`；快照的抓取时间和过期时间取全部必要来源中最保守的值，旧缓存不得被重写成新鲜数据。

### 分享与换账号

1. 用账号 A 点击文案明确的“分享给家人”按钮，把公开页分享给账号 B。普通右上角分享和朋友圈分享不得携带 `family_share`。
2. 账号 B 打开卡片，确认路径只含固定 `from=family_share`，页面和分享卡片均不含老人姓名、账号标识、健康信息、位置或设备标识。
3. 账号 B 完成登录后，确认家庭来源只消费一次。退出再登录或换成账号 C 时，不得继承 B 的来源归因。
4. 在共享设备上先退出账号 A，即使网络异常也应清理本机会话；再登录账号 B，确认看不到 A 的老人、用药、日记或账号设置。

### 匿名分析与第三方服务边界

- 公开浏览只看微信公众平台聚合统计，不安装第三方统计 SDK。自有分析只收固定枚举事件和最小账号级维度，原始事件保留 30 天，CSV 只导出聚合计数。
- D7、D15 和家庭分享效果只按成熟队列查看聚合人数与比例。内部测试账号写入服务端 `ANALYTICS_TEST_USER_IDS` 并从看板和 CSV 排除，小样本不对外发布细分结论。
- 管理看板的地区聚合使用社区编码，`ANALYTICS_MIN_LOCATION_COUNT=3` 表示至少 3 个家庭才显示该地区；生产环境会把更小配置强制提升到 3。
- 首发包不展示第三方消息推送设置，不接收第三方推送接收标识，不启动第三方消息投递或人工复核流程。发布验收确认相关前端入口不可达、服务端写入被拒绝、第三方消息服务调用数为 0。

## 验证完成后的执行顺序

1. 发布执行方确认账号已完成个人主体注册，检查时遮盖所有敏感字段。
2. 首次发布前人工核对服务器指纹并登记到本机 `known_hosts`；随后在受控服务器环境完成正式账号配置，并保持凭据不进入仓库。本机私密部署配置填写仓库外的绝对路径 `QWEATHER_JWT_PRIVATE_KEY_SOURCE`，对应文件必须是普通非符号链接文件、可读、权限精确为 `0600`，并能通过 Ed25519 离线校验；服务器路径 `QWEATHER_JWT_PRIVATE_KEY_PATH` 固定为 `DEPLOY_PROJECT_DIR/private/` 下的直接文件，项目目录与目标路径禁止重复斜杠、点路径段和尾斜杠。
3. 将 `DEPLOY_REQUIRE_WECHAT_READY=1`，通过不可变 release 流程部署小程序后端；发布脚本会先创建本轮唯一的本机 `0700` 临时目录，在任何 SSH、rsync 或远端变更前把原表单安全复制为单次 `0600` 临时快照，校验器和 loader 全程只读该快照，再依据同一次校验生成的 commit 票据导出代码快照。QWeather 私钥源只通过一次带 `O_RDONLY|O_NOFOLLOW|O_CLOEXEC` 的文件描述符打开，在同一描述符上确认普通文件、`0600` 权限、非空合理大小与读中稳定性；部署器再以 `O_CREAT|O_EXCL` 创建 `0600` 临时快照并从该描述符复制，OpenSSL 只对快照执行 Ed25519 离线校验。后续 SSH 文件 stdin 也只读取这份快照。远端先确认不可变 release 不存在，完成代码上传、候选 `staged.env` 建立及全部候选配置写入，再把私钥快照原子安装为本轮唯一的 `.qweather-jwt.pending-<release-id>`；该 pending 文件在全部候选校验和测试期间保持 `root:root 0600`，不会提前创建新的 runtime-group 可读 final 文件。激活事务耐久记录转换计划并安装 boot guard，确认全部受管单元和运行账号进程静默后，才以 no-clobber 方式创建或精确复用 `QWEATHER_JWT_PRIVATE_KEY_PATH`。create 路径先在 `0600` 状态下建立 final 硬链接、移除并 fsync pending 名称，随后把唯一 final 提升为 `root:case-weather 0640`；reuse 路径只接受内容、inode、属主和权限全部匹配的既有 final。任何回滚都先把本轮新 final 与 pending 收回到 root-only 事务归档，再允许旧单元恢复；身份不明、回收或 fsync 失败时保持业务单元停止并保留 guard，等待人工核对。新文件名轮换使用新的版本化 final 路径，旧 key 的吊销和退役作为另一次 root-only 运维操作。冻结 commit 同时写入 release 的 `private-metadata/source-commit.txt` 并由激活事务再次核对。原表单或私钥源中途改变都不会影响已经生成的本轮快照。退出时本机临时目录会被静默清理，工作目录中的忽略文件不会进入正式发布包；远端继续排除所有 `.env*` 与 `project.private.config.json`。私钥内容与本机路径不进入输出、Git 或服务器 `.env`。随后执行 `alembic upgrade head` 并强制核对数据库版本等于唯一 head。
4. 验证 `https://yilaoweather.org/mp/api/v1/bootstrap`，再配置 request 合法域名。
5. 在微信开发者工具选择正式账号、导入工程，确认工具已把公开 `touristappid` 配置与本机 `project.private.config.json` 的正式 AppID 合并后再编译。
6. 核对激活事务内的唯一一次受控真实天气同步和预算计数。外置 receipt 绑定冻结 commit 与天气语义配置指纹；`case-weather` 运行用户先完成 JWT 离线签名并读取 Redis 预算前值。两项通过后，root 生成随机 lease token，并在写入 `started` 前取得全局 Redis `SET NX EX 1800` 周期租约；租约忙或 Redis 异常时安全退出，不形成不可重试 receipt。`started` 耐久落盘后，root 才签发权限为 `0640`、绑定 commit、天气配置指纹与 lease token 摘要的一次性 ticket。同步进程以常量时间比较确认自己持有预占租约，再校验 binding、token SHA-256 和 lease token SHA-256，通过独立 Redis `SET NX` 消费标记后删除磁盘 ticket，之后才允许访问上游；任一步失败都在访问上游前退出，消费后无论成功或失败均禁止自动重试。成功后写入 `completed`、snapshot_id 与预算差值，并要求 `weather_now`、`weather_7d_forecast`、`weatheralert_v1_current` 三项各增加 1 次，总增量固定为 3。天气指纹只包含 QWeather 认证模式、凭据、API Base、canonical location、预算门禁、缓存 TTL、同步位置和天气不可用策略。AppID、AppSecret、隐私版本、GIS 开关、公开域名和动态网络闸门时间均不参与指纹，轮换这些字段不能获得第二次自动烟测机会。正式烟测传入 `--skip-nowcast`，关闭 Open-Meteo 与 mock 兜底，绕过实况、七日预报和预警三项内部新鲜缓存，并分别预占一次预算。30 分钟常规周期继续维护短时 nowcast。只有实况、七日预报和预警状态都来自 QWeather 官方源且快照新鲜可用时才通过；任一来源失败的周期可保存 degraded 状态供页面透明显示，但不会触发下游预警派发。相同绑定再次执行时只允许复用仍然新鲜的 completed 快照；started 未完成、completed 快照丢失或过期时立即关闭，必须人工核对，禁止自动再次请求。
   该烟测和候选 Gunicorn 统一以无登录权限的 `case-weather` 用户运行，候选进程仅获得 `env -i` 白名单中的发布环境。五个业务运行服务只允许写入 `instance/`、`storage/` 与 `run/`；root-only SQLite 备份服务关闭网络、限制 capability，只允许写入 `backups/daily`、`instance/` 与 `storage/`。所有运行服务均开启权限沙箱。首轮等待只以 bootstrap timer 的 active/enabled 状态、完整剩余窗口、缓存服务的 `OnSuccess`/`OnFailure` 与激活事务 `COMMITTED` 作为证据，不生成额外 marker。
7. 完成 Android、iOS 真机检查、隐私接口检查和无敏感信息截图；使用 `docs/miniprogram/REVIEW_SCREENSHOT_MANIFEST_TEMPLATE.md` 登记文件名、系统与机型、字号、时间、commit、审核用途和完成状态。
8. 根据后台实时选项填写发布资料，在确认完整功能、双名称关系和单次定位处理方式已准确披露后上传 `1.1.0`，记录上传版本、构建标识、冻结 commit、提交说明、上传回执和审核截图。只接受本轮冻结 commit 生成的新上传证据，旧 1.0.0 回执只作历史记录。
9. 在正式点击发布前，记录当前线上小程序版本、可用回退版本、代码 commit、后端 `current` release、数据库备份和部署事务状态，由用户确认发布与回滚目标后再继续。

## 发布、观察与回滚确认

- 最终确认单至少写明：正式生效日期、待发布版本、审核通过时间、目标 commit hash、对应法律与上架页面内容 hash、后端 release ID、隐私版本、当前线上版本、平台可回退版本、负责人和确认时间。确认单只记录非秘密标识。
- 发布后先观察 401、5xx、bootstrap 延迟、快照年龄、两阶段 30 分钟 timer、网络闸门、预算计数和匿名漏斗护栏，并确认第三方消息服务调用数持续为 0。
- 产品事件的每日清理只处理应用事件。私有部署事务副本的保留清理是独立 root 维护操作，执行前先确认事务已解决且不再需要回滚或审计。
- 小程序端出现严重问题时，优先使用平台当时可用的版本管理能力回退小程序，并保留 Web 公共服务。后台实际没有可用回退版本时停止发布，先准备修复包和用户告知方案。
- 后端在公网切换前失败时，激活事务会恢复数据库、旧 release 与原 systemd 状态。公网服务已经尝试启动后进入向前修复区间，保留新数据库与新 release，避免覆盖可能已经确认的用户写入。
- 发现 `ROLLBACK_REQUIRED.txt` 或 `POST_COMMIT_ATTENTION.txt` 时停止下一次部署。人工核对数据库、`current` 链接、systemd 状态和事务目录后，才可用指向该精确事务目录的 `DEPLOY_RECOVERY_ACKNOWLEDGED_TRANSACTION` 登记恢复确认。

## 私密表单与凭据边界

- 本机私密表单固定为 `.env.wechat-release`，权限保持 `0600`，并由 `.gitignore` 排除。
- 根目录 `project.private.config.json` 同样由 `.gitignore` 排除并保持权限 `0600`，只保存开发者工具所需的正式 AppID 和本机偏好。
- 表单可填写注册联系人、步骤完成状态、正式 AppID 和 AppSecret。正式部署把两项写入受控服务器环境；AppSecret 完成配置后不复制到 `project.private.config.json` 或其他文件。其余服务端随机密钥由发布脚本在服务器内生成。
- 私密表单、本机私有配置和账号页面截图均不得提交到 Git 或通过公开聊天发送。
- 身份证号码、人脸信息、验证码、银行卡信息和付款凭证不得写入交接文档或私密表单，只在微信官方页面内处理。
- 发布执行方读取表单时只检查必填项是否就绪，日志和终端输出不得回显联系人、AppID 或 AppSecret 的值。
- 直接校验和部署快照都通过安全文件描述符读取表单；目录、符号链接、非 UTF-8、超过 64 KiB、读取期间变化或 I/O 失败都会返回固定结构错误，不输出本机路径或文件内容。
