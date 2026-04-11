# 天气预警网站 / Weather Web

一个面向高温风险与脆弱人群保护场景的 Flask 产品仓库。项目聚焦“天气预警 + 健康风险评估 + 社区行动支持”，目标是把原来的个人 Demo 持续升级为可维护、可验证、可迭代的正式工程项目。

## 当前定位

- 产品方向：帮助老人、家属与社区在高温和极端天气下更快做出行动决策
- 工程目标：从“个人项目”升级为“有标准开发流程的产品仓库”
- 工作流原则：Notion 管需求与迭代，GitHub 管代码与变更历史

## 核心能力

- 实时天气与预报数据接入
- 健康风险评估与规则推荐
- 社区风险分析与避暑资源展示
- 照护人 / 被照护人配对与提醒流程
- 小程序与 Web 双入口联动

## 技术栈

- Backend: Flask, SQLAlchemy, Flask-Login, Flask-Limiter
- Data: pandas, numpy, scipy, scikit-learn
- Storage: SQLite（默认）/ 可扩展外部数据库
- Frontend: Jinja2, Bootstrap, 原生 JavaScript
- Testing: pytest

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

按需填写 `.env` 中的关键项，至少应确认：

- `SECRET_KEY`
- `PAIR_TOKEN_PEPPER`
- `DATABASE_URI`
- `QWEATHER_KEY` 或接受 Open-Meteo 兜底

### 3. 初始化数据库

```bash
flask init-db
```

### 4. 启动开发环境

```bash
python app.py
```

默认访问地址：

- Web: `http://127.0.0.1:5000`

首次进入仓库后，建议启用本地 Git 护栏：

```bash
git config core.hooksPath .githooks
chmod +x .githooks/pre-push
```

## 测试

运行默认测试集：

```bash
pytest
```

手动测试类用例默认不会执行；如需单独运行，请查看 `tests/manual/`。

小程序调试 / 联调前，必须先在 `miniprogram/config.js` 中填写真实 HTTPS API 地址。公开仓库默认留空，未配置时小程序请求会直接报错，不会再偷偷打到占位域名。

## 仓库结构

```text
app.py                薄入口，导出 Flask app
core/                 应用工厂、模型、配置、钩子与核心能力
blueprints/           路由蓝图
services/             业务服务层
templates/            Jinja2 模板
static/               CSS / JS / vendor 静态资源
miniprogram/          微信小程序端
tests/                自动化测试
docs/                 架构、重构、评审与状态文档
scripts/              部署与维护脚本
```

如果你要启用 QWeather，请在本地 `.env` 中显式设置 `QWEATHER_KEY` 和 `QWEATHER_API_BASE`。公开仓库默认不再内置 QWeather Host；未配置时系统会走 Open-Meteo / 阈值规则兜底。

## 标准开发流程

本仓库从第一阶段 officialization 起，默认使用以下流程：

1. 在 Notion“网站迭代库”中创建或领取一条条目
2. 从 `main` 拉出功能分支
3. 小步提交，使用规范化 commit 标题
4. 提交 PR，说明“改了什么 / 为什么改 / 怎么验证”
5. 验证通过后使用 squash merge 合并回 `main`
6. 回到 Notion 更新状态、分支名和 PR 链接

更完整的人工 / AI 协作与备份规则见 [docs/AI_COLLABORATION_AND_BACKUP_PLAYBOOK.md](./docs/AI_COLLABORATION_AND_BACKUP_PLAYBOOK.md)。

## Commit 规范

- `feat: ...` 新功能
- `fix: ...` 修复问题
- `refactor: ...` 重构
- `docs: ...` 文档更新
- `style: ...` 样式或非功能性调整

## 文档入口

- AI 协作与备份主说明：[`docs/AI_COLLABORATION_AND_BACKUP_PLAYBOOK.md`](./docs/AI_COLLABORATION_AND_BACKUP_PLAYBOOK.md)
- 内容去向政策：[`docs/REPOSITORY_CONTENT_POLICY.md`](./docs/REPOSITORY_CONTENT_POLICY.md)
- GitHub 保护设置清单：[`docs/GITHUB_PROTECTION_CHECKLIST.md`](./docs/GITHUB_PROTECTION_CHECKLIST.md)
- 项目总览：[`docs/PROJECT_CATALOG.md`](./docs/PROJECT_CATALOG.md)
- 架构说明：[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md)
- 重构计划：[`docs/REFACTOR_PLAN.md`](./docs/REFACTOR_PLAN.md)
- 仓库边界与清理分类：[`docs/REPO_BOUNDARY_AND_CLEANUP.md`](./docs/REPO_BOUNDARY_AND_CLEANUP.md)

## 仓库边界

这个仓库只保留天气网站产品本体相关内容：

- 产品代码
- 必要测试
- 必要部署脚本
- 产品文档

以下内容不应继续进入主仓库历史：

- 本地 AI / Agent 工具状态（如 `.claude/`）
- 测试快照与浏览器产物
- 重复备份文件（如 `* 2.*`）
- 与天气网站主线无关的历史资料

如果需要多人或多 AI 助手同时工作，优先使用 `git worktree` 或独立工作副本，不要共用同一个开发目录直接轮流改。

## 说明

当前仓库正处于 officialization 第一阶段，重点是先把开发流程、仓库边界和文档规范稳定下来，再继续功能扩展。
