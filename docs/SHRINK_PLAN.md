# 代码精简方案（功能零变化）

> 版本：v2（2026-09-25，按第一轮审核修订）· 状态：待批准 · 基线：`main@60ef21f`
> 已完成部分见 PR #52（分支 `claude/vigilant-edison-8dqovj`）；第一轮审核意见与处理见第 11 节

本文面向审核者（人或 AI）。背景、硬约束、证据、每一步怎么做、怎么证明没改坏，以及需要产品负责人决定的事项，都写在这里。

---

## 1. 背景

- 项目：面向都昌县老年人高温风险的 Flask 网站，另有微信小程序入口。技术栈：Flask 2.3、SQLAlchemy、Jinja2、原生 JS，默认 SQLite。
- 规模（自有代码，不含 vendor）：Python 约 1.8 万行，模板 58 个约 1.4 万行，CSS 约 6000 行，JS 约 2700 行，路由 131 条。
- 目标：减少代码量和重复，降低长期维护成本。
- **硬约束：用户可感知的一切都不能变。**

## 2. "功能零变化"的精确定义

### 2.1 必须保持不变的内容

| 类别 | 内容 |
|---|---|
| 地址 | URL、HTTP 方法、endpoint 名（`url_for` 依赖它） |
| 输出 | 每个页面的 HTML、每个接口的 JSON、状态码、重定向目标 |
| 响应头 | 全部响应头（含 `Content-Type` 字符集、`Content-Disposition` 下载文件名、缓存头、安全头）；只忽略 `Date`、`Content-Length`、`Server` |
| Cookie 与会话 | Cookie 名称和属性（`HttpOnly`、`SameSite`、`Path` 等）；会话内容（登录身份、flash 提示、CSRF） |
| 权限 | 登录要求、角色校验、提示信息 |
| 限流 | 限流规则、各地址在同一请求序列下的放行与拦截、计数键名称与计数值 |
| 数据 | 每个操作之后的数据库状态，包括外键关系和有业务含义的时间字段 |
| 导出文件 | Excel 的全部单元格；PDF 的页数和逐页文本；CSV 全文 |
| 前端 | 渲染像素、交互结果（筛选、表单提交、本地存储、图表）、浏览器控制台输出 |

### 2.2 各阶段允许的变化范围

| 阶段 | 允许变化的内容 | 其余一律不变 |
|---|---|---|
| P4 服务层 | 无。HTTP 层（2.1 全部各项）和数据库状态逐字一致 | 是 |
| P5 前端 | 只有两类：① HTML 标签之间无意义的空白；② 把内联 `<script>`、`<style>` 原样搬到 `static/` 下的文件。搬移时必须满足：位置不变、属性不变（不加 `defer`、`async`、`type="module"`）、同源加载、文件内容与原内联内容逐字相同 | 是：DOM 结构、属性、文本、像素、交互结果、控制台输出 |
| P6 离线脚本 | 本轮不改 | 是 |
| P7 文档 | 只改文档和生成脚本 | 是 |

确实需要改变输出的情况（例如修一个既有 bug），必须单独开 PR、打 `behavior-change` 标签，由产品负责人明确批准。

## 3. 安全网

### 3.1 行为锁（`scripts/behavior_lock.py`）

**录制方式**
- 固定种子数据（3 个村、4 类用户、480 条门诊、150 天天气、12 条预警、照护关系、避暑点等），时间冻结在 2026-07-20 10:00（北京时间），断开外网，固定 Python 哈希种子。
- 两种环境：`demo`（演示天气、默认功能开关）和 `live`（带"在线"来源标记的天气缓存、全部功能开关打开）。
- 5 种身份（游客、普通用户、照护人、社区、管理员）访问：
  - 全部可枚举的 GET 路由；
  - 22 个带筛选参数的页面变体（6 个分析页的分层、分箱、滞后、日期倒置、非法输入，以及试点看板的天数参数）；
  - 14 个 JSON 写接口，以及 3 个报告导出表单。
