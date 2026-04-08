# 仓库边界与第一阶段清理分类

> 目的：把主仓库收敛为“天气网站产品仓库”，不再混入本地工具状态、重复备份文件和无关历史材料。

## 1. 主仓库保留范围

以下内容属于主仓库，应继续保留并维护：

- `app.py`、`core/`、`blueprints/`、`services/`、`utils/`
- `templates/`、`static/`、`miniprogram/`
- `tests/`
- `scripts/` 中仍用于部署、同步、维护的脚本
- `docs/` 中与产品架构、状态、测试、评审相关的正式文档
- `requirements.txt`、`pytest.ini`、`.env.example` 等工程配置

## 2. 归档到主仓库之外的内容

以下内容不再属于天气网站产品本体，应迁到归档仓或本地归档区：

- `.claude/` 下的本地 Agent 配置和工作树副本
- 所有 `* 2.*` 重复备份文件
- 根目录中的非产品型历史材料，如：
  - `var_lpf2_cascade.m`
  - `项目大纲.docx`

### 第一阶段归档对象

- `.claude/`
- `docs/**/* 2.*`
- `static/**/* 2.*`
- `templates/**/* 2.*`
- `var_lpf2_cascade.m`
- `项目大纲.docx`

## 3. 只做本地忽略，不进入 Git

以下内容属于本地运行痕迹，应忽略而不是提交：

- `.playwright-cli/`
- `output/playwright/`
- `__pycache__/`
- `.pytest_cache/`
- `.DS_Store`
- `.env.backup`

### 忽略规则分层

- 整个项目都不该提交的内容，写进仓库根目录 `.gitignore`
- 只属于某一台电脑或某个执行者的临时文件，写进 `.git/info/exclude`
- 已经被 Git 跟踪过的噪音文件，先 `git rm --cached <path>`，再交给 ignore 规则处理

## 4. 第一阶段处理规则

### 保留

- 正在使用的主版本文件
- 当前产品页面、样式、脚本与测试
- 产品相关正式文档

### 归档

- 重复版本文件
- 本地 Agent 状态
- 与天气网站主线无关的历史资料

### 忽略

- 浏览器快照
- 测试缓存
- Python 缓存
- 本地环境临时文件

## 5. 实施顺序

1. 先补 `.gitignore` 和工程规范文档
2. 把应忽略的内容从版本管理中移出
3. 把重复文件和无关文件迁到归档区
4. 用新的 GitHub 流程提交 PR

## 6. 说明

本轮清理默认遵循两个原则：

- 不碰仍在使用的主版本文件
- 不直接粗暴删除历史内容，先归档再移出主仓库

当前已建立的本地归档预备目录：

- `/Users/imac/Desktop/老家/weather-web-archive-staging`

这个目录用于暂存从主仓库移出的重复文件和无关历史材料，后续可再推送为独立 GitHub 归档仓。

## 7. 与协作流程的关系

仓库边界和 Git 工作流必须一起看：

- `main` 只保留成品历史
- 日常开发一律在分支中进行
- 多 AI 助手同时工作时，优先使用 `git worktree` 或独立副本隔离
- 日常备份以“分支 push + Draft PR + Notion 回填”为准

具体执行细节见 `docs/AI_COLLABORATION_AND_BACKUP_PLAYBOOK.md`。
