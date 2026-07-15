# 都昌县 1 km 网格级热暴露 GIS 方法说明

版本：v1.1 研究原型

生成日期：2026-07-15
展示坐标系：WGS84，EPSG:4326

## 1. 研究目的

本页面用于描述都昌县夏季晴空地表温度、模型化老年人口比例、地表覆盖与表面高程的空间分布，并提供逐网格追溯入口。它不提供个人健康风险判断、临床建议、气温替代值或因果效应估计。

## 2. 空间单位

- 原生网格：NASA MODIS h28v06 正弦投影网格。
- 正弦投影球半径：6,371,007.181 m。
- 原生像元边长：约 926.625 m。
- 县域纳入规则：网格中心点严格位于 geoBoundaries 都昌县 ADM3 研究边界内。
- 展示规则：保留完整原生网格，不沿县界裁切。
- 最终县域中心网格：2,593 格。
- 正人口支持网格：1,721 格；零人口支持网格：872 格。

地图几何通过原生正弦投影仿射参数计算四角，再转换为 WGS84。生成器会逐格比较计算中心点与冻结 `cell_universe.csv` 中心点，容差为 1e-6 度，任何超限都会终止构建。

## 3. 时间与地表温度

- 产品：NASA Aqua MODIS MYD11A1.061。
- 时段：2020-06-01 至 2024-08-31，每年仅纳入 6、7、8 月，共 460 个日历日期。
- 官方目录可用场景：449。
- 本地冻结场景：448。
- Q3 合格定义：mandatory QA 为 00 或 01，且地表温度原始编码有效。
- Q3 合格网格日：250,270。
- 页面温度指标：各网格在研究期内 Q3 合格 Aqua 白天晴空地表温度均值。

温度换算和多年夏季均值为：

```text
LST_C = Raw × 0.02 − 273.15
Mean_LST = ΣLST_C / n_Q3
Q3_Coverage = 100 × n_Q3 / 448
```

地表温度代表卫星过境时地表辐射温度。云遮、云边缘、气溶胶和质量筛选会改变有效观测数量，因此页面同时展示每格 Q3 合格天数及其相对 448 个本地场景的覆盖率。

## 4. 年龄结构人口

- 数据：ASPECT 2020 年中国年龄结构人口栅格，100 m。
- 页面字段：模型化 65 岁及以上人口占模型化总人口的比例。
- 显示条件：仅在模型化总人口为正的网格显示比例。
- 隐私边界：页面不包含姓名、地址、家庭、健康档案或逐户人口记录。
- 单位边界：本地绝对人口计数单位仍未完成独立闭环，因此 v1 不公开人口总数、65+ 人数或受影响人数。

比例使用同一 MODIS 网格内 ASPECT 源像元聚合结果：

```text
Share65 = 100 × ΣP65 / ΣPtotal，仅在 ΣPtotal > 0 时输出
```

## 5. 地表覆盖与高程

- ESA WorldCover 2020 v100：树木、建成区与永久水体类别，原始分辨率 10 m。
- 页面比例：源像元覆盖权重聚合到原生 MODIS 网格后的比例。
- 永久水域字段：近似永久水域覆盖比例，不作为严格大地测量面积。
- Copernicus DEM GLO-30：30 m 数字表面模型。页面显示网格平均表面高程，它可能包含建筑和植被表面高度。

WorldCover 三类指标都使用 `100 × Σw(class) / Σw(valid)`，其中树木、建成区和永久水体分别对应类别 10、50 和 80。平均表面高程使用 `Σ(Elevation × w_valid) / Σw_valid`。这些值来自已冻结的上游审计产物，网页和公开发布器不会在线重新估计图层。

## 6. 独立复核与追溯

生成所依赖的独立复核程序报告状态为 `pass`，硬失败数为 0。复核范围包括：

- 2,593 个县域网格的标识、顺序与字段语义；
- 460 个夏季日历日期；
- 448 个本地场景及每场景三项资产；
- 1,344 个资产的大小、SHA-256 与读取稳定性；
- 观测摘要全量重算一致性；
- 抽样面板行的独立复算。

公开 GeoJSON 的 `metadata.input_fingerprints` 保存四个冻结输入的逻辑文件名和 SHA-256：

1. `cell_universe.csv`
2. `cell_observation_summary.csv`
3. `duchang_boundary.geojson`
4. `independent_validation_report.json`

GeoJSON 不写入本机绝对路径，避免暴露本地目录结构。

发布门槛为 `validation_pass = true`、`status = pass` 且 `hard_failures = 0`。网格 ID 不一致、研究边界要素异常或任一硬失败都会停止构建。该程序复核检查可重复性与内部一致性，不代表外部机构认证。

## 7. 图层分级

每个图层按全县具有有效值的网格计算六分位数色阶。65+ 人口比例的有效集合仅包含 1,721 个正人口支持网格。图例同时报告有效值范围、中位数与无值网格数；重复分位断点会合并显示，避免把空区间画成独立色阶。分位数色阶用于空间比较，不构成风险阈值、医学阈值或政策分级。

## 8. 数据与文献

1. NASA Aqua MODIS MYD11A1.061. DOI: [10.5067/MODIS/MYD11A1.061](https://doi.org/10.5067/MODIS/MYD11A1.061)
2. Duan et al. (2019). Validation of Collection 6 MODIS land surface temperature product. *Remote Sensing of Environment*. DOI: [10.1016/j.rse.2019.02.020](https://doi.org/10.1016/j.rse.2019.02.020)
3. ASPECT age-structured population dataset. *Scientific Data* (2025). DOI: [10.1038/s41597-025-05401-1](https://doi.org/10.1038/s41597-025-05401-1)
4. ESA WorldCover 2020 v100. DOI: [10.5281/zenodo.5571936](https://doi.org/10.5281/zenodo.5571936)
5. Copernicus DEM GLO-30. DOI: [10.5270/ESA-c5d3d65](https://doi.org/10.5270/ESA-c5d3d65)
6. Runfola et al. (2020). geoBoundaries. *PLOS ONE*. DOI: [10.1371/journal.pone.0231866](https://doi.org/10.1371/journal.pone.0231866)
7. WHO. [Climate change: Heat and health](https://www.who.int/news-room/fact-sheets/detail/climate-change-heat-and-health)
8. Reid et al. (2009). [Mapping community determinants of heat vulnerability](https://pubmed.ncbi.nlm.nih.gov/20049125/)

## 9. 运行、公开范围与回滚

- 页面入口和登录后路由由 `FEATURE_HEAT_EXPOSURE_GIS` 控制。设为 `0` 并重启应用即可隐藏“更多”入口并让页面返回 404。
- GeoJSON 由开放遥感、模型化人口与研究边界聚合而成，不含个人健康记录、逐户人口或本机路径，因此作为可下载研究产物放在静态目录。关闭页面开关不会删除这一公开文件。
- Leaflet 1.9.4 固定在仓库 `static/vendor/leaflet/`，地图运行不依赖外部 CDN。
- 代码发布前应创建独立 release 快照。若开关回滚仍不足，再恢复前一代码版本并重启；完整撤回公开产物时还需移除本次新增静态文件并清理对应边缘缓存。

## 10. 后续升级条件

v2 可以增加逐年或逐日时间滑块、乡镇聚合、导出和服务器端空间查询。健康风险或综合脆弱性图层只有在结局定义、模型验证、校准、外部验证和解释边界完整通过后才进入产品界面。
