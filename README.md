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

## 快速开始

### 1. 安装依赖

本机现行测试基线使用 Conda 环境 `case-weather-py312`：

```bash
conda run -n case-weather-py312 python -m pip install -r requirements.txt
```

不要默认使用仓库里的 `.venv` 或 `venv` 跑测试，避免拿到缺少 pytest 或平台不匹配的解释器。新机器可以创建等价的 Python 3.12 隔离环境后再安装 `requirements.txt`。

### 2. 配置环境变量

```bash
cp .env.example .env
```

按需填写 `.env` 中的关键项，至少应确认：

- `SECRET_KEY`
- `PAIR_TOKEN_PEPPER`
- `DATABASE_URI`
- `QWEATHER_KEY` 或接受 Open-Meteo 兜底
- 如需官方预警和推送链路，`QWEATHER_KEY` 与 `QWEATHER_API_BASE` 需要成对配置

### 3. 初始化数据库

```bash
flask init-db
```

### 4. 启动开发环境

```bash
python app.py
```

默认访问地址：

- Web: `http://127.0.0.1:5000`

首次进入仓库后，建议启用本地 Git 护栏：

```bash
git config core.hooksPath .githooks
chmod +x .githooks/pre-push
```

## 测试

运行默认测试集：

```bash
conda run -n case-weather-py312 python -m pytest -q
```

手动契约测试默认不会执行；本地无网络回归可运行：

```bash
conda run -n case-weather-py312 python -m pytest -q -m "manual and not network"
```

需要真实第三方 API 的诊断用例使用 `network` 标记，只在明确需要联网时单独运行 `-m "manual and network"`。

Python 3.12 兼容性专项检查可单独运行：

```bash
conda run -n case-weather-py312 python -m pytest -q -W error::DeprecationWarning
```

专项检查只过滤当前固定依赖中已知的 Flask-Login 与 python-dateutil 三方弃用警告，项目代码里的弃用警告仍按错误处理。

```bash
conda run -n case-weather-py312 python -m pytest -q -W error::DeprecationWarning -W ignore::DeprecationWarning:flask_login.login_manager -W ignore::DeprecationWarning:dateutil.tz.tz
```

小程序调试 / 联调前，必须先在 `miniprogram/config.js` 中填写真实 HTTPS API 地址。公开仓库默认留空，未配置时小程序请求会直接报错，不会再偷偷打到占位域名。

启用 QWeather 时优先 JWT（`QWEATHER_AUTH_MODE=jwt`）；也可人工回滚为 `api_key`。公开仓库默认不再内置 QWeather Host；未配置时系统会走 Open-Meteo / 阈值规则兜底。

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

## 避暑资源核验（CLI）

电话或现场核验后，用 CLI 写库并追加台账（只记封闭码与日期，不写姓名或电话）：

```bash
/opt/anaconda3/envs/case-weather-py312/bin/python scripts/cooling_verify.py \
  --id 12 --method phone --open yes --alert-note same_hours \
  --ac yes --water yes --seats yes --toilet no --step-free unknown --shade yes \
  --transport bus --result verified --note ok
```

台账文件为 `docs/data/cooling_verification_ledger.csv`。问题清单与允许值见 `docs/data/cooling_verification_protocol.md`。未跑 CLI 时也可按该表头手工追加一行，再在 `/admin/cooling` 录入。

