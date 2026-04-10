# 当前未提交改动分类清单

> 快照日期：2026-04-10
> 作用：把当前工作区里“还没提交的改动”按主题分清楚，避免后续继续混着开发。
> 重要说明：这份清单只是“分类”和“拆解说明”，不是删除文件，也不是放弃功能。

## 先理解这份清单在做什么

当前仓库里有一批未提交改动。  
Git 只知道“这些文件变了”，但 Git 不知道“这些改动分别属于哪个主题”。

这份清单的目的，是先回答 3 个问题：

1. 这些文件各自在改什么
2. 哪些文件应该和谁一起提交
3. 哪些文件不能整文件提交，而要按改动块拆开

这意味着：

- 这 6 类都属于同一个天气网站项目
- 它们不是 6 个独立仓库
- 也不是 6 个文件夹
- 它们只是为了后续拆 branch / commit / PR 而做的“主题分组”

## 标签说明

### 标签 1：主功能

这类文件直接在实现一个清楚的用户功能或主要能力。

例如：

- 社区风险缓存
- 地图安全代理
- 健康日记天气展示

### 标签 2：工程支撑

这类文件不是用户直接看到的功能，但在支撑功能运行。

例如：

- 配置项
- 部署脚本
- pytest 测试环境

### 标签 3：小修小补

这类文件不是完整功能线，而是轻量修补。

例如：

- 登录页布局小调整
- CSS 微调
- 文档脱敏

### 标签 4：混合文件

这类文件最重要。  
它的意思不是“文件有问题”，而是：

> 一个文件里同时混入了两个或更多主题，不能整文件直接归到某一类。

这种文件后续如果要拆 branch，必须按改动块拆，而不是整文件一起提交。

## 最终主题分组

这份清单最初是 5 组。  
经过 6 个并行只读审查后，最终改成 **6 组**，因为 `公开令牌 / 照护行动路由修复` 单独成组更清楚。

### A 类：社区风险缓存 / 地点解析 / 预计算

- 推荐 branch：`feature/community-risk-cache`
- 主标签：`1`
- 目标：让社区风险结果更快、更稳、更可缓存

#### 纯文件

- `services/api_service.py`
- `services/community_risk_cache.py`
- `services/pipelines/precompute_community_risk.py`
- `scripts/community_risk_precompute.sh`
- `tests/test_community_risk_cache.py`
- `tests/test_community_risk_precompute.py`
- `tests/test_location_resolver.py`

#### 次级混合文件

- `services/location_resolver.py`（主体属于 A，但混入 `AMAP_WEB_SERVICE_KEY` 迁移）

#### 说明

这一组是当前最大的一条后端功能线。  
它的主题是一致的：社区风险数据的缓存、地点解析稳定性和预热流程。

---

### B 类：高德地图代理 / 安全接入

- 推荐 branch：`fix/amap-proxy-hardening`
- 主标签：`1`
- 目标：前端地图接入更安全，避免直接暴露安全码

#### 纯文件

- `blueprints/public.py`
- `core/hooks.py`
- `templates/cooling.html`
- `tests/test_amap_proxy.py`

#### 混合文件

- `templates/community_risk.html`（同时混入社区风险页布局 / fallback）
- `tests/test_community_risk_page.py`（同时混入社区风险页布局 / fallback 验证）
- `services/public_service.py`（同时混入 F 类）
- `core/config.py`（同时混入 D 类）
- `scripts/deploy.sh`（同时混入 A / D 类）
- `tests/conftest.py`（主体属于 D，但也承载高德测试环境隔离）

#### 说明

这一组的核心主题是高德地图代理和地图接入配置。  
其中 `community_risk.html` 和对应测试虽然借用了代理能力，但已经混入页面布局和 fallback 展示，所以只能作为混合支持文件看待。

---

### C 类：健康日记页面增强

- 推荐 branch：`feature/health-diary-weather`
- 主标签：`1`
- 目标：健康日记补充成员映射和历史天气展示

#### 纯文件

- `blueprints/health.py`
- `templates/health_diary.html`
- `tests/test_health_diary_page.py`

#### 说明

这一组主题最清楚，适合后续单独拆成一个很干净的小功能分支。

---

### D 类：配置 / 部署 / 测试环境

- 推荐 branch：`chore/runtime-and-test-hardening`
- 主标签：`2`
- 目标：让配置、部署和测试环境更稳定

