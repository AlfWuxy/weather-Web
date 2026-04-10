# 全面修复计划 (Comprehensive Fix Plan)

**创建日期**: 2026-01-22
**目标**: 一次性修复代码审查中发现的 22 个问题
**预计时间**: 2-3 小时
**风险等级**: 中等（涉及数据库模型、安全配置）

---

## 修复策略总览

### 阶段 1: 关键安全问题（立即执行）
- **优先级**: P0 (Critical)
- **影响**: 安全、数据完整性
- **可回滚**: 是

### 阶段 2: 高优先级问题（同批次）
- **优先级**: P1 (High)
- **影响**: 稳定性、安全
- **可回滚**: 是

### 阶段 3: 中低优先级问题（同批次）
- **优先级**: P2-P3 (Medium-Low)
- **影响**: 代码质量、可维护性
- **可回滚**: 是

---

## 详细修复清单

### ✅ 阶段 1: 关键问题修复 (3 项)

#### 1.1 修复 datetime.utcnow() 废弃问题
**文件**: `core/db_models.py` (19 处)
**问题**: 使用已废弃的 `datetime.utcnow()`
**修复方案**:

```python
# 修复前:
from datetime import datetime
created_at = db.Column(db.DateTime, default=datetime.utcnow)

# 修复后:
from datetime import datetime, timezone
created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
```

**影响范围**: 所有包含时间戳的数据库模型
**测试要求**:
- 验证新记录时间戳正确
- 验证现有记录不受影响
- 确认时区信息正确保存

**回滚方案**:
```python
# 如果出现问题，临时回滚:
default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
```

---

#### 1.2 环境变量安全加固
**文件**: `config.py`, `.env.example` (新建)
**问题**: API 密钥暴露在 .env 文件
**修复方案**:

**步骤 1**: 创建 `.env.example` 模板
```bash
# .env.example (安全模板)
SECRET_KEY=<使用 python -c 'import secrets; print(secrets.token_hex(32))' 生成>
PAIR_TOKEN_PEPPER=<使用 python -c 'import secrets; print(secrets.token_hex(32))' 生成>
DEBUG=false

# 外部 API 密钥
QWEATHER_KEY=<从和风天气控制台获取>
AMAP_KEY=<从高德地图控制台获取>
AMAP_SECURITY_JS_CODE=<可选>
SILICONFLOW_API_KEY=<从 SiliconFlow 控制台获取>

# 数据库配置
DATABASE_URI=sqlite:///storage/health_weather.db

# 部署配置（仅生产环境）
DEPLOY_SERVER=
DEPLOY_USER=
```

**步骤 2**: 更新 `.gitignore`
```bash
# 确保 .env 不被提交
.env
.env.local
.env.*.local
*.db
storage/
```

**步骤 3**: 加强 `config.py` 验证
```python
# config.py 增强验证
def validate_production_config():
    """生产环境配置验证"""
    if not DEBUG:
        required_vars = {
            'SECRET_KEY': '会话加密密钥',
            'PAIR_TOKEN_PEPPER': '配对令牌加密盐',
            'QWEATHER_KEY': '天气 API 密钥',
        }

        missing = []
        for var, desc in required_vars.items():
            if not os.getenv(var):
                missing.append(f"{var} ({desc})")

        if missing:
            raise RuntimeError(
                f"生产环境缺少必需的环境变量:\n" +
                "\n".join(f"  - {m}" for m in missing)
            )

# 在配置加载后调用
if __name__ != '__main__':
    validate_production_config()
```

**安全行动检查清单**:
- [ ] 从和风天气控制台撤销旧的 `QWEATHER_KEY=<已脱敏旧密钥>`
- [ ] 从高德地图控制台撤销旧的 `AMAP_KEY=<已脱敏旧密钥>`
- [ ] 从 SiliconFlow 控制台撤销旧的 `SILICONFLOW_API_KEY=OLD_TOKEN_REDACTED`
- [ ] 重新生成 `SECRET_KEY` 和 `PAIR_TOKEN_PEPPER`
- [ ] 更新服务器部署凭证
- [ ] 确认 `.env` 不在 git 历史中（如果在，需要 BFG Repo-Cleaner）

---

#### 1.3 异常处理精细化
**文件**: `blueprints/api.py`, `services/*.py`
**问题**: 过于宽泛的 `except Exception` 捕获
**修复方案**:

```python
# 修复前:
@bp.route('/api/v1/ml/predict', methods=['POST'])
def api_v1_ml_predict():
    try:
        # ... 业务逻辑
    except Exception as exc:
        logger.exception("ML预测失败")
        return jsonify({'success': False, 'error': GENERIC_ERROR_MESSAGE})

# 修复后:
@bp.route('/api/v1/ml/predict', methods=['POST'])
def api_v1_ml_predict():
    try:
        # ... 业务逻辑
    except (ValueError, KeyError, TypeError) as exc:
        # 预期的输入错误
        logger.warning("ML预测参数错误: %s", exc)
        return jsonify({
            'success': False,
            'error': '输入参数格式不正确，请检查后重试'
        }), 400
    except FileNotFoundError as exc:
        # 模型文件缺失
        logger.error("ML模型文件缺失: %s", exc)
        return jsonify({
            'success': False,
            'error': '服务暂时不可用，请稍后再试'
        }), 503
    except Exception as exc:
        # 未预期的系统错误
        logger.exception("ML预测发生未预期错误")
        return _handle_api_error(exc, "ML预测失败")
```

