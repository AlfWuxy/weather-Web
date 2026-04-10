# Repository Content Policy

> 目的：定义“这个文件应该留在哪里”，适用于人工、Codex、Claude Code、其他 AI 助手。

## Purpose

这份文档只解决一个问题：

- 一个文件应该留在产品主仓库
- 进入本地归档区
- 进入私有 ops 仓
- 只做本地忽略

目标是让主仓库只承载产品本体和长期协作规则。

## Priority

- 如果和一次性清理记录冲突，以本文件为准
- 如果和临时执行说明冲突，以本文件为准
- AI 助手改文件前，先按本文件分类

## Four Destinations

### 1. 产品主仓库

允许进入：

- 产品代码
- 必要测试
- 必要脚本模板
- 长期治理文档
- 架构文档
- 公开协作文档

### 2. 本地归档区

允许进入：

- 历史草稿
- 重复副本
- 一次性报告
- AI 提示词
- 旧方案快照
- 临时比对材料

### 3. 私有 ops 仓

允许进入：

- 真实服务器信息
- 真实部署目标
- 真实备份路径
- 运维脚本
- 真实域名或 IP
- 内部 runbook

### 4. 本地忽略区

允许进入：

- 缓存
- 快照
- 工作树副本
- 调试产物
- 个人机器临时文件

## Hard Rules

- 公开产品仓库禁止出现真实 API key、真实服务器 IP、默认口令、备份路径、内部账号信息
- 主仓库禁止提交 `.claude/`、测试快照、缓存目录、`* 2.*` 重复副本
- 主仓库允许保留模板化脚本，前提是不含真实目标值
- 含真实隐私数据的资料不能进入公开主仓库，也不应留在普通归档区，应进入受控私有位置

## Decision Tree

按下面顺序判断：

1. 这个文件是否参与运行、测试、部署或协作入口
2. 是否包含真实环境信息或个人隐私
3. 是否只是历史记录、一次性产物、AI 工作材料
4. 是否只是本机缓存或工具状态

结论只能落到四个去向之一：

- 产品主仓库
- 本地归档区
- 私有 ops 仓
- 本地忽略区

## Keep In Product Repo

- `app.py`
- `core/`
- `blueprints/`
- `services/`
- `templates/`
- `static/`
- `tests/`
- `miniprogram/`
- `README.md`
- `CONTRIBUTING.md`
- `CHANGELOG.md`
- `.github/`
- `docs/ARCHITECTURE.md`
- `docs/REPO_BOUNDARY_AND_CLEANUP.md`
- `docs/AI_COLLABORATION_AND_BACKUP_PLAYBOOK.md`
- `docs/GITHUB_PROTECTION_CHECKLIST.md`
- `.env.example`

## Archive Locally

- 一次性修复报告
- 阶段性评审报告
- 历史验证报告
- AI prompt
- 个人错误记录
- 与主版本内容一致的 `* 2.*` 重复副本
- 旧页面、旧样式、旧文案快照
- 已被替代但仍需参考的内容

本仓库例子：

- `BUG_FIX_PROMPT.md`
- `.learnings/ERRORS.md`
- `docs/reports/*`
- `docs/status/*`

## Move To Private Ops Repo

- 含真实服务器地址、真实 deploy target、备份下载目标、SSH 流程的脚本
- 运维 runbook、上线手册、机器专属步骤
- 真实第三方服务 Host
- 任何会暴露线上环境结构的资料

本仓库例子：

- `scripts/download_backup.sh`
- 带真实目标值的部署脚本版本
- 私有部署说明

## Ignore Locally

- `.claude/`
- `.playwright-cli/`
- `output/playwright/`
- `__pycache__/`
- `.pytest_cache/`
- `.DS_Store`

个人机器专属临时文件写入 `.git/info/exclude`。

## Repo-Specific Examples

| 内容类别 | 允许位置 | 禁止进入的位置 | 本仓库例子 |
| --- | --- | --- | --- |
| 产品运行链路 | 产品主仓库 | 本地忽略区 | `miniprogram/`, `data/models/final_single_model_ar1_profile.json` |
| AI 协作材料 | 本地归档区 | 产品主仓库 | `BUG_FIX_PROMPT.md`, `.learnings/ERRORS.md` |
| 重复副本 | 本地归档区 | 产品主仓库 | `* 2.*` |
| 运维私密信息 | 私有 ops 仓 | 产品主仓库 | `scripts/download_backup.sh` |
| 工具状态与缓存 | 本地忽略区 | 产品主仓库 | `.claude/worktrees/`, `.playwright-cli/` |

## AI Execution Checklist

1. 改动前先分类，再动文件
2. 分类结果写进 PR 描述或执行记录
3. 涉及删除、迁移、去敏前，先列出文件清单
4. 如果一个文件同时含产品逻辑和敏感运维信息，先拆分，再分别归位
5. 遇到不确定归类，标记为“待裁决”，不要自行猜测

## Exception Process

边界文件先标记为“待裁决”，并补四项信息：

- 用途
- 是否参与运行
- 是否含敏感信息
- 建议去向

未裁决前，不直接进入主仓库。
