# Open Source Integration ADR

> Status: accepted for staged implementation
> Date: 2026-05-31

## Context

本站目标是把高温天气预警、健康风险评估和社区行动支持做成可验证的申请证据。开源项目应增强真实能力和证据链，不能把生产代码变成难维护的依赖堆。

## Decision

采用“已有路径加固、证据化、轻量试点”的方式融入 5 个开源项目：

| Project | Role in this repo | Runtime dependency | Evidence value |
| --- | --- | --- | --- |
| Open-Meteo | 天气兜底、多模型预报、短临小时数据参照 | 使用 HTTP API，不引入 SDK | 真实天气数据来源和透明 attribution |
| gasparrini/dlnm | DLNM/RR 方法参照和离线校准源头 | 不引入 R runtime | 支撑高温暴露到健康风险的学术方法 |
| Ushahidi Platform | 行动闭环工作流参照：收集、分类、定位、地图发布 | 不部署平台 | 支撑 trusted messenger 和社区响应叙事 |
| CLIMADA | 气候风险评估方法矩阵和未来离线 notebook 参照 | 不加入 production requirements | 支撑下一阶段气候适应能力路线 |
| pygeoapi | GeoJSON/OpenAPI 输出标准参照 | 不启动 OGC API server | 支撑社区风险地图的数据标准化 |

## Implementation Boundaries

- Open-Meteo 只保留现有 HTTP 调用路径，新增合同测试和透明度说明。
- DLNM 线上服务继续读取 `data/models/final_single_model_ar1_profile.json`，不在 Flask 进程里运行 R、Rscript、rpy2 或 CLIMADA。
- Ushahidi 只影响产品方法：复用现有 `action_checkin`、`DailyStatus`、`community_dashboard` 和避暑资源，不接入 Ushahidi 数据库或 Docker 服务。
- CLIMADA 和 pygeoapi 先进入方法文档与 API contract 设计，等试点数据稳定后再评估单独离线环境。
- 当前阶段不新增 CLIMADA、Ushahidi、pygeoapi runtime dependency。

## Rollout Order

1. 先修测试环境复现和 Python 3.12 dependency warning。
2. 再合并 Open-Meteo provider contract 与透明度 attribution。
3. 再固化 DLNM profile contract 和模型卡。
4. 最后推进社区行动试点证据包与 GeoJSON/OpenAPI 文档。

## Verification

- `conda run -n case-weather-py312 python -m pytest -q`
- `conda run -n case-weather-py312 python -m pytest -q -m manual`
- `conda run -n case-weather-py312 python -m pytest tests/test_open_source_contracts.py -q`
- `conda run -n case-weather-py312 python -m pytest -q -W error::DeprecationWarning -W ignore::DeprecationWarning:flask_login.login_manager`
- 线上 smoke 只允许匿名 GET `/`、`/robots.txt`、`/login`、`/admin`、`/register`。

## Admissions Claim Boundary

申请材料中可以声明：项目接入 Open-Meteo 作为天气兜底和多模型参照，并使用 Python DLNM/RR profile 做高温健康风险估计。

申请材料中暂不声明：生产系统已部署 CLIMADA、Ushahidi 或 pygeoapi；也不声明 Flask 后端直接运行 R `dlnm` 包。
