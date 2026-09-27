# PRD-01 可审计老人行动链（ActionEvent）

状态：APPROVED 2026-09-05 · 负责人：心远 · 实施：Codex · 冻结事件字典，不再增减 stage

## 问题

`daily_status.confirmed_at` 只记录"我很安全"按钮的点击时刻，可被覆盖，被 `community_daily.confirm_rate` 当分子。它不能区分：老人看到了、看懂了、做到了、家属核实了。求助按钮只置 `help_flag`，页面文案却写"照护人将收到提醒"，实际没有推送。没有结案。

## 目标

一条 append-only 事件链，把老人当日行动拆成独立、可停留 `unknown` 的状态；老人端三个按钮；家属端核验 / 接收 / 结案；管理端带分母漏斗。所有指标都写明"不代表安全或健康"。

## 事件字典（冻结）

表 `action_events`（新，append-only，无 update / delete 路由）

| 列 | 类型 | 说明 |
|---|---|---|
| id | int pk | |
| pair_id | int fk pairs.id, index | 匿名配对，不存姓名 |
| local_date | date, index | 老人所在时区当日（Asia/Shanghai） |
| stage | str(32), index | 见下表，只允许这 9 个值 |
| actor_role | str(16) | elder / caregiver / community / system |
| channel | str(24) | web_shortcode / web_token / elder_mode / miniprogram / wxpusher / manual |
| script_version | str(16) nullable | 话术版本（PRD-04），delivered / seen 事件携带 |
| action_id | str(32) nullable | 当日行动清单里的行动键（如 stay_indoor_noon / drink_water / cooling_site / call_family） |
| alert_id | int fk weather_alerts.id nullable | |
| delivery_id | int fk alert_deliveries.id nullable | |
| meta_json | text nullable | 只允许封闭字段：teachback_action_id、verified_without_self_report(bool)、misclick_suspect(bool)。禁止自由文本、姓名、电话、医疗原因 |
| created_at | datetime utc, index | |

stage 允许值与允许的前驱（同 pair 同 local_date 内）：

| stage | 谁写 | 允许前驱 | 触发 |
|---|---|---|---|
| delivered | system / caregiver | 无 | AlertDelivery status=sent；或家属点"已转告老人"（channel=manual，带 script_version + messenger_role 写入 usage_events.meta） |
| seen | system | 任意或无 | GET /e/<token> 渲染成功；POST /action 短码查找成功；/elder-mode 渲染（登录态） |
| understood | elder | seen | 按"我看懂了" |
| action_selected | elder | seen / understood | teach-back 单选或勾选行动时隐式写入，带 action_id |
| self_reported | elder | seen / understood / action_selected | 按"我做到一项"，带 action_id；同时写 daily_status.confirmed_at（语义 = self_reported_at） |
| help_requested | elder | seen / understood / action_selected | 按"做不到，需要帮助"；触发推送 |
| help_acknowledged | caregiver / community | help_requested | 家属或社区点"已收到求助" |
| caregiver_verified | caregiver | delivered 及之后任意 | 家属点"核验：老人已做到"。若当日无 self_reported，则 meta.verified_without_self_report=true |
| closed | caregiver / community | caregiver_verified / help_acknowledged | 点"结案" |

规则：

- 非法转移返回 400 `{"error":"invalid_transition","from":...,"to":...}`，不写事件，不写 usage_event。
- 同 pair 同 date 同 stage 同 actor 60 秒内重复提交：幂等，返回 200 并复用已有事件 id，不新增。
- `unknown` 不存储：当日 active pair 无任何事件即为 unknown，由查询计算。
- 误触启发：help_requested 后 60 秒内出现 understood 或 self_reported，则后者的 meta.misclick_suspect=true（仅标注，不删除任何事件）。
- 保留周期：事件表保留 365 天；撤回：家属在 /pairs 解除绑定后 30 天内可申请删除该 pair 全部事件（管理员执行，写 audit_logs）。写进 `/transparency#privacy`。

## DailyStatus 变更

保留表和列。新增 nullable 列：`understood_at`、`verified_at`、`help_acknowledged_at`、`closed_at`。`confirmed_at` 保留，只在 self_reported 时写入，文档语义改为 self_reported_at。`help_flag`、`relay_stage`、`actions_done_count` 继续维护以兼容现有页面。

## CommunityDaily 变更

新增列 `understood_rate`、`self_report_rate`、`verified_rate`、`open_help_count`、`unknown_count`。`confirm_rate` 保留 = self_report_rate，metric 说明标 deprecated。

## 老人端 UI

三个页面共用同一组件 `templates/partials/elder_three_buttons.html`：

- `templates/action_checkin.html`（短码页，POST /action/confirm|help 已存在）
- `/e/<token>`（同模板不同路由）
- `/elder-mode`（`blueprints/user.py`，需登录态或 token；当前零写操作，接入三按钮）

按钮与文案（大字号、高对比、每个按钮独占一行、最小高度 72px）：

