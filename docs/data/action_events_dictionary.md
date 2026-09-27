# 行动事件数据字典（PRD-01）

冻结于 2026-09-05。本表为 append-only；无更新/删除业务路由。

## 保留与撤回

- **保留周期**：`action_events` 保留 365 天。
- **撤回**：家属在 `/pairs` 解除绑定后 30 天内可申请删除该 pair 的全部行动事件；由管理员执行，并写入 `audit_logs`。

透明度页隐私段落由后续 PRD-02 维护；本文件为事件字典与上述规则的权威说明。

## 表 `action_events`

| 列 | 类型 | 说明 |
|---|---|---|
| id | int pk | |
| pair_id | int fk pairs.id | 匿名配对，不存姓名 |
| local_date | date | 老人所在时区当日（Asia/Shanghai） |
| stage | str(32) | 仅允许下表 9 个值 |
| actor_role | str(16) | elder / caregiver / community / system |
| channel | str(24) | web_shortcode / web_token / elder_mode / miniprogram / wxpusher / manual |
| script_version | str(16) nullable | 话术版本（PRD-04），delivered / seen 携带 |
| action_id | str(32) nullable | 当日行动键；「还没想好」= `undecided` |
| alert_id | int fk weather_alerts.id nullable | |
| delivery_id | int fk alert_deliveries.id nullable | |
| meta_json | text nullable | 仅白名单字段 |
| created_at | datetime utc | |

## 冻结 stage 与前驱

| stage | 谁写 | 允许前驱 | 触发 |
|---|---|---|---|
| delivered | system / caregiver | 无 | AlertDelivery status=sent；或家属「已转告老人」（channel=manual） |
| seen | system | 任意或无 | 短码页 / `/e/<token>` / `/elder-mode` 渲染成功 |
| understood | elder | seen | 「我看懂了」 |
| action_selected | elder | seen / understood | teach-back 单选，带 action_id |
| self_reported | elder | seen / understood / action_selected | 「我做到一项」，写 `daily_status.confirmed_at` |
| help_requested | elder | seen / understood / action_selected | 「做不到，需要帮助」 |
| help_acknowledged | caregiver / community | help_requested | 「已收到求助」 |
| caregiver_verified | caregiver | delivered 及之后任意 | 「核验：老人已做到」 |
| closed | caregiver / community | caregiver_verified / help_acknowledged | 「结案」 |

规则：

- 非法转移返回 400 `{"error":"invalid_transition","from":...,"to":...}`，不写事件。
- 同 pair / date / stage / actor（及 action_id）60 秒内重复提交幂等。
- `unknown` 不存储：当日 active pair 无任何事件即 unknown。
- `help_requested` 后 60 秒内出现 understood 或 self_reported，则后者 `meta.misclick_suspect=true`。

## meta 白名单

只允许：

- `teachback_action_id`
- `verified_without_self_report`（bool）
- `misclick_suspect`（bool）

禁止自由文本、姓名、电话、医疗原因。
