# GitHub 清理记录 2026-04-10

> 目标：把公开 GitHub 仓库继续收敛为“天气网站产品仓库”，把内部材料、重复副本和敏感痕迹移到本地归档或替换为占位值。

## 本地归档位置

- `/Users/imac/Desktop/老家/weather-web-github-archive-2026-04-10`

归档目录分为两层：

- `current-worktree/`
  说明：本次从当前工作树移出的文件备份
- `origin-main-snapshot/`
  说明：`origin/main` 历史快照中已存在、但治理分支已移除的文件备份

## 本次直接移出 GitHub 仓库的内容

- AI / 本地协作材料
  - `.learnings/ERRORS.md`
  - `BUG_FIX_PROMPT.md`
- 含真实病历样例或内部说明的资料
  - `docs/data/**`
  - `docs/guides/使用说明*.txt`
  - `docs/guides/天气API集成说明*.txt`
  - `docs/guides/快速测试*.txt`
  - `docs/status/**`
- 重复副本
  - `data/raw/逐日数据 2.csv`
  - `origin/main` 快照中的 `* 2.*` 重复文件

## 本次保留但去敏的内容

- `docs/ARCHITECTURE.md`
- `PROJECT_OVERVIEW.md`
- `docs/reports/COMPREHENSIVE_FIX_PLAN.md`
- `.env.example`
- `scripts/reset_admin.py`
- `scripts/deploy.sh`
- `scripts/sync.sh`
- `scripts/test_fixes.py`
- `miniprogram/config.js`

## 本次移到私有 ops 范围的内容

- `scripts/download_backup.sh`
  - 原因：直接接触生产备份下载路径，不适合继续留在公开产品仓库

## 后续规则

- 公开仓库只保留产品代码、必要测试、长期治理文档、可复用脚本模板
- 真实服务器地址、真实部署路径、密码式运维流程、专属第三方 Host 进入私有 ops 环境
- 一次性报告、AI 提示词、个人笔记、带 ` 2` 后缀的重复文件，不再进入主仓库历史