- **请求之间完全隔离**：每个请求前，数据库恢复到种子状态，会话恢复到该身份刚登录后的 cookie。

**对比内容**：状态码、`Location`、全部响应头、Cookie 属性、解码后的会话内容、HTML/JSON 正文、导出文件内容（Excel 单元格，PDF 页数与文本）。规范化的只有：CSRF token、静态资源版本号、代码目录路径、每次随机生成的请求 ID 和游客 ID。

**有效性验证**（第一轮审核后加固）

| 检查 | 结果 |
|---|---|
| 连续录制两次 | 1230 个响应完全一致 |
| 在一条提示文字末尾加一个句号 | 抓到，报 22 个响应变化 |
| 改 PDF 报告内容（审核者复现的漏洞） | 抓到 |
| 改下载文件名（审核者复现的漏洞） | 抓到 |
| 改 Excel 单元格 | 抓到 |
| Cookie 的 `SameSite` 从 `Lax` 改为 `Strict` | 抓到，564 个响应变化 |
| 改权限不足的 flash 文字 | 抓到，210 个响应变化 |
| 全站新增 `Cache-Control` 响应头 | 抓到，1230 个响应变化 |
| `main` 与 PR #52 对比（加固后重录） | 零差异 |

覆盖率：`blueprints/analysis.py` 82%，`blueprints/api.py` 95%。

**CI**：每个 PR 用 `origin/<base>` 和 PR 的代码各录一份再做 diff；打了 `behavior-change` 标签的 PR 跳过。

### 3.2 路由清单锁（`tests/test_route_manifest.py`）

131 条路由的（URL、方法、endpoint 名）存成清单，缺一条或多一条都会让测试失败。

### 3.3 限流锁（`scripts/ratelimit_lock.py`，已接入 CI）

行为锁录制时会关闭限流，这个脚本专门补上：把阈值调低，按固定顺序交替请求 10 组接口的兼容地址和 v1 地址，共 122 次；记录每次的状态码，以及限流存储里的全部计数键和值。当前结果：40 次被拦截，28 个计数键；连跑两次一致，`main` 与 PR #52 一致。

### 3.4 已知盲区，以及补强措施

| 盲区 | 影响阶段 | 措施 |
|---|---|---|
| 连续操作流程（绑定、打卡、求助、升级等状态变化） | P4 | 场景锁（见 5.1） |
| 数据库写入 | P4 | 每一步之后的数据库快照纳入对比（见 5.1） |
| 浏览器端执行顺序、交互、本地存储 | P5 | 浏览器交互用例加像素锁（见 5.2） |
| 定时任务、CLI、离线脚本 | 无 | 本方案不改动这些入口（原 4g 已移出）。以后若要改，先为该入口单独建新旧对比 |
| 真实外部 API 的返回 | 无 | 不触碰天气抓取和外部 API 调用逻辑 |
| 单一时间点（7 月） | 全部 | D3：增加冬季时间点，种子数据按冻结时间整体平移 |
| 性能（查询次数、耗时） | P4、P5 | 不在锁内。每个 PR 对关键页面记录一次 SQL 查询次数和耗时，作为参考写进 PR 描述 |

---

## 4. 已完成（PR #52）

| 阶段 | 内容 | 结果 |
|---|---|---|
| P0 安全网 | 行为锁、路由清单锁、限流锁，接入 CI | 见第 3 节 |
| P1 死代码 | 删除 4 个已执行完的一次性改码脚本；删除 `api_service.py` 中 36 个从未被调用的包装函数 | -760 行 |
| P2 API 注册表 | `blueprints/api.py` 改为声明式表格加注册循环；endpoint 名、视图函数全名、登录、限流都不变 | 287 行减到约 90 行 |
| P3 分析蓝图 | 25 个纯统计函数移到 `services/analysis_stats.py`；新增 `admin_route` 装饰器替换 11 处重复校验；6 个分析页共用筛选、日期、查询、阈值、基线逻辑 | `analysis.py` 3364 行减到 2599 行 |
| 审核后加固 | 行为锁增加导出内容、响应头、Cookie、会话对比和请求隔离；限流锁脚本化并接入 CI | 加固后重录，`main` 与 PR 零差异 |

