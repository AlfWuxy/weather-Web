# 时间外验证与先验研究

公开成绩单并列显示两条独立证据链：

- 严格事前归档：读取 `tmp/backtest_report.json`；缺报告或旧版报告显示未验证，不纳入版本控制。
- 回顾性探索：读取已去敏的 `static/data/retrospective_exploratory.json`；本文件仅含汇总成绩、来源哈希和方法边界，可纳入版本控制。

透明度页与管理页使用同一解释。**这是记录计数回顾性比较，尚未验证高温健康模型。** 探索性结果不解除覆盖核验、归档或前瞻验证门槛。

## 生成成绩单

```sh
conda run -n case-weather-py312 python scripts/backtest_forecast.py --daily /path/to/approved_daily.csv --predictions /path/to/issued_forecasts.csv
```

日聚合 CSV 必须包含 `date,cases_60plus,coverage_confirmed`。覆盖未确认的日期不能补零，也不能用于训练或评分。比较归档预测时还必须含 `available_at`（带时区），记录结局当时何时可以获取。

预测 CSV 必须包含 `target_date,issued_at,training_end,inputs_available_at,prediction,outcome,model_version`，可附 `lower_80,upper_80,lower_95,upper_95`。所有证据时间须含时区；训练必须早于发行，所有特征须在发行前可用，预测必须在目标日开始前发行。不同提前天数分别出成绩单。文件由经过来源审核的归档流程提供，不能事后伪填时间字段来证明前瞻性。

逐日起点比较同一组日期上的预测与每7日季节朴素基线，报告 MAE、RMSE、80/95% 实际覆盖率及各覆盖率样本数。基线区间只用该起点以前的残差经验分位数，不宣称理论校准。未提供预测归档时只能报告基线，生产模型继续显示未验证。真实前瞻验证须有另外审核的实时研究记录，当前管线不会把回测算成前瞻样本。

原脚本加载全历史生产曲线后回代同一段历史，不是滚动起点验证；新版不再公布这类结果为预测能力。不把历史再分析天气当作当时已发行的预报，不把旧口径 `elderly_cases` 重标为年龄60岁及以上门诊量。

