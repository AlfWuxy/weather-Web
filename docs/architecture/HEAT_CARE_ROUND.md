# 宜老天气通 · 高温照护回合（产品决定与实现范围）

> 日期：2026-09-06  
> 读者：心远  
> 分支：`cursor/yilao-heat-care-flow-160f`  
> 基线：PR #48（`feature/web-mp-help-interop-20260905`，契约见 `HELP_FAMILY_INTEROP.md`）  
> 范围：只做高温照护协作。不新增灾种。  
> 契约：`2026-09-06.help-family-v2`

本页写清本轮回答了什么、谁负责、记录怎么走、复用了什么、什么没做。不是效果结论，也不是发布授权。

## 1. 本轮回答的产品问题（高温）

都昌访谈与话术预注册里，家属能读预警，转述后老人常当成天气预报，而不是今天要改的一件事。本轮只钉三件事：

1. **为什么提醒**：提醒必须带「今天先做哪一件 + 为什么」，不能只报等级。未由医生账号亲自发布的文案一律标「平台规则提醒」，不得写「徐医生已审核」。
2. **谁传、走哪条通道**：家属是主联系人；熟人口吻与渠道（电话 / 当面 / 微信等）记在行动链上。平台求助队列不是急救通道。服务商接受 ≠ 用户打开 ≠ 家属接手 ≠ 已协助。
3. **跟进卡在哪**：未接手保持 `pending_ack`，不假装已有人响应。跟进责任只在明确接手后落到 `assignee`。请求医生或志愿者协助会清掉当前负责人、回到等待接手；对方未 ack 前不转移责任。失访留在分母。

不做寒潮产品化，不扩台风 / 洪涝等新灾种。`AdviceContent.scenario` 默认 `heat`。

## 2. 角色

| 角色 | 做什么 | 不做什么 |
|---|---|---|
| 家属主联系人（`owner` / `caregiver`） | 建档并勾选加入天气照护；接收 / 处理 / 结案；按需请求协助 | 不能用自己的定位顶替老人所在地 |
| 徐医生 | 解释者、内容审核者、按需接手人。仅当家属提出 `requested_support_role=doctor` 且医生 ack 后成为 `assignee` | 非 24/7；不自动审核每条预警；不对未回复工单自动负责；管理员不能代发「医生建议」 |
| 志愿者 | 家庭邀请授权后可接手、处理 | 未授权前不可见、不可结案 |
| 老人 | 反馈主体：短码 / token / 老人模式 / 授权终端上的看懂、做到、求助 | 不强制注册账号 |

医生工作台只列出明确请求医生接手的未结工单。科普与短提醒模板须医生本人起草并发布；`reviewer_user_id` 须等于作者且角色为 `doctor`，才可标注「徐医生已审核」。

## 3. 共用记录链与 HelpRequest 状态

两条链并存、不混写：冻结的当日 `ActionEvent`（9 个 stage，不再增减）记录「看见 / 看懂 / 做到 / 求助」；长期 `HelpRequest` + `HelpRequestEvent` 记录跨天工单。通知走 `NotificationOutbox`，与求助同事务写入。同一 Pair 最多一条未结。

```
pending_ack 等待接手
    → acknowledged 已接手（写入 assignee）
    → in_progress 处理中（可选）
    → resolved | cancelled
```

请求协助：`acknowledged` / `in_progress` → 再入 `pending_ack`，`assignee_user_id=null`，`requested_support_role` ∈ {doctor, volunteer}。

结案必须带结果码。**只有 `assisted`（已协助处理）算成功。** 其余（`transferred_confirmed` / `withdrawn` / `unreachable` / `declined` / `false_alarm` / `other`）结束工单但不记成功。别名 `reached_elder`、`action_done` 归一到 `assisted`。

**`pending_ack` 不能结案。** 能看见工单的人（家属、被请求的医生）调用 `/resolve` 得到 **409** `invalid_transition`，文案是「需要先接手，才能记录处理结果」。陌生人仍是 **404**，避免用结案接口探测工单是否存在。取消（误触、重复、老人已无事等）是另一条终态，不是成功。无人接手时保持等待，跨天仍出现在未结列表。

## 4. 复用（PR #48）与本轮新增

**沿用：** `HelpRequest` / `FamilySpace` / `FamilyMembership` / 邀请；`NotificationOutbox`（默认 `HELP_NOTIFY_SANDBOX=1`）；冻结 `ActionEvent` 字典与老人三按钮；网页与小程序同一服务、只做认证适配；Alembic `0017_family_help_outbox`、`0018_health_consent_care`。