产品代码合计净减 1223 行。原有 417 个默认测试、10 个手工契约测试均通过。

---

## 5. 待执行阶段

每个阶段单独一个 PR，可以单独回滚。

### 5.1 P4 服务层去重（预计 -250 到 -400 行）

**前置：场景锁与数据库快照**

按完整业务场景执行。场景内部保留状态（cookie 和数据库），场景之间恢复种子。

| 场景 | 步骤 |
|---|---|
| S1 老人正常流程 | 短码绑定 → 打开行动页 → 打卡 → 求助 → 复盘 |
| S2 重复提交 | 重复打卡、重复求助、重复复盘 |
| S3 失效与错误 | 过期短码、错误短码（连续失败直到锁定）、已撤销或已过期的行动令牌 |
| S4 照护人 | 创建配对 → 记录行动 → 设置备用联系人 → 升级 → 查看详情 |
| S5 越权 | 照护人 B 操作照护人 A 的配对；普通用户访问照护人和社区接口；游客执行写操作 |
| S6 社区 | 发布通知、查看社区详情与日统计 |
| S7 家庭与健康 | 家庭成员和用药提醒的新增、编辑、删除、开关提醒 |
| S8 管理后台 | 用户、社区、避暑点的新增、编辑、删除 |

每一步之后记录：响应（按 3.1 的全部维度）和完整的数据库快照。快照保留所有字段，包括自增 id、外键和 `confirmed_at`、`expires_at`、`redeemed_at`、`used_at` 等业务时间（时间已冻结，这些值是确定的）。随机生成的值（令牌哈希、随机短码）映射为场景内稳定的占位符，并保留"哪些值彼此相等"的关系。

新增冬季时间点（D3）：冻结时间和种子里的天气、门诊、缓存时间、令牌和短码有效期整体平移，避免测到的都是过期数据。

**改动清单**

| 编号 | 位置 | 问题 | 做法 |
|---|---|---|---|
| 4a | `services/public_service.py` 与 `services/user/_common.py`、`_helpers.py` | `_action_plan`、`_risk_level_value`、`_build_recent_series` 整段复制，语法树完全一致 | 删除副本，改为导入 |
| 4b | 同上 | `_refresh_community_daily`、`_short_code_expires_at` 同名，写法不同，结果等价（附录 C） | 保留 `services/user` 版；由场景锁的数据库快照最终确认 |
| 4c | `services/user/caregiver_service.py` 与 `community_service.py` | "取天气、判断在线、算连续高温、算热风险"约 40 行重复 | 抽成共用函数 |
| 4d | `services/public_service.py` 内部 | 短码表单处理重复 3 次，行动页渲染重复 3 次 | 抽成内部辅助函数 |
| 4e | `services/user_service.py`（48 行兼容层） | 只被 `blueprints/user.py` 使用，导出列表与 `services/user/__init__.py` 重复 | 改为直接导入 `services.user`，删除兼容层 |
| 4f | `services/user/dashboard_service.py` 内部 | 查询最近预警的代码重复一次（约 18 行） | 合并 |
| 4h | 附录 A 的 14 个疑似死函数 | 静态分析标记为无调用 | 按三证据规则逐个核实（第 7 节），齐全才删除 |

原 4g（两个天气同步脚本的写入逻辑去重）已移出：这两个是生产用的定时任务入口，网页行为锁覆盖不到；两处写入周围的极端天气字段处理并不相同；收益只有约 13 行。

**验证**：场景锁、行为锁、限流锁零差异；路由清单锁、全部测试通过。

### 5.2 P5 前端去重（预计 -600 到 -900 行）

**前置：前端安全网**

