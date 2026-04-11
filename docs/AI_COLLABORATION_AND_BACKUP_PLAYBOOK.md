# AI 协作与备份作业手册

> 最后审阅：2026-04-08
> 适用范围：本仓库内所有人工开发、Codex、Claude Code、其他 AI 助手
> 目标：把“个人项目式开发”变成“可回看、可验证、可交接、可备份”的正式流程

## 先读这个

以后任何人或 AI 助手，只要准备在这个仓库里改代码、备份改动、开分支、提 PR，都必须先读下面 5 个文件：

1. `docs/REPO_OPERATION_PROTOCOL.md`
2. `docs/AI_COLLABORATION_AND_BACKUP_PLAYBOOK.md`
3. `CONTRIBUTING.md`
4. `docs/REPO_BOUNDARY_AND_CLEANUP.md`
5. `README.md`

如果这些文件之间有冲突，以 `docs/REPO_OPERATION_PROTOCOL.md` 为准。

## 不可打破的规则

1. `main` 只能保存“已经合并完成的成品历史”，不能作为日常开发分支。
2. 任何代码、文档、样式、测试改动，都必须先开分支，再提交，再走 PR，再合并。
3. 每个分支只做一件事，每个 PR 只解决一个清楚的问题。
4. `git add .` 不是默认操作。只暂存这次真的要提交的文件。
5. commit 标题必须清楚，不能写成 `update`、`test`、`misc` 这种模糊描述。
6. 本地工具痕迹不能靠肉眼躲避，必须靠 `.gitignore`、`.git/info/exclude` 和分支保护来挡。
7. 多个 AI 助手不能共用同一个工作分支；更稳妥的做法是一个助手一个 `worktree`。
8. 如果没有完成验证，就开 Draft PR，不直接假装“已经完成”。

## 这套流程为什么适合这个项目

这套规则不是为了“看起来专业”，而是为了解决这个仓库已经真实遇到过的问题：

- `main` 上混入临时改动，后面很难回看“哪次改动为什么出现”
- 不同 AI 助手或不同人共用一个工作区，容易互相覆盖
- 本地工具文件、测试产物、重复备份文件混入 Git 历史
- commit 和 PR 太大，后面很难复盘、回滚和继续接手

结合 GitHub 官方文档、Git 官方文档和开发者社区共识，这套规则对当前项目是“低风险、高收益”的：

- `main` 更干净，回看历史更容易
- AI 助手更容易自动遵守固定流程
- 推送和备份更稳定，不容易把不该提交的东西带上去
- 以后增加协作者、CI、部署时也不用重来一遍

## 三个平台怎么分工

### Notion

负责“为什么做、做什么、优先级是什么”：

- 想法
- 产品迭代库
- 用户问题
- 发布节奏
- 验证结果摘要

### GitHub

负责“代码怎么改、改了什么、怎么被合并”：

- 分支
- commit
- PR
- 合并历史
- release
- issue / 备注

云端保护设置的手动步骤见 `docs/GITHUB_PROTECTION_CHECKLIST.md`。

### 本地仓库

负责“实际开发和第一层保护”：

- `.gitignore`
- `.git/info/exclude`
- `.githooks/pre-push`
- `git worktree`
- 运行与测试环境

## 首次进入仓库后的本地护栏初始化

### 启用本仓库的 Git hooks

本仓库已经跟踪了 `.githooks/pre-push`，但每个本地副本都要单独启用一次：

```bash
git config core.hooksPath .githooks
chmod +x .githooks/pre-push
```

启用后，Git 会在你尝试直接 push 到 `main` 或 `master` 时拦截并提醒你走正确流程。

### 可选：补充你本机自己的忽略规则

如果你电脑上还有一些不适合写进仓库共享 `.gitignore` 的私人临时文件，可以写进：

```bash
.git/info/exclude
```

## 标准开发流程

### 1. 开工前

先跑这 4 个命令：

```bash
git branch --show-current
git status --short
git remote -v
git diff --staged
```

