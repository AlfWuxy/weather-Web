# 天气变化与社区居民健康风险预测系统

## 项目概述

本系统是一个基于 Flask 的综合性健康风险预测平台，旨在监测极端天气对社区居民健康的影响，特别关注脆弱人群（老年人、慢性病患者）的健康风险预测和干预。

### 核心目标

- **天气数据采集和分析**：实时获取和缓存天气数据，检测极端天气事件
- **健康风险模型预测**：多模型融合（ML + DLNM + 规则引擎）进行精准预测
- **社区级别早期预警**：基于社区脆弱性指数的分层预警系统
- **照护链管理**：照护人与被照护人的配对、行动追踪和升级机制

---

## 技术栈

| 层级 | 技术 | 版本 |
|------|------|------|
| Web 框架 | Flask | 2.3.3 |
| WSGI 服务器 | Werkzeug | 3.1.3 |
| ORM | SQLAlchemy | 2.0.23 |
| 数据库扩展 | Flask-SQLAlchemy | 3.0.5 |
| 用户认证 | Flask-Login | 0.6.3 |
| 速率限制 | Flask-Limiter | 3.7.0 |
| XSS 防护 | bleach | 6.2.0 |
| 机器学习 | scikit-learn | ≥1.3.0 |
| 科学计算 | scipy | ≥1.11.0 |
| 数据处理 | pandas | 2.1.3 |
| 数值计算 | numpy | 1.26.4 |
| PDF 导出 | reportlab | 4.2.2 |
| Excel 导出 | openpyxl | 3.1.2 |
| HTTP 请求 | requests | 2.31.0 |
| 数据库迁移 | alembic | 1.13.1 |
| 测试框架 | pytest | 8.2.2 |
| 生产部署 | gunicorn + nginx + systemd | - |

---

## 代码统计

| 模块 | 行数 | 文件数 | 说明 |
|------|------|--------|------|
| Core | 2,415 | 17 | 核心框架、数据库模型、配置 |
| Blueprints | 2,694 | 8 | 路由层，约 110 个端点 |
| Services | 12,021 | 34 | 业务逻辑服务层 |
| Utils | 519 | 7 | 工具函数和验证器 |
| **Python 总计** | **~17,650** | **66+** | 不含测试和迁移 |
| 模板 | - | 52 | Jinja2 HTML 模板 |
| 迁移 | - | 8 | 数据库版本 |

---

## 目录结构

