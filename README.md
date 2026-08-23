# 天气预警网站 / Weather Web

一个面向高温风险与脆弱人群保护场景的 Flask 产品仓库。项目聚焦“天气预警 + 健康风险评估 + 社区行动支持”。


## 核心能力

- 实时天气与预报数据接入
- 健康风险评估与规则推荐
- 社区风险分析与避暑资源展示
- 照护人 / 被照护人配对与提醒流程
- 小程序与 Web 双入口联动

## 技术栈

- Backend: Flask, SQLAlchemy, Flask-Login, Flask-Limiter
- Data: pandas, numpy, scipy, scikit-learn
- Storage: SQLite（默认）/ 可扩展外部数据库
- Frontend: Jinja2, Bootstrap, 原生 JavaScript
- Testing: pytest


## 仓库结构

```text
app.py                薄入口，导出 Flask app
core/                 应用工厂、模型、配置、钩子与核心能力
blueprints/           路由蓝图
services/             业务服务层
templates/            Jinja2 模板
static/               CSS / JS / vendor 静态资源
miniprogram/          微信小程序端
tests/                自动化测试
docs/                 架构、流程与治理文档
scripts/              部署与维护脚本
```

如果你要启用 QWeather，请在本地 `.env` 中显式设置 `QWEATHER_KEY` 和 `QWEATHER_API_BASE`。公开仓库默认不再内置 QWeather Host；未配置时系统会走 Open-Meteo / 阈值规则兜底。