**需要修复的文件**:
- `blueprints/api.py`: 8 处异常处理
- `services/ai_question_service.py`: 文件操作异常
- `blueprints/analysis.py`: 1 处 bare `pass`
- `blueprints/public.py`: 事务回滚处理

**统一异常处理助手**:
```python
# utils/error_handlers.py (新建)
from typing import Tuple, Optional
from flask import jsonify, current_app
import logging

logger = logging.getLogger(__name__)

class APIError(Exception):
    """API 业务异常基类"""
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)

class ValidationError(APIError):
    """输入验证错误"""
    def __init__(self, message: str):
        super().__init__(message, 400)

class ServiceUnavailableError(APIError):
    """服务不可用"""
    def __init__(self, message: str = '服务暂时不可用，请稍后再试'):
        super().__init__(message, 503)

def handle_api_exception(exc: Exception, context: str) -> Tuple[dict, int]:
    """统一 API 异常处理"""
    if isinstance(exc, APIError):
        logger.warning("%s: %s", context, exc.message)
        return jsonify({
            'success': False,
            'error': exc.message
        }), exc.status_code

    # 已知业务异常
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        logger.warning("%s - 参数错误: %s", context, exc)
        return jsonify({
            'success': False,
            'error': '输入参数格式不正确'
        }), 400

    # 未知系统异常
    logger.exception("%s - 系统错误", context)

    error_detail = str(exc) if current_app.config.get('DEBUG') else None
    response = {'success': False, 'error': '服务暂时不可用，请稍后再试'}
    if error_detail:
        response['error_detail'] = error_detail

    return jsonify(response), 500
```

---

### ✅ 阶段 2: 高优先级问题修复 (7 项)

#### 2.1 修复时区信息丢失问题
**文件**: `blueprints/user.py`, `blueprints/public.py`, `core/guest.py`
**问题**: `utcnow().replace(tzinfo=None)` 丢弃时区信息
**修复方案**:

**策略选择**:
```python
# 选项 A: 数据库存储 timezone-aware datetime（推荐）
# 优点: 信息完整，支持多时区
# 缺点: 需要数据库迁移

# 选项 B: 统一使用 naive UTC datetime（兼容现有数据）
# 优点: 无需迁移，行为保持一致
# 缺点: 时区信息在应用层维护

# 采用选项 B（向后兼容）
```

**实施方案**:
```python
# core/time_utils.py 增强
def utcnow_naive():
    """返回 naive UTC datetime，用于数据库存储

    注意: 此函数返回的 datetime 对象没有时区信息，但保证是 UTC 时间。
    仅用于兼容现有数据库 schema。新项目应使用 timezone-aware datetime。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)

def local_now():
    """返回本地时区的当前时间（naive datetime）"""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo('Asia/Shanghai')
    return datetime.now(tz).replace(tzinfo=None)

# 全局替换
# 修复前: utcnow().replace(tzinfo=None)
# 修复后: utcnow_naive()
```

**批量替换命令**:
```bash
# 查找所有使用 .replace(tzinfo=None) 的地方
rg "utcnow\(\)\.replace\(tzinfo=None\)" --type py

# 替换为 utcnow_naive()
find . -name "*.py" -type f -exec sed -i '' 's/utcnow()\.replace(tzinfo=None)/utcnow_naive()/g' {} +

# 添加导入
find . -name "*.py" -type f -exec sed -i '' 's/from core\.time_utils import utcnow$/from core.time_utils import utcnow, utcnow_naive/g' {} +
```

**更新文档**:
```python
# core/db_models.py 顶部注释更新
"""Database models.

时区处理策略：
- 数据库存储: 统一使用 naive UTC datetime (为了向后兼容)
- 推荐使用: core.time_utils.utcnow_naive() 获取当前 UTC 时间
- 显示给用户: 使用 core.time_utils.to_local() 转换为本地时间
- 未来改进: 迁移到 timezone-aware datetime (需要数据库迁移)

时间戳字段默认值:
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    # SQLAlchemy 会自动处理时区信息
"""
```

---

#### 2.2 JSON 解析安全加固
**文件**: `core/hooks.py`, `core/guest.py`
**问题**: 无大小限制、无 schema 验证
**修复方案**:

```python
# core/hooks.py
def from_json_filter(value):
    """JSON 反序列化过滤器（安全增强版）"""
    if not value:
        return []

    # 大小限制：防止 DoS 攻击
    if len(str(value)) > 10000:  # 10KB 限制
        logger.warning("JSON 数据超过大小限制，已截断")
        return []

    try:
        data = json.loads(value)

        # 深度限制：防止嵌套炸弹
        def check_depth(obj, max_depth=5, current_depth=0):
            if current_depth > max_depth:
                raise ValueError("JSON 嵌套深度超过限制")
            if isinstance(obj, dict):
                for v in obj.values():
                    check_depth(v, max_depth, current_depth + 1)
            elif isinstance(obj, list):
                for item in obj:
                    check_depth(item, max_depth, current_depth + 1)

        check_depth(data)
        return data

    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("JSON 解析失败: %s", exc)
        return []

# core/guest.py
def update_guest_assessment(guest_user, data):
    """更新访客健康评估（增强异常处理）"""
    try:
        assessment_date_str = data.get('assessment_date')
        if not assessment_date_str:
            assessment_date = utcnow_naive()
        else:
            # 安全的日期解析
            try:
                assessment_date = datetime.fromisoformat(assessment_date_str)
            except (ValueError, TypeError) as exc:
                logger.warning("日期格式不正确，使用当前时间: %s", exc)
                assessment_date = utcnow_naive()

        # ... 其余逻辑
    except Exception as exc:
        logger.exception("更新访客评估失败")
        raise  # 向上层传播异常
```