```
天气预警网站/
├── app.py                           # 应用入口（薄层）
├── config.py                        # 配置管理
├── requirements.txt                 # 依赖声明
├── alembic.ini                      # 数据库迁移配置
│
├── core/                            # 核心模块
│   ├── app.py                       # Flask 应用工厂 & 蓝图注册
│   ├── extensions.py                # Flask 扩展初始化
│   ├── config.py                    # 应用配置验证
│   ├── db_models.py                 # 数据库模型定义 (21 个表)
│   ├── auth.py                      # 用户认证逻辑
│   ├── security.py                  # CSRF、速率限制、令牌加密
│   ├── hooks.py                     # Flask 请求/响应钩子
│   ├── weather.py                   # 天气数据获取和缓存
│   ├── health_profiles.py           # 健康档案计算
│   ├── notifications.py             # 通知管理
│   ├── guest.py                     # 游客会话管理
│   ├── analytics.py                 # 统计分析工具
│   ├── audit.py                     # 审计日志辅助
│   ├── helpers.py                   # 核心辅助函数
│   ├── time_utils.py                # 时区处理工具
│   └── constants.py                 # 共享常量
│
├── blueprints/                      # 路由蓝图
│   ├── public.py                    # 公开路由 (登录、注册、短码确认)
│   ├── user.py                      # 用户路由 (仪表板、配置文件)
│   ├── admin.py                     # 管理员路由 (用户、社区管理)
│   ├── api.py                       # RESTful API 路由
│   ├── health.py                    # 健康相关路由
│   ├── analysis.py                  # 数据分析路由
│   └── tools.py                     # 工具路由 (报告导出)
│
├── services/                        # 业务逻辑服务层
│   ├── __init__.py                  # 服务初始化
│   ├── weather_service.py           # 天气数据服务
│   ├── api_service.py               # API 业务逻辑
│   ├── health_risk_service.py       # 健康风险评估（部分历史逻辑）
│   ├── prediction_service.py        # 预测服务协调（已标记废弃）
│   ├── ml_prediction_service.py     # 机器学习预测
│   ├── dlnm_risk_service.py         # 分布滞后非线性模型
│   ├── chronic_risk_service.py      # 慢性病风险预测
│   ├── forecast_service.py          # 天气预报服务
│   ├── heat_action_service.py       # 热浪行动系统
│   ├── ai_question_service.py       # AI 问答服务
│   ├── community_risk_service.py    # 社区风险评估
│   ├── emergency_triage.py          # 紧急分诊
│   ├── user_service.py              # 兼容层 (转发到 user/)
│   │
│   ├── user/                        # 用户服务模块 (重构后)
│   │   ├── __init__.py              # 统一导出
│   │   ├── _common.py               # 通用常量和工具
│   │   ├── _helpers.py              # 内部辅助函数
│   │   ├── dashboard_service.py     # 仪表板逻辑
│   │   ├── profile_service.py       # 个人档案管理
│   │   ├── caregiver_service.py     # 照护人工作台
│   │   └── community_service.py     # 社区管理
│   │
│   └── pipelines/                   # 数据处理流程
│       ├── import_data.py           # 数据导入
│       ├── sync_weather_data.py     # 天气数据同步
│       ├── sync_weather_cache.py    # 天气缓存同步
│       ├── train_multiclass_model.py# 多分类模型训练
│       └── ...                      # 其他训练脚本
│
├── models/                          # 预训练 ML 模型
│   ├── disease_predictor.pkl        # 疾病预测器
│   ├── feature_config.json          # 特征配置
│   ├── label_encoder.pkl            # 标签编码器
│   └── scaler.pkl                   # 特征缩放器
│
├── utils/                           # 工具模块
│   ├── parsers.py                   # 参数解析工具
│   ├── validators.py                # 输入验证
│   ├── audit_log.py                 # 审计日志工具
│   ├── database.py                  # 数据库工具
│   ├── i18n.py                      # 国际化
│   └── error_handlers.py            # 错误处理
│
├── templates/                       # Jinja2 HTML 模板 (52 个)
│   ├── base.html                    # 基础模板
│   ├── index.html                   # 首页
│   ├── login.html / register.html   # 认证页面
│   ├── user_dashboard.html          # 用户仪表板
│   ├── elder_dashboard.html         # 老年人简化界面
│   ├── caregiver_dashboard.html     # 照护人工作台
│   ├── community_*.html             # 社区相关页面
│   ├── health_*.html                # 健康相关页面
│   ├── admin_*.html                 # 管理后台页面
│   ├── analysis_*.html              # 数据分析页面
│   └── ...
│
├── static/                          # 静态资源
│   ├── css/                         # 样式表
│   └── js/                          # JavaScript
│
├── migrations/                      # Alembic 数据库迁移
│   └── versions/                    # 8 个迁移版本
│
├── tests/                           # 测试套件
│   ├── conftest.py                  # Pytest 配置
│   ├── test_smoke.py                # 烟雾测试
│   └── manual/                      # 手动测试
│
├── docs/                            # 文档
├── scripts/                         # 部署和维护脚本
├── storage/                         # SQLite 数据库
└── .env                             # 环境变量配置
```

---

## 数据库模型

### 模型总览 (21 个表)

#### 用户系统
| 表名 | 主要字段 | 用途 |
|------|----------|------|
| `users` | id, username, email, role, age, gender, community, chronic_diseases | 用户账户 |
| `audit_logs` | id, actor_id, action, resource_type, ip_address, user_agent | 审计日志 |

#### 天气数据
| 表名 | 主要字段 | 用途 |
|------|----------|------|
| `weather_data` | id, date, location, temperature, humidity, pm25, is_extreme | 日度天气记录 |
| `weather_cache` | id, location, fetched_at, payload, is_mock | API 响应缓存 |
| `forecast_cache` | id, location, days, fetched_at, payload | 预报缓存 |
| `weather_alerts` | id, location, alert_type, alert_level, affected_communities | 预警记录 |

#### 健康管理
| 表名 | 主要字段 | 用途 |
|------|----------|------|
| `medical_records` | id, patient_name, visit_time, diagnosis, disease_category | 病历记录 |
| `health_risk_assessments` | id, user_id, risk_score, risk_level, recommendations | 风险评估 |
| `health_diary` | id, user_id, entry_date, symptoms, severity, notes | 健康日记 |
| `medication_reminders` | id, user_id, medicine_name, dosage, frequency, weather_trigger | 用药提醒 |
| `notifications` | id, user_id, title, message, level, is_read | 站内通知 |

