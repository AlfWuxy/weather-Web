# 宜老天气通 · 网站侧终端契约

> 范围：本仓库已实现的 **HTTP 终端接口**（`blueprints/device_api.py`、`services/device_link_service.py`）。  
> 验证状态：`verification_mode=simulated`。在对应固件、实体设备与设备侧日志齐备之前，不得把联调结果写成「已真实验证」。  
> 本文件不描述 MQTT、OTA、串口、厂商云或固件内部 API——那些接口不在本仓库。仓库外的真实固件改动须 **心远确认** 后才能当作产品契约。

实现版本以当前代码为准。事件名、字段语义以本文冻结；不要把终端事件并入网页 `ActionEvent` 行动链。

---

## 1. 两条通道，不要混用

| 通道 | 调用方 | 认证 | CSRF | 路径前缀 |
|---|---|---|---|---|
| 照护端授权 | 已登录家属网页（非游客） | Flask-Login session；`manage` 权限 | **需要**（`X-CSRF-Token` / `csrf_token`） | `/api/v1/devices` |
| 设备端拉取与上报 | 终端持 `device_token` | `Authorization: Bearer <device_token>`（或 `X-Device-Token`） | 设备无登录会话；见第 8 节联调条件 | `/device/api/v1/` |

游客（`@reject_guest`）不能授权或撤销设备。无权对象统一 **404** `not_found`，避免枚举 `pair_id` / `public_id`。

设备表 `care_devices` **只存 token 哈希**，不含健康档案、姓名、疾病、电话。明文 `device_token` **只在授权响应里出现一次**，服务端不回读。

---

## 2. 验证模式（强制）

所有授权、撤销、内容与事件成功响应都带 `verification_mode: "simulated"`。

含义：

- 网站可以模拟「已授权终端、可拉内容、可收事件」。
- 这 **不等于** 固件已接入、设备已在现场、老人已听到、防护已发生。
- 在固件 + 实体设备 + 设备侧日志三类证据齐备、并经心远确认之前，保持 simulated，不得改成 production / verified。

---

## 3. 照护端：授权与撤销

### 3.1 `POST /api/v1/devices`

登录 + CSRF + 非游客。JSON body：

| 字段 | 必填 | 说明 |
|---|---|---|
| `pair_id` | 是 | 整数，照护对象。无法解析则 **400** `invalid_pair_id` |
| `label` | 否 | 最长 80；缺省 `home-terminal` |

需要对该 Pair 的 **`manage` 权限**（家庭空间 `owner`，或尚未回填空间时的原 caregiver；`admin` 可过）。医生支援 / 志愿者 / 社区只读角色不能授权。

成功 **200**：

```json
{
  "success": true,
  "verification_mode": "simulated",
  "data": {
    "id": "<public_id>",
    "pair_id": 1,
    "label": "home-terminal",
    "authorized_at": "<ISO-8601>",
    "revoked": false,
    "verification_mode": "simulated",
    "online": false,
    "last_heartbeat_at": null,
    "last_seen_at": null,
    "unconfirmed": true,
    "device_token": "<明文，仅此一次>",
    "token_note": "明文只显示一次，请保存在设备侧。"
  }
}
```

`data.id` 即后续路径里的 `public_id`（32 位 hex）。之后任何接口都不再返回 `device_token`。

### 3.2 `POST /api/v1/devices/<public_id>/revoke`

登录 + CSRF + 非游客 + 对该设备所属 Pair 的 `manage`。

成功后 `revoked_at` 写入。重复撤销幂等，仍返回当前序列化结果。

撤销后的设备侧效果（由 `device_by_token` 拒绝已撤销记录实现）：

- `GET /device/api/v1/content` → **401** `unauthorized`
- `POST /device/api/v1/events` → **401** `unauthorized`

**撤销即停止内容。** 已发出去的缓存不由本仓库回收；网站侧不再提供新模板。

---

## 4. 设备端：内容与事件

限流：内容 `60/minute`，事件 `120/minute`。缺失、错误或已撤销 token → **401**：

```json
{"success": false, "error": "unauthorized", "verification_mode": "simulated"}
```

### 4.1 `GET /device/api/v1/content`

Header：`Authorization: Bearer <device_token>`。

成功时更新 `last_seen_at`，**不会**自动写入 `content_pulled` 事件；若要记拉取，设备须另报 `POST .../events`。

