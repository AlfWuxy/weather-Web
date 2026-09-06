# 宜老天气通 · 高温照护轮次去标识研究记录格式

版本 1.0 · 2026-09-06 · 配合 `docs/research/script_test_prereg.md`、`docs/data/action_events_dictionary.md`、`services/action_events.py`。

性质：描述性研究导出 schema，不是效果研究，不是急救记录。本文件定义**可离开生产库的去标识记录**；不替代生产表。改动本文件需新增版本号并保留旧版。

一条记录 = **一次高温照护轮次（heat-care round）**：同一 `local_date`、同一家庭空间、同一照护配对，围绕一条高温提醒发生的投递尝试、家属联系、困难分类、转交与结案。不是一人一生轨迹，不是健康结局。

## 1. 目的与上限

可写：在最小化可识别信息的前提下，描述提醒是否发出、渠道是否被服务商接受、设备是否确认收到、家属是否申报已联系、是否求助、是否转交、以何种封闭码结案。

不可写：某人已完成防护行动；老人理解了提醒；失联等于未理解；转交成功等于健康改善；测试会话人数等于真实覆盖。

三条不等式始终成立：

**家属自报 ≠ 已核实行动 ≠ 健康改善。**

服务商接受 ≠ 用户打开 ≠ 家属确认收到 ≠ 已解决。设备确认收到 ≠ 行动完成。家属扮演老人 ≠ 真实老人理解。

## 2. 去标识原则

只导出哈希、枚举、粒度到社区/乡镇的地点、UTC 时间戳。禁止：姓名、电话、证件号、精确门牌/街道地址、经纬度、OpenID、明文 token、pair 数字 id、family_space 数字 id / public_id / 空间名、用户 id、自由文本、通话/聊天转写、健康档案、用药、录音。

`n<5` 的交叉表单元格遮蔽。研究文件与合成示例一律用虚构标识；本文 JSON 为合成，不含真人名。

## 3. 测试 / 演练 与真实轮次

`is_test=true` 的记录（测试 pair、`qa_` 前缀、演练家庭空间、可用性会话）**必须与真实轮次分开统计**，不得并入生产漏斗。生产漏斗默认排除测试，见 `services.action_events.is_test_pair` 与 `analysis_pair_ids(include_test=False)`。

家属志愿者在可用性会话中**扮演老人**（见 `docs/research/usability_session_guide.md` 任务 B）只记为 `test_sessions` 上的可用性观察，**不得**记为真实老人理解、teach-back 正确或真实行动。

真实轮次中家属代为点击老人端，记 `contact_result.proxy=true`，不升格为老人本人理解。

## 4. 分析单位与分母

分析前先声明单位，禁止混用人数与事件数。

| 单位 | 字段 | 含义 |
|---|---|---|
| persons | 去重 `pair_hash` | 照护对象（配对），不是账号数 |
| families | 去重 `family_space_hash` | 家庭空间 |
| reminder_events | 记录条数 | 高温提醒轮次 |
| help_requests | 有求助的记录 | 对应 `help_requests` 工单 |
| test_sessions | `is_test=true` 的会话 | 可用性/演练，单独报表 |

分母规则（与 `services.action_events.funnel` / `analysis_pair_ids` 一致）：

- 观察期内已建档对象进入分母；停用后仍保留，避免失联从分母消失。
- **联系不上（unreachable）、无回复、未结（open）留在分母**，不进「成功」分子。
- `unknown`（当日无任何行动事件）计入分母，不单独删掉。
- 未点击 ≠ 未理解；无回复 ≠ 未发生。
- `outcome_success` 仅当 `outcome_code=assisted` 为 true；`transferred_confirmed` 等其余结案码 **不是** 成功。

分子必须与单位同口径：不能用已建档家庭数代替提醒事件分母，不能用测试会话代替真实 persons。

## 5. 记录字段

时间一律 **UTC**，ISO-8601（例 `2026-09-06T04:30:00Z`）。`local_date` 是老人所在时区（Asia/Shanghai）的自然日，仅用于对齐天气日，不是事件时刻。

### 5.1 标识

