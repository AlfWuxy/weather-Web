# 宜老天气通微信小程序

本目录说明微信小程序版的开发、联调、发布和维护边界。小程序客户端位于仓库根目录 `miniprogram/`，Flask 适配层使用 `/mp/api/v1`。

## 产品范围

普通用户打开后可直接查看都昌县天气、七日预报、官方预警、行动建议、社区脆弱性、避暑点和 1 km 热暴露 GIS。照护、健康评估、健康日记和用药记录在用户主动登录并同意隐私说明后启用。

后台用户管理、原始病历、研究导出和高权限运营操作继续留在 Web 管理面。小程序只接收聚合或去标识数据。

## 本地导入

1. 安装微信开发者工具。
2. 导入仓库根目录，开发者工具会按根目录 `project.config.json` 的 `miniprogramRoot` 只编译 `miniprogram/`。
3. 游客调试可保留 `touristappid`；正式上传前在开发者工具中选择已认证的小程序 AppID。个人本机设置放进根目录 `project.private.config.json`，不要提交。
4. 参考 `miniprogram/config.example.js`，在本机临时把已经备案的正式 HTTPS API 域名填入 `miniprogram/config.runtime.js`。
5. 上传完成后立即用 `git diff -- miniprogram/config.runtime.js` 复核，并恢复为空值；禁止把真实域名或密钥提交到公开分支。

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

AppSecret 只允许出现在服务器环境变量。小程序包、日志、错误消息和 Git 历史都不能包含 AppSecret、QWeather key 或微信 CI 私钥。

新 Web Token 会绑定生成时的隐私版本并在 30 天后过期。历史无期限 Token 在迁移后必须轮换；隐私版本升级也会让旧 Token 返回 428。用药和求助只保存记录，不承诺自动送达。

`config.runtime.js` 始终保留在仓库中且默认值为空，保证源码可以正常编译。域名为空时请求层会明确终止，不会误连占位服务。

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

上架前按 [RELEASE_CHECKLIST.md](./RELEASE_CHECKLIST.md) 完成账号侧配置，并按 [TEST_PLAN.md](./TEST_PLAN.md) 留存验收结果。隐私文案初稿见 [PRIVACY_NOTICE_TEMPLATE.md](./PRIVACY_NOTICE_TEMPLATE.md)。
