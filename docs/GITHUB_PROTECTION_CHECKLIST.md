# GitHub 保护设置清单

> 最后更新：2026-04-11
> 仓库：`AlfWuxy/weather-Web`
> 目的：把 GitHub 云端设置补齐，让 `main` 真正成为“只保存成品历史”的受保护分支

## 先看结论

本仓库的本地护栏已经部分生效：

- 已有 PR 模板
- 已有 `CODEOWNERS`
- 已有 `.gitignore`
- 已有本地 `pre-push` 拦截

但这些还不等于 GitHub 云端保护已经开启。  
真正要防止误操作，还需要你在 GitHub Settings 里把 `main` 的保护规则打开。

## 推荐方案

### 方案 1：只开最核心的 4 项

优点：

- 操作最少
- 立刻能挡住大多数误推和误合并

缺点：

- 对 review 和 CI 的约束还不够完整

建议开启：

1. Require a pull request before merging
2. Require conversation resolution before merging
3. Require linear history
4. 只保留 `squash merge`

### 方案 2：做成完整治理版

优点：

- 更接近正式团队项目
- 后面接入 CI、多人协作更稳

缺点：

- 前期约束更强
- 如果测试还没稳定，required checks 可能先卡住合并

在方案 1 基础上，再逐步增加：

1. Require status checks to pass before merging
2. Require branches to be up to date before merging
3. Require review from code owners
4. Restrict who can push to matching branches

## 我建议你现在怎么做

先用方案 1，等测试和 CI 稳定后，再升级到方案 2。

原因：

- 你现在最主要的问题是“误推主分支”和“改动历史不清楚”
- 不是“审核流程太弱”
- 先把 `main` 护起来，收益最大、风险最小

## GitHub 网页端操作步骤

GitHub 的界面可能会显示为 `Branches`、`Branch protection rules` 或 `Rulesets`。  
如果名称略有变化，不用纠结，核心目标是一致的：让 `main` 不能被随手直接改。

### 第一步：打开仓库设置

进入：

- `https://github.com/AlfWuxy/weather-Web/settings`

### 第二步：找到分支保护或规则集

优先寻找以下入口之一：

- `Branches`
- `Rules`
- `Rulesets`

### 第三步：为 `main` 建规则

目标分支：

- `main`

建议启用：

- Require a pull request before merging
- Require conversation resolution before merging
- Require linear history

如果页面支持合并策略限制，再设置：

- 启用 `squash merge`
- 关闭普通 merge commit

### 第四步：暂时不要开的项

下面这些先不要强制开启，等测试稳定后再说：

- Required status checks
- Require branches to be up to date before merging
- 严格的 reviewer 数量限制

## 开启后的验收标准

只要满足下面几条，就算第一阶段 GitHub 云端保护完成：

1. 直接向 `main` 开 PR 之外的改动会被 GitHub 拦住
2. 合并 `main` 前必须经过 PR
3. `main` 历史不再出现杂乱 merge commit
4. 以后默认都走 `squash merge`

## 与当前仓库文件的关系

这个清单依赖以下本地治理文件共同生效：

- `docs/AI_COLLABORATION_AND_BACKUP_PLAYBOOK.md`
- `CONTRIBUTING.md`
- `.github/pull_request_template.md`
- `.github/CODEOWNERS`
- `.githooks/pre-push`

## 验收记录模板

这份清单本身只描述目标状态，不长期记录某一天的阶段性完成情况。
如果你要做一次当日验收，请把结果写到 PR 描述、Notion 条目或本地归档说明里，避免把临时状态长期留在规则文档中。

每次验收至少记录：

- 验收日期
- 验收人
- 当前规则目标是否满足
- 哪些项仍需手动完成

## 参考依据

- [GitHub Docs: About protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [GitHub Docs: About merge methods on GitHub](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/about-merge-methods-on-github)
- [GitHub Docs: About code owners](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners)