**本轮加上：**

- 工单：`assignee_user_id`、`requested_support_role`、`proxy_basis`；结果码与成功标记
- 入组：`FamilyMemberProfile.weather_care_enabled`、`location_query`；`enroll_weather_care`（缺老人地点则拒绝，不默认家属当前位置）
- 内容：`AdviceContent`（草稿 → 发布 / 撤回；禁止未核实承诺）
- 终端：`CareDevice` / `DeviceEvent`（只存 token 哈希；联调为模拟核验）
- 路由：`/doctor/*`、`/api/v1/devices`、设备拉内容与上报

## 5. 非目标与暂停核验

本轮**不**：生产部署、微信提审 / 上传、向真实用户发消息、刷固件、把未发布文案写成医嘱、把求助队列当急救、把「转交」或「联系不上」记成成功、用测试数据充当真实使用。

暂停核验（PR #48 未授权项在本轮仍暂停）：正式站迁移、真机合法域名、脱敏生产库演练、真实 WxPusher。设备接口返回 `verification_mode=simulated`，不代替固件证据。

地点：小程序公共天气仍钉**都昌县**研究点（`116.20,29.27`）。网页加入天气照护必须显式填写老人所在地，不能用家属定位或县默认值顶替。合并本分支 ≠ 微信与正式网页已经互通。

## 6. 贡献边界

| 谁 | 负责 | 尚未发生 |
|---|---|---|
| 心远 | 产品问题、角色边界、状态机、结果码、分析单位、非目标 | — |
| AI | 按上述决定改服务、迁移、医生工作台与模拟设备链路 | 不代替现场核验或医生审核 |
| 徐医生 | 用医生账号起草、审核、发布后，文案才可标注医生建议；按需 ack 工单 | **截至本页：无实际审核、无已发布医生内容** |

## 7. 分析单位

只做描述性计数。失访 / 未完成 / `unknown` **留在分母**，不从分母里抠掉，也不记成安全。

| 单位 | 指什么 | 注意 |
|---|---|---|
| 人 | 家属账号；老人作为反馈主体（可无账号） | 不把会话次数写成用户数 |
| 家庭 | `FamilySpace` / 活跃 Pair | 一人多对象要分开算 |
| 提醒 | 投递、话术复制、已发布模板 | 投递 ≠ 看懂 ≠ 做到 |
| 求助工单 | 一条 `HelpRequest` | 成功率只计 `assisted` |
| 测试会话 | 可用性会话 / 模拟设备 | `is_test=true` 默认排除生产漏斗 |

测试数据与真实使用必须分列。`qa_` 前缀与 `is_test` 不进入对外漏斗；需要看测试时显式 `include_test`。本轮没有真实用户消息，不得用 fixture 填效果数字。

## 8. 文件与迁移

`migrations/versions/0019_heat_care_collaboration.py` **revises** `0018_health_consent_care`。

升级内容：`family_member_profiles` 增加 `location_query`、`weather_care_enabled`；`help_requests` 增加 `assignee_user_id`、`requested_support_role`、`proxy_basis`；新建 `advice_contents`、`care_devices`、`device_events`；活跃 Pair 对 `member_id` 部分唯一。

对应实现：`services/help_request_service.py`、`services/care_enrollment.py`、`services/advice_content_service.py`、`services/device_link_service.py`、`blueprints/doctor.py`、`blueprints/device_api.py`。互通契约仍以 `docs/architecture/HELP_FAMILY_INTEROP.md` 为准，本页只覆盖高温照护这一回合的产品决定与落地边界。

## 9. 本轮验证（2026-09-06）

自动化（Flask test client / Node，**不是**真机或真实通知）：

```text
/workspace/.venv/bin/python -m pytest -q --tb=line
# 844 passed, 11 deselected
node --test miniprogram/tests/page-resilience.test.js
```

覆盖的产品场景：家属自行接手并 `assisted` 结案；请求医生后 `pending_ack` 跨天仍未结；可见账号 409、陌生人 404；未审核内容不可归因；设备事件去重且 `verification_mode=simulated`；天气不可用不得复制肯定建议。

**未验证（暂停，不伪造通过）：** 生产部署与正式站迁移、微信提审 / 开发者工具真机、向真实参与者发消息、WxPusher 生产通道、实体终端固件联调、徐医生实际审核发布、真实老人理解或健康改善。

测试与演练数据默认 `is_test` / `qa_` 前缀，不得并入生产漏斗。