---

#### 2.3 API 密钥模板化传递
**文件**: `core/hooks.py`
**问题**: AMAP_KEY 直接传递到模板
**修复方案**:

```python
# core/hooks.py
def inject_context_processors(app):
    """注入全局模板上下文（安全增强版）"""

    @app.context_processor
    def inject_global_vars():
        # 仅在需要的页面传递 API 密钥
        from flask import request

        # 白名单路由
        map_routes = ['/map', '/community/risk-map', '/analysis/map']
        needs_amap_key = any(request.path.startswith(route) for route in map_routes)

        amap_config = {}
        if needs_amap_key:
            amap_key = app.config.get('AMAP_KEY', '')
            # 验证密钥格式
            if amap_key and 20 <= len(amap_key) <= 100:
                amap_config['amap_key'] = amap_key
                amap_config['amap_security_js_code'] = app.config.get('AMAP_SECURITY_JS_CODE', '')

        return {
            'app_name': app.config.get('APP_NAME', '健康天气风险预测系统'),
            'demo_mode': app.config.get('DEMO_MODE', False),
            **amap_config,  # 条件性包含
        }
```

**模板更新**:
```html
<!-- templates/base.html -->
{% if amap_key %}
<script>
  window._AMapSecurityConfig = {
    securityJsCode: '{{ amap_security_js_code }}',
  }
</script>
<script src="https://webapi.amap.com/maps?v=2.0&key={{ amap_key }}"></script>
{% else %}
<!-- 地图功能未配置 -->
{% endif %}
```

---

#### 2.4 数据库查询 None 检查
**文件**: `blueprints/public.py`
**问题**: 查询结果未验证就访问属性
**修复方案**:

```python
# blueprints/public.py - 短码兑换逻辑

# 修复前:
pair = Pair.query.filter_by(short_code_hash=short_code_hash, status='active').first()
link = PairLink.query.filter_by(short_code_hash=short_code_hash, status='active').first()
pair = Pair.query.filter_by(id=link.pair_id).first() if link.pair_id else None
# ❌ 如果 link 是 None，link.pair_id 会抛出 AttributeError

# 修复后:
def redeem_short_code_internal(short_code: str, community_code: str):
    """兑换短码（增强错误处理）"""
    short_code_hash = hash_pair_token(short_code)

    # 步骤 1: 查找配对记录
    pair = Pair.query.filter_by(
        short_code_hash=short_code_hash,
        status='active'
    ).first()

    # 步骤 2: 查找链接记录（可选）
    link = PairLink.query.filter_by(
        short_code_hash=short_code_hash,
        status='active'
    ).first()

    # 步骤 3: 如果有链接，优先使用链接对应的配对
    if link:
        if not link.pair_id:
            logger.warning("PairLink %s 缺少 pair_id", link.id)
            return None, '短码配置错误，请联系管理员'

        pair = Pair.query.get(link.pair_id)
        if not pair:
            logger.error("PairLink %s 关联的 Pair %s 不存在", link.id, link.pair_id)
            return None, '短码已失效'

    # 步骤 4: 验证配对记录存在
    if not pair:
        return None, '短码不存在或已失效'

    # 步骤 5: 检查过期时间
    if pair.expires_at:
        now = utcnow_naive()
        if now > pair.expires_at:
            logger.info("Pair %s 已过期 (expires_at=%s)", pair.id, pair.expires_at)
            return None, '短码已过期'

    # 步骤 6: 验证社区匹配
    if pair.community_code != community_code:
        logger.warning(
            "社区代码不匹配: pair.community_code=%s, 请求=%s",
            pair.community_code, community_code
        )
        return None, '短码与当前社区不匹配'

    # 步骤 7: 更新兑换时间（防止重复更新）
    if link and not link.redeemed_at:
        link.redeemed_at = utcnow_naive()
        link.status = 'redeemed'
        try:
            db.session.flush()
        except Exception as exc:
            logger.exception("更新 PairLink 兑换状态失败")
            db.session.rollback()
            return None, '短码兑换失败，请重试'

    return pair, None
```

---

#### 2.5 数据库事务回滚加固
**文件**: `blueprints/public.py`, `blueprints/user.py`
**问题**: 缺少异常时的 rollback 处理
**修复方案**:

```python
# 统一事务上下文管理器
# utils/database.py (新建)
from contextlib import contextmanager
from core.extensions import db
import logging

logger = logging.getLogger(__name__)

@contextmanager
def atomic_transaction(description: str = "数据库操作"):
    """原子性事务上下文管理器

    用法:
        with atomic_transaction("创建用户"):
            user = User(...)
            db.session.add(user)
            db.session.flush()
    """
    try:
        yield db.session
        db.session.commit()
        logger.debug("%s - 事务提交成功", description)
    except Exception as exc:
        db.session.rollback()
        logger.exception("%s - 事务回滚: %s", description, exc)
        raise

# 使用示例
from utils.database import atomic_transaction

@bp.route('/register', methods=['POST'])
def register():
    # ... 验证逻辑

    try:
        with atomic_transaction("用户注册"):
            # 创建用户
            user = User(username=username, role='user')
            user.set_password(password)
            db.session.add(user)
            db.session.flush()  # 获取 user.id

            # 创建访客记录
            guest = GuestUser(user_id=user.id)
            db.session.add(guest)
            db.session.flush()

            # 发送欢迎邮件（如果失败不影响注册）
            try:
                send_welcome_email(user.email)
            except Exception as e:
                logger.warning("欢迎邮件发送失败: %s", e)

        flash('注册成功！', 'success')
        return redirect(url_for('auth.login'))

    except ValueError as exc:
        flash(f'注册失败: {exc}', 'error')
        return redirect(url_for('auth.register'))
    except Exception:
        flash('注册失败，请稍后重试', 'error')
        return redirect(url_for('auth.register'))
```

---

#### 2.6 文件操作异常分类
**文件**: `services/ai_question_service.py`
**问题**: 文件错误和 JSON 错误混在一起
**修复方案**:

```python
# services/ai_question_service.py

def _load_knowledge_base(self):
    """加载知识库（增强异常处理）"""
    kb_path = os.path.join(
        os.path.dirname(__file__),
        'data',
        'health_weather_kb.json'
    )

    # 文件不存在 - 正常情况，使用空知识库
    if not os.path.exists(kb_path):
        self.logger.info("知识库文件不存在: %s，使用空知识库", kb_path)
        AIQuestionService._knowledge_cache = []
        return

    # 文件存在但读取失败
    try:
        with open(kb_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except IOError as exc:
        self.logger.error("知识库文件读取失败: %s", exc)
        AIQuestionService._knowledge_cache = []
        return

    # JSON 解析失败 - 配置错误，需要修复
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        self.logger.error(
            "知识库 JSON 格式错误 (文件: %s, 行: %s, 列: %s): %s",
            kb_path, exc.lineno, exc.colno, exc.msg
        )
        AIQuestionService._knowledge_cache = []
        return

    # 数据验证
    if not isinstance(data, list):
        self.logger.error("知识库格式错误: 期望列表，得到 %s", type(data))
        AIQuestionService._knowledge_cache = []
        return

    AIQuestionService._knowledge_cache = data
    self.logger.info("知识库加载成功，共 %d 条记录", len(data))
```

---

#### 2.7 SECRET_KEY 严格验证
**文件**: `config.py`
**问题**: 某些情况下仍可能使用临时密钥
**修复方案**:

```python
# config.py

# 当前实现（有风险）
_secret_key_env = os.getenv('SECRET_KEY')
if _secret_key_env:
    SECRET_KEY = _secret_key_env
else:
    import secrets
    SECRET_KEY = secrets.token_hex(32)

# 稍后验证
if not _secret_key_env:
    if not DEBUG:
        raise RuntimeError("生产环境必须设置 SECRET_KEY...")

# 改进实现（更安全）
def get_secret_key():
    """获取 SECRET_KEY（严格验证）"""
    secret_key = os.getenv('SECRET_KEY')

    # 生产环境强制要求
    if not DEBUG:
        if not secret_key:
            raise RuntimeError(
                "生产环境必须设置 SECRET_KEY 环境变量！\n"
                "请在 .env 文件中添加：SECRET_KEY=<随机生成的密钥>\n"
                "可使用以下命令生成：\n"
                "  python -c 'import secrets; print(secrets.token_hex(32))'"
            )

        # 验证密钥强度
        if len(secret_key) < 32:
            raise RuntimeError(
                f"SECRET_KEY 长度不足（当前 {len(secret_key)} 字符，至少需要 32 字符）"
            )

        # 警告弱密钥
        weak_keys = ['dev', 'test', 'secret', 'password', '123456', 'hw-risk']
        if any(weak in secret_key.lower() for weak in weak_keys):
            raise RuntimeError(
                "检测到弱 SECRET_KEY！生产环境禁止使用包含常见词汇的密钥"
            )

    # 开发环境自动生成（带警告）
    if not secret_key:
        secret_key = secrets.token_hex(32)
        print("\n" + "=" * 60)
        print("⚠️  警告: 未配置 SECRET_KEY，已自动生成临时密钥")
        print("⚠️  重启后所有会话将失效！")
        print("=" * 60 + "\n")

    return secret_key

SECRET_KEY = get_secret_key()
```

---

### ✅ 阶段 3: 中低优先级问题修复 (10 项)

#### 3.1 登录速率限制加强
**文件**: `config.py`, `blueprints/auth.py`
**修复方案**:

```python
# config.py
app.config.setdefault('RATE_LIMIT_LOGIN', '5 per 5 minutes')  # 从 10/分钟 降低到 5/5分钟
app.config.setdefault('RATE_LIMIT_SHORT_CODE', '3 per hour')  # 从 20/小时 降低到 3/小时

# blueprints/auth.py (如果存在登录路由)
from flask_limiter.util import get_remote_address

@bp.route('/login', methods=['POST'])
@limiter.limit(
    lambda: current_app.config.get('RATE_LIMIT_LOGIN', '5 per 5 minutes'),
    key_func=lambda: f"login:{get_remote_address()}:{request.form.get('username', '')}"
)
def login():
    """登录（按 IP + 用户名限流，防止暴力破解）"""
    # ... 登录逻辑
```

---

#### 3.2 CSRF 令牌验证
**文件**: `blueprints/api.py`
**修复方案**:

```python
# core/extensions.py
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()

# core/app.py
from core.extensions import csrf

def create_app():
    app = Flask(__name__)
    csrf.init_app(app)

    # API 端点豁免 CSRF（使用 API key 认证）
    csrf.exempt('blueprints.api')  # 如果 API 使用 token 认证

    return app

# 或者，为每个 API 端点显式验证
from flask_wtf.csrf import validate_csrf

@bp.route('/api/v1/ml/predict', methods=['POST'])
@login_required
def api_v1_ml_predict():
    """ML 预测（CSRF 保护）"""
    try:
        # 验证 CSRF 令牌
        csrf_token = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
        if not csrf_token:
            return jsonify({'success': False, 'error': 'CSRF 令牌缺失'}), 403

        validate_csrf(csrf_token)
    except Exception as exc:
        logger.warning("CSRF 验证失败: %s", exc)
        return jsonify({'success': False, 'error': 'CSRF 验证失败'}), 403

    # ... 业务逻辑
```

**前端配置**:
```html
<!-- templates/base.html -->
<meta name="csrf-token" content="{{ csrf_token() }}">

<script>
// 所有 AJAX 请求自动包含 CSRF 令牌
$(document).ajaxSend(function(e, xhr, options) {
    const token = $('meta[name="csrf-token"]').attr('content');
    if (token) {
        xhr.setRequestHeader('X-CSRF-Token', token);
    }
});
</script>
```

---

#### 3.3 数据库连接池配置
**文件**: `config.py`
**修复方案**:

```python
# config.py

# SQLAlchemy 引擎选项
SQLALCHEMY_ENGINE_OPTIONS = {
    # 连接池大小
    'pool_size': 10,                # 常驻连接数
    'max_overflow': 20,             # 最大溢出连接数

    # 连接回收
    'pool_recycle': 3600,           # 1小时后回收连接（避免 MySQL gone away）
    'pool_pre_ping': True,          # 使用前 ping 检查连接有效性

    # 超时设置
    'pool_timeout': 30,             # 获取连接超时时间（秒）

    # 调试（仅开发环境）
    'echo_pool': DEBUG,             # 记录连接池事件
}

# 生产环境优化
if not DEBUG:
    SQLALCHEMY_ENGINE_OPTIONS.update({
        'pool_size': 20,
        'max_overflow': 40,
        'pool_recycle': 1800,       # 30分钟回收
    })
```

---

#### 3.4 时间戳重复设置防护
**文件**: `blueprints/public.py`
**修复方案**:

```python
# blueprints/public.py

# 修复前:
link.redeemed_at = utcnow_naive()  # 可能被多次调用

# 修复后:
if not link.redeemed_at:
    link.redeemed_at = utcnow_naive()
    logger.info("PairLink %s 首次兑换于 %s", link.id, link.redeemed_at)
else:
    logger.warning("PairLink %s 已于 %s 兑换，忽略重复操作", link.id, link.redeemed_at)
```

---

#### 3.5 安全审计日志
**文件**: `blueprints/user.py`, `blueprints/public.py`
**修复方案**:

```python
# utils/audit_log.py (新建)
import logging
from datetime import datetime, timezone
from flask import request, current_user

audit_logger = logging.getLogger('audit')

def log_security_event(event_type: str, **kwargs):
    """记录安全事件

    Args:
        event_type: 事件类型 (login, logout, short_code_generate, pair_create, etc.)
        **kwargs: 事件详情
    """
    user_id = current_user.id if current_user.is_authenticated else None
    ip_address = request.remote_addr

    audit_logger.info(
        "SECURITY_EVENT: type=%s, user_id=%s, ip=%s, details=%s",
        event_type, user_id, ip_address, kwargs
    )

# 使用示例
from utils.audit_log import log_security_event

# blueprints/user.py
@bp.route('/generate-short-code', methods=['POST'])
def generate_short_code():
    # ... 生成逻辑

    log_security_event(
        'short_code_generate',
        short_code_hash=short_code_hash[:8],  # 只记录前8位哈希
        community_code=community_code,
        expires_at=expires_at
    )

# blueprints/public.py
def redeem_short_code_internal(short_code, community_code):
    # ... 兑换逻辑

    log_security_event(
        'short_code_redeem',
        short_code_hash=short_code_hash[:8],
        community_code=community_code,
        pair_id=pair.id if pair else None,
        success=pair is not None
    )
```

**日志配置**:
```python
# config.py
import logging

# 配置审计日志独立文件
audit_handler = logging.FileHandler('logs/audit.log')
audit_handler.setLevel(logging.INFO)
audit_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))

audit_logger = logging.getLogger('audit')
audit_logger.addHandler(audit_handler)
audit_logger.setLevel(logging.INFO)
```