1. **DOM 对比**：把 HTML 解析成 DOM 后对比，只忽略标签之间无意义的空白。用于验证 Jinja 宏重构。
2. **资源外提的静态校验**：对每个被外提的脚本或样式，检查：外部文件内容与原内联内容逐字相同；`<script>`/`<link>` 标签位于原位置；没有新增 `defer`、`async`、`type="module"`；同源加载。
3. **浏览器交互用例**（Playwright）：

   | 用例 | 记录内容 |
   |---|---|
   | 6 个分析页提交筛选 | 结果 URL、关键区域文本 |
   | 高温阈值调节（`risk.html`、`action_checkin.html`），刷新后保持 | 页面显示值、`localStorage` 内容 |
   | 风险趋势图与分析图表渲染，悬停提示 | 图表是否渲染、提示文本 |
   | 管理后台新增/编辑表单提交 | 跳转结果、数据库快照 |
   | AI 浮窗打开关闭、模板复制按钮 | DOM 状态、剪贴板写入内容 |
   | 全部用例 | 控制台输出、发出的网络请求列表 |

4. **像素锁**：约 20 个关键页面，3 种宽度（390、768、1440）。基线和新版在同一个 CI 任务、同一容器、同一个 Chromium 可执行文件、同一套字体下截图；设备缩放比固定为 1，时区和语言固定，关闭动画，拦截外部请求（地图等）。容忍度为 0，任何差异都判失败。

以上几层合起来构成很强的证据，但仍然不等于数学意义上的证明。所以 P5 的每一处改动都限定在 2.2 允许的两类变化之内。

**改动清单**（依据：跨模板重复检测，约 1700 行位于重复片段中）

| 编号 | 位置 | 做法 |
|---|---|---|
| 5a | 4 个病例分析页、2 个预警页的筛选表单（成对重复 20 到 38 行） | 抽成 Jinja 宏 `templates/_macros/analysis_filters.html` |
| 5b | `action_checkin.html` 与 `caregiver_pair_detail.html` 的风险趋势图脚本（约 60 行相同） | 抽成 `static/js/risk-trend-chart.js` |
| 5c | `action_checkin.html` 与 `risk.html` 的高温阈值调节脚本（约 43 行相同） | 抽成 `static/js/heat-threshold.js` |
| 5d | 管理后台 3 组"新增/编辑"成对模板（用户、社区、避暑点） | 共用部分抽成 include，路由仍渲染原模板名 |
| 5e | `caregiver_wechat_template.html` 与 `community_wechat.html` | 抽出共用片段 |
| 5f | `admin_dashboard.html` 与 `admin_statistics.html` 的图表配置 | 抽出共用配置 |
| 5g | 7 个分析页的内联样式（约 1100 行，每页用自己的前缀重写了卡片、表格、徽章） | 只合并去掉前缀后完全相同的规则，放进 `static/css/analysis.css` |

5b、5c 两页之间的脚本若只是"相似"而非"相同"，就必须参数化，超出 2.2 允许的范围。这种情况先列出差异，由产品负责人决定做不做。

**不做**：不引入前端框架或构建工具，不改任何视觉设计。

### 5.3 P6 离线脚本：本轮只调查，不删除

- `services/pipelines/` 下有 5 个训练脚本（共 1440 行）和几个研究脚本。离线训练本来就通过命令行运行，"网站代码里没有引用"不能说明废弃。仓库里的模型配置与某个脚本吻合，也不能证明线上模型的来源。
- `services/pipelines/analyze_surnames.py` 被入口契约测试 `tests/test_pipeline_entrypoints.py` 明确引用（v1 方案把它误列为一次性脚本，已更正）。
- 本轮产出一份调查报告，逐个脚本核清：输入数据及其是否在仓库中、输出产物、调用方式（命令行、定时任务、文档）、能否复现当前的 `models/feature_config.json`、最近修改时间与作者。去留另行决定。

### 5.4 P7 文档与代码同步