#### 纯文件

- `tests/conftest.py`
- `tests/test_deploy_script.py`

#### 混合文件

- `core/config.py`（同时混入 B 类）
- `scripts/deploy.sh`（同时混入 A / B 类）

#### 说明

这组不是直接给用户看的页面功能，而是运行时支撑层。  
后续如果要拆提交，应该避免和社区风险主功能、地图代理功能混在一起。

---

### E 类：UI 微调 / 文档收尾

- 推荐 branch：`fix/ui-polish-and-doc-redaction`
- 主标签：`3`
- 目标：处理页面小调整和文档收尾

#### 纯文件

- `templates/login.html`
- `static/css/style.css`
- `docs/reports/COMPREHENSIVE_FIX_PLAN.md`

#### 说明

这组不属于主功能线，适合最后收尾时单独处理。  
如果追求“一个 PR 只做一件事”，这一组还可以再细分成：

- `UI 微调`：`templates/login.html` + `static/css/style.css`
- `文档 / 安全脱敏`：`docs/reports/COMPREHENSIVE_FIX_PLAN.md`

---

### F 类：公开令牌 / 照护行动路由修复

- 推荐 branch：`fix/public-token-routes`
- 主标签：`1`
- 目标：让 `/e/<token>/...` 相关行动路由保持正确的 token 化跳转

#### 纯文件

- `tests/test_public_token_flow.py`

#### 混合文件

- `services/public_service.py`（同时混入 B 类）

#### 说明

这一组是 6 个审查结果里新增出来的。  
它不属于地图代理，也不属于部署配置，单独成组更清楚。

## 混合文件拆解说明

### 1. `core/config.py`

- 标签：`2 + 4`
- 原因：一个文件里同时有地图配置和缓存/运行配置

#### 属于 B 类的改动块

- `AMAP_JS_API_KEY`
- `AMAP_WEB_SERVICE_KEY`
- `AMAP_KEY` 的兼容逻辑
- 高德 key 的 warning 文案

#### 属于 D 类的改动块

- `COMMUNITY_RISK_CACHE_TTL_SECONDS`
- `COMMUNITY_RISK_CACHE_LOCK_SECONDS`
- `COMMUNITY_RISK_CACHE_WAIT_SECONDS`
- `LOCATION_CACHE_TTL_DAYS`

#### 后续处理原则

这个文件不能整文件归到 B 或 D，后续应按改动块拆。

---

### 2. `services/public_service.py`

- 标签：`1 + 4`
- 原因：一个文件里同时有地图接入改造和公开令牌路由修复

#### 属于 B 类的改动块

- `amap_service_host`
- cooling 页面地图接入从 `securityJsCode` 改为代理 host

#### 属于 F 类的改动块

- `_resolve_action_routes(...)` 相关调用增加 `token=token`
- 与 `tests/test_public_token_flow.py` 对应的 token flow 修正

#### 后续处理原则

这个文件后续如果要拆分支，必须按地图接入和 token flow 两块拆开。

---

### 3. `scripts/deploy.sh`

- 标签：`2 + 4`
- 原因：一个文件里混了地图环境变量、社区风险预热部署和通用部署稳健性改动

#### 属于 B 类的改动块

- `AMAP_JS_API_KEY`
- `AMAP_WEB_SERVICE_KEY`
- `AMAP_SECURITY_JS_CODE`

#### 属于 A 类的改动块

- `case-weather-risk-precompute.service`
- `case-weather-risk-precompute.timer`
- 社区风险预热定时器启停逻辑

#### 属于 D 类的改动块

- `set -e`
- `check_remote_unit_active`
- 停止/启动服务的更严格检查

#### 后续处理原则

这是当前最典型的三路混合文件。  
后续拆提交时，不能整文件一起进入某一类。

---

### 4. `templates/community_risk.html`

- 标签：`1 + 4`
- 原因：一个文件里同时有地图代理接入和社区风险页自身布局 / fallback 调整

#### 属于 B 类的改动块

- 改用 `serviceHost`
- 不在前端暴露 `securityJsCode`

#### 属于 A 类的改动块

- 社区风险页的地图回退提示
- 页面布局与结果区展示优化

#### 后续处理原则

这个文件不适合整文件放进纯 B 组，也不适合整文件放进纯 A 组。

---

### 5. `tests/test_community_risk_page.py`

