# 宜老天气通微信小程序

本目录说明微信小程序版的开发、联调、发布和维护边界。小程序客户端位于仓库根目录 `miniprogram/`，Flask 适配层使用 `/mp/api/v1`。

## 产品范围

普通用户打开后可直接查看都昌县天气、七日预报、官方预警、行动建议、社区脆弱性、避暑点和 1 km 热暴露 GIS。避暑资源页可在用户逐次确认后单次读取位置，只在端内对 GCJ-02 资源点做直线距离排序；用户坐标不上传至本项目服务器，也不进入本项目持久存储，拒绝后可手选社区。家人档案、天气行动评估、生活记录和日常用品服用备忘需要用户主动登录并完成一般隐私确认，首次进入时还要勾选默认未选中的健康敏感个人信息单独同意。

后台用户管理、原始病历、研究导出和高权限运营操作继续留在 Web 管理面。小程序只接收聚合或去标识数据。

1.1.1 的跨端账号串联从网页发起。网页用户主动填写的手机号只作待验证跨端账号标识，当前未经过短信验证，不能用于网页登录或证明号码归属；不同账号填写相同未验证号码不会互相获得资料，也不会阻断一次性绑定码。微信小程序不请求微信手机号，也不调用微信手机号接口。网页用户使用用户名登录并复验当前密码后生成一次性 8 位绑定码，10 分钟内有效且服务器只保存哈希；连续失败会临时锁定。成功绑定只迁移空白微信临时占位账号的身份，旧会话失效，占位账号去关联；账号注销需要 fresh `wx.login` 且微信身份与当前会话一致。

今日行动库包含 120 条预先编写的家庭提醒。小程序 GIS 中的 65+ 是正人口支持网格的模型化 65 岁及以上人口比例，LST 是 Aqua MODIS 白天晴空地表温度。小程序使用项目后端聚合数据、端内 Canvas 和微信原生地图；网页端高德底图、后台高德地理编码候选与小程序边界分开，候选资源经人工核对后才公开。

## 本地导入

1. 安装微信开发者工具。
2. 导入仓库根目录，开发者工具会按根目录 `project.config.json` 的 `miniprogramRoot` 只编译 `miniprogram/`；该受版本控制文件固定使用 `touristappid`。
3. 在被 Git 忽略的根目录 `project.private.config.json` 中配置正式 AppID 并保留开发者工具生成的本机偏好，文件权限保持 `0600`。开发者工具会把本机私有配置与公开工程配置合并；AppSecret 绝不写入该文件。
4. 正式分支已把公开 API 域名 `https://yilaoweather.org` 固定在 `miniprogram/config.runtime.js`，保证目标 commit 可直接编译和复现。
5. 微信后台 `request` 合法域名、私密发布确认单和目标 commit 中的 API 域名必须一致。正式 AppID 与 AppSecret 保存到本机私密发布表单并下发受控服务器环境；AppSecret、上传密钥及第三方密钥均不得提交。

## 后端配置

生产环境至少确认：

- `WX_MINIPROGRAM_APPID`
- `WX_MINIPROGRAM_SECRET`
- `WX_MINIPROGRAM_OPENID_PEPPER`：至少 32 位独立随机值
- `WX_MINIPROGRAM_SESSION_SECRET`：至少 32 位独立随机值，不与其他密钥复用
- `WX_MINIPROGRAM_PRIVACY_VERSION`
- `WX_MINIPROGRAM_SESSION_TTL_SECONDS=604800`
- `WX_MINIPROGRAM_MAX_ACTIVE_SESSIONS=5`
- `API_TOKEN_TTL_DAYS=30`
- `PAIR_TOKEN_PEPPER`
- `PUBLIC_BASE_URL`：填写与小程序合法域名一致的正式 HTTPS 地址
- `QWEATHER_CANONICAL_LOCATION=116.20,29.27`
- `WEATHER_CACHE_TTL_MINUTES=30`
- `FORECAST_CACHE_TTL_MINUTES=30`
- `QWEATHER_WARNING_CACHE_TTL_MINUTES=30`
- `QWEATHER_MONTHLY_REQUEST_LIMIT=40000`
- `QWEATHER_BUDGET_FAIL_CLOSED=1`
- `RATE_LIMIT_MP_PUBLIC=600 per minute`