| 字段 | 类型 | 规则 |
|---|---|---|
| `record_id` | string | 不透明导出键。不可逆，不使用生产主键、public_id、短码。建议 `sha256(pepper \|\| round_key)[:16]` 的 hex。同一轮次稳定，跨环境不可链接回库。 |
| `schema_version` | string | 本文件版本，如 `2026-09-06.deid-heat-care-v1`。 |
| `is_test` | bool | 测试/演练/可用性会话为 true。来源：`pairs.is_test`、`family_spaces.is_test`、`help_requests.is_test`、`qa_` 前缀。任一条为真则整条记录 `is_test=true`。 |
| `family_space_hash` | string \| null | **不要**写 `family_spaces.id` 或 `public_id`。构造与 `pair_hash` 同思路：密钥加盐后截断哈希。无家庭空间时为 null。 |
| `pair_hash` | string | **不要**写 `pairs.id`。使用 `services.action_events.pair_hash(pair_id)`：`sha256(SECRET_KEY \|\| pair_id)` 的前 12 位 hex。与 `/analysis/pilot/export.csv` 列名一致。 |
| `elder_location_granularity` | object | 只到 **社区或乡镇**。`level` ∈ {`community`, `township`}；`label` 为社区/乡镇编码或标准化地名（如已有 `community_code`），禁止街道、门牌、楼栋、GPS。无法落到该粒度则 `level=township` 且 `label` 用县域占位，不得补精确地址。 |

`pair_hash` 实现位置：`services/action_events.py` 中 `pair_hash`。研究侧 `family_space_hash` 应对 `family_space_id` 做同等处理，不复用空间公开 id。

### 5.2 提醒 `reminder`

对应当日高温提醒内容，不存正文全文（正文可从 `content_version` 回溯模板）。

| 字段 | 类型 | 允许值 / 说明 |
|---|---|---|
| `weather_type` | string | 封闭：`heat` \| `cold` \| `other`。高温照护轮次应为 `heat`（含官方高温预警与平台高温阈值 `heat_threshold`）。 |
| `place` | string | 与 `elder_location_granularity.label` 同粒度，不写街道。 |
| `time_window` | string | 行动时段标签，如 `11:00-16:00` 或 `noon`，不是个人日程。 |
| `source_kind` | string | `official_alert` \| `platform_rule` \| `doctor_advice`。官方气象预警（如和风 `/warning/now`）= `official_alert`；平台阈值规则（如最高气温 ≥ 35°C）= `platform_rule`；经医生本人审核发布的模板 = `doctor_advice`。未由医生本人审核的科普不得标 `doctor_advice`。 |
| `content_version` | string | 话术或模板版本，如 `v2_gist_why` 或 `advice:<version>`。对应 `action_events.script_version` / `advice_contents.version`。 |
| `published_at` | string \| null | 内容发布时间 UTC。官方预警用预警生效/入库时刻；平台规则用该轮次生成时刻；医生建议用 `advice_contents.published_at`。 |

禁止把预警原文、医生姓名、家属称呼写入本对象。

### 5.3 渠道尝试 `channel_attempts[]`

每次投递或拉取尝试一条。记录「是否发出/是否被通道确认」，**不**记录行动完成。

| 字段 | 类型 | 说明 |
|---|---|---|
| `channel` | string | 封闭，与生产通道对齐：`wxpusher` \| `miniprogram` \| `web_shortcode` \| `web_token` \| `elder_mode` \| `device` \| `in_app` \| `manual`。 |
| `attempted_at` | string | UTC。 |
| `provider_accepted` | bool | 服务商或站内通道接受该次投递。对应 `notification_outbox.provider_accepted_at` 非空，或 `alert_deliveries.status=sent`。接受 ≠ 已读。 |
| `device_confirmed` | bool | 授权终端确认收到/展示（如 `device_events` 的 `content_pulled`、心跳窗口内在线）。**不是**防护行动完成。设备接口明确 `not_action_completed`。 |

**禁止字段：`action_completed`。** 不得用按键、TTS、心跳、打开页面推断「已做到」。核实行动若存在，只属于生产 `action_events.caregiver_verified`，且本去标识记录默认**不导出**该推断为成功结局。

### 5.4 联系结果 `contact_result`

| 字段 | 类型 | 说明 |
|---|---|---|
| `family_reported_contacted` | bool \| null | 家属申报已联系到老人。自报。null = 尚未申报。 |
| `proxy` | bool | 是否由代理人操作（家属/代言角色，非老人本人）。对应 `help_requests.is_proxy` 或家属代点老人端。 |
| `proxy_basis` | string \| null | **类别码，不是转写。** 仅当 `proxy=true`。封闭：`family_check` \| `unanswered_then_proxy` \| `elder_asked_proxy` \| `device_sos` \| `community_relay` \| `usability_roleplay` \| `other`。生产库若暂存自由文本 `proxy_basis`，导出前必须映射到上表；无法映射则 `other`，丢弃原文。可用性扮演老人用 `usability_roleplay`，且 `is_test=true`。 |

