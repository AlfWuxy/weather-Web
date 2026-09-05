# 网页与小程序求助互通契约（长期）

> 实现版本：`2026-09-06.help-family-v1`  
> 基线分支：`feature/web-mp-help-interop-20260905`（从 `release/2026-09-freeze` 拉出）

## 身份适配

| 端 | 认证 | 授权 |
|---|---|---|
| 网页 | Flask-Login session + CSRF | `FamilyMembership` 角色矩阵 |
| 小程序 | 可撤销 `MiniProgramSession` 或存量 `ApiToken` Bearer | 同一矩阵；客户端不可伪造 owner/admin |

无权对象统一 **404**（`not_found`）。错误体含 `request_id`，不含堆栈/SQL。

## 角色

| 角色 | 读 | 发起求助 | 接收/开始处理 | 结案 | 邀请 |
|---|---|---|---|---|---|
| owner | 是 | 是 | 是 | 是 | 是 |
| caregiver | 是 | 否 | 是 | 是 | 否 |
| elder_proxy | 是 | 是 | 否 | 否 | 否 |
| community_limited | 是（限授权社区） | 否 | 是 | 是 | 否 |

## HelpRequest 状态

`pending_ack` → `acknowledged` → `in_progress`（可选）→ `resolved`  
取消：`cancelled`（需权限与原因码）

写操作需要 `expected_version`；冲突 **409** `version_conflict`。  
同一 Pair 最多一条未结（部分唯一索引）。再点求助返回现有记录并记 `remind`。

## HTTP（同一服务）

| 能力 | 网页 | 小程序 |
|---|---|---|
| 能力 | `GET /api/v1/capabilities` | `GET /mp/api/v1/capabilities` |
| 话术 | `GET /api/v1/scripts` | `GET /mp/api/v1/scripts` |
| 启动 | — | `GET /mp/api/v1/bootstrap`（只读天气缓存） |
| 列表 | `GET /api/v1/help-requests` | `GET /mp/api/v1/help-requests` 与 `/pending` |
| 发起 | `POST /api/v1/help-requests` | `POST /mp/api/v1/help-requests`；旧 `POST /mp/api/v1/actions/<id>/help` |
| 接收/处理/结案 | `/ack` `/start` `/resolve` `/cancel` | 同路径 |
| 邀请 | `POST /api/v1/family-invites`；GET 预览不消费；POST accept 才授权 | 同语义 `/mp/api/v1/family-invites/...` |

## 通知

`NotificationOutbox` 与求助同事务写入。默认 `HELP_NOTIFY_SANDBOX=1` 不向真实用户发 WxPusher。  
服务商接受 ≠ 用户打开 ≠ 家属确认收到 ≠ 已解决。

## 迁移

Alembic `0017_family_help_outbox` revises `0016_cooling_verification`。当前 head 为 `0018_health_consent_care`（健康同意列，接在家庭/求助迁移之后）。  
回填：`python scripts/backfill_family_help.py --dry-run`；真正写入需 `--commit`。

## 发布顺序（未授权前不执行）

1. 后端兼容旧新客户端  
2. 数据库迁移与回填演练  
3. 小程序候选包（不上传）  
4. 正式部署另取授权
