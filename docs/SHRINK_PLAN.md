# 代码精简方案（功能零变化）

> 版本：2026-09-25 · 状态：待审核 · 基线：`main@60ef21f`
> 已完成部分见 PR #52（分支 `claude/vigilant-edison-8dqovj`）

本文面向审核者（人或 AI）。它自成一体：背景、硬约束、证据、每一步怎么做、怎么证明没改坏，以及需要产品负责人决定的事项，都写在这里。

---

## 1. 背景

- 项目：面向都昌县老年人高温风险的 Flask 网站，另有微信小程序入口。技术栈：Flask 2.3、SQLAlchemy、Jinja2、原生 JS，默认使用 SQLite。
- 规模（自有代码，不含 vendor）：Python 约 1.8 万行，模板 58 个约 1.4 万行，CSS 约 6000 行，JS 约 2700 行，路由 131 条。
- 目标：减少代码量和重复，降低长期维护成本。
- **硬约束：用户可感知的一切都不能变。**

## 2. "功能零变化"的精确定义

下列任何一项变化都算违约：

| 类别 | 必须保持不变的内容 |
|---|---|
| 地址 | URL、HTTP 方法、endpoint 名（`url_for` 依赖它） |
| 输出 | 每个页面的 HTML、每个接口的 JSON、状态码、重定向目标 |
| 权限 | 登录要求、角色校验、提示信息（flash） |
| 限流 | 限流规则，以及计数键（Redis 里正在计数的桶不能换名） |
| 数据 | 对数据库的写入内容 |
| 文件 | 导出文件的类型与内容结构（PDF、Excel、CSV） |
| 前端 | 页面像素级外观、交互脚本行为、浏览器控制台无新增报错 |

**允许变化的只有代码的内部组织。** 确实需要改输出的情况（例如修一个既有 bug），必须单独开 PR，并在 PR 上打 `behavior-change` 标签，由产品负责人明确批准。

## 3. 安全网：先锁住行为，再动代码

### 3.1 行为锁（`scripts/behavior_lock.py`，已上线）

- 用固定的种子数据（3 个村、4 类用户、480 条门诊、150 天天气、12 条预警、照护关系、避暑点等），把时间冻结在 2026-07-20 10:00（北京时间），断开所有外网，并固定 Python 哈希种子。
- 分两种环境录制：`demo`（演示天气、默认功能开关）和 `live`（带"在线"来源标记的天气缓存、全部功能开关打开）。
- 以 5 种身份（游客、普通用户、照护人、社区、管理员）访问：
  - 全部可枚举的 GET 路由（路由参数用种子里的对象填充）；
  - 22 个带筛选参数的页面变体（6 个分析页的分层、分箱、滞后、日期倒置、非法输入，以及试点看板的天数参数）；
  - 14 个 JSON 写接口，以及 3 个报告导出表单。
- 每次请求前把数据库恢复到种子状态，确保请求之间互不影响。
- 共 1230 个响应。规范化 CSRF token、静态资源版本号和代码目录路径后逐字对比。
- CI：每个 PR 自动用 `origin/<base>` 的代码录一份、用 PR 的代码录一份，再做 diff。加了 `behavior-change` 标签的 PR 跳过这一步。
- 有效性已验证：
  - 连续录制两次结果完全一致；
  - 在一条提示文字末尾加一个句号，会报出 22 个响应变化，并定位到具体行；
  - 覆盖率：`blueprints/analysis.py` 82%，`blueprints/api.py` 95%。

### 3.2 路由清单锁（`tests/test_route_manifest.py`，已上线）

131 条路由的（URL、方法、endpoint 名）存成清单文件，缺一条或多一条都会让测试失败。

### 3.3 专项对比（按需）

- **限流**：行为锁会关闭限流，所以在改动路由注册方式时，单独让新旧代码跑同一组请求序列，对比状态码序列（包含 429）和计数键。P2 已做过，两边完全一致。

### 3.4 已知盲区，以及由哪个阶段补上

| 盲区 | 影响的阶段 | 补强措施 |
|---|---|---|
| 表单 POST 流程（老人短码绑定、打卡、求助、复盘，照护人和社区的操作） | P4 | P4 动手前，先把这些流程加入行为锁，并记录请求后数据库的变化（见 4.1） |
| 数据库写入不在对比范围内 | P4 | 行为锁新增"请求后数据库差异"快照 |
| 浏览器端 JS 行为和页面外观 | P5 | 新增像素锁和 DOM 对比（见 5.1） |
| 定时任务、CLI、离线脚本 | P6 | 这些脚本不改逻辑，只做去留决策 |
| 真实外部 API 的返回 | 全部 | 不在本方案改动范围内，不触碰天气抓取逻辑 |
| 单一时间点（冻结在 7 月） | 全部 | 行为锁可增加一个冬季时间点的录制，成本约 +7 秒 |

