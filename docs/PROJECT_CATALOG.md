# 项目文件全量目录与分类清单

> 版本: 2026-01-14  
> 说明: 本文档覆盖仓库中所有文件，按职责分类并标注用途、依赖和可疑点。

---

## 1. 仓库树状结构

```
case-weather/
├── app.py                          # 主应用 (4334行) [ENTRYPOINT, ROUTES, CONFIG, MODEL]
├── config.py                       # 配置文件 [CONFIG]
├── requirements.txt                # 依赖清单 [CONFIG]
├── alembic.ini                     # 数据库迁移配置 [CONFIG]
├── scripts/                        # 运维与维护脚本 [SCRIPT]
│   ├── deploy.sh                   # 部署脚本
│   ├── backup.sh                   # 数据库自动备份脚本（保留30天）
│   ├── start.bat                   # Windows启动脚本
│   ├── sync.sh                     # 快速同步脚本
│   ├── download_backup.sh          # 下载备份脚本
│   ├── weather_sync.sh             # 天气同步脚本
│   ├── weather_cache_sync.sh       # 天气缓存同步脚本
│   ├── quick_fix.sh                # 快速修复脚本
│   ├── complete_manual_fixes.sh    # 手动修复辅助脚本
│   ├── apply_security_fixes.py     # 自动化修复脚本
│   ├── test_fixes.py               # 修复验证脚本
│   └── test_config_validation.py   # 配置验证测试脚本
├── import_data.py                  # 数据导入脚本 [SCRIPT]
│
├── analyze_for_model.py            # 离线数据分析 [SCRIPT] ⚠️ 离线工具
├── analyze_surnames.py             # 姓氏分析脚本 [SCRIPT] ⚠️ 离线工具
├── train_binary_model.py           # 二分类模型训练 [SCRIPT] ⚠️ 离线工具
├── train_multiclass_model.py       # 多分类模型训练 [SCRIPT] ⚠️ 离线工具
├── train_optimized_model.py        # 优化模型训练 [SCRIPT] ⚠️ 离线工具
├── train_real_model.py             # 真实模型训练 [SCRIPT] ⚠️ 离线工具
├── train_xgboost_model.py          # XGBoost模型训练 [SCRIPT] ⚠️ 离线工具
│
├── test_services.py                # 服务测试脚本 [TEST, SCRIPT]
├── test_all_services.py            # 全服务测试脚本 [TEST, SCRIPT] ⚠️ DUPLICATE
├── test_weather_api.py             # 天气API测试 [TEST, SCRIPT]
│
├── 使用说明.txt                     # 使用文档 [DOC]
├── 天气API集成说明.txt              # API集成文档 [DOC]
├── 快速测试.txt                     # 测试说明 [DOC]
├── 数据分析结果.txt                 # 分析结果 [DOC]
├── 数据真实性说明.txt               # 数据说明 [DOC]
├── 模块完成度检查.txt               # 完成度检查 [DOC]
├── 系统完成说明.md                  # 系统说明 [DOC]
├── CODE_REVIEW.md                   # 代码审查报告 [DOC]
│
├── 数据.xlsx                        # 原始病历数据 [DATA]
├── 逐日数据.csv                     # 逐日天气数据 [DATA]
│
├── data/
│   └── medical_kb.json             # 医学知识库 [DATA]
│
├── docs/
│   ├── ARCHITECTURE.md             # 架构文档 [DOC]
│   ├── PROJECT_CATALOG.md          # 本文件 [DOC]
│   └── reports/                    # 修复/测试报告 [DOC]
│
├── instance/
│   └── health_weather.db           # SQLite数据库（生产路径见 /opt/case-weather/storage/health_weather.db） [DATA]
│
├── migrations/
│   ├── env.py                      # Alembic环境 [CONFIG, SCRIPT]
│   ├── script.py.mako              # 迁移模板 [CONFIG]
│   └── versions/
│       └── 0001_feature_extensions.py  # 迁移脚本 [SCRIPT]
│
├── models/
│   ├── disease_predictor.pkl       # 训练好的模型 [DATA]
│   └── feature_config.json         # 特征配置 [CONFIG, DATA]
│
├── services/
│   ├── ai_question_service.py      # AI问答服务 [SERVICE, CLIENT]
│   ├── chronic_disease_service.py  # 慢病服务(旧版) [SERVICE] 🔴 LEGACY
│   ├── chronic_risk_service.py     # 慢病风险服务 [SERVICE]
│   ├── community_risk_service.py   # 社区风险服务 [SERVICE]
│   ├── data_driven_prediction.py   # 数据驱动预测 [SERVICE] 🔴 UNUSED
│   ├── dlnm_risk_service.py        # DLNM风险服务 [SERVICE]
│   ├── emergency_triage.py         # 紧急分诊服务 [SERVICE]
│   ├── external_api.py             # 外部API工具 [UTILS, CLIENT]
│   ├── forecast_service.py         # 预测服务 [SERVICE]
│   ├── health_risk_service.py      # 健康风险服务 [SERVICE] ⚠️ 部分废弃
│   ├── ml_prediction_service.py    # ML预测服务 [SERVICE]
│   ├── prediction_service.py       # 预测服务(旧版) [SERVICE] 🔴 UNUSED
│   └── weather_service.py          # 天气服务 [SERVICE, CLIENT]
│
├── static/
│   ├── css/
│   │   ├── ai-floating-chat.css    # AI浮动聊天样式 [STATIC]
│   │   ├── animations.css          # 动画样式 [STATIC]
│   │   ├── page-transitions.css    # 页面切换 [STATIC]
│   │   └── style.css               # 主样式 [STATIC]
│   └── js/
│       └── ai-floating-chat.js     # AI聊天脚本 [STATIC]
│
├── templates/                       # 35个Jinja2模板 [TEMPLATE]
│   ├── base.html                   # 基础模板
│   ├── index.html                  # 首页
│   ├── login.html / register.html  # 认证页面
│   ├── user_dashboard.html         # 用户仪表板
│   ├── admin_*.html                # 管理端页面 (9个)
│   └── ...                         # 其他功能页面
│
└── tests/
    └── test_smoke.py               # 冒烟测试 [TEST]
```