#### 社区管理
| 表名 | 主要字段 | 用途 |
|------|----------|------|
| `communities` | id, name, location, latitude, longitude, vulnerability_index | 社区信息 |
| `community_daily` | id, community_code, date, total_people, confirm_rate, risk_distribution | 社区日聚合 |
| `cooling_resources` | id, community_code, name, resource_type, coordinates, accessibility | 避暑资源 |

#### 照护系统
| 表名 | 主要字段 | 用途 |
|------|----------|------|
| `family_members` | id, user_id, name, relation, age, chronic_diseases | 家庭成员 |
| `family_member_profiles` | id, member_id, allergies, medications, risk_tags, weather_thresholds | 成员扩展档案 |
| `pairs` | id, caregiver_id, elder_code, short_code_hash, status | 照护关系 |
| `pair_links` | id, caregiver_id, short_code, token_hash, expires_at | 配对短码链接 |
| `short_code_attempts` | id, key_hash, failed_count, locked_until | 防枚举计数 |
| `daily_status` | id, pair_id, status_date, risk_level, confirmed, help_flag | 日度行动状态 |
| `debriefs` | id, date, community_code, pair_id, difficulty, feedback | 行动复盘 |

---

## API 端点

### 公开路由
```
GET  /                              # 首页
GET  /entry                         # 角色选择入口
GET/POST /login                     # 登录
GET/POST /register                  # 注册
GET/POST /action                    # 短码确认入口
POST /action/confirm                # 行动确认
POST /action/help                   # 求助
POST /action/debrief                # 行动复盘
GET  /elder                         # 长者入口
GET  /e/<token>                     # 长者短码入口
```

### 用户路由
```
GET  /dashboard                     # 用户仪表板
GET  /elder-mode                    # 老年人简化界面
GET/POST /pairs                     # 照护绑定管理
GET  /caregiver                     # 照护工作台
POST /caregiver/pair/create         # 创建绑定短码
GET  /caregiver/pair/<id>           # 照护关系详情
POST /caregiver/pair/<id>/action-log # 行动记录
POST /pairs/<id>/escalate           # 升级链推进
POST /pairs/<id>/backup             # 标记已联系备选
```

### 健康路由
```
GET/POST /health-assessment         # 健康评估
GET/POST /health-diary              # 健康日记
GET/POST /medication-reminders      # 用药提醒
GET/POST /family-members            # 家庭成员管理
GET  /profile                       # 个人档案
POST /location                      # 更新位置
```

### 社区路由
```
GET  /community                     # 社区仪表板
GET  /community-risk                # 社区风险
GET  /cooling                       # 避暑资源
GET  /risk                          # 公开风险页面
GET  /forecast-7day                 # 7 天预报
GET  /chronic-risk                  # 慢性病风险
GET  /transparency                  # 透明度报告
```

### 分析路由
```
GET/POST /analysis/heatmap          # 热力图
GET/POST /analysis/lag              # 滞后分析
GET/POST /analysis/history          # 历史分析
GET/POST /analysis/community-compare # 社区对比
GET/POST /alerts/history            # 预警历史
GET/POST /alerts/accuracy           # 预警准确性
GET  /ml-prediction                 # ML 预测结果
GET  /annual-report                 # 年度报告
```

### 管理员路由
```
GET  /admin                         # 管理仪表板
GET  /admin/users                   # 用户管理
GET/POST /admin/user/add            # 添加用户
GET/POST /admin/user/<id>/edit      # 编辑用户
POST /admin/user/<id>/delete        # 删除用户
GET  /admin/records                 # 病历管理
GET  /admin/communities             # 社区管理
GET/POST /admin/community/add       # 添加社区
GET  /admin/cooling                 # 避暑资源管理
GET  /admin/statistics              # 统计数据
```

