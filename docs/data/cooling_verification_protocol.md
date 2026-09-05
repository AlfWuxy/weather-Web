# 避暑资源点核验规程（电话 / 现场）

版本 1.0 · 2026-09-05 · 适用于 PRD-03。目标：在申请季前核验 5–10 个都昌资源点，让 `/cooling` 上每个亮起的点都有"最后核验时间 + 方式"。

## 原则

- 只记录封闭码与日期，不记录接电话者姓名、私人手机号、任何老人信息。
- 核验的是"高温警报当天老人能不能用"，不是"这个地方存在不存在"。
- 打不通、答不上、拒绝回答都是有效结果，照实记 `no_answer` / `unknown`。
- 每个点最多打 2 次（间隔 ≥ 1 天），仍无结果就标 `unverified`，不再追。

## 电话核验问题（按顺序，5 分钟内）

| # | 问题 | 记录到字段 | 允许值 |
|---|---|---|---|
| 0 | 开场：我是宜老天气通志愿者，想确认高温天老人能否到您这儿歇凉，占用两分钟。 | — | — |
| 1 | 平时开门时间？ | `open_hours`（已有） | 文本时段 |
| 2 | 气象局发高温预警（橙色 / 红色）那天，开不开？时间一样吗？ | `open_during_alert` / `alert_open_note_code` | yes / no / unknown / conditional；same_hours / extended / closed_on_alert / staff_dependent |
| 3 | 里面有空调吗？中午开吗？ | `amenities.ac` | true / false / null |
| 4 | 有免费饮水（开水或桶装水）吗？ | `amenities.water` | true / false / null |
| 5 | 有能坐下歇一会儿的椅子 / 长凳吗？ | `amenities.seats` | true / false / null |
| 6 | 有厕所吗？ | `amenities.toilet` | true / false / null |
| 7 | 进门有没有台阶？拄拐或轮椅能进吗？ | `amenities.step_free` | true / false / null |
| 8 | 门口有树荫或遮阳吗？ | `amenities.shade` | true / false / null |
| 9 | 附近老人一般怎么过来？走路 / 公交 / 要人送？ | `transport_need` | walkable / bus / ride_needed / unknown |
| 10 | 结束：谢谢，我们只标注日期和方式，不写您的名字。 | — | — |

现场核验额外看：门牌与地图位置是否一致（不一致 → `notes_code=relocated`）、门口有没有"避暑 / 纳凉"标识、最热时段（13:00–16:00）实际开着门的人数是否 ≥1。

## 结果码

`result_code`：verified / unverified / closed / relocated
`notes_code`：ok / no_answer / wrong_number / hours_changed / relocated / refused

## 记录方式

首选 CLI（写库 + 追加台账）：

```bash
/opt/anaconda3/envs/case-weather-py312/bin/python scripts/cooling_verify.py \
  --id 12 --method phone --open yes --alert-note same_hours \
  --ac yes --water yes --seats yes --toilet no --step-free unknown --shade yes \
  --transport bus --result verified --note ok
```

备用：直接在 `docs/data/cooling_verification_ledger.csv` 追加一行（表头见该文件），随后由管理员在 `/admin/cooling` 录入。

## 目标清单（心远填写，按乡镇分散）

先选：县城 2 个（社区服务中心 / 图书馆或文化馆）、乡镇卫生院 2 个、村委会或党群服务中心 3 个、有空调的大型商超 1 个、寺庙或祠堂等老人常聚点 1–2 个。理由：这些是老人本来就会去的地方，"有空调的地方"只有老人愿意去才算资源。
