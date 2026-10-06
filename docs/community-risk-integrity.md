# 社区风险与置信度边界

## 排名与资源数量分别表达

社区总人口 `population` 不是老年人口。本模块只接受提供方明确声明 `baseline_population_scope=all_residents` 的全居民基线，不用老龄率反推“实测老年分母”。`baseline_visits` 表示 `baseline_period_days` 天内的基线总门诊人次（或等价统计期间总量），不能把月总量当作日均。

- 日基线人次 = `baseline_visits / baseline_period_days`。
- 每千居民日基线率 = `baseline_visits / baseline_period_days / population × 1000`。
- 天气超额率 = `max(RR-1,0) × 每千居民日基线率`，单位是 **人次/千居民/日**。
- 天气危险度 `Hazard = 100 × (1-exp(-天气超额率/Efold))`。
- 资源需求 = `max(RR-1,0) × 日基线人次`，单位是 **预计额外人次/日**，单独展示。

同 RR、同日基线率的社区不因人口不同而得到不同天气危险度。内部原始分、相对分位、危险度、综合风险和影响矩阵全部不再使用额外总人次；同综合分并列排名。数量仅用于资源准备。SVI 只计权一次；完整权重仍为天气 0.45、SVI 0.35、历史负担 0.20，缺历史时前两项归一。历史负担与 SVI 仍可能因社区真实资料不同而改变综合排名。

新归一尺度配置为 `COMMUNITY_RISK_EXCESS_RATE_EFOLD`，默认 10，单位为人次/千居民/日。旧 `COMMUNITY_RISK_EXCESS_EFOLD` 是人次尺度，不再读取。此归一尺度与档位是项目展示参数，**不是经过临床校准的个人风险阈值**。统一县级 RR 与社区基线结合仍属情景筛查；真实外推效果需要验证，数量也不能被称为已验证就诊预测。

人口、基线统计天数须有限且大于零；基线人次须有限非负；缺口径、缺分母或仅有老年病例而无同口径人口时风险保持未知，不悄悄默认统计天数、年龄口径或居民人数。内置人口乘固定率的档案仍标为离线代理，不参与正式排名。

## 可实施的只读汇总输入

当前 Community ORM 没有完整基线与环境字段。可由运营方显式配置 `COMMUNITY_RISK_PROFILE_PATH`，指向受控本地 JSON；默认不配置、不读取，不改变部署环境。也可通过 Flask app.config 同名项提供路径。无需将数据或机器路径提交产品仓库。

入口要求顶层 `schema_version=1`、`source_kind=observed_aggregate`、非空 `source`、有效 ISO `reviewed_at` 与 `profiles` 数组；每行名称必须匹配已登记 Community，且 `uses_proxy_values=false`。每行必填人口、老龄率、慢病率、绿地率、热岛指数、医疗可达性、基线门诊人次、基线统计天数及全居民口径。程序白名单读取字段、检查有限值和范围，**不会自动证明来源真实、审核有效或年龄/空间/时间可比**；页面称“提供方声明核验”，提供方须保留对应来源及人工核验依据。配置文件有误时整个临床汇总输入失败关闭，不回退成另一套排名。静态 GIS 筛查仍可独立使用。

下面仅演示结构，使用合成值并明确标记 synthetic，生产入口会拒绝。真实部署必须另行准备有来源证据的真实汇总，不能把此示例改标签冒充观测。

```json
{
  "schema_version": 1,
  "source_kind": "synthetic",
  "source": "仅结构示例，不是真实社区证据",
  "reviewed_at": "2026-10-06",
  "profiles": [{
    "name": "合成社区", "uses_proxy_values": false,
    "population": 1000, "elderly_ratio": 0.3, "chronic_disease_ratio": 0.2,
    "green_space_ratio": 0.2, "heat_island_index": 0.3, "medical_accessibility": 0.7,
    "baseline_visits": 300, "baseline_period_days": 30,
    "baseline_population_scope": "all_residents"
  }]
}
```

