# 网页与小程序求助互通契约（长期）

> 实现版本：`2026-09-06.help-family-v2`  
> 基线分支：`feature/web-mp-help-interop-20260905`（从 `release/2026-09-freeze` 拉出）

## 身份适配

| 端 | 认证 | 授权 |
|---|---|---|
| 网页 | Flask-Login session + CSRF | `FamilyMembership` 角色矩阵 |
| 小程序 | 可撤销 `MiniProgramSession` 或存量 `ApiToken` Bearer | 同一矩阵；客户端不可伪造 owner/admin |

无权对象统一 **404**（`not_found`）。错误体含 `request_id`，不含堆栈/SQL。

家人入组须明确勾选 `join_weather_care`，并填写老人所在地（`elder_location_query` / `location_query`）；不能默认家属当前位置。历史档案不静默开启天气照护（`weather_care_enabled` 默认否）。不按姓名合并既有对象；短窗口内仅同称呼+关系+地点视为重复提交。

## 角色

| 角色 | 读 | 发起求助 | 接收/开始处理 | 结案 | 邀请 | 健康档案 |
|---|---|---|---|---|---|---|
| owner | 是 | 是 | 是 | 是 | 是 | 是 |
| caregiver | 是 | 是 | 是 | 是 | 否 | 是 |
| elder_proxy | 是 | 是 | 否 | 否 | 否 | 是 |
| community_limited | 是（限授权社区） | 否 | 否 | 否 | 否 | 否 |
| doctor_support | 是 | 否 | 是 | 是 | 否 | 否 |
| volunteer | 是 | 否 | 是 | 是 | 否 | 否 |

`CAN_VIEW_HEALTH` 仅 owner / caregiver / elder_proxy。`doctor_support`、`volunteer` 排除在外；`community_limited` 默认没有健康档案权限。

`User.role=doctor` 不是家庭成员角色：只看见 `requested_support_role=doctor` 的工单。管理员不能冒充医生发布（`publish_advice` 拒绝非医生）。

## HelpRequest 状态

`pending_ack` → `acknowledged` → `in_progress`（可选）→ `resolved`  
取消：`cancelled`（需权限与原因码）  
请求协助：仅 `acknowledged` / `in_progress` 可 `request_support`，写 `requested_support_role` 后回到 `pending_ack`（清空跟进人）。**不能从 `pending_ack` 结案**；需先接手。收到求助 ≠ 已解决。

| 状态 | 展示文案 |
|---|---|
| pending_ack | 等待接手；若 `requested_support_role=doctor` 为等待医生接手，若 `volunteer` 为等待协助者接手 |
| acknowledged | 已接手 |
| in_progress | 处理中 |
| resolved | 用结案结果文案（`outcome_label`），不用「已解决」当成功 |
| cancelled | 已取消 |

允许动作：`pending_ack`=`ack`/`cancel`；`acknowledged`=`start`/`resolve`/`cancel`/`request_support`；`in_progress`=`resolve`/`cancel`/`request_support`。

| 结案码 | 成功 | 文案 | 别名 |
|---|---|---|---|
| assisted | 是 | 已协助处理 | `reached_elder`、`action_done` |
| transferred_confirmed | 否 | 已转交并确认接收 | `referred` |
| withdrawn | 否 | 本人撤回 | — |
| unreachable | 否 | 联系不上 | — |
| declined | 否 | 暂不受理 | — |
| false_alarm | 否 | 误报 | — |
| other | 否 | 其他结果 | — |

只有 `assisted` 记为成功（`outcome_success=true`）。别名在写入前规范化（`reached_elder` → `assisted`）。

写操作需要 `expected_version`；冲突 **409** `version_conflict`。  
同一 Pair 最多一条未结（部分唯一索引）。再点求助返回现有记录并记 `remind`。

## HTTP（同一服务）

| 能力 | 网页 | 小程序 |
|---|---|---|
| 能力 | `GET /api/v1/capabilities`（`not_emergency_channel`） | `GET /mp/api/v1/capabilities` |
| 话术 | `GET /api/v1/scripts` | `GET /mp/api/v1/scripts` |
| 启动 | — | `GET /mp/api/v1/bootstrap`（只读天气缓存） |
| 列表 | `GET /api/v1/help-requests` | `GET /mp/api/v1/help-requests` 与 `/pending` |
| 发起 | `POST /api/v1/help-requests` | `POST /mp/api/v1/help-requests`；旧 `POST /mp/api/v1/actions/<id>/help` |
| 接收/处理/结案 | `/ack` `/start` `/resolve` `/cancel` | 同路径 |
| 请求协助 | `POST /api/v1/help-requests/<id>/request-support` | `POST /mp/api/v1/help-requests/<id>/request-support` |
| 邀请 | `POST /api/v1/family-invites`；GET 预览不消费；POST accept 才授权 | 同语义 `/mp/api/v1/family-invites/...` |

## 通知

`NotificationOutbox` 与求助同事务写入。默认 `HELP_NOTIFY_SANDBOX=1` 不向真实用户发 WxPusher。  
服务商接受 ≠ 用户打开 ≠ 家属确认收到 ≠ 已解决。  
能力与序列化均带 `not_emergency_channel=true`：平台求助队列不是实时急救通道。无人接手时保持等待，不会假装已有人响应。

## 迁移

Alembic `0017_family_help_outbox` revises `0016_cooling_verification`。`0018_health_consent_care`（健康同意列）接在家庭/求助迁移之后。当前 head 为 `0019_heat_care_collaboration`（结案结果、医生内容、设备、`requested_support_role`、档案地点/`weather_care_enabled`），revises `0018_health_consent_care`。  
回填：`python scripts/backfill_family_help.py --dry-run`；真正写入需 `--commit`。

## 发布顺序（未授权前不执行）

1. 后端兼容旧新客户端  
2. 数据库迁移与回填演练  
3. 小程序候选包（不上传）  
4. 正式部署另取授权