---

## 4. 已完成（PR #52）

| 阶段 | 内容 | 结果 |
|---|---|---|
| P0 安全网 | 行为锁、路由清单锁，接入 CI | CI 上已跑通：1230 个响应，缺失 0、变化 0、新增 0 |
| P1 死代码 | 删除 4 个已执行完的一次性改码脚本；删除 `api_service.py` 中 36 个从未被调用的包装函数 | -760 行 |
| P2 API 注册表 | `blueprints/api.py` 改为声明式表格加注册循环；endpoint 名、视图函数全名、登录、限流都不变 | 287 行减到约 90 行；限流专项对比一致 |
| P3 分析蓝图 | 25 个纯统计函数移到 `services/analysis_stats.py`；新增 `admin_route` 装饰器替换 11 处重复校验；6 个分析页共用筛选、日期、查询、阈值、基线逻辑 | `analysis.py` 3364 行减到 2599 行 |

产品代码合计净减 1223 行。原有 417 个测试全部通过。

---

## 5. 待执行阶段

每个阶段单独开一个 PR，可以单独回滚。

### P4 服务层去重（预计 -250 到 -400 行）

**4.1 前置：先补安全网**
- 行为锁增加表单 POST 流程：短码绑定、`/e/<token>/checkin`、`/help`、`/debrief`，照护人的 `pairs/<id>/escalate`、`backup`、`action-log`，家庭成员和用药提醒的增删改，管理后台的增删改。
- 新增"请求后数据库差异"：每个写请求执行后，导出所有表的行（去掉自增 id 和时间戳字段之外的非确定字段），纳入对比。

**4.2 改动清单**

| 编号 | 位置 | 问题 | 做法 |
|---|---|---|---|
| 4a | `services/public_service.py` 与 `services/user/_common.py`、`_helpers.py` | `_action_plan`、`_risk_level_value`、`_build_recent_series` 三个函数整段复制，逐语法树比对完全一致 | 删除 `public_service.py` 里的副本，改为导入 |
| 4b | 同上 | `_refresh_community_daily`、`_short_code_expires_at` 两个函数同名，写法不同，但结果等价（论证见附录 C） | 保留 `services/user` 版，删除 `public_service.py` 里的副本；由行为锁的数据库差异对比最终确认 |
| 4c | `services/user/caregiver_service.py` 与 `community_service.py` | "取天气、判断在线、算连续高温、算热风险"这一段约 40 行重复 | 抽成一个共用函数 |
| 4d | `services/public_service.py` 内部 | 短码表单处理重复 3 次，动作页渲染重复 3 次 | 抽成内部辅助函数 |
| 4e | `services/user_service.py`（48 行兼容层） | 只被 `blueprints/user.py` 使用，且导出列表与 `services/user/__init__.py` 重复 | `blueprints/user.py` 改为直接导入 `services.user`，删除兼容层 |
| 4f | `services/user/dashboard_service.py` 内部 | 查询最近预警的代码重复一次（约 18 行） | 合并 |
| 4g | `services/pipelines/sync_weather_cache.py` 与 `sync_weather_data.py` | 写入或更新记录的逻辑约 13 行重复 | 合并 |
| 4h | 静态分析标记为"无调用"的 14 个函数或方法（清单见附录 A） | 疑似死代码 | 按"三证据规则"逐个核实（见第 7 节），证据齐全才删除 |

**验证**：行为锁（含新增的 POST 流程和数据库差异）零差异；路由清单锁通过；全部测试通过。

### P5 前端去重（预计 -600 到 -900 行）

**5.1 前置：先补安全网**
- **DOM 对比模式**：先把 HTML 解析成 DOM，再忽略标签之间无意义的空白后对比。Jinja 宏重构只会改变空白，用这个模式可以证明结构完全一致。
- **资源内联对比**：对比前，把页面引用的本地 `.js`、`.css` 文件内容内联回页面。这样"把内联脚本原样搬到静态文件"这类改动，规范化后的结果完全一致，不需要人工判断。
- **像素锁**：环境里自带 Chromium，用 Playwright 截图，覆盖约 20 个关键页面，每页 3 种宽度（390 手机、768 平板、1440 桌面）。截图前注入样式关闭动画，并等待字体和图表渲染完成。像素差异容忍度为 0；抗锯齿导致的极少量差异会单独列出，交给人工复核。
- **浏览器控制台**：记录每个页面的 JS 报错和失败的请求，新旧版本必须一致。