## API 兼容与单位

`calculate_community_risk_score.risk_score`、`components.excess_risk_score` 和 `hazard_formula.excess` 现在都是天气超额率；`components.baseline_rate` 是每千居民日基线率。地图/排名列表原有 `risk_score` 仍为 0–100 危险度，避免破坏绘图消费者；显式新增 `excess_rate_per_1000_residents_day` 提供原始率，并带 `rate_unit`、`count_unit`。`expected_excess_visits` 保持数量用途，但统一为每天，不再混入危险度或影响矩阵。`hazard_formula` 补人口、全居民口径、统计天数、日基线率与日数量，便于复算。汇总提供 `hazard_rate_unit`、`resource_count_unit`。缓存升级 v7，输入指纹包含新元数据，禁止复用旧数量结果。

## 置信度与研究边界

风险不因资料不足而折减。`uncertainty_penalty=1.0` 是兼容字段，before/after 相等。置信度描述样本充分性和区间宽度，**不是预测模型校准概率**。无历史分量、低充分性或不确定性达到 70 时标低置信；综合分达到 60 且低置信标“优先核实”，保留防护。缺失温度、模拟天气、模型异常与非法 RR 均传播未知。

网格仅展示历史结构脆弱性：人口与覆盖来自 2020 年底数，地表温度是 2020–2024 年夏季。实时天气仅在县级解释。

## 排名敏感性

`conda run -n case-weather-py312 python scripts/community_risk_sensitivity.py --synthetic --output /tmp/community-rate-sensitivity.json` 产生合成方法检查，不能当作实测验证。

真实分析输入为 `source_kind=observed_aggregate`、`legacy_count_efold`（旧数量归一尺度）与 `rankings`。每行 `hazard_formula` 提供 weather_rr、baseline_visits、baseline_period_days、population、population_scope、efold；另需 vulnerability_index、svi_percentile、burden_percentile、uncertainty_index。`old_vs_new` 对比最初重复 VI/折减的数量公式，`count_vs_rate` 单独对比上一版无 VI 数量公式与新率公式。比较保留同一输入数值；如果旧系统把统计期间总量误当成日量，这一错误也属于旧算法差异，报告不将它视作有效日尺度。公开 API 的舍入分量只能近似重算，精确审计应使用未舍入汇总。

输出 tau-b、前十重合率及每项权重 ±20% 后归一的稳定性，标记排除样本、不可计算 tau 和前十边界同分；不输出名称或个人字段。一次性报告留临时或受控忽略目录。


## 生产工作台整合（2026-10）

生产默认 `/heat-exposure-gis` 保留工作台、检索、乡镇筛选、地点核验、打印与性能约束；`?ui=legacy` 保留历史科研视图。两者网格均不叠加实时天气。人口与覆盖底数为2020，LST为2020–2024夏季；不可统称所有数据都是2020。

`/heat-exposure-gis/daily.json` 的 `days` 与 `action_cards` 仅为县级天气与行动参考。旧 `priority` 逐日村级清单固定为空；新 `structural_priority` 含 `villages`（前5）、`village_ids`（全部已合格点）、`basis=historical_structure_only`、`weather_used=false`。响应另有 `realtime_risk_spatial_scale=county`、`grid_weather_used=false`。村级行 `daily_level=null`，`structural_level` 为历史结构等级，按结构分排序，人口数量不作排序权重。天气不可用时历史结构清单仍可读，县级行动卡保持未知。

下载的冻结工作台JSON保留旧研究生成记录，包括已停用的 `daily.matrix`；页面明确说明该规则不再执行。未改冻结GeoJSON字节、摘要及严格公开白名单，所有点位/目录/来源安全边界保留。社区未知天气汇总也使用 `community_scope`，不得暴露授权范围外的社区数量；病例SQL与缓存作用域延续生产实现。