### RESTful API (`/api/v1/`)
```
GET  /api/v1/weather/current                    # 当前天气
GET  /api/v1/community/risk-map                 # 社区风险地图
POST /api/v1/community/risk-map-v2              # 风险地图 v2
GET  /api/v1/community/list                     # 社区列表
GET  /api/v1/community/vulnerability/<name>     # 社区脆弱性
GET  /api/v1/statistics/disease-weather         # 疾病-天气统计
POST /api/v1/ml/predict                         # ML 个人预测
POST /api/v1/ml/predict-community               # ML 社区预测
GET  /api/v1/ml/status                          # ML 模型状态
POST /api/v1/dlnm/risk                          # DLNM 风险计算
GET  /api/v1/dlnm/summary                       # DLNM 摘要
POST /api/v1/forecast/7day                      # 7 天健康预测
POST /api/v1/forecast/daily                     # 单日诊所预测
POST /api/v1/chronic/individual                 # 慢性病个人预测
POST /api/v1/chronic/population                 # 慢性病群体预测
GET  /api/v1/chronic/rules-version              # 慢性病规则版本
POST /api/v1/alert/comprehensive                # 综合预警
POST /api/v1/ai/ask                             # AI 问答
```

---

## 核心业务流程

### 1. 天气数据流

```
┌─────────────────┐
│  和风天气 API   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ WeatherService  │
│ (fetch/parse)   │
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
┌────────┐ ┌────────────┐
│ Cache  │ │ WeatherData│
│(分钟级)│ │  (日度)    │
└────────┘ └─────┬──────┘
                 │
                 ▼
         ┌──────────────┐
         │ 极端天气检测  │
         └──────┬───────┘
                │
                ▼
         ┌──────────────┐
         │ 触发风险评估  │
         └──────────────┘
```

### 2. 健康风险预测流程

```
┌─────────────────────────────────────────────────────┐
│                    输入数据                          │
│  天气数据 + 用户档案 + 历史病历 + 家庭成员信息        │
└─────────────────────┬───────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────┐
│                  多模型融合预测                       │
├─────────────────┬─────────────────┬─────────────────┤
│   ML 模型       │   DLNM 模型     │  慢性病规则     │
│ (scikit-learn)  │ (滞后非线性)    │  (疾病特异性)   │
│                 │                 │                 │
│ - 特征工程      │ - 温度滞后效应  │ - 阈值判断      │
│ - 分类预测      │ - 非线性关系    │ - 组合条件      │
│ - 概率输出      │ - 累积风险      │ - 权重调整      │
└────────┬────────┴────────┬────────┴────────┬────────┘
         │                 │                 │
         └────────────────┬┴─────────────────┘
                          │
                          ▼
                ┌─────────────────┐
                │ 综合风险评分     │
                │ (加权融合)       │
                └────────┬────────┘
                         │
                         ▼
         ┌───────────────────────────────┐
         │ HealthRiskAssessment 记录     │
         │ - 风险等级                     │
         │ - 建议措施                     │
         │ - 可解释输出                   │
         └───────────────┬───────────────┘
                         │
                         ▼
                ┌─────────────────┐
                │ 通知系统        │
                │ (站内/推送)     │
                └─────────────────┘
```

### 3. 照护链管理流程

```
┌─────────────────────────────────────────────────────┐
│                    照护人操作                        │
└─────────────────────┬───────────────────────────────┘
                      │
                      ▼
            ┌─────────────────┐
            │ 创建配对短码     │
            │ (6位数字)        │
            └────────┬────────┘
                     │
                     ▼
            ┌─────────────────┐
            │ PairLink 记录    │
            │ - 短码哈希       │
            │ - 令牌哈希       │
            │ - 过期时间       │
            └────────┬────────┘
                     │
         ┌───────────┴───────────┐
         ▼                       ▼
┌─────────────────┐     ┌─────────────────┐
│ 分享短码给被照护人│     │ 短码过期自动失效 │
└────────┬────────┘     └─────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────┐
│                  被照护人操作                        │
└─────────────────────┬───────────────────────────────┘
                      │
                      ▼
            ┌─────────────────┐
            │ 输入短码验证     │
            │ (防枚举保护)     │
            └────────┬────────┘
                     │
              ┌──────┴──────┐
              ▼             ▼
     ┌─────────────┐  ┌─────────────┐
     │ 确认安全    │  │ 发出求助    │
     │ (confirm)   │  │ (help)      │
     └──────┬──────┘  └──────┬──────┘
            │                │
            ▼                ▼
     ┌─────────────┐  ┌─────────────┐
     │ DailyStatus │  │ 升级链触发   │
     │ 记录确认    │  │ 通知照护人   │
     └─────────────┘  └──────┬──────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │ 备选联系人介入   │
                    │ (escalate)      │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │ 行动复盘        │
                    │ (Debrief)       │
                    └─────────────────┘
```

---

## 配置说明

### 必需环境变量

