# PRD-06 历史情境回放 + 证据包导出

状态：APPROVED 2026-09-05 · 实施：Codex

## A. 历史情境回放（W6）

### 目的

在没有真实高温的申请季，用 2024 年都昌真实高温日 + 合成 pair 跑一遍完整行动链，验证漏斗、推送隔离、导出与缺口清单能在压力下产出。全部产物标注"历史情境模拟 / 合成用户"。它证明流程能跑，不证明老人会行动。

### 输入

- `data/duchang_2024_heat_days.csv`：从本地离线气象清洁表中提取 2024 年 Tmax ≥ 35°C 的日期与 Tmax（约 26 天），只含 `date,tmax,tmin`，公开气象数据，不含任何个人信息。若清洁表不可用，从 `weather_data` 表提取；两者都不可用则脚本报错退出，不伪造。
- 合成 pair：N=30，`qa_sim_` 前缀，`is_test=true`，在独立 SQLite（`instance/replay_<timestamp>.sqlite`）里创建，绝不写生产库。

### 脚本 `scripts/replay_historical_heat.py`

- 强制环境：`YILAO_SIMULATION=1`、`PUSH_DISABLED=1`；启动时断言 WxPusher / notification 发送函数被替换为记录器，否则退出。
- 每个高温日：为每个 pair 生成 delivered（channel 随机于 manual / wxpusher，script_version 随机三版，messenger_role 随机）；按可配置概率生成 seen → understood → action_selected → self_reported 或 help_requested → help_acknowledged → closed；一部分 pair 停在 unknown；一部分产生 verified_without_self_report；一部分产生误触序列。概率写在脚本顶部并输出到报告。
- 资源：为 8 个合成资源点设置 verified / unverified / closed_reported 混合，生成 cooling_feedback。
- 输出到 `outputs/replay_<timestamp>/`：`funnel.csv`、`events.csv`（pair_hash）、`scripts_crosstab.csv`、`resource_gaps.csv`、`report.md`（首行大字：**历史情境模拟 · 合成用户 · 不代表真实老人行为**），以及"演练后缺口清单"：哪些阶段的中位时长超过阈值、未结求助数、无可行避暑选择的 pair 比例、事件来源完整率不足的渠道。
- 仪表板：当应用检测到 `YILAO_SIMULATION=1` 时，所有页面顶部显示红色横幅「模拟环境」，`/analysis/pilot` 标题加「（模拟）」。

### 测试

- 脚本在临时目录跑通并生成全部文件；report.md 首行含"模拟"。
- 生产配置下（无 YILAO_SIMULATION）脚本拒绝运行。
- 推送记录器捕获到的调用数 = help_requested 事件数，且真实发送函数从未被调用。

## B. 证据包导出（W7）

### 脚本 `scripts/export_evidence_pack.py`

输入：生产库只读连接（或指定的 SQLite 路径）、日期范围。输出 `outputs/evidence_pack_<date>.zip`：

- `funnel_daily.csv`、`events.csv`（pair_hash，无明文 id）、`scripts_crosstab.csv`、`resource_gaps.csv` + `resource_gaps_summary.json`、`cooling_verification_ledger.csv`（复制自 docs/data）、`usability_log.csv`（复制自 docs/research 若存在）、`missing_and_failures.md`（unknown 比例、未结求助、失败到访、事件来源不完整、测试 pair 数）、`traffic_note.md`（模板：Cloudflare 窗口、2026-08-12–16 自动化测试污染说明，数字由人工填写）、`claim_ceiling.md`（从 `core/status_content.py` 生成，与 `/status` 一致）、`version.txt`（git describe + 提交日期）。
- 排除 is_test pair；如包含则文件名带 `_with_test`。
- 全部文件经过 PII 扫描：正则检查手机号、身份证号、常见姓氏+名模式，命中则中止并报路径。

### README 与 `/status` 对齐

`README.md`（英文段）与 `/status` 的英文三行、一句话边界、VERIFIED / PROTOTYPE / UNVERIFIED / NO-GO 四栏必须由同一来源生成：新增 `scripts/render_status_readme.py` 把 `core/status_content.py` 渲染成 README 的 `<!-- STATUS:BEGIN -->…<!-- STATUS:END -->` 区块；CI 或 pytest 断言两者一致。

### Release

- `git tag -a v2026.10-freeze -m ...` 在 W7 由人工执行；脚本只检查工作树干净并打印建议命令。
- 60 秒录屏为人工任务：`docs/product/recording_script_60s.md` 给出镜头顺序（短码页三按钮 → 家属核验 → 结案 → /analysis/pilot 漏斗 → /status），不含任何真实姓名。