### 5.5 困难分类 `difficulty_category`

与 `help_requests.category` 一致。无求助则为 null。

`cannot_complete` \| `need_checkin` \| `need_cooling` \| `other`

### 5.6 转交 `handoff`

无转交请求则为 null。角色只写角色，不写姓名。

| 字段 | 类型 | 说明 |
|---|---|---|
| `requested_support_role` | string \| null | `doctor` \| `volunteer`。对应 `help_requests.requested_support_role`。 |
| `status` | string | `waiting` \| `accepted` \| `declined`。`waiting` = 工单仍 `pending_ack` 或已请求协助尚未接手；`accepted` = 已 ack / 已开始；`declined` = 结案码 `declined` 或明确不受理。无人接手时保持 `waiting`，不得伪装已响应。 |
| `assignee_role` | string \| null | 接手人角色：`caregiver` \| `owner` \| `doctor` \| `volunteer` \| `community_limited`。**禁止姓名、user_id。** |

### 5.7 结局

| 字段 | 类型 | 说明 |
|---|---|---|
| `outcome_code` | string \| null | 未结为 null（仍在分母）。封闭码与 `services.help_request_service.RESOLUTION_DEFINITIONS` 一致：`assisted` \| `transferred_confirmed` \| `withdrawn` \| `unreachable` \| `declined` \| `false_alarm` \| `other`。别名在导出前归一：`reached_elder`/`action_done` → `assisted`，`referred` → `transferred_confirmed`。 |
| `outcome_success` | bool \| null | **仅 `assisted` 为 true。** 其余码均为 false。未结为 null。`transferred_confirmed` 表示已转交并确认接收，不是本平台协助成功，也不是健康改善。 |

### 5.8 轮次时间

| 字段 | 类型 | 说明 |
|---|---|---|
| `local_date` | string | `YYYY-MM-DD`，Asia/Shanghai 天气日。 |
| `round_started_at` | string | 本轮次第一条渠道尝试或提醒生成时刻，UTC。 |
| `round_updated_at` | string | 本记录最后一次状态变化，UTC。 |
| `resolved_at` | string \| null | 结案时刻 UTC；未结为 null。 |

## 6. 明确不入库 / 不导出

- 生产数字主键：`pair_id`、`user_id`、`family_space_id`、`help_requests.id`、`assignee_user_id`
- `action_completed`、行动清单勾选原文、teach-back 自由文本
- 通话记录、聊天转写、`proxy_basis` 原文
- 街道地址、GPS、设备序列号、明文 device token
- 医生姓名（角色用 `doctor` 即可；站内「徐医生已审核」属产品展示，研究导出只用 `source_kind=doctor_advice` + `content_version`）

## 7. 与生产对象的对应（便于抽取，不是把生产表原样拷出）

| 研究字段 | 生产来源 |
|---|---|
| `pair_hash` | `services.action_events.pair_hash` |
| `family_space_hash` | 对 `family_space_id` 同等加盐截断哈希（研究层新增，生产尚未导出） |
| `is_test` | `pairs` / `family_spaces` / `help_requests` / `advice_contents` |
| `elder_location_granularity` | `pairs.community_code`（不得用 `location_query` 中的街道） |
| `reminder.source_kind` | 官方预警 / 平台阈值 / 已审核 `advice_contents` |
| `channel_attempts.provider_accepted` | `notification_outbox.provider_accepted_at`、`alert_deliveries.status` |
| `channel_attempts.device_confirmed` | `device_events`（`content_pulled` 等）；禁止当行动完成 |
| `difficulty_category` | `help_requests.category` |
| `handoff` | `requested_support_role` + 工单状态；`assignee_role` 由 membership.role 映射 |
| `outcome_*` | `resolution_code` + `resolution_success()` |

## 8. JSON 示例（合成）

下面两条均为虚构。第一条真实口径轮次（未结、留在分母）；第二条测试会话（家属扮演老人，不得当真实理解）。