目的不是“形式主义”，而是确认三件事：

- 当前是不是在错误的分支上
- 工作区里有没有不属于本次任务的改动
- 暂存区里有没有误加的文件

### 2. 先在 Notion 立项

先在“网站迭代库”里确认或新建条目，再开始改代码。每条条目都要能回答：

- 这次到底解决什么问题
- 这次最小可交付版本是什么
- 这次对应哪个分支

### 3. 再开分支

#### 人工分支命名

- `feature/<short-name>`
- `fix/<short-name>`
- `chore/<short-name>`

#### AI 助手分支命名

推荐加上执行者前缀，避免多个助手混淆：

- `codex/feature/<short-name>`
- `codex/fix/<short-name>`
- `codex/chore/<short-name>`
- `claude/feature/<short-name>`

示例：

- `codex/chore/repo-governance-docs`
- `claude/fix/mobile-login-copy`
- `feature/wechat-entry`

### 4. 在分支上小步提交

commit 标题统一使用：

- `feat: 新功能`
- `fix: 修复问题`
- `refactor: 重构`
- `docs: 文档更新`
- `style: 样式调整`

示例：

```text
feat: 新增老人模式入口
fix: 修复社区风险页地图空白
docs: 固化 AI 协作与备份流程
```

### 5. 本次改动只暂存本次文件

推荐：

```bash
git add README.md CONTRIBUTING.md docs/AI_COLLABORATION_AND_BACKUP_PLAYBOOK.md
git diff --staged
```

不推荐：

```bash
git add .
```

如果误加了不该提交的文件：

```bash
git restore --staged <path>
```

### 6. 提 Draft PR

PR 默认回答这三件事：

1. 这次改了什么
2. 为什么要改
3. 怎么验证

如果还没验证完，就保持 Draft，不要急着合并。

### 7. 合并方式

默认使用 `squash merge`，保持 `main` 历史整洁。

### 8. 回写 Notion

PR 创建或合并后，回到 Notion 更新：

- 状态
- 分支名
- PR 链接
- 验证方式
- 结果摘要

## 备份流程

### 日常工作备份

这是最常用、最重要的备份方式。

1. 在功能分支上完成本轮改动
2. 本地 commit
3. 推送分支到 GitHub
4. 创建或更新 Draft PR
5. 把 PR 链接回填到 Notion

这代表：

- 代码在 GitHub 上有远端副本
- 改动范围有 PR 说明
- 需求侧有 Notion 对应条目

### 会话结束前的最小备份清单

无论是人还是 AI 助手，结束本轮工作前至少完成：

1. `git status --short` 确认没有误暂存
2. 本轮改动有清楚 commit
3. 当前分支已经 push
4. Draft PR 已存在，或者明确记录了为什么暂时不开
5. Notion 已写入当前分支名和 PR 链接

### 仓库级冷备份

如果要做“完整仓库镜像备份”，优先使用 GitHub 官方推荐的镜像克隆方式：

```bash
git clone --mirror <repo-url>
```

适用场景：

- 迁移机器
- 做长期离线备份
- 在独立磁盘保存仓库镜像

不适用场景：

- 日常开发
- 替代分支 + PR 工作流

### 本地归档最小规则

当某些文件需要“保留证据，但不能继续留在公开产品仓库”时，使用本地归档目录，而不是继续塞回 Git 历史。

最小要求：

1. 先记录当前 `origin/main` 的 commit SHA
2. 归档目录必须区分：
   - `current-worktree/`
   - `origin-main-snapshot/`
3. 归档目录必须包含 `manifest.json`
4. 归档目录至少要有一份人类可读说明
5. PR 或清理记录里必须回填归档路径

本仓库的内容去向判断，优先遵循 `docs/REPOSITORY_CONTENT_POLICY.md`。

## 多个 AI 助手同时工作时怎么隔离

如果 Codex、Claude Code、你本人可能同时碰这个仓库，推荐每个执行者一个 `worktree`。

### 推荐目录