---

#### 3.6 过期时间强制检查
**文件**: `core/db_models.py`, `blueprints/public.py`
**修复方案**:

```python
# core/db_models.py

class Pair(db.Model):
    # ... 现有字段

    @property
    def is_expired(self):
        """检查配对是否过期"""
        if not self.expires_at:
            return False
        return datetime.now(timezone.utc).replace(tzinfo=None) > self.expires_at

    @property
    def is_active(self):
        """检查配对是否有效"""
        return self.status == 'active' and not self.is_expired

class PairLink(db.Model):
    # ... 现有字段

    @property
    def is_expired(self):
        """检查链接是否过期"""
        if not self.expires_at:
            return False
        return datetime.now(timezone.utc).replace(tzinfo=None) > self.expires_at

# blueprints/public.py
def redeem_short_code_internal(short_code, community_code):
    # ... 查询逻辑

    # 使用 property 检查
    if not pair.is_active:
        if pair.is_expired:
            return None, '短码已过期'
        else:
            return None, '短码已失效'

    # ... 其余逻辑
```

---

#### 3.7 短码强度增强
**文件**: `blueprints/user.py`
**修复方案**:

```python
# blueprints/user.py

# 修复前:
def _generate_short_code():
    """生成6位数字短码"""
    for _ in range(20):
        code = str(secrets.randbelow(1000000)).zfill(6)  # 只有 1M 种可能
        # ...

# 修复后:
def _generate_short_code():
    """生成 8 位数字短码（增强安全性）

    可能性: 100,000,000 (1亿)
    碰撞概率: 生日悖论，约 10,000 个码后 1% 碰撞率
    """
    for attempt in range(20):
        code = str(secrets.randbelow(100000000)).zfill(8)  # 8 位数字

        # 检查数据库唯一性
        code_hash = hash_pair_token(code)
        exists = Pair.query.filter_by(short_code_hash=code_hash).first()

        if not exists:
            return code

        logger.warning("短码碰撞（尝试 %d/20）: %s", attempt + 1, code[:2] + '******')

    raise RuntimeError("短码生成失败：20 次尝试均碰撞")

# 或者使用字母数字混合码（更强）
def _generate_alphanumeric_code(length=6):
    """生成字母数字混合短码

    可能性: 62^6 ≈ 56.8B (568亿)
    更高安全性，但输入不便
    """
    import string
    alphabet = string.ascii_uppercase + string.digits  # 36 种字符
    # 排除易混淆字符: O0, I1, etc.
    alphabet = alphabet.replace('O', '').replace('I', '').replace('0', '').replace('1', '')

    for _ in range(20):
        code = ''.join(secrets.choice(alphabet) for _ in range(length))
        code_hash = hash_pair_token(code)

        if not Pair.query.filter_by(short_code_hash=code_hash).first():
            return code

    raise RuntimeError("短码生成失败")
```

**配置选项**:
```python
# config.py
SHORT_CODE_LENGTH = 8              # 短码长度
SHORT_CODE_TYPE = 'numeric'        # 类型: 'numeric' | 'alphanumeric'
SHORT_CODE_EXPIRY_HOURS = 24      # 默认过期时间（小时）
```

---

#### 3.8 静默失败消除
**文件**: `blueprints/analysis.py`
**修复方案**:

```python
# blueprints/analysis.py

# 修复前:
try:
    # ... 业务逻辑
except Exception:
    pass  # ❌ 完全静默

# 修复后:
try:
    # ... 业务逻辑
except Exception as exc:
    logger.warning("操作失败，已忽略: %s", exc, exc_info=DEBUG)
    # 或者根据具体情况返回错误
    if current_app.config.get('STRICT_MODE'):
        raise
```

**全局查找**:
```bash
# 查找所有 bare pass
rg "except.*:\s*pass" --type py

# 查找所有 except Exception
rg "except Exception:" --type py -A 1
```

---

#### 3.9 错误消息国际化
**文件**: `blueprints/api.py`, `utils/validators.py`
**修复方案**:

```python
# utils/i18n.py (新建 - 简化版)
from flask import request

ERROR_MESSAGES = {
    'zh': {
        'generic_error': '服务暂时不可用，请稍后再试',
        'validation_error': '输入参数格式不正确',
        'auth_required': '需要登录',
        'permission_denied': '权限不足',
        'not_found': '资源不存在',
    },
    'en': {
        'generic_error': 'Service temporarily unavailable, please try again later',
        'validation_error': 'Invalid input parameters',
        'auth_required': 'Authentication required',
        'permission_denied': 'Permission denied',
        'not_found': 'Resource not found',
    }
}

def get_error_message(key: str, lang: str = None) -> str:
    """获取本地化错误消息"""
    if lang is None:
        # 从请求头推断语言
        lang = request.accept_languages.best_match(['zh', 'en']) or 'zh'

    return ERROR_MESSAGES.get(lang, ERROR_MESSAGES['zh']).get(key, key)

# 使用示例
from utils.i18n import get_error_message

return jsonify({
    'success': False,
    'error': get_error_message('validation_error')
})
```

**长期方案**: 使用 Flask-Babel
```python
# 安装: pip install flask-babel
from flask_babel import Babel, gettext as _

babel = Babel(app)

# 使用
return jsonify({
    'success': False,
    'error': _('服务暂时不可用，请稍后再试')
})
```

