# Code Review

This file summarizes the fixes applied from the review.

## Fixes Applied
- Removed hard-coded defaults for admin credentials and secret key; now uses env/config with warnings.
- Disabled forced debug mode; runtime now respects configuration and environment.
- Added CSRF protection for forms and JSON POSTs, plus global fetch header injection.
- Hardened admin user/community validation and safe numeric parsing.
- Added weather API error fallback for dashboard and health assessment.
- Moved test API key to environment variable and normalized training data handling.

## Configuration Notes
- Set `SECRET_KEY` for production deployments.
- Set `DEFAULT_ADMIN_USERNAME` and `DEFAULT_ADMIN_PASSWORD` to create the initial admin.
- Optional: `DEFAULT_ADMIN_EMAIL`, `DEBUG`, `DATABASE_URI`, `FLASK_HOST`, `FLASK_PORT`.
- For weather API test script: set `QWEATHER_KEY`.

---

# 代码重构审查报告 (2025-01-14)

## 🎯 执行摘要

### 本次重构完成情况

| 阶段 | 任务 | 状态 | 影响 |
|------|------|------|------|
| P0.1 | 创建 utils/ 模块 | ✅ 完成 | +180 行 (新增复用模块) |
| P0.2 | 标记废弃服务 | ✅ 完成 | 3 个文件添加警告 |
| P1.2 | app.py 使用 utils | ✅ 完成 | -130 行 |
| **净效果** | | | **-130 行 + 更好的模块化** |

---

## 📁 新增/修改文件

### 新创建文件

| 文件 | 用途 |
|------|------|
| `docs/PROJECT_CATALOG.md` | 项目目录分类表 |
| `docs/ARCHITECTURE.md` | 系统架构文档 |
| `docs/REFACTOR_PLAN.md` | 重构分步计划 |
| `utils/__init__.py` | 工具包入口 |
| `utils/validators.py` | 输入验证函数 (6个) |
| `utils/parsers.py` | 数据解析函数 (10个) |

### 修改文件

| 文件 | 改动 |
|------|------|
| `app.py` | 从 utils 导入，删除 ~130 行重复定义 |
| `services/prediction_service.py` | 添加废弃警告注释 |
| `services/chronic_disease_service.py` | 添加废弃警告注释 |
| `services/data_driven_prediction.py` | 添加废弃警告注释 |

---

## 🔍 发现的问题与处理

### 已解决

| 问题 | 位置 | 处理 |
|------|------|------|
| 重复函数 `parse_age` | 6处 | ✅ 统一到 `utils/parsers.py` |
| 重复函数 `get_age_group` | 4处 | ✅ 统一到 `utils/parsers.py` |
| 验证函数散落 | app.py | ✅ 提取到 `utils/validators.py` |

### 已标记待删除

| 文件 | 行数 | 原因 |
|------|------|------|
| `services/prediction_service.py` | 175 | app.py 未引用 |
| `services/chronic_disease_service.py` | 489 | 被 chronic_risk_service.py 替代 |
| `services/data_driven_prediction.py` | 600 | app.py 未引用 |

---

## ✅ 验证状态

- [x] Python 语法检查通过 (`py_compile app.py`)
- [x] utils 模块独立测试通过
- [x] IDE 无语法/类型错误
- [ ] 完整冒烟测试 (需安装依赖)

---

## 📊 后续建议

1. **立即**: 确认后删除3个废弃服务文件 (可减少 ~1264 行)
2. **短期**: 其他服务文件也使用 utils 模块
3. **中期**: 拆分 app.py 模型到 models/ 目录
4. **长期**: 拆分路由到 Blueprint

详细计划见 [docs/REFACTOR_PLAN.md](docs/REFACTOR_PLAN.md)