- 新增 `scripts/gen_overview.py`：从代码生成路由表和代码规模统计，写入 `PROJECT_OVERVIEW.md` 的指定区块；CI 检查生成结果与仓库一致。
- `docs/REFACTOR_PLAN.md`（2026-01，已过时）标注为被本文取代；同步更新 `docs/PROJECT_CATALOG.md`。

---

## 6. 需要产品负责人决定的事项

| 编号 | 问题 | 建议（已采纳审核意见） |
|---|---|---|
| **D1** | 离线训练和研究脚本的去留 | **本轮全部保留**。先完成 5.3 的调查，再单独决定 |
| **D2** | 24 个 API 地址在前端、小程序、测试里都没有调用方（例如 `/api/dlnm/summary`、`/api/v1/ml/status`） | 可以查线上访问日志，但 30 天零访问只作为线索；**接口一律保留**，下线属于功能变化 |
| **D3** | 行为锁增加冬季时间点 | **增加**，并按冻结时间整体平移天气、门诊、缓存、令牌和短码的日期 |

## 7. 执行纪律

1. **先锁后改**：每个阶段先补齐该阶段的安全网，在 `main` 上录好基线，并用破坏性测试证明新增的锁能抓到对应的变化，再动代码。
2. **一个 PR 一个主题**：P4、P5、P6 调查、P7 各自一个 PR，可以单独 `git revert`。
3. **每个提交都通过验证**：行为锁、场景锁（P4 起）、限流锁零差异；路由清单锁；全部测试（弃用警告视为错误）；手工契约测试；对整个分支相对 `main` 运行 `git diff --check`。
4. **三证据删除规则**，三条同时满足才删除：
   - 静态分析（vulture）判定无调用；
   - 全仓库搜索（代码、模板、JS、测试、脚本、文档、定时任务配置）无引用；
   - 行为锁和场景锁全量运行后的覆盖率显示从未执行。

   任何一条不满足就保留。动态调用（`getattr`、字符串拼接）要人工确认。**只适用于网站运行链路上的代码；离线脚本不适用**，原因见 5.3。
5. **遇到分叉就停**：看起来重复、实际有差别的代码，先证明等价；证明不了就不合并，列入决策清单。
6. **不顺手修 bug**：发现的问题记录下来，另开 PR。

## 8. 明确不做

- 不改变任何对外行为（第 2 节）；
- 不升级依赖（Flask 2.3 升到 3.x 另行评估）；
- 不改数据库结构；
- 不处理 ML 模型文件缺失的问题；
- 不改天气抓取、外部 API 调用、定时任务和离线脚本的逻辑；
- 不重做视觉设计；
- 不下线任何接口。

## 9. 第二轮审核请重点检查

1. 2.2 的"各阶段允许变化范围"是否足够严格，特别是 P5 的资源外提条件。
2. 5.1 的场景清单是否覆盖了会被 P4 改动的全部代码路径（`public_service.py`、`services/user/` 下的各服务）。
3. 5.2 的浏览器用例和像素锁环境固定方式，是否足以支撑 P5 的改动范围。
4. 3.1 的规范化规则（CSRF、版本号、目录路径、请求 ID、游客 ID）是否可能掩盖真实差异。

## 10. 顺序与预估

| 顺序 | 阶段 | 前置条件 | 预计净变化 |
|---|---|---|---|
| 1 | 合并 PR #52（P0 到 P3，含审核后加固） | 批准 | -1223 行（产品代码） |
| 2 | P4 服务层 | 场景锁、数据库快照、冬季时间点 | -250 到 -400 行 |
| 3 | P5 前端 | DOM 对比、外提校验、浏览器用例、像素锁 | -600 到 -900 行 |
| 4 | P6 调查报告 | 无 | 0 行 |
| 5 | P7 文档同步 | 无 | 文档不再过时 |

行数为估算，以每个 PR 的实际 diff 为准。

## 11. 第一轮审核记录

审核对象：PR #52 `d9937b2`。审核者独立复现：行为锁 1230 个响应零差异；默认测试 417 通过；手工契约测试 10 通过、1 跳过；25 个迁出的统计函数忽略改名后语法树一致；"产品代码净减 1223 行"成立。结论：方向正确，修改方案后再通过。

