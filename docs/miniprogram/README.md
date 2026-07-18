# 宜老天气通微信小程序

本目录说明微信小程序版的开发、联调、发布和维护边界。小程序客户端位于仓库根目录 `miniprogram/`，Flask 适配层使用 `/mp/api/v1`。

## 产品范围

普通用户打开后可直接查看都昌县天气、七日预报、官方预警、行动建议、社区脆弱性、避暑点和 1 km 热暴露 GIS。家人档案、天气行动评估、生活记录和日常用品服用备忘在用户主动登录并同意隐私说明与用户协议后启用。

后台用户管理、原始病历、研究导出和高权限运营操作继续留在 Web 管理面。小程序只接收聚合或去标识数据。

## 本地导入

1. 安装微信开发者工具。
2. 导入仓库根目录，开发者工具会按根目录 `project.config.json` 的 `miniprogramRoot` 只编译 `miniprogram/`。
3. 小程序发布分支已在根目录 `project.config.json` 固定正式 AppID。导入后核对项目名称和主体；个人界面偏好放进 `project.private.config.json`，AppSecret、代码上传密钥和会话密钥只进入受限私密文件，均不得提交。
4. 正式分支已把公开 API 域名 `https://yilaoweather.org` 固定在 `miniprogram/config.runtime.js`，保证目标 commit 可直接编译和复现。
5. 微信后台 `request` 合法域名、私密发布确认单和目标 commit 中的 API 域名必须一致；AppSecret、上传密钥及第三方密钥仍不得提交。

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

AppSecret 只允许出现在服务器环境变量。小程序包、日志、错误消息和 Git 历史都不能包含 AppSecret、QWeather key 或微信 CI 私钥。

正式小程序不使用第三方生成式人工智能。正式 Web 后端固定 `FEATURE_WEB_AI=0`、`SILICONFLOW_API_KEY` 为空，发布校验会拒绝开启状态或密钥。

新 Web Token 会绑定生成时的隐私版本并在 30 天后过期。历史无期限 Token 在迁移后必须轮换；隐私版本升级也会让旧 Token 返回 428。用药和求助只保存记录，不承诺自动送达。

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

上架前按 [RELEASE_CHECKLIST.md](./RELEASE_CHECKLIST.md) 完成账号侧配置，并按 [TEST_PLAN.md](./TEST_PLAN.md) 留存验收结果。隐私文案见 [PRIVACY_NOTICE_TEMPLATE.md](./PRIVACY_NOTICE_TEMPLATE.md)，服务规则见 [USER_AGREEMENT_TEMPLATE.md](./USER_AGREEMENT_TEMPLATE.md)。

`DEPLOY_REQUIRE_WECHAT_READY=0` 只能在本地微信开发者工具预览。远程发布脚本只接受 `DEPLOY_REQUIRE_WECHAT_READY=1`，并会在任何 SSH、上传或服务器修改前验证正式门禁。