响应 `data`：

| 字段 | 含义 |
|---|---|
| `verification_mode` | 恒为 `simulated` |
| `server_time` | 服务器 UTC |
| `pair_id` | 内部对象编号，**不是**姓名 |
| `templates` | 与网页同一套已发布短提醒，见第 6 节 |
| `notes` | 固定说明：不含姓名/疾病/联系方式；拉取 ≠ 听到；真实联调需固件、设备、日志 |

每条 template：

| 字段 | 来源 |
|---|---|
| `content_id` | `AdviceContent.public_id` |
| `version` | 与医生工作台 `/doctor/content` 同一 `version` |
| `scenario` | 当前实现固定拉 `heat` |
| `body` | 已发布模板正文 |
| `source` | 来源标注 |
| `doctor_attributed` / `attribution_label` | 与网页 `serialize_advice` 相同规则 |
| `valid_until` | 有效期截止 |

**禁止出现：** 老人姓名、疾病、电话、用药、健康档案、家属联系方式。本接口也不返回 `title` 以外的网页私密字段；当前实现甚至不把 `title` 下发给设备，只给 `body`。

**拉取成功 ≠ 老人听到。** 服务器把模板交给持 token 的调用方，不证明扬声器播放、不证明在场、不证明理解。

### 4.2 `POST /device/api/v1/events`

JSON body：

| 字段 | 必填 | 说明 |
|---|---|---|
| `client_event_id` | 是 | 设备侧唯一编号，最长 64。缺则 **400** `missing_client_event_id` |
| `event_name` | 是 | **仅允许**第 5 节五值。其它 **400** `invalid_event_name` |
| `device_occurred_at` | 否 | 设备本地发生时刻，ISO-8601；无时区按 UTC。解析失败则存 `null` |
| `payload` | 否 | 必须是 JSON **对象**；否则 **400** `invalid_payload`。不要放姓名/电话/病名 |

成功 **200**：

```json
{
  "success": true,
  "data": {
    "accepted": true,
    "created": true,
    "event_name": "heartbeat",
    "client_event_id": "...",
    "device_occurred_at": "<ISO-8601 或 null>",
    "server_received_at": "<ISO-8601>",
    "not_action_completed": true,
    "verification_mode": "simulated"
  }
}
```

`not_action_completed` **恒为 true**：网站收妥日志，不表示防护完成。

幂等：同一 `device_id` + `client_event_id` 已存在时 **不插入新行、不重复计数**，`created=false`，返回首次那条的 `event_name` / 时间戳。心跳去重仍会刷新 `last_seen_at` / `last_heartbeat_at`（在线判定用），但 `device_events` 行数不增加。

---

## 5. 事件名（封闭集合）

只允许：

| `event_name` | 设备能力（可以记） | 明确 **不是** |
|---|---|---|
| `heartbeat` | 设备在本次请求时仍能连上网站 | 老人在家、老人安全、健康改善 |
| `button_pressed` | 某按键被按下（硬件能力） | 已采取防护行动；不是 `ActionEvent.self_reported` / `understood` |
| `tts_command_sent` | 设备已发出 TTS 指令（命令已送出） | 播放完成、老人听到、老人听懂 |
| `sos_hold` | 长按/保持 SOS 手势被设备记录 | 已创建 `HelpRequest`；家属已收到；救援已出发 |
| `content_pulled` | 设备声称已拉取内容 | 内容已播报；老人已听到 |

事件名只反映 **设备真实能力**，不把按键记成防护行动。终端事件写入 `device_events`，**不**写入 `action_events`。

语义红线（必须写进联调记录与产品文案）：

1. **服务器收到日志 ≠ 老人听到。** `accepted` / `created` 只说明网站落库。
2. **`tts_command_sent` ≠ 播放完成。** 没有 `playback_completed` 事件；本仓库也不接受自造播放完成名。
3. **`button_pressed` ≠ 已采取防护行动。** 不得据此提高「已防护」计数或关闭当日风险。
4. **重复 `client_event_id` 不重复计数。** 去重键是 `(device_id, client_event_id)`，不是事件名。
5. **时间与在线状态必须分列**，见下一节。

---

## 6. 时间戳、在线、未确认