```json
{
  "schema_version": "2026-09-06.deid-heat-care-v1",
  "record_id": "a7c3e91b04d26f18",
  "is_test": false,
  "family_space_hash": "9f2c1a8b3d07",
  "pair_hash": "c4e8b1a9027d",
  "elder_location_granularity": {
    "level": "township",
    "label": "duchang_township_north"
  },
  "local_date": "2026-09-06",
  "round_started_at": "2026-09-06T01:05:00Z",
  "round_updated_at": "2026-09-06T04:40:00Z",
  "resolved_at": null,
  "reminder": {
    "weather_type": "heat",
    "place": "duchang_township_north",
    "time_window": "11:00-16:00",
    "source_kind": "official_alert",
    "content_version": "v2_gist_why",
    "published_at": "2026-09-06T00:50:00Z"
  },
  "channel_attempts": [
    {
      "channel": "wxpusher",
      "attempted_at": "2026-09-06T01:05:00Z",
      "provider_accepted": true,
      "device_confirmed": false
    },
    {
      "channel": "device",
      "attempted_at": "2026-09-06T01:06:12Z",
      "provider_accepted": true,
      "device_confirmed": true
    }
  ],
  "contact_result": {
    "family_reported_contacted": false,
    "proxy": false,
    "proxy_basis": null
  },
  "difficulty_category": "need_checkin",
  "handoff": {
    "requested_support_role": "volunteer",
    "status": "waiting",
    "assignee_role": null
  },
  "outcome_code": null,
  "outcome_success": null
}
```

```json
{
  "schema_version": "2026-09-06.deid-heat-care-v1",
  "record_id": "b1d0aa44e8c17f02",
  "is_test": true,
  "family_space_hash": "11ab9c0e2f44",
  "pair_hash": "0e7a2c91b5d3",
  "elder_location_granularity": {
    "level": "community",
    "label": "duchang_community_qa"
  },
  "local_date": "2026-09-06",
  "round_started_at": "2026-09-06T06:10:00Z",
  "round_updated_at": "2026-09-06T06:22:00Z",
  "resolved_at": "2026-09-06T06:22:00Z",
  "reminder": {
    "weather_type": "heat",
    "place": "duchang_community_qa",
    "time_window": "noon",
    "source_kind": "platform_rule",
    "content_version": "v3_kin_time",
    "published_at": "2026-09-06T06:10:00Z"
  },
  "channel_attempts": [
    {
      "channel": "web_token",
      "attempted_at": "2026-09-06T06:12:00Z",
      "provider_accepted": true,
      "device_confirmed": false
    }
  ],
  "contact_result": {
    "family_reported_contacted": true,
    "proxy": true,
    "proxy_basis": "usability_roleplay"
  },
  "difficulty_category": "cannot_complete",
  "handoff": null,
  "outcome_code": "false_alarm",
  "outcome_success": false
}
```

第二条只进入 `test_sessions`。`family_reported_contacted=true` 在此表示志愿者完成了扮演任务，**不是**真实老人理解，也不是 `outcome_success`。

医生建议来源的提醒示例片段（合成，可嵌在真实口径记录的 `reminder` 中）：

```json
{
  "weather_type": "heat",
  "place": "duchang_township_east",
  "time_window": "11:00-16:00",
  "source_kind": "doctor_advice",
  "content_version": "advice:2",
  "published_at": "2026-09-05T08:00:00Z"
}
```

## 9. 报表口径速查

| 问题 | 单位 | 分母 | 分子 | 排除 |
|---|---|---|---|---|
| 提醒是否发出 | reminder_events | 观察日全部轮次（含无回复） | 至少一次 `provider_accepted=true` | 测试单独报 |
| 设备是否确认收到 | reminder_events | 同上 | 至少一次 `device_confirmed=true` | 不得当行动完成 |
| 家属申报已联系 | persons 或 reminder_events（须声明） | 成熟观察队列 | `family_reported_contacted=true` | 自报 ≠ 核实 |
| 求助是否被接手 | help_requests | 全部求助含 waiting / unreachable | `handoff.status=accepted` | 测试单独报 |
| 平台协助成功 | help_requests | 全部求助含 open / unreachable | `outcome_success=true`（即 `assisted`） | 转交、撤回、误报不是成功 |
| 话术好不好懂 | test_sessions | 完成会话协议的测试会话 | 预注册中的 teach-back 规则 | **不得**用真实 persons |

未结、无回复、`unreachable` 全部留在分母。成功率分母变小即视为口径错误。

## 10. 伦理与保留

导出集只含本 schema 字段。志愿者可要求删除其 `is_test=true` 会话对应的 `record_id`。真实用户按产品撤回规则：解除绑定后 30 天内可申请删除该 pair 行动事件（见行动事件数据字典）；研究副本须同步剔除对应 `pair_hash`，不得靠哈希反查个人。

本格式不证明覆盖面、依从性或健康获益。