---

## 2. 分类标签说明

| 标签 | 说明 |
|------|------|
| ENTRYPOINT | 应用入口文件 |
| CONFIG | 配置文件 |
| ROUTES | 路由/视图/控制器 |
| SERVICE | 业务服务层 |
| CLIENT | 外部API客户端 |
| MODEL | 数据库模型 |
| UTILS | 工具函数 |
| TEMPLATE | Jinja2模板 |
| STATIC | 静态资源 |
| DATA | 数据文件 |
| SCRIPT | 一次性/运维脚本 |
| DOC | 文档 |
| TEST | 测试文件 |
| LEGACY | 遗留/废弃代码 |
| UNUSED | 未使用(可删除) |
| DUPLICATE | 功能重复 |

---

## 3. 核心文件详细说明

### 3.1 应用入口

| 文件 | 分类 | 用途 | 行数 | 问题 |
|------|------|------|------|------|
| `app.py` | ENTRYPOINT, ROUTES, MODEL, CONFIG | 主应用文件，包含所有路由、模型、工具函数 | **4334** | 🔴 **极度臃肿**，需要拆分 |
| `config.py` | CONFIG | API密钥和系统配置 | ~120 | ✅ 结构清晰 |

### 3.2 服务层状态

| 文件 | 状态 | 被app.py引用 | 说明 |
|------|------|--------------|------|
| `weather_service.py` | ✅ 活跃 | 是 | 核心天气服务 |
| `chronic_risk_service.py` | ✅ 活跃 | 是 | 慢病风险预测 |
| `dlnm_risk_service.py` | ✅ 活跃 | 是 | DLNM风险计算 |
| `forecast_service.py` | ✅ 活跃 | 是 | 7天健康预测 |
| `community_risk_service.py` | ✅ 活跃 | 是 | 社区风险评估 |
| `ml_prediction_service.py` | ✅ 活跃 | 是 | 机器学习预测 |
| `ai_question_service.py` | ✅ 活跃 | 是 | AI问答服务 |
| `health_risk_service.py` | ⚠️ 部分活跃 | 是 | 仅calculate_community_vulnerability_index被使用 |
| `emergency_triage.py` | ✅ 活跃 | 是 | 紧急分诊 |
| `external_api.py` | ✅ 活跃 | 被服务引用 | API计时工具 |
| `chronic_disease_service.py` | 🔴 LEGACY | 否 | 已被chronic_risk_service取代 |
| `prediction_service.py` | 🔴 UNUSED | 否 | 未被任何文件引用 |
| `data_driven_prediction.py` | 🔴 UNUSED | 否 | 未被任何文件引用 |

