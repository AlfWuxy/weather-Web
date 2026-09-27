# 话术可用性测试会话指南（家属志愿者）

版本 1.0 · 2026-09-05 · 配合 `docs/research/script_test_prereg.md`（先读预注册，再跑会话）。这是可用性研究，不是效果研究；参与者是家属志愿者，不是老人。

## 一次会话（约 15 分钟）

1. 说明与同意（2 分钟）：口头说明目的（"测这段话好不好懂、好不好传"）、只记录角色类别 / 版本 / 渠道 / 封闭码 / 时间，不记姓名与录音；可随时退出并要求删除。志愿者口头同意后开始。
2. 生成测试 pair（志愿者不操作）：
   ```bash
   /opt/anaconda3/envs/case-weather-py312/bin/python scripts/usability_session.py new --role child --version v2_gist_why --scenario heat
   ```
   脚本打印 session_id、短码、老人端 token 链接。测试 pair 带 `is_test`，不进生产漏斗。
3. 任务 A · 复制并转述（志愿者，手机）：打开话术页 → 选自己的角色与渠道 → 复制 → 对着测试人员把这段话用自己平时对老人说话的方式说一遍（可方言）。测试人员只计时，不纠正。
4. 任务 B · 老人端（志愿者扮演老人，用老人端链接）：按「我看懂了」→ 回答"今天你准备先做哪一步？" → 视情况按「我做到一项」或「做不到，需要帮助」。
5. 三个问题（志愿者回答，测试人员选封闭码）：
   - 这段话里哪句你觉得老人听不懂或不会照做？（`unclear_word` / `too_long` / `wrong_tone` / `dialect_mismatch` / `fear_cost` / `other` / 无）
   - 如果让你改一个字或一句，你改哪里？（记 revision_note_code，同上一组码）
   - 你会用哪个渠道真的发给老人？（channel）
6. 结束：`scripts/usability_session.py close --session <id>` 导出该会话事件与耗时到 `docs/research/usability_log.csv`（追加一行）。

## 轮换

每位志愿者最多跑 2 个版本，顺序随机（抛硬币）；同一版本同一志愿者只算第一次。三版各 ≥ 6 次会话为目标，不足照实写。

## 记录字段（`usability_log_template.csv`）

`session_id,date,volunteer_role,script_version,scenario,channel,task,completed,seconds,teachback_correct,misclick,refusal_code,revision_note_code`

- `task` ∈ {copy_relay, elder_understood, elder_teachback, elder_report_or_help}
- `completed` true/false；`seconds` 从任务开始到提交
- `teachback_correct`：选项是否落在当日推荐行动集合（「还没想好」= false）
- 所有 *_code 只允许预注册第 6 节的封闭码

## 不做

- 不让志愿者提供老人的真实信息。
- 不在真实高温日打扰任何老人做测试。
- 不把会话次数写成"用户数"。
