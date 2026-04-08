# Contributing

本项目当前主要由个人维护，但开发流程按正式产品仓库执行。目标不是“随手改完就算”，而是让每次改动都能被回看、被验证、被复盘。

完整主说明见 `docs/AI_COLLABORATION_AND_BACKUP_PLAYBOOK.md`。如果和其他文档冲突，以那份作业手册为准。

## 一条迭代的标准路径

1. 先在 Notion“网站迭代库”中创建或确认条目
2. 明确本次只解决一件事
3. 从 `main` 拉出分支
4. 在分支上开发并小步提交
5. 补充测试或验证记录
6. 提交 PR
7. 通过后使用 squash merge 合并
8. 回 Notion 更新状态、PR 链接和验证结果

## 分支命名

推荐格式：

- `feature/<short-name>`
- `fix/<short-name>`
- `chore/<short-name>`

如果是 AI 助手执行，推荐使用带执行者前缀的格式：

- `codex/feature/<short-name>`
- `codex/fix/<short-name>`
- `codex/chore/<short-name>`
- `claude/feature/<short-name>`

示例：

- `feature/wechat-entry`
- `fix/mobile-login`
- `chore/repo-cleanup`

## Commit 规范

提交标题统一使用前缀：

- `feat: 新功能`
- `fix: 修复问题`
- `refactor: 重构`
- `docs: 文档更新`
- `style: 样式调整`

示例：

```text
feat: 新增老人模式快捷入口
fix: 修复社区风险页地图空白问题
docs: 补充仓库开发流程说明
```

要求：

- 一次 commit 尽量只表达一个动作
- 不使用 `update`、`misc`、`test` 这类模糊标题
- 文档、样式、重构尽量和功能修复分开提交
- commit 前先看 `git diff --staged`
- 默认不要直接使用 `git add .`

## Pull Request 要求

每个 PR 默认只做一件事，并在描述中回答三件事：

1. 这次改了什么
2. 为什么要改
3. 怎么验证改动生效

默认合并方式：

- 使用 `squash merge`
- 保持 `main` 历史整洁

默认状态：

- 未验证完成前使用 Draft PR
- 每个 PR 都要写清楚当前分支名与对应 Notion 条目

## 测试与验证

在提交 PR 前，至少完成下列之一：

- 运行相关 pytest 用例
- 手动验证受影响页面 / API
- 写清楚无法自动化验证的原因与手动步骤

常用命令：

```bash
pytest
python app.py
```

## 开工前检查

每次开始前，先运行：

```bash
git branch --show-current
git status --short
git diff --staged
```

如果当前在 `main` 且准备修改文件，先新建分支，不要直接在 `main` 上开发。

## 多 AI / 多人协作

如果 Codex、Claude Code 和人工可能同时修改仓库，推荐使用 `git worktree` 做隔离。

示例：

```bash
git worktree add ../weather-web-codex -b codex/chore/<short-name> main
git worktree add ../weather-web-claude -b claude/feature/<short-name> main
```

不要让多个执行者共用同一个工作分支。

## 仓库边界

主仓库只保留产品本体相关内容。以下内容不要再提交进来：

- `.claude/` 等本地 Agent / AI 工具状态
- `.playwright-cli/`、`output/playwright/` 等调试产物
- `__pycache__/`、`.DS_Store` 等本地缓存
- `* 2.*` 这类重复备份文件

如需保留历史参考，请迁移到归档仓或本地归档区，不要继续污染主仓库历史。

本地只属于你自己的临时文件，优先放进 `.git/info/exclude`，不要一律塞进仓库共享的 `.gitignore`。