---

## 4. 重复代码识别

### 4.1 `parse_age` 函数重复 (6处)

| 位置 | 文件 |
|------|------|
| 1 | `services/data_driven_prediction.py:53` |
| 2 | `services/dlnm_risk_service.py:135` |
| 3 | `services/ml_prediction_service.py` (隐式) |
| 4 | `train_binary_model.py:38` |
| 5 | `train_multiclass_model.py:15` |
| 6 | `analyze_for_model.py:27` |

**建议**: 提取到 `utils/parsers.py`

### 4.2 `get_age_group` 函数重复 (4处)

| 位置 | 文件 |
|------|------|
| 1 | `services/data_driven_prediction.py:69` |
| 2 | `services/ml_prediction_service.py:95` |
| 3 | `services/chronic_risk_service.py:375` |
| 4 | `train_multiclass_model.py:42` |

**建议**: 提取到 `utils/parsers.py`

### 4.3 测试脚本重复

- `test_services.py` 和 `test_all_services.py` 功能高度重叠
- **建议**: 合并为一个脚本或移入 `tests/`

---

## 5. app.py 臃肿分析

**当前行数**: 4334行

**内容分布**:
| 区块 | 行数范围 | 内容 | 建议 |
|------|----------|------|------|
| 1-250 | 工具函数 | 输入验证、解析、CSRF | → `utils/validators.py` |
| 250-500 | 业务工具 | 天气缓存、通知、审计 | → `utils/helpers.py` |
| 500-700 | 计算工具 | 风险计算、档案完善度 | → `services/` |
| 700-900 | 配置加载 | Flask配置初始化 | → 保留 |
| 900-1200 | 数据库模型 | 12个SQLAlchemy模型 | → `models/` |
| 1200-1350 | 游客支持 | GuestUser类 | → `models/guest.py` |
| 1350-3500 | 路由 | 50+个路由函数 | → Blueprint拆分 |
| 3500-4300 | API | 30+个API端点 | → Blueprint拆分 |
| 4300-4334 | 初始化 | init_db, main | → 保留 |

**减重目标**: 从4334行减少到<500行(仅保留入口和配置)

---

## 6. 依赖关系图

```
app.py
├── config.py (配置)
├── services/
│   ├── weather_service.py
│   │   └── external_api.py
│   ├── chronic_risk_service.py
│   │   └── dlnm_risk_service.py
│   ├── forecast_service.py
│   │   └── dlnm_risk_service.py
│   ├── community_risk_service.py
│   │   └── dlnm_risk_service.py
│   ├── ml_prediction_service.py
│   ├── ai_question_service.py
│   │   └── external_api.py
│   ├── emergency_triage.py
│   └── health_risk_service.py
├── templates/ (35个模板)
├── static/ (5个静态文件)
└── data/medical_kb.json
```

---

*文档生成时间: 2026-01-14*