| 字段 | 谁产生 | 含义 | 不含义 |
|---|---|---|---|
| `device_occurred_at` | 设备声称 | 设备侧发生时刻（可空、可与服务器时钟不一致） | 服务器已核验该时刻为真 |
| `server_received_at` | 网站 | 首次成功写入该 `client_event_id` 的 UTC 时刻 | 设备当时在线；老人当时在场 |
| `last_seen_at` | 网站 | 最近一次内容拉取或任意事件上报 | 心跳健康 |
| `last_heartbeat_at` | 网站 | 最近一次名为 `heartbeat` 的上报（含去重刷新） | 老人状态 |
| `online` | 网站计算 | `last_heartbeat_at` 或 `last_seen_at` 在 **180 秒**内 | 老人安全；内容已送达耳朵 |
| `unconfirmed` | 网站计算 | **未撤销** 且 **当前不算 online** | 设备损坏；老人失联已证实 |
| `revoked` | 网站 | 照护端已撤销授权 | 硬件物理销毁 |

展示与统计必须同时保留这些字段，禁止把 `server_received_at` 显示成「已播报」，禁止把 `online` 显示成「老人正常」。

---

## 7. 与网页提醒同一内容版本

设备模板来自 `services.advice_content_service.active_templates('heat')`：

- `kind=template`
- `status=published`
- `scenario=heat`
- 当前时刻落在 `valid_from`～`valid_until`
- 必须有 `reviewer_user_id`（医生本人审核发布）

这与医生工作台发布的 **AdviceContent 已发布短提醒** 是同一批记录、同一个 `version`。网页撤回（`POST /doctor/content/<id>/withdraw`）后，该版本不再进入 `active_templates`，设备下次拉取也拿不到。

设备 **不得** 使用另一套文案、另一版本号或固件写死的医疗承诺。禁止词与网页草稿/发布扫描相同（如「保证安全」「必须喝」等），见 `FORBIDDEN_CLAIM_MARKERS`。

---

## 8. 真实联调条件（未齐备则保持 simulated）

同时满足以下条件之前，只允许网站侧模拟验证，不能对外声称「终端已联通」：

1. **本仓库 HTTP 契约不变**：仅使用本文四个端点与五个事件名；不新增 MQTT topic、不自造固件私有 URL。
2. **实体设备**：可指认的样机（型号、序列或标签），绑定到测试 Pair 的 `public_id`。
3. **固件**：能保存一次性 `device_token`，按 Bearer 拉内容、上报事件；固件源码/构建 **不在本仓库**。任何固件协议增删须 **心远书面确认**。
4. **设备侧日志**：能对照 `client_event_id`，区分「命令已送出」与「播放完成」（后者本仓库无接口，日志里若有也不得映射成网站事件）。
5. **撤销抽检**：revoke 之后内容接口必须 401，设备不再读到新模板。
6. **隐私抽检**：内容 JSON 不含姓名、疾病、电话；事件 `payload` 同样不含。
7. **幂等抽检**：同一 `client_event_id` 重放，`device_events` 不增行，`created=false`。
8. **语义抽检**：不得用 `button_pressed` / `tts_command_sent` / `content_pulled` 去更新防护完成率或「老人已听到」。
9. **CSRF 与会话**：照护端授权/撤销必须带登录 CSRF。设备端无 cookie。当前全局 CSRF 钩子（`core/hooks.py`）只豁免 `/mp/api/`；真实设备 `POST /device/api/v1/events` 若被 400 CSRF 拦住，属 **网站豁免范围待确认**，改钩子仍须心远确认，且不得顺便发明新设备 API。
10. **内容版本抽检**：设备 `templates[].version` / `content_id` 与当时 `/doctor/content` 已发布模板一致。

缺任一项：保持 `verification_mode=simulated`，联调记录写「未证实」。

---

## 9. 本仓库明确没有的东西

不要在对接文档或固件 issue 里当作已存在：

- MQTT / WebSocket / 长连接推送
- 固件 OTA、配网、Wi-Fi 配参 API
- 播放完成、音量、TTS 引擎状态接口
- 把终端事件写入 `action_events` 的通道
- 设备主动创建求助 `HelpRequest`
- 内容推送（只有设备来拉）
- 再次查询明文 `device_token`
- 生产级 `verification_mode`

网站侧最小联动到此为止。扩展固件行为属于仓库外工作，须心远确认后再改本文。
