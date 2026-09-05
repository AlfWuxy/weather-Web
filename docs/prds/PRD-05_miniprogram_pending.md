# PRD-05 小程序家属端「待处理」页

状态：APPROVED 2026-09-05 · 实施：Codex · 边界：小程序是家属端，不做老人三按钮

## 问题

`miniprogram/` 六页只做绑定 / 档案 / 看预警 / 复制话术。`config.js` 的 `API_BASE_URL` 默认空串。求助、核验、结案在小程序上不存在；话术硬编码且复制不带版本。

## 改动

### config

`miniprogram/config.js`：`API_BASE_URL` 默认 `https://yilaoweather.org`，允许开发者工具用 `__DEV__` 覆盖。

### 新页 `pages/pending/`

列出当前家属（token 持有者）所有 active pair 的当日状态，分三组：

1. 未结求助：存在 help_requested 且无 help_acknowledged。按钮「已收到」→ POST help_acknowledged。
2. 待核验：存在 self_reported 且无 caregiver_verified。按钮「核验：已做到」→ POST caregiver_verified。
3. 可结案：存在 caregiver_verified 或 help_acknowledged 且无 closed。按钮「结案」→ POST closed。

每张卡片还有「已转告老人」→ POST delivered（弹出 messenger_role / channel 选择，默认上次值）。卡片顶部状态条与网页一致（delivered · seen · understood · self_reported · verified · help · closed）。

tabBar 增加「待处理」，带未结求助角标。

### `pages/template/`

不再硬编码文案：启动时 `GET /mp/api/v1/scripts` 拉取版本与文案，缓存 24h。复制前必选 messenger_role、channel；复制事件带 `script_version, messenger_role, channel, scenario`。

### mp_api（`blueprints/mp_api.py`，Bearer Token，与网页同一 service 层）

- `GET /mp/api/v1/pending` → `{pairs:[{pair_id, elder_label(仅家属自设称呼，不含姓名字段), today:{delivered,seen,understood,self_reported,help_requested,help_acknowledged,caregiver_verified,closed}}]}`
- `POST /mp/api/v1/pairs/<id>/events` body `{stage, messenger_role?, channel?, script_version?}`；stage 只允许 delivered / help_acknowledged / caregiver_verified / closed；其余 403。非法转移 400 同网页。
- `GET /mp/api/v1/scripts` → `{version_hash, default, versions:{...}}`。
- `POST /mp/api/v1/events` 扩展校验：template_copy 必带四字段。

### 推送

家属侧 WxPusher 已有；求助推送由 PRD-01 的服务层发出，小程序不重复实现。

## 发布

只上传微信「体验版」，不提交审核。发布记录写进 `docs/product/miniprogram_release_log.md`（版本、日期、体验版二维码不入库）。`/status` 页把小程序标为「原型 · 体验版 · 未公开发布」。

## 测试

- pytest：pending 端点只返回 token 所属家属的 pair；stage 白名单；非法转移 400。
- scripts 端点哈希与网页一致。
- 小程序侧：微信开发者工具手工跑一遍并截图存 `docs/product/miniprogram_qa_20260910/`（不含个人信息）。
