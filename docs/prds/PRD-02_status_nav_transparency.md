# PRD-02 状态与边界页、导航收口、透明度分层

状态：APPROVED 2026-09-05 · 实施：Codex

## 问题

首页与"更多"菜单是功能超市（GIS / ML / AI 问答 / 用药 / 健康日记 / 慢病），核心链被淹没。`/transparency` 把约 40 个指标平铺，DLNM / RandomForest / O-E / Gi* 与行动指标并列。外部读者点开链接 30 秒内看不出：问题是什么、原型做什么、测过什么、数据从哪来。

## 目标

1. 新页 `/status`（"项目状态与边界"），成为申请材料 Additional Info 里唯一链接。
2. 主导航只留四项，其余收进一个"研究与方法（探索性）"入口。
3. `/transparency` 分两层：行动链指标（核心）在上；探索性模型（研究中）折叠在下。

## `/status` 页规格

路由 `GET /status`（`blueprints/public.py`，公开，无需登录，robots 允许）。模板 `templates/status.html`。内容来自 `core/status_content.py`（Python 常量，便于测试断言），不是数据库。

页首（英文，恰好 3 行，每行 ≤ 140 字符）：

```
Prototype heat-warning tool for older adults in Duchang County, Jiangxi, relayed through family members. Not a clinical or emergency service.
Currently at feasibility stage: the elder action chain, messenger-script test, and cooling-site verification are being built and tested with volunteers.
No claims about users, adoption, or health outcomes. Everything verified, prototyped, or untested is listed below. Source: GitHub link at the bottom.
```

正文（中文）分栏，每条一行，带阶段标签（`basic` / `feasibility` / `efficacy` / `effectiveness` / `impact`，全站当前只用前两级）：

- 一句话：`宜老天气通是一个原型：把高温、寒潮预警翻译成老人今天能做的少数几件事，由家属转述，老人用三个按钮回应。仍是原型，未被采用（still a prototype, not adopted）。`
- 已核验（VERIFIED）：官方预警与天气来源（和风 / Open-Meteo，含 fail-closed）；1 km 热暴露 GIS 的数据口径（MODIS LST、65+ 人口比例）与复核门槛；卫生室就诊—气象关联的离线分析边界。
- 原型中（PROTOTYPE · feasibility）：老人三按钮行动链；家属核验 / 求助接收 / 结案；避暑资源核验字段与反馈；话术版本记录与 teach-back；小程序家属端。每项注明"合成用户 / 志愿者可用性测试；无真实高温期数据"。
- 未验证（UNVERIFIED · HOLD）：老人是否因预警而行动；家属转述是否提高理解；任何健康结局；资源点在高温警报期的真实开放情况。
- 不做（NO-GO）：个体疾病预测；AI 自动健康建议；医疗判断；宣称用户数、覆盖村数、部署数。
- 数据从哪来：天气 / 预警来源；健康数据仅离线去标识汇总；行动链事件为匿名 pair 级别，不含姓名、电话、自由文本；流量统计 2026-08-12 至 08-16 含自动化测试，不代表使用。
- 版本：显示 `git describe --tags --always`（启动时读取一次，失败则显示 `dev`）与构建日期。
- 页脚：GitHub 仓库链接；`/transparency`；`/transparency#privacy`。

禁止出现的词：Cornell、任何大学名、课号、users、deployed、adopted、launched、已保护、已覆盖、任何访问量数字。测试断言这些词不出现在 `/status` 与首页 HTML。

## 导航

`templates/base.html`（或对应布局）主导航改为：今天（/risk 或首页锚点）· 行动（/action）· 避暑资源（/cooling）· 状态与边界（/status）。登录后追加：家庭照护（/pairs）。

"更多"下拉改名"研究与方法（探索性）"，内含：热暴露 GIS、社区风险、机器学习分类、7 天预测、慢病评估、AI 问答、用药提醒、健康日记、透明度。每项后缀「探索性」小标。

首页 `templates/index.html`：Hero 下方"四步"保留；把 GIS 地图区块缩为一行链接进入研究区；新增一行边界句（同 `/status` 一句话）；页脚加 GitHub 链接与 `/status`。移除任何数字化"成果"文案（如有）。

## `/transparency` 分层

`templates/transparency.html` 结构改为：

1. 顶部说明 + 目录。
2. 「行动链指标（核心）」：understood_rate、self_report_rate、verified_rate、help_ack_median_minutes、open_help_count、unknown_count、event_source_completeness、升级触发率、求助率（PRD-01 口径）。每项加阶段标签 feasibility。
3. `<details>` 折叠「探索性模型（研究中）」：热风险评分、7 天预测、个人健康、机器学习、社区空间筛查、GIS、模型可靠性。每项加标签 `basic` 或 `feasibility`，并保留原有"仍需验证"段。
4. 「输入从哪里来」「系统会保存什么」「隐私」保留；隐私段新增 PRD-01 的保留周期与撤回规则。

不改任何指标的定义或公式。

## 测试

- `/status` 200；包含英文 3 行；不含禁用词。
- 首页 HTML 不含禁用词；主导航恰有四个一级项（匿名）。
- `/transparency` 行动链区块在探索性区块之前。
- `git describe` 失败时页面仍 200 并显示 `dev`。
