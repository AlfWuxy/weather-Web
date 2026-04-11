# 仓库操作硬规则

> 最后更新：2026-04-11
> 作用：给人工和 AI 助手一个最短可执行版本，进入仓库前先读这份。

## 适用范围

适用于本仓库内所有代码、文档、样式、脚本、测试和备份动作。

## 先做什么

每次开始工作前，先确认 5 件事：

1. 当前目录就是天气网站仓库
2. 本地基线已经和远端同步
3. 当前基线来自 `main`
4. 本轮工作有独立分支
5. 本轮工作只解决一个清楚的问题

建议先跑：

```bash
git fetch origin --prune
git branch --show-current
git status --short
git remote -v
git diff --staged
```

## 不可打破的规则

1. `main` 只保存已经合并完成的成品历史。
2. 日常开发必须先开分支，再 commit，再提 PR，再合并。
3. 一个分支只做一件事，一个 PR 只解决一个问题。
4. `git add .` 不是默认操作，只暂存本轮真的要提交的文件。
5. commit 标题必须清楚，禁止使用 `update`、`test`、`misc` 这类模糊标题。
6. 本地工具痕迹、缓存、测试产物必须靠 `.gitignore` 或 `.git/info/exclude` 挡住。
7. 含真实 key、真实 host、真实服务器路径、默认口令的内容不能进入公开产品仓库。
8. 一次性报告、WIP 清单、AI 提示词、历史快照不留在主仓库，进入本地归档区或私有仓。
9. 多个 AI 助手不能共用同一个开发分支，优先一个助手一个 `worktree`。
10. 未完成验证的分支只开 Draft PR，不直接合并。

## 文件去向规则

改文件前，先判断它属于哪一类：

- 产品主仓库：运行代码、必要测试、必要脚本模板、长期治理文档
- 本地归档区：一次性报告、历史快照、WIP 分类、AI 提示词、重复副本
- 私有 ops 仓：真实部署目标、真实备份路径、真实服务器资料、私有 runbook
- 本地忽略区：缓存、快照、工作树副本、个人机器临时文件

如果分类不确定，先标记“待裁决”，不要直接提交。

## 标准工作流

1. 先同步本地基线：`git fetch origin --prune`，必要时在 `main` 上执行 `git pull --ff-only`
2. 在 Notion“网站迭代库”里确认本轮条目
3. 如果当前执行者没有 Notion 访问能力，先在 PR 描述中写清条目占位和待回填责任
4. 从 `main` 拉出分支
5. 小步提交
6. 提 Draft PR
7. 写清楚“改了什么 / 为什么改 / 怎么验证”
8. 验证通过后使用 squash merge
9. 回填 Notion 的分支名、PR 链接和验证结果

## 命名约定

分支前缀：

- `feature/`
- `fix/`
- `chore/`
- `codex/feature/`
- `codex/fix/`
- `codex/chore/`

commit 前缀：

- `feat:`
- `fix:`
- `refactor:`
- `docs:`
- `style:`

## AI 助手执行规则

1. 改文件前先说明要改哪些文件。
2. 涉及迁移、归档、去敏时，先列出文件清单。
3. 涉及多种方案时，先写清楚优缺点，再执行。
4. 开始改动前，先读 `docs/REPOSITORY_CONTENT_POLICY.md` 判断文件去向。
5. 如果发现当前工作区已经有别的未提交改动，优先用独立 `worktree` 或独立副本处理。
6. 如果本轮目标是“让仓库更干净”，优先删噪音文档、重复副本和一次性记录，不碰运行链路。

## 合并前检查

- PR 是否只覆盖一个问题
- 暂存区里是否只有本轮文件
- 是否把该归档的内容留在了主仓库
- 是否误带了真实环境信息
- README / PR 模板 / 协作文档是否仍然一致

## 冲突优先级

如果多份文档有冲突，优先级如下：

1. `docs/REPO_OPERATION_PROTOCOL.md`
2. `docs/REPOSITORY_CONTENT_POLICY.md`
3. `docs/AI_COLLABORATION_AND_BACKUP_PLAYBOOK.md`
4. `README.md`
