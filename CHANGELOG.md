# Changelog

本文件记录对外可见或对仓库维护方式有影响的重要变更。

## [Unreleased]

### Added

- 热暴露 GIS 升级为“热风险医生工作台”（默认视图）：天地图 / 高德底图与 GCJ-02 纠偏、湖面屏蔽、OSM 乡镇边界、综合热风险分（危险性 × 暴露 × 脆弱性）、高温 × 高龄双变量图、Getis-Ord Gi* 热点、权重扰动排名稳定性、医疗点可达距离、7 天逐日热危险等级（35/37/40 °C + 本地热夜阈值）、村级巡访优先清单、行动卡与打印巡访单
- 新增 `/heat-exposure-gis/daily.json` 逐日接口与 `HEAT_EXPOSURE_GIS_UI`、`TIANDITU_TK` 配置；v1.2 科研版保留为 `?ui=legacy` 回滚目标，方法与回滚步骤见 `docs/gis/heat-risk-workbench-method.md`
- 热风险工作台新增“夜间地表温度”图层：Aqua MYD11A1 v061 夜间 LST（2020–2024 年 6–8 月，448 景，Q3 + 误差 ≤ 2 K），经 Microsoft Planetary Computer 获取，抓取脚本 `scripts/fetch_duchang_night_lst.py`，同口径复现白天图层逐格一致
- 补充仓库标准开发流程说明
- 新增 PR 模板
- 新增仓库边界与清理分类文档

### Changed

- 综合热风险分的危险性分量改为白天与夜间地表温度百分位的平均再排名（schema 2.1.0）；滨湖低地村庄风险上升，等级分布变为 229/498/504/346/29
- 逐日热危险上调规则改为法国 SACS 式 3 日均温双触发：3 日平均最高 ≥ 34.2 °C 且 3 日平均最低 ≥ 26.3 °C（都昌 1,036 天本地 P95）时上调一级，取代单日热夜与连续高温日规则
- 仓库切换到 `分支 + commit + PR + squash merge` 工作流
- README 从占位内容升级为正式项目首页说明
- `.gitignore` 增强，隔离本地工具状态与测试产物

### Removed

- 计划从主仓库历史中移出本地 Agent 工作树、重复备份文件和调试产物