---

#### 3.10 环境变量验证增强
**文件**: `config.py`
**修复方案**:

```python
# config.py

def validate_production_config():
    """生产环境配置完整性检查"""
    if DEBUG:
        return  # 开发环境跳过

    # 必需配置
    required = {
        'SECRET_KEY': '会话加密密钥',
        'PAIR_TOKEN_PEPPER': '配对令牌加密盐',
    }

    # 推荐配置（警告但不阻止）
    recommended = {
        'QWEATHER_KEY': '天气 API 密钥（影响天气查询功能）',
        'DATABASE_URI': '数据库连接（默认使用 SQLite）',
    }

    # 检查必需配置
    missing_required = []
    for var, desc in required.items():
        value = os.getenv(var) or globals().get(var)
        if not value:
            missing_required.append(f"  ❌ {var}: {desc}")

    if missing_required:
        raise RuntimeError(
            "\n生产环境缺少必需配置:\n" +
            "\n".join(missing_required) +
            "\n\n请在 .env 文件中配置或设置环境变量。"
        )

    # 检查推荐配置
    missing_recommended = []
    for var, desc in recommended.items():
        value = os.getenv(var) or globals().get(var)
        if not value:
            missing_recommended.append(f"  ⚠️  {var}: {desc}")

    if missing_recommended:
        print("\n" + "=" * 60)
        print("⚠️  生产环境建议配置以下项:")
        print("\n".join(missing_recommended))
        print("=" * 60 + "\n")

# 在模块加载时验证
if __name__ != '__main__':
    try:
        validate_production_config()
    except RuntimeError as e:
        print(f"\n配置验证失败: {e}\n")
        import sys
        sys.exit(1)
```

---

## 测试策略

### 单元测试
```python
# scripts/test_fixes.py
import pytest
from datetime import datetime, timezone

def test_utcnow_naive():
    """测试 utcnow_naive 返回 naive datetime"""
    from core.time_utils import utcnow_naive

    now = utcnow_naive()
    assert now.tzinfo is None

    # 验证时间接近 UTC
    utc_now = datetime.now(timezone.utc)
    diff = abs((utc_now.replace(tzinfo=None) - now).total_seconds())
    assert diff < 2  # 允许 2 秒误差

def test_database_models_default_time():
    """测试数据库模型时间戳默认值"""
    from core.db_models import User
    from core.extensions import db

    user = User(username='test')
    db.session.add(user)
    db.session.flush()

    # 验证 created_at 已设置
    assert user.created_at is not None
    assert isinstance(user.created_at, datetime)

def test_json_size_limit():
    """测试 JSON 大小限制"""
    from core.hooks import from_json_filter

    # 正常大小
    small_json = '{"key": "value"}'
    assert from_json_filter(small_json) == {'key': 'value'}

    # 超大 JSON
    large_json = '[' + ','.join(['1'] * 100000) + ']'
    result = from_json_filter(large_json)
    assert result == []  # 应该被拒绝

def test_pair_expiration():
    """测试配对过期检查"""
    from core.db_models import Pair
    from datetime import timedelta

    # 已过期
    expired_pair = Pair(
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    )
    assert expired_pair.is_expired is True
    assert expired_pair.is_active is False

    # 未过期
    valid_pair = Pair(
        status='active',
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    )
    assert valid_pair.is_expired is False
    assert valid_pair.is_active is True

def test_short_code_generation():
    """测试增强的短码生成"""
    from blueprints.user import _generate_short_code

    code = _generate_short_code()
    assert len(code) == 8  # 8 位数字
    assert code.isdigit()

    # 生成 100 个码，检查唯一性
    codes = set(_generate_short_code() for _ in range(100))
    assert len(codes) == 100  # 无碰撞

def test_audit_logging(caplog):
    """测试审计日志"""
    from utils.audit_log import log_security_event

    log_security_event('test_event', user='test_user', action='test_action')

    assert 'SECURITY_EVENT' in caplog.text
    assert 'test_event' in caplog.text
```

### 集成测试
```python
# tests/test_integration.py
def test_short_code_flow(client, db_session):
    """测试完整的短码流程"""
    # 1. 生成短码
    response = client.post('/generate-short-code', data={
        'community_code': 'TEST_COMMUNITY'
    })
    assert response.status_code == 200

    # 2. 兑换短码
    short_code = response.json['short_code']
    response = client.post('/redeem-short-code', data={
        'short_code': short_code,
        'community_code': 'TEST_COMMUNITY'
    })
    assert response.status_code == 200

    # 3. 验证过期
    from core.db_models import Pair
    pair = Pair.query.filter_by(community_code='TEST_COMMUNITY').first()
    assert pair.is_expired is False

def test_transaction_rollback(client, db_session):
    """测试事务回滚"""
    from core.db_models import User
    from core.extensions import db
    from utils.database import atomic_transaction

    initial_count = User.query.count()

    # 模拟失败的事务
    try:
        with atomic_transaction("测试事务"):
            user = User(username='test')
            db.session.add(user)
            db.session.flush()

            # 触发异常
            raise ValueError("测试异常")
    except ValueError:
        pass

    # 验证回滚
    assert User.query.count() == initial_count
```

---

## 部署检查清单

