# 天气预警网站 Bug 修复任务

## 项目背景
这是一个基于 Flask 的天气预警与健康风险评估网站。经过代码审查，发现存在**系统性的时区处理问题**和其他若干 bug，需要一次性修复。

---

## 修复策略（必须遵循）

### 时间戳统一策略
**决策：全部使用 timezone-aware UTC 时间**

| 场景 | 使用函数 | 返回类型 |
|------|----------|----------|
| 数据库时间戳字段 | `utcnow()` | aware UTC |
| 时间比较运算 | `utcnow()` | aware UTC |
| 用户界面显示 | `now_local()` 改为 aware | aware 本地时区 |
| 日期字段（无时间） | `today_local()` | date 对象（保持不变） |

---

## 需要修复的文件和具体改动

### 1. 修改 `core/time_utils.py`

**当前问题**：`now_local()` 返回 naive datetime，导致与 aware datetime 混用。

**修改方案**：
```python
# 修改 now_local() 函数（约第24-26行）
# 原代码：
def now_local():
    tz = _resolve_timezone()
    return datetime.now(tz).replace(tzinfo=None)

# 改为：
def now_local():
    """返回 timezone-aware 的本地时间"""
    tz = _resolve_timezone()
    return datetime.now(tz)
```

**新增函数**（用于需要 naive 本地时间的遗留场景）：
```python
def now_local_naive():
    """返回 naive 本地时间（仅用于遗留代码兼容，新代码请用 now_local()）"""
    tz = _resolve_timezone()
    return datetime.now(tz).replace(tzinfo=None)
```

---

### 2. 修改 `core/weather.py`

**问题1**：第258行和第331行使用 `utcnow_naive()` 与数据库 aware datetime 比较。

**修改位置和方案**：

```python
# 第258行，函数 get_weather_with_cache() 中
# 原代码：
now = utcnow_naive()

# 改为：
now = utcnow()

# 第331行，函数 get_forecast_with_cache() 中
# 原代码：
now = utcnow_naive()

# 改为：
now = utcnow()
```

**同时检查**：第285、290行写入 `fetched_at` 时也需要使用 `utcnow()`（如果还在用 `now` 变量则无需改动）。

---

### 3. 修改 `services/user/_helpers.py`

**问题**：第33行使用 `utcnow_naive()` 与 `DailyStatus.created_at`（aware）比较。

**修改位置和方案**：

```python
# 第33行，函数 _auto_escalate_overdue_statuses() 中
# 原代码：
now = utcnow_naive()

# 改为：
now = utcnow()

# 确保文件顶部 import 包含 utcnow：
from core.time_utils import now_local, today_local, utcnow  # 添加 utcnow
# 移除 utcnow_naive 的导入（如果不再使用）
```

**第206、212行**：检查 `last_active_at` 赋值，改为 `utcnow()`。

---

### 4. 修改 `services/user/dashboard_service.py`

**问题**：第106、112、137、155行使用 `now_local()` 与 `WeatherAlert.alert_date`（aware UTC）比较和写入。

**修复策略**：预警时间应统一使用 UTC。

```python
# 第106行附近的查询
# 原代码：
WeatherAlert.alert_date >= now_local() - timedelta(hours=6)

# 改为：
WeatherAlert.alert_date >= utcnow() - timedelta(hours=6)

# 第112行，写入 alert_date
# 原代码：
alert_date=now_local(),

# 改为：
alert_date=utcnow(),

# 第137行
# 原代码：
WeatherAlert.alert_date >= now_local() - timedelta(days=1),

# 改为：
WeatherAlert.alert_date >= utcnow() - timedelta(days=1),

# 第155行同理
# 确保文件顶部添加 utcnow 导入
```

---

### 5. 修改 `core/db_models.py`

**问题**：第192行 `default=today_local` 缺少函数调用括号（应为 lambda）。

```python
# 第192行附近（DailyEntry 或类似模型）
# 原代码：
entry_date = db.Column(db.Date, default=today_local)

# 改为：
entry_date = db.Column(db.Date, default=today_local)  # today_local 本身是函数，这里是正确的
# 注意：如果原代码是 default=today_local()，则需要改为 default=lambda: today_local()
```

**验证方式**：检查 `today_local` 是否被作为可调用对象传递（正确）还是被立即调用（错误）。

---

### 6. 修改 `services/user_service.py`

**问题**：第4行使用通配符导入。

```python
# 第4行
# 原代码：
from services.user import *  # noqa: F403

# 改为显式导入（需要先检查实际使用了哪些符号）：
from services.user import (
    # 列出实际使用的函数和类
)
```

**注意**：需要先运行 `grep -r "from services.user_service import"` 或分析文件中实际使用的符号。

---

### 7. 修改 `blueprints/admin.py`

**问题**：第44-48行数据库方言检查不完整，只支持 PostgreSQL 和 SQLite。

```python
# 第44-48行
# 原代码：
dialect = db.session.bind.dialect.name
if dialect == 'postgresql':
    month_expr = func.to_char(MedicalRecord.visit_time, 'YYYY-MM')
else:
    month_expr = func.strftime('%Y-%m', MedicalRecord.visit_time)

# 改为：
dialect = db.session.bind.dialect.name
if dialect == 'postgresql':
    month_expr = func.to_char(MedicalRecord.visit_time, 'YYYY-MM')
elif dialect == 'mysql':
    month_expr = func.date_format(MedicalRecord.visit_time, '%Y-%m')
else:  # SQLite 及其他
    month_expr = func.strftime('%Y-%m', MedicalRecord.visit_time)
```

