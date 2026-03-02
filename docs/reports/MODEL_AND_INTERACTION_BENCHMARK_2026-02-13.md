# 天气预报模型与交互对标（学术/官方证据版）

更新日期：2026-02-13

## 1) 对标结论（证据驱动）

### A. 多模型融合与概率化
- 官方业务实践：NOAA NBM 采用多模型集合融合并输出概率产品（官方说明见 NCEP EMC NBM）。
- 全球业务基线：ECMWF ENS 为集合预报体系（官方 ENS 页面）。
- 学术方法：EMOS/后处理校准（Gneiting et al., 2005, Monthly Weather Review）与集合预报理论（Leutbecher & Palmer, 2008, Journal of Computational Physics）。

### B. 不确定性表达
- 业务平台实践：meteoblue 明确展示多模型差异和可预报性。
- 学术共识：概率预报应同时提供中心估计和分布/不确定性信息（Gneiting et al., 2007, JASA）。

### C. 超短临（小时级/分钟级）
- 商业平台能力：AccuWeather MinuteCast 提供分钟级降水趋势。
- 平台型天气 API：Tomorrow 文档提供事件驱动与短时更新能力。

### D. 事件语义标准化
- 国际标准：CAP（Common Alerting Protocol）定义了 `severity/certainty/urgency` 等语义字段。
- 实务意义：有利于“自动化决策链路”与跨系统互操作。

### E. 交互侧规范化展示
- NWS Hazards Map 强调统一图例、固定刷新机制与风险一致性表达。
- Met Office 采用“Impact × Likelihood”矩阵增强行动决策可读性。

## 2) 本次代码改造（与证据映射）

### A. 多模型融合 + 概率化
- 文件：`services/weather_service.py`
- 改造：
  - 新增 QWeather + Open-Meteo 逐日融合；
  - 输出 `temperature_ensemble_p10/p50/p90/std`、`model_count`、`model_names`；
  - 输出 `predictability_score/predictability_label`（基于模型离散度+提前期）。

### B. 预报后处理吸收模型离散度
- 文件：`services/forecast_service.py`
- 改造：
  - `quantile_mapping` 增加 `model_spread` 输入；
  - 不确定性区间随 lead day 与模型 spread 联动；
  - 7 天预测结果增加 `predictability`、`model_fusion` 字段；
  - summary 增加 `impact_likelihood_matrix` 与 `predictability` 汇总。

### C. 短临时间轴能力
- 文件：`services/weather_service.py`, `services/api_service.py`, `blueprints/api.py`
- 改造：
  - 新增 Open-Meteo 小时级降水时间轴；
  - 新增 `/api/weather/nowcast` 与 `/api/v1/weather/nowcast`。

### D. CAP 语义字段标准化
- 文件：`services/warning_service.py`
- 改造：
  - 在原有预警输出基础上新增 `severity/certainty/urgency/response/instruction`；
  - 增加中文等级到 CAP severity 的兼容映射。

### E. 交互升级
- 文件：`templates/forecast_7day.html`
- 改造：
  - 增加“可预报性”展示（卡片+表格）；
  - 增加“未来6小时降水时间轴”；
  - 增加“影响 × 可能性矩阵”。

## 3) 参考来源（官方/论文）

- NOAA/NCEP EMC NBM: https://www.emc.ncep.noaa.gov/emc/pages/numerical_forecast_systems/nbm.php
- ECMWF Ensemble forecasts: https://www.ecmwf.int/en/forecasts/dataset/ecmwf-ensemble-predictions
- meteoblue forecast models: https://content.meteoblue.com/en/research-education/weather-data-accuracy/weather-forecast-models
- AccuWeather MinuteCast API: https://developer.accuweather.com/minutecast
- Tomorrow API reference: https://docs.tomorrow.io/reference
- Tomorrow Events overview: https://docs.tomorrow.io/reference/events-overview
- CAP standard (OASIS): https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2.html
- NWS Hazards Map help: https://www.weather.gov/help-map
- Met Office warnings guide: https://weather.metoffice.gov.uk/guides/warnings
- Met Office impact-based warnings: https://weather.metoffice.gov.uk/warnings-and-advice/seasonal-advice/when-and-why-do-we-issue-warnings
- Gneiting et al. (2005) EMOS: https://doi.org/10.1175/MWR2904.1
- Leutbecher & Palmer (2008) Ensemble forecasting: https://doi.org/10.1016/j.jcp.2008.02.014
- Gneiting et al. (2007) Probabilistic forecasts: https://doi.org/10.1198/016214506000001437