- 标签：`1 + 4`
- 原因：同时验证了代理相关行为和社区风险页自己的 fallback / 布局行为

#### 属于 B 类的改动块

- 代理模式下的地图接入校验

#### 属于 A 类的改动块

- 社区风险页 fallback 文案与页面展示校验

#### 后续处理原则

如果后续拆测试提交，这个文件也要按测试目的拆，不应整文件直接归组。

## 当前文件清单总览

| 文件 | 当前状态 | 主题分组 | 标签 | 说明 |
| --- | --- | --- | --- | --- |
| `services/api_service.py` | 已修改 | A | 1 | 社区风险结果缓存接入 |
| `services/location_resolver.py` | 已修改 | A + B | 1 + 4 | 主体是地点缓存，次级混入高德服务端 key 迁移 |
| `templates/community_risk.html` | 已修改 | A + B | 1 + 4 | 代理接入 + 社区风险页 fallback / 布局混合 |
| `tests/test_community_risk_page.py` | 已修改 | A + B | 1 + 4 | 代理校验 + 社区风险页 fallback 校验混合 |
| `tests/test_location_resolver.py` | 已修改 | A | 1 | 地点解析测试 |
| `services/community_risk_cache.py` | 未跟踪 | A | 1 | 社区风险缓存实现 |
| `services/pipelines/precompute_community_risk.py` | 未跟踪 | A | 1 | 预计算 pipeline |
| `scripts/community_risk_precompute.sh` | 未跟踪 | A | 1 | 风险预热脚本 |
| `tests/test_community_risk_cache.py` | 未跟踪 | A | 1 | 社区风险缓存测试 |
| `tests/test_community_risk_precompute.py` | 未跟踪 | A | 1 | 预计算测试 |
| `blueprints/public.py` | 已修改 | B | 1 | 高德代理路由 |
| `core/hooks.py` | 已修改 | B | 1 | 模板注入代理 host |
| `templates/cooling.html` | 已修改 | B | 1 | cooling 页面接入代理 |
| `tests/test_amap_proxy.py` | 未跟踪 | B | 1 | 高德代理测试 |
| `blueprints/health.py` | 已修改 | C | 1 | 健康日记查询逻辑增强 |
| `templates/health_diary.html` | 已修改 | C | 1 | 健康日记展示增强 |
| `tests/test_health_diary_page.py` | 未跟踪 | C | 1 | 健康日记页面测试 |
| `tests/conftest.py` | 已修改 | D | 2 | 测试环境隔离 |
| `tests/test_deploy_script.py` | 未跟踪 | D | 2 | deploy 脚本测试 |
| `templates/login.html` | 已修改 | E | 3 | 登录页对齐调整 |
| `static/css/style.css` | 已修改 | E | 3 | 样式微调 |
| `docs/reports/COMPREHENSIVE_FIX_PLAN.md` | 已修改 | E | 3 | 文档脱敏 |
| `core/config.py` | 已修改 | B + D | 2 + 4 | 地图配置 + 缓存配置混合 |
| `tests/test_public_token_flow.py` | 已修改 | F | 1 | 公开令牌路由测试补强 |
| `services/public_service.py` | 已修改 | B + F | 1 + 4 | 地图接入 + token flow 混合 |
| `scripts/deploy.sh` | 已修改 | A + B + D | 2 + 4 | 预热部署 + 地图变量 + 部署稳健性混合 |

## 推荐的后续顺序

如果后续真的要把当前未提交改动拆成更清楚的 branch / commit，推荐顺序如下：

1. A 类：社区风险缓存 / 地点解析 / 预计算
2. B 类：高德地图代理 / 安全接入
3. F 类：公开令牌 / 照护行动路由修复
4. C 类：健康日记页面增强
5. D 类：配置 / 部署 / 测试环境
6. E 类：UI 微调 / 文档收尾

原因：

- A / B / C 是清楚的功能线
- D 是工程支撑层
- E 是最轻的收尾层

## 这份清单的使用方法

以后你、Codex、Claude Code 或其他 AI 助手看到当前工作区里这批改动时，先不要直接 `git add .`。  
先打开这份清单，回答两件事：

1. 这次只准备继续哪一类主题
2. 当前准备提交的文件里，有没有混合文件

如果答案里包含混合文件，就不要整文件直接提交，而要按改动块拆。