### 部署前
- [ ] 所有测试通过 (`pytest tests/ -v`)
- [ ] 代码审查完成
- [ ] `.env.example` 已更新
- [ ] 文档已更新
- [ ] 数据库备份已完成

### 安全配置
- [ ] 撤销旧 API 密钥
- [ ] 生成新 SECRET_KEY
- [ ] 生成新 PAIR_TOKEN_PEPPER
- [ ] 配置新的外部 API 密钥
- [ ] 验证 `.env` 不在版本控制中

### 数据库迁移（如果需要）
```bash
# 备份数据库
cp storage/health_weather.db storage/health_weather.db.backup_$(date +%Y%m%d_%H%M%S)

# 运行迁移（如果有）
flask db upgrade

# 验证迁移
python3 -c "from core.app import create_app; app = create_app(); app.app_context().push(); from core.extensions import db; db.create_all(); print('✅ 数据库验证通过')"
```

### 环境变量配置
```bash
# 生产环境 .env 配置
cat > .env << 'EOF'
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
PAIR_TOKEN_PEPPER=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
DEBUG=false
DATABASE_URI=sqlite:///storage/health_weather.db

# 外部 API 密钥（需要从控制台获取新密钥）
QWEATHER_KEY=<新密钥>
AMAP_KEY=<新密钥>
SILICONFLOW_API_KEY=<新密钥>

# 速率限制
RATE_LIMIT_LOGIN=5 per 5 minutes
RATE_LIMIT_AI=30 per hour
RATE_LIMIT_SHORT_CODE=3 per hour
EOF

# 执行环境变量替换
python3 << 'PYEOF'
import secrets
import os

with open('.env', 'r') as f:
    content = f.read()

content = content.replace(
    'SECRET_KEY=$(python3 -c \'import secrets; print(secrets.token_hex(32))\')',
    f'SECRET_KEY={secrets.token_hex(32)}'
)
content = content.replace(
    'PAIR_TOKEN_PEPPER=$(python3 -c \'import secrets; print(secrets.token_hex(32))\')',
    f'PAIR_TOKEN_PEPPER={secrets.token_hex(32)}'
)

with open('.env', 'w') as f:
    f.write(content)

print('✅ .env 配置完成')
PYEOF
```

### 部署后验证
```bash
# 运行健康检查
curl http://localhost:5000/health

# 验证速率限制
for i in {1..10}; do curl -X POST http://localhost:5000/login -d "username=test&password=test"; done

# 检查日志
tail -f logs/app.log logs/audit.log
```

---

## 回滚计划

### 如果部署失败
```bash
# 1. 停止应用
sudo systemctl stop case-weather

# 2. 恢复数据库
cp storage/health_weather.db.backup_YYYYMMDD_HHMMSS storage/health_weather.db

# 3. 恢复代码
git revert <commit_hash>

# 4. 恢复环境变量
cp .env.backup .env

# 5. 重启应用
sudo systemctl start case-weather
```

---

## 预估影响

### 兼容性
- ✅ **向后兼容**: 所有修复保持向后兼容
- ✅ **数据迁移**: 无需数据库迁移（时间戳仍为 naive UTC）
- ⚠️ **会话失效**: SECRET_KEY 更改后所有用户需要重新登录

### 性能影响
- ✅ **数据库连接池**: 提升并发性能
- ✅ **异常处理**: 略微增加开销（<1ms）
- ✅ **审计日志**: 磁盘 I/O 增加（异步写入）

### 安全提升
- 🔒 **XSS 防护**: 已有 bleach 保护，无变化
- 🔒 **CSRF 防护**: 新增 API 端点保护
- 🔒 **暴力破解**: 速率限制从 10/分钟 降至 5/5分钟
- 🔒 **短码强度**: 从 10^6 提升至 10^8

---

## 监控指标

### 部署后监控
```python
# 添加监控指标
from prometheus_client import Counter, Histogram

# 速率限制触发次数
rate_limit_hits = Counter('rate_limit_hits', 'Rate limit hit count', ['endpoint'])

# 短码生成失败次数
short_code_failures = Counter('short_code_generation_failures', 'Short code generation failures')

# API 响应时间
api_latency = Histogram('api_request_duration_seconds', 'API request latency', ['endpoint'])
```

### 告警规则
```yaml
# alerts.yml
- alert: HighRateLimitHits
  expr: rate(rate_limit_hits[5m]) > 10
  annotations:
    summary: "频繁触发速率限制"

- alert: ShortCodeGenerationFailure
  expr: short_code_failures > 0
  annotations:
    summary: "短码生成失败"
```

---

## 总结

### 修复统计
- **关键问题**: 3 项
- **高优先级**: 7 项
- **中低优先级**: 10 项
- **新增文件**: 4 个
- **修改文件**: ~15 个
- **新增测试**: ~10 个

### 预计工作量
- **代码修改**: 2-3 小时
- **测试验证**: 1-2 小时
- **部署配置**: 0.5-1 小时
- **总计**: 4-6 小时

### 风险评估
- **技术风险**: 低（向后兼容）
- **数据风险**: 低（无 schema 变更）
- **业务风险**: 低（功能无破坏性变更）
- **安全风险**: 极低（纯加固）

---

**下一步**: 执行一键修复命令（见下方 Prompt）