产品分析只使用服务端固定事件和登录后的最小匿名维度，保存 30 天；公开浏览不接入第三方统计 SDK。指标定义、事件边界和验证 SQL 见 [ANALYTICS_SPEC.md](./ANALYTICS_SPEC.md)。

AppSecret 只允许出现在权限为 `0600` 且被 Git 忽略的本机私密发布表单，以及受控服务器环境变量。小程序包、开发者工具私有配置、日志、错误消息和 Git 历史都不能包含 AppSecret、QWeather key 或微信 CI 私钥。

正式小程序不使用第三方生成式人工智能。正式 Web 后端固定 `FEATURE_WEB_AI=0`、`SILICONFLOW_API_KEY` 为空，发布校验会拒绝开启状态或密钥。

1.1.1 小程序界面提供微信快捷登录和一次性网页账号绑定码入口，不请求微信手机号，也不提供手动粘贴 Web Token 的入口。网页端主动填写且未短信验证的手机号属于网页账号字段。服务端历史 Web Token 兼容属于 Web 端迁移边界，不能作为小程序账号身份。用药和求助只保存记录，不承诺自动送达。

`config.runtime.js` 始终保留在正式小程序分支中并固定公开生产域名。请求层只允许 HTTPS 且只访问同一主机，不接受跨域绝对 URL。

## 请求模型

小程序首页、预报、预警、行动页和照护页共享 `GET /mp/api/v1/bootstrap` 返回的同一份都昌快照。客户端将快照保存 30 分钟，并合并并发请求。页面切换和重复打开不会分别触发天气请求。

公共接口不会临时刷新 QWeather。天气刷新只由服务器的 30 分钟定时任务执行，预算达到上限或预算存储不可用时按 fail-closed 规则停止 QWeather 请求。

## 验证命令

以下验证不访问真实 QWeather：

```bash
conda run -n case-weather-py312 python -m pytest -q
find miniprogram -name '*.js' -print0 | xargs -0 -n1 node --check
find miniprogram -name '*.json' -print0 | xargs -0 -n1 jq empty
node --test miniprogram/tests/*.test.js
git diff --check
```

带 `network` 标记的真实第三方诊断不属于常规回归。离线手工契约使用 `-m "manual and not network"`；发布前的真实天气检查必须走单次受控流程，并在执行前确认当月预算。

## 发布入口

个人主体账号持有人先按 [PERSONAL_SUBJECT_ACTION_SHEET.md](./PERSONAL_SUBJECT_ACTION_SHEET.md) 完成平台实际出现的适用实名、人脸、短信、扫码确认和必要付款步骤。项目维护者再按 [RELEASE_CHECKLIST.md](./RELEASE_CHECKLIST.md) 完成账号配置与正式门禁，并按 [TEST_PLAN.md](./TEST_PLAN.md) 留存验收结果。隐私文案见 [PRIVACY_NOTICE_TEMPLATE.md](./PRIVACY_NOTICE_TEMPLATE.md)，服务规则见 [USER_AGREEMENT_TEMPLATE.md](./USER_AGREEMENT_TEMPLATE.md)。

需要先上线网页和后端时，显式设置 `DEPLOY_MODE=web_backend_only` 与 `DEPLOY_REQUIRE_WECHAT_READY=0`。该模式不读取微信私密表单、不替换或生成 AppID、AppSecret、OpenID pepper 和会话密钥，也不代表微信包已完成审核。升级旧正式环境且只缺新增绑定码密钥时，服务器会 if-empty 生成独立的 `ACCOUNT_LINK_CODE_PEPPER`，它不属于微信平台凭据。该模式仍要求干净 Git HEAD，并继续走不可变 release、候选环境校验、备份、迁移、健康检查和回滚事务。网页/后端先行部署只复用服务器已有的 QWeather 运行态私钥，部署环境必须清空 `QWEATHER_JWT_PRIVATE_KEY_SOURCE`；私钥轮换继续留在完整正式事务。

微信正式发布使用 `DEPLOY_MODE=wechat_formal` 与 `DEPLOY_REQUIRE_WECHAT_READY=1`。脚本会在任何 SSH、上传或服务器修改前验证正式表单和冻结提交；两种模式及 ready 值交叉混用都会直接拒绝。命令行显式模式与 `ENV_FILE` 冲突时同样在 SSH 前停止。候选环境复制时会固定活动 `.env` 与 `current` 链接摘要，激活取得部署锁后执行 CAS；期间出现另一轮部署或人工配置变化时，本轮候选停止并要求重建。