---

### 8. 修改 `core/notifications.py`

**问题**：第27-29行异常处理过于宽泛。

```python
# 原代码：
except Exception:
    db.session.rollback()
    return 0

# 改为：
except (SQLAlchemyError, ValueError, TypeError) as exc:
    logger.warning("通知处理失败: %s", exc)
    db.session.rollback()
    return 0

# 确保导入：
from sqlalchemy.exc import SQLAlchemyError
```

---

### 9. 修改 `utils/validators.py`

**问题**：第102-107行 bleach 模块缺失时无警告。

```python
# 在文件顶部或模块初始化时添加警告
import logging
logger = logging.getLogger(__name__)

try:
    import bleach
    _BLEACH_AVAILABLE = True
except ImportError:
    _BLEACH_AVAILABLE = False
    logger.warning("bleach 模块未安装，将使用较弱的 HTML 清理方案。建议安装: pip install bleach")
```

---

### 10. 修改 `core/health_profiles.py`

**问题**：第93-96行应捕获 `json.JSONDecodeError`。

```python
# 原代码：
except (TypeError, ValueError):
    return False, None

# 改为：
except (TypeError, ValueError, json.JSONDecodeError):
    return False, None
```

---

## 修复后的验证步骤

### 1. 语法检查
```bash
python -m py_compile core/time_utils.py
python -m py_compile core/weather.py
python -m py_compile services/user/_helpers.py
python -m py_compile services/user/dashboard_service.py
# ... 对所有修改的文件执行
```

### 2. 导入检查
```bash
python -c "from core.time_utils import utcnow, now_local, today_local; print('time_utils OK')"
python -c "from core.weather import get_weather_with_cache; print('weather OK')"
python -c "from services.user._helpers import _auto_escalate_overdue_statuses; print('_helpers OK')"
```

### 3. 时区一致性验证
```python
# 创建临时测试脚本验证
from datetime import datetime, timezone, timedelta
from core.time_utils import utcnow, now_local

# 验证 utcnow() 返回 aware datetime
utc_now = utcnow()
assert utc_now.tzinfo is not None, "utcnow() 应返回 aware datetime"

# 验证 now_local() 返回 aware datetime
local_now = now_local()
assert local_now.tzinfo is not None, "now_local() 应返回 aware datetime"

# 验证两者可以安全相减
diff = utc_now - local_now.astimezone(timezone.utc)
assert isinstance(diff, timedelta), "aware datetime 应可安全相减"

print("时区一致性验证通过")
```

### 4. 运行现有测试
```bash
cd /Users/imac/Downloads/04_Research_Projects/Climate_Health/天气预警网站
python -m pytest tests/ -v
```

---

## 注意事项

1. **不要修改数据库 schema**：`db_models.py` 中的默认值定义不需要迁移，只是运行时行为改变。

2. **向后兼容**：如果数据库中已有 naive datetime 数据，查询时可能需要处理。可以添加兼容逻辑：
   ```python
   # 如果需要兼容旧数据
   if cache.fetched_at.tzinfo is None:
       fetched_at = cache.fetched_at.replace(tzinfo=timezone.utc)
   else:
       fetched_at = cache.fetched_at
   ```

3. **保留 `utcnow_naive()` 和 `now_local_naive()`**：作为兼容函数保留，但在函数文档中标记为 deprecated。

4. **模板显示**：如果模板中显示时间，确保使用 Jinja2 过滤器转换时区：
   ```python
   # 在 app.py 或模板配置中添加过滤器
   @app.template_filter('localtime')
   def localtime_filter(utc_dt):
       if utc_dt is None:
           return ''
       from core.time_utils import _resolve_timezone
       local_tz = _resolve_timezone()
       return utc_dt.astimezone(local_tz).strftime('%Y-%m-%d %H:%M')
   ```

---

## 修改文件清单

| 文件 | 修改类型 | 优先级 |
|------|----------|--------|
| `core/time_utils.py` | 函数行为变更 | P0 |
| `core/weather.py` | 使用 `utcnow()` | P0 |
| `services/user/_helpers.py` | 使用 `utcnow()` | P0 |
| `services/user/dashboard_service.py` | 使用 `utcnow()` | P0 |
| `core/db_models.py` | 检查默认值语法 | P1 |
| `blueprints/admin.py` | 添加 MySQL 支持 | P1 |
| `core/notifications.py` | 细化异常处理 | P2 |
| `utils/validators.py` | 添加缺失模块警告 | P2 |
| `core/health_profiles.py` | 完善异常类型 | P2 |
| `services/user_service.py` | 移除通配符导入 | P2 |

---

## 执行顺序

1. 先修改 `core/time_utils.py`（基础依赖）
2. 再修改使用时间函数的文件（weather.py, _helpers.py, dashboard_service.py）
3. 最后修改其他文件
4. 运行验证脚本
5. 运行完整测试套件