1. 「我看懂了」→ POST understood。成功后同页出现 teach-back 单选："今天你准备先做哪一步？"，选项 = 当日行动清单 + 「还没想好」；选择后 POST action_selected（action_id；「还没想好」= action_id `undecided`）。
2. 「我做到一项」→ 展开当日行动清单（复选，沿用现有 actions_done），提交 POST self_reported，每个勾选一条事件。
3. 「做不到，需要帮助」→ POST help_requested → 显示"已通知家属，家属确认收到后这里会变绿"。页面轮询 /action/state 每 30 秒或刷新时显示 help_acknowledged 状态。

删除「我很安全」按钮与文案。保留可选复盘（debrief）入口但移到底部次级链接。

路由（`blueprints/public.py`）：现有 `POST /action/confirm`、`POST /action/help`、`POST /e/<token>/checkin`、`POST /e/<token>/help` 全部改为调用 `services/action_events.py::record_event(pair, stage, actor_role, channel, ...)`；新增 `POST /action/understood`、`POST /action/select`、`GET /action/state`（返回当日各 stage 是否存在，用于按钮变色），token 版同样四个。

## 家属端

`GET /caregiver/pair/<id>`（`blueprints/user.py`）新增四个动作按钮（扩 `POST /caregiver/pair/<id>/action-log`，payload `{"event":"delivered|help_acknowledged|caregiver_verified|closed"}`）：

- 「已转告老人」→ delivered（channel=manual）。表单同时选 messenger_role（子女 / 孙辈 / 配偶 / 邻居 / 村干部 / 村医 / 本人）与 channel（wechat_text / wechat_voice / phone_call / in_person），写 usage_events.meta（PRD-04）。
- 「已收到求助」→ help_acknowledged。只在当日存在 help_requested 时显示。
- 「核验：老人已做到」→ caregiver_verified。
- 「结案」→ closed。只在存在 caregiver_verified 或 help_acknowledged 时显示。

pair 卡片显示当日状态条：delivered · seen · understood · self_reported · verified · help · closed，每格 ✓ / — ，unknown 灰。

## 求助推送（真实）

`services/public_service.py::_handle_action_help` 改为：写 help_requested 事件 → `create_notification(caregiver_id, type='help_requested', ...)` → 若 caregiver.push_enabled 且 wxpusher_uid，调用现有 WxPusher 发送（复用 AlertDelivery 记录 channel=wxpusher, status）→ relay_stage=caregiver。推送失败不阻断事件写入，记 AlertDelivery status=failed。老人页文案改为"已记录，正在通知家属"，不再承诺"将收到"。

## 管理端漏斗 `/analysis/pilot`

页首声明：分析单位 = pair；抽样框 = 当日 status=active 的 pair；"以下为描述性统计，不能推断预警导致了行动（因果 HOLD）"。

漏斗（当日 / 近 7 日 / 近 30 日切换），每行 = 阶段，列 = 去重 pair 数 / 分母 / 比例：delivered → seen → understood → action_selected → self_reported → caregiver_verified；并列显示 help_requested → help_acknowledged → closed；unknown 数。

附加指标：任务中位时长（seen→self_reported 分钟）、求助接收中位时长（help_requested→help_acknowledged）、结案中位时长、未结求助数、误触疑似数、事件来源完整率（channel 非空且 delivered/seen 带 script_version 的比例）、verified_without_self_report 数。

`GET /analysis/pilot/export.csv` 改为事件级：`pair_hash,local_date,stage,actor_role,channel,script_version,action_id,created_at,meta`。`pair_hash = sha256(SECRET_KEY + pair_id)[:12]`。

新增 `GET /analysis/pilot/funnel.csv`：聚合表。

## 指标口径

`core/metric_explanations.py`：删除 `action_confirmation_rate` 的旧描述，新增 understood_rate / self_report_rate / verified_rate / help_ack_median_minutes / open_help_count / unknown_count / event_source_completeness，每项含公式、分母、时间窗、缺失处理、"局限：不代表老人安全或健康改善；未点击不等于未理解"。`services/user/_helpers.py` 同步实现。

## 迁移

alembic 新 revision：建 `action_events` + 索引 (pair_id, local_date, stage)；DailyStatus 加 4 列；CommunityDaily 加 5 列。SQLite 兼容（batch_alter_table）。

## 测试（pytest）

- 合法转移全路径；每个非法转移返回 400 且不落库。
- 60 秒幂等。
- help_requested 触发 create_notification；WxPusher 失败不影响事件。
- caregiver_verified 无 self_reported 时 meta 标记。
- 漏斗分母 = active pairs；unknown 计算正确；export.csv 无 pair_id 明文、无姓名。
- /elder-mode 三按钮渲染与 POST。
- Playwright（若仓库已有 playwright 依赖）：短码页三按钮一遍。

## 不做

- 不改小程序老人端（小程序是家属端，见 PRD-05）。
- 不给 stage 增加"健康结局"类状态。
- 不存自由文本。
