# PRD-04 话术版本、传递者角色、渠道与 teach-back

状态：APPROVED 2026-09-05 · 前置：`docs/research/script_test_prereg.md` 已写定（同批提交）· 实施：Codex

## 问题

系统预设"家属是有效信使"，但从未记录：谁传、用哪版话、走什么渠道、老人是否复述得出下一步。网页 `caregiver_wechat_template.html` 与小程序 `miniprogram/pages/template/index.js` 各自硬编码文案，复制事件 `template_copy` 不带版本。

## 单一话术源

新建 `core/scripts.py`（或 `services/messaging/scripts.py`）：

```python
SCRIPT_VERSIONS = {
  "v1_official": {...},   # 官方转述型
  "v2_gist_why": {...},   # 要点先行 + 说明为什么
  "v3_kin_time": {...},   # 熟人口吻 + 具体时段 + 电费安抚
}
DEFAULT_SCRIPT_VERSION = "v2_gist_why"
```

每个版本包含 heat / cold / normal 三种情境的模板，占位符：`{elder_call}`（称呼，家属自填，不入库）、`{tmax}`、`{window}`（如 11:00–16:00）、`{action_1}`、`{action_2}`、`{callback_time}`。文案正文见预注册文档附录，逐字复制，不改写。

网页与小程序都从同一来源取文案：网页直接 import；小程序通过 `GET /mp/api/v1/scripts` 拉取（带版本号与哈希），本地缓存一天，不再硬编码。

## 记录字段

- `usage_events.meta_json` 在 `template_view` / `template_copy` 事件里必须带：`script_version`、`messenger_role`、`channel`、`scenario`（heat/cold/normal）。
- `alert_deliveries` 新增列 `script_version`（推送正文用的版本）。
- `action_events.script_version`：delivered（manual 与 wxpusher）与 seen 事件携带；seen 从当日最近一条 delivered 继承。
- messenger_role 枚举：child / grandchild / spouse / neighbor / village_cadre / village_doctor / self。只存类别。
- channel 枚举：wechat_text / wechat_voice / phone_call / in_person / wxpusher。

家属复制话术前必须选 messenger_role 与 channel（默认记住上次选择，存 localStorage / 小程序 storage，不入用户表）。版本选择器默认 DEFAULT，允许切换；切换写 `template_switch_version` 事件。

## teach-back

老人按「我看懂了」后，同页出现单选："今天你准备先做哪一步？" 选项 = 当日行动清单（3–4 项）+ 「还没想好」。选择写 `action_selected` 事件，meta.teachback_action_id。理解正确率定义（预注册）：teachback 选项 ∈ 当日推荐行动集合的比例；「还没想好」计入分母、不计入分子。

## 分析

`/analysis/pilot` 新增「话术与传递者」区块：按 script_version × messenger_role × channel 交叉的：复制次数、delivered 数、understood 率、teach-back 正确率、self_report 率、help 率、误触疑似数。最小单元格 n<5 显示 `<5`。页首重申"描述性；版本间差异不能推断因果"。导出 `GET /analysis/pilot/scripts.csv`。

## 可用性测试支持（志愿者，非老人效果研究）

`scripts/usability_session.py`：为一名家属志愿者生成测试 pair（`qa_usability_` 前缀，隔离标记 `is_test=true`，不进生产漏斗），打印短码与 token 链接，会话结束时导出该 pair 的事件与耗时。测试 pair 在漏斗查询中默认排除，`/analysis/pilot?include_test=1` 可显示。

`docs/research/usability_log_template.csv`：`session_id,date,volunteer_role,script_version,channel,task,completed(bool),seconds,misclick(bool),refusal_code,revision_note_code`。refusal_code / revision_note_code 只允许封闭码（too_long / unclear_word / wrong_tone / dialect_mismatch / fear_cost / other）。

## 测试

- 三个版本 × 三个情境渲染无缺占位符。
- 小程序脚本接口返回与网页一致的哈希。
- template_copy 缺 messenger_role 或 channel 时 400。
- seen 事件继承当日 delivered 的 script_version。
- 交叉表 n<5 遮蔽。
- 测试 pair 默认排除。