- `../weather-web-codex`
- `../weather-web-claude`
- `../weather-web-human`

### 示例命令

```bash
git worktree add ../weather-web-codex -b codex/chore/<short-name> main
git worktree add ../weather-web-claude -b claude/feature/<short-name> main
```

这样做的好处：

- 文件不会互相覆盖
- 每个助手有自己独立工作目录
- 即使同时开发，也更容易看清各自改了什么

不建议多个助手共用同一个工作目录直接轮流改。

## 忽略规则怎么分层

### 共享给整个项目的忽略规则

写进仓库根目录 `.gitignore`。适合：

- `.claude/`
- `.playwright-cli/`
- `output/playwright/`
- `__pycache__/`
- `.env`

### 只属于你本机的忽略规则

写进 `.git/info/exclude` 或全局 Git ignore。适合：

- 只有你自己电脑上才会出现的临时文件
- 不适合写进仓库共享规则的个人工具文件

### 如果文件已经被 Git 跟踪了

只写 `.gitignore` 不够，还要先把它移出版本管理：

```bash
git rm --cached <path>
```

## GitHub 仓库设置建议

以下设置最值得在 GitHub 网页端打开：

### 对 `main` 做保护

- Require a pull request before merging
- Require conversation resolution before merging
- Require linear history
- 在 CI 稳定后启用 required status checks

### 合并策略

- 保留 `squash merge`
- 关闭普通 merge commit

### 模板与责任边界

- 保留 PR 模板
- 保留 `CODEOWNERS`

说明：

- 当前个人仓库也适合做这些基础保护
- 如果以后多人长期协作，迁到 GitHub Organization 后，权限和规则会更好管
- GitHub 网页端逐步设置清单见 `docs/GITHUB_PROTECTION_CHECKLIST.md`

## 给 AI 助手的执行清单

任何 AI 助手在这个仓库里工作前，都应按下面顺序执行：

1. 阅读本文件、`CONTRIBUTING.md`、`docs/REPO_BOUNDARY_AND_CLEANUP.md`
2. 运行 `git branch --show-current` 和 `git status --short`
3. 如果当前在 `main` 且需要修改文件，先创建新分支
4. 如果存在别人的未提交改动，不主动回滚，不顺手清理
5. 只修改当前任务直接相关的文件
6. commit 前先看 `git diff --staged`
7. push 后创建或更新 Draft PR
8. 如有对应条目，回 Notion 更新分支、PR、验证信息

## 出错时怎么补救

### 不小心在 `main` 上改了文件

立即把当前改动转到新分支：

```bash
git switch -c chore/<short-name>
```

### 不小心暂存了别人的文件

把不属于本次任务的文件移出暂存区：

```bash
git restore --staged <path>
```

### 不确定这次改动是否应该提交

先不要 push，先做这三步：

1. `git status --short`
2. `git diff`
3. `git diff --staged`

如果还是不确定，就先保留在分支里，不要往 `main` 推。

## 参考依据

以下资料是本手册的主要依据：

- [GitHub Docs: About protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [GitHub Docs: Creating a pull request template for your repository](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository)
- [GitHub Docs: About merge methods on GitHub](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/about-merge-methods-on-github)
- [GitHub Docs: Ignoring files](https://docs.github.com/en/enterprise-cloud%40latest/get-started/git-basics/ignoring-files)
- [GitHub Docs: Backing up a repository](https://docs.github.com/en/repositories/archiving-a-github-repository/backing-up-a-repository)
- [Git: githooks](https://git-scm.com/docs/githooks)
- [Git: git-worktree](https://git-scm.com/docs/git-worktree)
- [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
- [Google Testing Blog: In praise of small pull requests](https://testing.googleblog.com/2024/07/in-praise-of-small-pull-requests.html)

## 一句话总结

这套仓库以后默认遵循这条线：

Notion 立项 -> 新分支开发 -> 小步 commit -> Draft PR -> 验证 -> squash merge -> 回写 Notion -> 必要时做镜像备份
