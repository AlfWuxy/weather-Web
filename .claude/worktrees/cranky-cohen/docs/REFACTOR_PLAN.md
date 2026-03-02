# 重构方案与分步实施计划

> 版本: 2026-01-14  
> 状态: P0 进行中

---

## 总体目标

1. **不破坏功能**: 所有API、页面、数据库保持兼容
2. **减少代码行数**: 从 ~5500 行减少到 ~3500 行 (目标减少 30%+)
3. **提高可维护性**: 清晰的模块边界、消除重复代码
4. **增量改造**: 每步可独立运行、可回滚

---

## P0: 安全整理 (零风险)

### P0.1 提取工具模块 ✅ 已完成

**改动**:
- 创建 `utils/` 目录
- 创建 `utils/validators.py` - 输入验证函数
- 创建 `utils/parsers.py` - 数据解析函数
- 创建 `utils/__init__.py` - 统一导出

**收益**:
- 消除 `parse_age` 6处重复
- 消除 `get_age_group` 4处重复
- 为后续服务层复用提供基础

**风险**: 无 (新增文件，不修改现有代码)

**回滚**: 删除 `utils/` 目录

---

### P0.2 标记废弃服务 (待执行)

**改动**:
- `services/prediction_service.py` 添加废弃警告注释
- `services/chronic_disease_service.py` 添加废弃警告注释
- `services/data_driven_prediction.py` 添加废弃警告注释

**收益**:
- 明确标识不再维护的代码
- 为后续删除做准备

**风险**: 无 (仅添加注释)

---

### P0.3 清理 health_risk_service.py 废弃方法 (待执行)

**改动**:
- `assess_user_risk()` 方法已标注废弃，保留空实现
- `generate_community_risk_map_data()` 方法已标注废弃，保留空实现
- 仅保留 `calculate_community_vulnerability_index()` 方法

**收益**: 减少约 100 行死代码

**风险**: 低 (方法已返回 None/空值)

---

### P0.4 合并重复测试脚本 (待执行)

**改动**:
- 合并 `test_services.py` 和 `test_all_services.py`
- 保留更完整的版本，删除重复文件

**收益**: 减少约 160 行重复代码

**风险**: 无 (测试脚本不影响生产)

---

## P1: 结构归类 (低风险)

### P1.1 服务层使用统一工具

**改动**:
- `services/dlnm_risk_service.py` 使用 `utils.parsers.parse_age`
- `services/ml_prediction_service.py` 使用 `utils.parsers`
- `services/chronic_risk_service.py` 使用 `utils.parsers`
- `services/data_driven_prediction.py` 使用 `utils.parsers` (如保留)

**收益**: 
- 减少约 80 行重复代码
- 统一行为，便于维护

**风险**: 低 (逻辑相同，仅改变导入路径)

**回滚**: 恢复原有函数定义

---

### P1.2 app.py 使用 utils 模块 (待执行)

**改动**:
- 删除 app.py 中的验证/解析函数定义
- 从 `utils` 导入

**收益**: app.py 减少约 150 行

**风险**: 低 (逻辑相同)

**回滚**: 恢复函数定义

---

### P1.3 删除未使用的服务文件 (待执行)

**条件**: 确认无外部系统调用

**改动**:
- 删除 `services/prediction_service.py` (175行)
- 删除 `services/chronic_disease_service.py` (489行)
- 删除 `services/data_driven_prediction.py` (600行)

**收益**: 减少约 1264 行死代码

**风险**: 中 (需确认无隐藏依赖)

**回滚**: 从 git 恢复文件

---

## P2: 进一步减重 (中等风险)

### P2.1 拆分 app.py 中的数据库模型 (规划中)

**改动**:
- 创建 `models/` 包
- 将 12 个模型类移动到对应文件
- 在 app.py 保留兼容导入

**收益**: app.py 减少约 300 行

**风险**: 中 (需处理循环导入)

---

### P2.2 拆分 app.py 路由到 Blueprint (规划中)

**改动**:
- 创建 `routes/` 包
- 将路由按功能拆分到不同文件
- 在 app.py 注册 Blueprint

**收益**: app.py 减少约 2500 行

**风险**: 中 (需测试所有路由)

---

### P2.3 统一 API 响应格式 (规划中)

**改动**:
- 创建 `utils/response.py`
- 提供 `success_response()` 和 `error_response()` 工具

**收益**: 代码更简洁，响应格式统一

**风险**: 低

---

## 实施进度

| 阶段 | 任务 | 状态 | 减少行数 |
|------|------|------|----------|
| P0.1 | 提取工具模块 | ✅ 完成 | +180 (新增) |
| P0.2 | 标记废弃服务 | ✅ 完成 | 0 |
| P0.3 | 清理废弃方法 | 📋 待执行 | -100 |
| P0.4 | 合并测试脚本 | 📋 待执行 | -160 |
| P1.1 | 服务层复用工具 | 📋 待执行 | -80 |
| P1.2 | app.py 复用工具 | ✅ 完成 | -130 |
| P1.3 | 删除废弃服务 | 📋 待执行 | -1264 |
| **P0+P1 总计** | | | **约 -1574 行** |

---

## 验证清单

### 每步完成后验证:

```bash
# 1. 运行冒烟测试
cd /Users/imac/Downloads/case-weather
python -m pytest tests/test_smoke.py -v

# 2. 启动应用检查
python app.py &
sleep 3

# 3. 测试关键页面
curl -s http://127.0.0.1:5000/ | head -5
curl -s http://127.0.0.1:5000/login | head -5

# 4. 测试关键API
curl -s http://127.0.0.1:5000/api/weather/current
curl -s http://127.0.0.1:5000/api/community/list
curl -s http://127.0.0.1:5000/api/chronic/rules-version
curl -s http://127.0.0.1:5000/api/ml/status

# 5. 停止应用
pkill -f "python app.py"
```

---

*文档更新时间: 2026-01-14*