**5.2 改动清单**（依据：跨模板重复检测，约 1700 行位于重复片段中）

| 编号 | 位置 | 做法 |
|---|---|---|
| 5a | 4 个病例分析页、2 个预警页的筛选表单（csrf、社区、病种、分层、最少样本天数等下拉框，成对重复 20 到 38 行） | 抽成 Jinja 宏文件 `templates/_macros/analysis_filters.html` |
| 5b | `action_checkin.html` 与 `caregiver_pair_detail.html` 的风险趋势图脚本（约 60 行相同） | 抽成 `static/js/risk-trend-chart.js` |
| 5c | `action_checkin.html` 与 `risk.html` 的高温阈值调节脚本（约 43 行相同） | 抽成 `static/js/heat-threshold.js` |
| 5d | 管理后台 3 组"新增/编辑"成对模板（用户、社区、避暑点） | 每组合并成一个表单模板，用参数区分新增和编辑。路由仍渲染原来的模板名，由原模板 include 共用部分 |
| 5e | `caregiver_wechat_template.html` 与 `community_wechat.html` | 抽出共用片段 |
| 5f | `admin_dashboard.html` 与 `admin_statistics.html` 的图表配置 | 抽出共用的图表配置 |
| 5g | 7 个分析页的内联样式（约 1100 行）。每页用自己的前缀重写了卡片、表格、徽章 | 只合并去掉前缀后完全相同的规则，放进 `static/css/analysis.css`；由像素锁把关 |

**不做**：不引入前端框架或构建工具，不修改任何视觉设计。

### P6 离线脚本去留（需要决策 D1，最多 -1600 行）

- `services/pipelines/` 下有 5 个训练脚本，共 1440 行。当前线上模型配置（`models/feature_config.json`）的类型是 RandomForest 多分类，与 `train_multiclass_model.py` 对应。其余 4 个（binary、optimized、real、xgboost）在代码中没有引用。
- 研究用的一次性脚本：`services/pipelines/analyze_surnames.py`、`analyze_for_model.py`、`scripts/analyze_temp_visits.py`。
- 这些都不在网站运行链路上，删除后仍保留在 git 历史里。是否删除由 D1 决定。

### P7 文档与代码同步（预计净减文档行数）

- 新增 `scripts/gen_overview.py`：从代码自动生成路由表和代码规模统计，写入 `PROJECT_OVERVIEW.md` 的指定区块。CI 检查生成结果与仓库里的文件一致，让文档不会再过时。
- `docs/REFACTOR_PLAN.md`（2026-01，目标与现状已不符）标注为被本文取代。
- 同步更新 `docs/PROJECT_CATALOG.md`。

---

## 6. 需要产品负责人决定的事项

| 编号 | 问题 | 选项 | 建议 |
|---|---|---|---|
| **D1** | P6 的离线训练和研究脚本 | A. 只保留 `train_multiclass_model.py`，其余删除<br>B. 全部保留<br>C. 移到 `archive/` 目录 | **A**。git 历史可以随时找回 |
| **D2** | 24 个 API 地址在前端、小程序、测试里都没有调用方（例如 `/api/dlnm/summary`、`/api/v1/ml/status`） | A. 保留<br>B. 先查线上 nginx 访问日志，30 天零访问的再讨论下线 | **B**。只读现有日志，不改代码；下线本身属于功能变化，不在本方案内 |
| **D3** | 行为锁是否增加一个冬季时间点 | A. 加<br>B. 不加 | **A**。成本约 7 秒，可以覆盖寒潮相关分支 |

## 7. 执行纪律

1. **先锁后改**：每个阶段先补齐该阶段需要的安全网，并在 `main` 上录好基线，再动代码。
2. **一个 PR 一个主题**：P4、P5、P6、P7 各自一个 PR，可以单独回滚（`git revert`）。
3. **每个提交都通过验证**：行为锁零差异、路由清单锁、全部测试（默认测试把弃用警告当错误处理）、手工契约测试、`git diff --check`。
4. **三证据删除规则**，三条同时满足才删除一段代码：
   - 静态分析（vulture）判定无调用；
   - 全仓库搜索（代码、模板、JS、测试、脚本、文档、定时任务配置）无引用；
   - 行为锁全量运行后的覆盖率显示从未执行。

   任何一条不满足就保留。动态调用（`getattr`、字符串拼接）要人工确认。
5. **遇到分叉就停**：发现"看起来重复、实际有差别"的代码，先证明是否等价；证明不了就不合并，列入决策清单。
6. **不顺手修 bug**：发现的问题记录下来，另开 PR 处理。