方法参考：[Forecasting: Principles and Practice 的时间序列交叉验证](https://otexts.com/fpp3/tscv.html)与[预测区间](https://otexts.com/fpp3/prediction-intervals.html)。

## 多城市先验与本地更新

`services/research_prior.py` 提供系数层正态近似更新接口，必须输入已审核文献的系数与完整协方差，以及同口径本地估计。该接口不等于完整计数层贝叶斯 DLNM，也不会写生产 profile。结局、年龄组、温度单位、基函数、滞后窗口和参考温度必须一致；死亡效应不可直接当作门诊效应先验。

研究参考：[Gasparrini 等，多参数非线性关联的多变量荟萃分析](https://pmc.ncbi.nlm.nih.gov/articles/PMC3546395/)与[DLNM 系数约化及荟萃分析](https://pmc.ncbi.nlm.nih.gov/articles/PMC3599933/)。这些资料说明系数与协方差的处理方式，不提供本项目可直接采用的中国多城市门诊先验。

仍需真实工作：获取并审核匹配结局的国内多城市系数/协方差；确认本地逐日覆盖与零值；争取2018–2023医院历史数据；进行弱/强先验敏感性、时间外比较和前瞻验证。数据授权和医院交付不能由代码实现替代。


## 固定旧快照的回顾性探索（2026-10-06）

```sh
conda run -n case-weather-py312 python scripts/backtest_forecast.py \
  --mode retrospective_exploratory \
  --daily /path/to/analysis/duchang_health_weather/outputs/clean/analysis_daily.csv \
  --coverage-ledger /path/to/analysis/core_algorithm_optimization_v1_1/coverage_ledger.csv
```

原始聚合源位于本机主工作区的忽略目录；命令显式传入路径，不自动读取个人病历。台账与快照日期、结局、逐日记录数必须完全一致；这种一致性不等于台账覆盖已核验。公开文件不含逐日低计数、姓名、病情或地址。它附带输入/台账/实现文件 SHA-256 与运行库版本，便于对照复算。重新运行会改变生成时间，但相同输入、实现与库下的指标保持一致。

### 已核对的输入口径

`analysis_daily.csv` 为 2023-12-13 至 2025-01-30 的 415 日快照。`cases_60plus` 是旧清洗程序按年龄 >=60 汇总的**记录计数**，不能和另一历史表的 `elderly_cases`（疑似65+）混用。恢复的清洗脚本曾补齐无记录日为0、没有去重；标签调查发现重复记录和报告结构变化，此次不改标签或裁决零值。台账415日的来源覆盖、结局最终性与首次可用时间均未核验，`eligible_primary` 全为False。

主分析保留快照中144个60+零记录日（其中139日全部年龄无记录），意为模型比较这个有瑕疵的记录快照，不表示这些日子真的无人就诊。页面同时给出只评分全部年龄有记录目标日的敏感性结果；按目标记录筛选有选择偏倚，不能选取更好看的结果取代主结果。训练数据与预测在敏感性分析中完全不变。

### 预先固定的方法

候选 `record-count-poisson-ridge-v1` 是独立的历史记录计数模型，不是生产DLNM，也未更换任何生产曲线。使用星期的6个哑变量、`log1p(y[t-7])`、`log1p(mean(y[t-7:t-1]))`、`log1p(mean(y[t-28:t-1]))`；不使用目标天气、目标计数或未来信息。PoissonRegressor固定 `alpha=1.0,max_iter=300,tol=1e-8`，每个起点重新拟合；标准化也仅在当次历史训练样本拟合。没有全样本选参或按这次成绩选择候选。Poisson只估计均值，不假设真实计数方差等于均值。

最初28日只用于形成滞后特征；之后至少84个特征完整的历史训练行，逐日扩展训练窗口。再积累28个真正“先预测、后见目标”的滚动残差起点；主评分从2024-05-01开始，至2025-01-30，共275日。共140日未进入评分（28+84+28）。这是假设前一日日终计数可用的一日步长回放，不是经发行时间证明的提前一天预测。目标日实测天气也未使用，因此此候选不属于天气hindcast，也不能回答高温效应是否有效。

基线为同一目标日的前7日记录数。两种方法均用各自在当前起点之前积累的滚动预测绝对残差的80/95%经验分位数（`method=higher`）作为对称区间半宽，下界截为0。最初28个校准起点不计分；每个测试日评分后才把该日残差用于下一起点。没有从全样本残差、目标残差或训练拟合残差校准。覆盖率与平均宽度均是测试期实测表现，不保证将来覆盖。

### 真实运行结果

| 集合 / 方法 | n | MAE | RMSE | 80%覆盖 | 95%覆盖 |
|---|---:|---:|---:|---:|---:|
| 主要 / 固定候选 | 275 | 1.8259 | 2.5775 | 79.27% | 96.00% |
| 主要 / 每周季节朴素 | 275 | 2.5818 | 3.6571 | 81.82% | 96.36% |
| 排除无记录目标 / 同一候选 | 241 | 1.7929 | 2.6000 | 81.33% | 95.44% |
| 排除无记录目标 / 同一基线 | 241 | 2.6432 | 3.7344 | 82.99% | 95.85% |

主要集合的候选MAE比基线低29.28%；敏感性集合排除34个全部年龄无记录目标日，MAE低32.17%。这些是旧计数快照的开发性误差差异，不能宣传成临床风险准确率、稳定高温效应、生产DLNM性能或真实前瞻收益。全期间只有一个夏季；零值、重复记录、覆盖与录入延迟仍需来源审定。`prospective_n=0`、`production_model_validated=false`。

实现参考：[PoissonRegressor官方文档](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.PoissonRegressor.html)与[逐日起点时间序列交叉验证](https://otexts.com/fpp3/tscv.html)。