| # | 审核意见 | 处理 | 状态 |
|---|---|---|---|
| 1 | 行为锁不检查导出内容和响应头；改 PDF 内容、下载文件名都发现不了；Cookie、缓存策略变化也发现不了 | 已先复现（改 PDF 内容和文件名后，旧版锁报零差异）。加固后对比 Excel 单元格、PDF 页数与文本、全部响应头、Cookie 属性、会话内容；6 类破坏性测试全部抓到（3.1） | 已完成 |
| 2 | P4 需要连续操作流程；数据库对比要保留外键和业务时间 | 改为场景锁 S1 到 S8，场景内保留状态；数据库快照保留全部字段。同时修正了现有锁的会话串扰问题：原先 flash 提示会在请求之间累积（5.1、3.1） | 方案已修订 |
| 3 | P5 把"内容相同"推成"行为等价"，结论过强；要明确允许的变化范围；像素锁要固定环境 | 新增 2.2 各阶段允许变化范围；增加外提静态校验和浏览器交互用例；明确像素锁的环境固定方式；措辞改为"强证据"（5.2） | 方案已修订 |
| 4 | 4g 两个同步脚本没有对应安全网，且写入逻辑周围不同 | 4g 移出；盲区表改为"本方案不改动这些入口" | 方案已修订 |
| 5 | P6 默认删除训练脚本依据不足；`analyze_surnames.py` 被入口契约测试引用 | 已核实并更正；P6 改为只调查不删除，三证据规则明确不适用于离线脚本（5.3、第 7 节） | 方案已修订 |
| 6 | 附录 C 等价论证认可；P2 的限流论证成立，建议保留专项比较脚本 | 已保存为 `scripts/ratelimit_lock.py` 并接入 CI（3.3） | 已完成 |
| 7 | D1 先全部保留；D2 日志只作线索、接口保留；D3 增加冬季点，并平移数据日期 | 全部采纳（第 6 节） | 已采纳 |

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
python scripts/ratelimit_lock.py record --root /tmp/base --out rl-base.json
python scripts/ratelimit_lock.py record --root . --out rl-head.json
python scripts/ratelimit_lock.py diff rl-base.json rl-head.json
python -m pytest -q -W error::DeprecationWarning -W ignore::DeprecationWarning:flask_login.login_manager
python -m pytest -q -m manual
git diff --check origin/main...HEAD
```

## 附录 C：两个同名函数的等价性论证

**`_refresh_community_daily(community_code, status_date)`**

| 差异点 | `public_service.py` 版 | `services/user/_helpers.py` 版 | 为什么结果相同 |
|---|---|---|---|
| 统计对象 | `DailyStatus` 关联 `Pair`，要求社区一致且配对为 active | 先取该社区 active 配对的 id，再用 `pair_id IN (...)` 过滤 | 两者选出的是同一批记录 |
| 升级判定 | `relay_stage in ('backup', 'community', 'emergency')` | `rank(relay_stage) >= rank('backup')`，顺序为 `none < caregiver < backup < community < emergency`；未知值和空值的等级为 0 | 判定为真的集合相同 |
| 截断 | 已确认数、升级数用 `min(..., total_people)` 截断；待确认数用 `max(..., 0)` | 不截断 | `daily_status` 表对 `(pair_id, status_date)` 有唯一约束，每个 active 配对每天至多一条记录，计数不可能超过 `total_people`，截断永远不会生效 |

**`_short_code_expires_at()`**：两版都读取 `SHORT_CODE_TTL_DAYS`，解析失败时回退到 90 天，并保证至少 1 天。唯一差别是 `services/user` 版在没有应用上下文时直接返回默认值；该函数只在请求处理中被调用，这条分支在现有调用路径上不会触发。

以上结论在"当前唯一约束、相同数据状态"下成立。P4 实施时由场景锁的数据库快照做最终确认。