```bash
# Flask 核心
SECRET_KEY=<至少32位随机字符串>

# 数据库
DATABASE_URI=sqlite:///storage/health_weather.db

# 配对令牌加密
PAIR_TOKEN_PEPPER=<独立的加密盐>
```

### 可选 API 配置

```bash
# 和风天气 API
QWEATHER_KEY=<你的和风天气 API 密钥>
QWEATHER_API_BASE=https://devapi.qweather.com

# 高德地图
AMAP_KEY=<你的高德地图 API 密钥>
AMAP_SECURITY_JS_CODE=<高德地图安全代码>

# 硅基流动 AI
SILICONFLOW_API_KEY=<你的硅基流动 API 密钥>
SILICONFLOW_API_BASE=https://api.siliconflow.cn
```

### 系统配置

```bash
# 运行模式
DEBUG=false
DEMO_MODE=0

# 默认位置
DEFAULT_CITY=都昌
DEFAULT_LOCATION=116.20,29.27

# 缓存配置
WEATHER_CACHE_TTL_MINUTES=30
FORECAST_CACHE_TTL_MINUTES=20
```

### 速率限制

```bash
RATE_LIMITS=200 per minute
RATE_LIMIT_LOGIN=5 per 5 minutes
RATE_LIMIT_SHORT_CODE=3 per hour
RATE_LIMIT_WEATHER=120 per minute
RATE_LIMIT_ML=60 per minute
```

---

## 安全措施

| 安全层面 | 实现方式 |
|---------|---------|
| 认证 | Flask-Login 会话管理 |
| 密码存储 | Werkzeug security 哈希 |
| CSRF 防护 | 自定义钩子验证令牌 |
| XSS 防护 | bleach 库清理输出 |
| SQL 注入 | SQLAlchemy ORM 参数化查询 |
| 速率限制 | Flask-Limiter 全局 + 针对性限制 |
| 审计日志 | AuditLog 记录所有敏感操作 |
| 短码安全 | 哈希存储 + 失败计数 + 锁定机制 |

---

## 部署架构

```
                    ┌─────────────┐
                    │   客户端    │
                    │ (浏览器/APP)│
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │   nginx     │
                    │ (反向代理)   │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │  gunicorn   │
                    │ (3 workers) │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │  Flask App  │
                    └──────┬──────┘
                           │
         ┌─────────────────┼─────────────────┐
         ▼                 ▼                 ▼
  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
  │   SQLite    │   │ 和风天气API  │   │ 硅基流动AI  │
  │  (数据库)   │   │  (天气数据)  │   │  (AI问答)   │
  └─────────────┘   └─────────────┘   └─────────────┘
```

---

## 数据库迁移历史

| 版本 | 说明 | 日期 |
|------|------|------|
| 0001 | 初始特性扩展 | 2024-01-13 |
| 0002 | 模式修复 | 2024-01-16 |
| 0003 | 热浪行动系统 | 2024-01-17 |
| 0004 | 短码失败计数 | 2024-01-18 |
| 0005 | 照护人行动字段 | 2024-01-18 |
| 0006 | 避暑资源字段 | 2024-01-19 |
| 0007 | 短码哈希 | 2024-01-19 |
| 0008 | 天气数据唯一约束 | 2024-01-22 |

---

## 快速开始

### 本地开发

```bash
# 1. 克隆项目
cd 天气预警网站

# 2. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env 填入必需配置

# 5. 初始化数据库
flask init-db

# 6. 运行开发服务器
python app.py
```

### 生产部署

```bash
# 运行部署脚本
./deploy.sh
```

---

## 项目特色

1. **多模型融合预测**
   - 机器学习分类模型
   - DLNM 气象滞后效应模型
   - 慢性病规则引擎
   - 加权融合输出

2. **照护链管理系统**
   - 短码配对机制
   - 多级升级流程
   - 行动确认追踪
   - 复盘反馈闭环

3. **社区级分析**
   - 人口脆弱性指数
   - 日度聚合统计
   - 风险地图可视化
   - 社区对比分析

4. **AI 健康咨询**
   - 硅基流动 API 集成
   - 上下文感知问答
   - 健康建议生成

5. **完善的安全体系**
   - 多层速率限制
   - 全链路审计日志
   - 输入验证和清理
   - 防枚举攻击保护

---

## 维护联系

- 项目目录：`/Users/imac/Downloads/04_Research_Projects/Climate_Health/天气预警网站`
- 服务器地址：172.245.126.42
- 部署目录：`/opt/case-weather`

---

*文档生成时间：2026-01-24*
