# PRD-03 可核验避暑资源路径

状态：APPROVED 2026-09-05 · 实施：Codex（代码）+ 心远（电话核验 5–10 个点）

## 问题

`cooling_resources` 有类型、地址提示、坐标、开放时间、has_ac、is_accessible、contact_hint、is_active，但没有：最后核验时间与方式、高温警报期间是否开放、饮水 / 座椅 / 厕所 / 台阶、是否需要接送、失败反馈。生产环境已核验点 = 0。地图上有点不等于老人当天能进去。

## 数据模型

`cooling_resources` 新增列（全部 nullable，SQLite batch）：

| 列 | 类型 | 说明 |
|---|---|---|
| last_verified_at | datetime | 最后核验时间 |
| verified_by_role | str(16) | student / community / admin（不存姓名） |
| verify_method | str(16) | phone / onsite / official_doc |
| open_during_alert | str(16) | yes / no / unknown / conditional |
| alert_open_note_code | str(32) | 封闭码：same_hours / extended / closed_on_alert / staff_dependent |
| amenities_json | text | 封闭键：ac, water, seats, toilet, step_free, shade；值 true/false/null |
| transport_need | str(16) | walkable / bus / ride_needed / unknown |
| verify_status | str(16) | verified / stale（>30 天）/ unverified / closed_reported，由服务层计算写入 |

新表 `cooling_feedback`（append-only）：`id, resource_id fk, pair_id fk nullable, code str(16) ∈ {reachable, need_ride, closed, not_found}, channel str(24), created_at`。不存自由文本。

## 公开页 `/cooling`

卡片显示：名称 · 类型 · 开放时间 · 「最后核验：YYYY-MM-DD · 电话 / 现场」；未核验点灰显并标「未核验」，排序时核验点优先；设施图标（空调 / 饮水 / 座椅 / 厕所 / 无台阶）；交通标签（步行可达 / 需公交 / 需接送）；高温警报期开放状态。

每张卡片三个反馈按钮（需登录家属或短码老人会话）：「能到达」「需要接送」「已关闭 / 找不到」→ `POST /cooling/<id>/feedback`。closed 反馈 ≥2 条来自不同 pair 且晚于 last_verified_at → verify_status=closed_reported，卡片顶部显示「有用户反馈已关闭，待复核」。need_ride 只进入人工跟进列表，页面文案："我们会记录，不提供接送服务"。

页顶说明：`资源点信息由志愿者电话或现场核验，标注核验日期；未核验点仅供参考。`

## 管理端

`/admin/cooling` 列表增加核验列与「记录核验」表单（method、open_during_alert、amenities 复选、transport_need）。提交即写 last_verified_at=now、verified_by_role=admin。

CLI `scripts/cooling_verify.py`：`--id N --method phone --open yes --ac yes --water yes --seats yes --toilet no --step-free unknown --transport bus`，供本人电话核验时逐条记录；同时把一行追加到 `docs/data/cooling_verification_ledger.csv`（见下）。

## 台账 `docs/data/cooling_verification_ledger.csv`

表头：`verified_at,resource_id,resource_type,township,method,open_during_alert,alert_open_note_code,ac,water,seats,toilet,step_free,shade,transport_need,result_code,notes_code`
`notes_code` 只允许封闭码（no_answer / wrong_number / hours_changed / relocated / ok）。不存电话号码、不存人名。README 一段说明如何用 CLI 追加。

## 缺口清单导出

`GET /analysis/pilot/resource_gaps.csv`（管理员）：

- 每资源点一行：id, type, township, verify_status, last_verified_at, open_during_alert, transport_need, closed_feedback_count, need_ride_feedback_count。
- 文件末尾聚合行（或另一个 `resource_gaps_summary.json`）：unverified_count, verified_count, stale_count, verified_within_7d_ratio, closed_reported_count, need_ride_count, `households_with_one_viable_option_ratio` = 当日 active pair 中，其社区（pairs.community_code）内至少有一个 verify_status=verified 且 open_during_alert∈{yes, conditional} 的资源点的比例。

`/analysis/pilot` 页面增加「资源缺口」区块显示这些聚合值，标注"资源可达性审计，不代表公共服务能力"。

## 测试

- 新列迁移在 SQLite 下可升可降。
- 未核验点灰显与排序。
- 两条不同 pair 的 closed 反馈触发 closed_reported；同一 pair 两条不触发。
- 反馈未登录 401/302；不存自由文本字段。
- households_with_one_viable_option_ratio 计算含边界（无 active pair → null，页面显示 --）。
- CLI 在临时 SQLite 与临时 CSV 上跑通。