## 8. 明确不做

- 不改变任何对外行为（见第 2 节）；
- 不升级依赖（Flask 2.3 升到 3.x 另行评估）；
- 不改数据库结构；
- 不处理 ML 模型文件缺失的问题（这是产品决策）；
- 不改天气抓取和外部 API 调用逻辑；
- 不重做视觉设计。

## 9. 请审核者重点检查

1. 第 2 节的"零变化"定义有没有遗漏的维度？例如响应头、Cookie、缓存头、日志格式。
2. 第 3.4 节列出的盲区之外，还有没有行为锁覆盖不到、但本方案会碰到的路径？
3. P2 用"同名函数全名"保持限流计数键，这个论证是否成立？（依据：Flask-Limiter 3.7 的 `get_qualified_name` 返回 `module.__name__.__qualname__`）
4. P5 的"资源内联对比"和"像素锁"能否可靠证明前端没变？容忍度设为 0 在 CI 上是否现实？
5. 附录 C 对两个同名函数“结果等价”的论证是否成立？
6. 三证据删除规则是否足够保守？
7. 有没有比本方案更省力、同样安全的做法？

## 10. 顺序与预估

| 顺序 | 阶段 | 前置条件 | 预计净变化 |
|---|---|---|---|
| 1 | 合并 PR #52（P0 到 P3） | 审核通过 | 已完成，-1223 行 |
| 2 | P4 服务层 | 补 POST 流程和数据库差异 | -250 到 -400 行 |
| 3 | P5 前端 | 补像素锁、DOM 对比、资源内联对比 | -600 到 -900 行 |
| 4 | P6 离线脚本 | D1 | 0 到 -1600 行 |
| 5 | P7 文档同步 | 无 | 文档不再过时 |

以上行数都是估算，以每个 PR 的实际 diff 为准。

---

## 附录 A：待核实的疑似死代码（vulture，置信度 60%）

```
core/time_utils.py:60                    utcnow_naive
services/api_service.py:81               _handle_api_error        # 被测试引用，保留
services/community_risk_cache.py:33      clear_local_community_risk_cache
services/community_risk_service.py:1340  update_community_sensitivity
services/dlnm_risk_service.py:1007       calculate_attributable_fraction
services/forecast_service.py:1086        calculate_forecast_accuracy
services/health_risk_service.py:288      assess_user_risk
services/health_risk_service.py:292      generate_community_risk_map_data
services/qweather_budget.py:168          get_qweather_budget_snapshot
services/user/_helpers.py:237            _ensure_demo_statuses
services/user/caregiver_service.py:108   _create_pair_link
services/weather_service.py:1302         analyze_weather_disease_correlation
services/weather_service.py:1402         calculate_risk_index
utils/i18n.py:26                         get_error_message
```

## 附录 B：复现验证

```bash
git worktree add /tmp/base origin/main
python scripts/behavior_lock.py record --root /tmp/base --out base.json
python scripts/behavior_lock.py record --root . --out head.json
python scripts/behavior_lock.py diff base.json head.json
python -m pytest -q -W error::DeprecationWarning -W ignore::DeprecationWarning:flask_login.login_manager
python -m pytest -q -m manual
```

## 附录 C：两个同名函数的等价性论证

**`_refresh_community_daily(community_code, status_date)`**

| 差异点 | `public_service.py` 版 | `services/user/_helpers.py` 版 | 为什么结果相同 |
|---|---|---|---|
| 统计对象 | `DailyStatus` 关联 `Pair`，要求社区一致且配对为 active | 先取该社区 active 配对的 id，再用 `pair_id IN (...)` 过滤 | 两者选出的是同一批记录 |
| 升级判定 | `relay_stage in ('backup', 'community', 'emergency')` | `rank(relay_stage) >= rank('backup')`，其中顺序为 `none < caregiver < backup < community < emergency`；未知值和空值的等级为 0 | 判定为真的集合相同 |
| 截断 | 已确认数、升级数用 `min(..., total_people)` 截断；待确认数用 `max(..., 0)` | 不截断 | `daily_status` 表对 `(pair_id, status_date)` 有唯一约束，每个 active 配对每天至多一条记录，所以计数不可能超过 `total_people`，截断永远不会生效 |

**`_short_code_expires_at()`**：两版都读取 `SHORT_CODE_TTL_DAYS`，解析失败时回退到 90 天，并保证至少 1 天。唯一差别是 `services/user` 版在没有应用上下文时直接返回默认值；该函数只在请求处理中被调用，这条分支在现有调用路径上不会触发。

P4 实施时，由行为锁新增的"请求后数据库差异"对比做最终确认。
