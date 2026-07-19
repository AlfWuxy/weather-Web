# 系统架构文档

> 版本: 2026-01-29  
> 项目: 天气变化与社区居民健康风险预测系统

---

## 1. 当前架构概览

### 1.1 系统架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                         客户端层                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│  │ 浏览器   │  │ 管理后台 │  │ 老人模式 │  │ 游客模式 │        │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘        │
└───────┼─────────────┼─────────────┼─────────────┼───────────────┘
        │             │             │             │
┌───────┴─────────────────────────────────────────────────────────┐
│                         Flask 应用层                              │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  app.py (薄入口)  →  core/app.py (工厂/CLI/蓝图注册)        │ │
│  │  blueprints/ (路由层)  +  core/ (配置/扩展/安全/钩子)       │ │
│  └────────────────────────────────────────────────────────────┘ │
└───────┬─────────────────────────────────────────────────────────┘
        │
┌───────┴─────────────────────────────────────────────────────────┐
│                         服务层 (services/)                       │
│  weather / forecast / dlnm / chronic / ml / ai / community ...  │
└───────┬─────────────────────────────────────────────────────────┘
        │
┌───────┴─────────────────────────────────────────────────────────┐
│                         外部服务层                               │
│  QWeather / Open-Meteo / AMap / SiliconFlow / Redis             │
└───────┬─────────────────────────────────────────────────────────┘
        │
┌───────┴─────────────────────────────────────────────────────────┐
│                         数据层                                   │
│  SQLAlchemy + SQLite/PostgreSQL + 天气缓存/预测缓存              │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 当前问题

| 问题 | 严重性 | 说明 |
|------|--------|------|
| 文档与代码不一致 | 🔴 高 | 架构文档仍描述旧的巨型 app.py 与目录结构 |
| 服务层依赖框架上下文 | 🟡 中 | 部分服务仍依赖 request/current_app，影响可测性 |
| 废弃模块未清理 | 🟡 中 | 部分服务文件已标记废弃但仍保留 |
| API 规范化不足 | 🟡 中 | 缺少统一的 OpenAPI 文档与请求校验 |
| 可观测性不足 | 🟢 低 | 仅日志+可选 Sentry，缺少指标/Tracing |

---

## 2. 目标架构（对齐当前实现）

### 2.1 目录结构

```
天气预警网站/
├── app.py                    # 应用入口（薄层）
├── config.py                 # 静态配置与常量
├── requirements.txt
│
├── core/                     # 核心框架
│   ├── app.py                # 应用工厂/蓝图注册/CLI
│   ├── db_models.py          # SQLAlchemy 模型（21 表）
│   ├── extensions.py         # Flask 扩展初始化
│   ├── config.py             # 环境变量解析与配置校验
│   ├── hooks.py              # 请求/响应钩子
│   ├── security.py           # CSRF/限流/加密
│   └── ...                   # 其他核心模块
│
├── blueprints/               # 路由层（页面 + API）
│   ├── public.py
│   ├── user.py
│   ├── admin.py
│   ├── api.py
│   └── ...
│
├── services/                 # 业务服务层
│   ├── weather_service.py
│   ├── ml_prediction_service.py
│   ├── dlnm_risk_service.py
│   ├── ai_question_service.py
│   └── ...
│
├── utils/                    # 工具函数
│   ├── parsers.py
│   ├── validators.py
│   └── error_handlers.py
│   ├── validators.py        # 输入验证
│   ├── parsers.py           # 数据解析 (parse_age, parse_int等)
│   ├── helpers.py           # 通用工具 (天气缓存, 通知等)
│   └── response.py          # JSON响应封装
│
├── templates/                # 模板 (保持现状)
├── static/                   # 静态资源 (保持现状)
├── data/                     # 数据文件
├── tests/                    # 测试
└── scripts/                  # 离线脚本
    ├── import_data.py
    ├── train_model.py       # 合并后的训练脚本
    └── analyze_data.py      # 合并后的分析脚本
```

### 2.2 模块边界

```
┌────────────────────────────────────────────────────────────────┐
│                    blueprints/ (视图层)                         │
│  接收HTTP请求 → 调用服务 → 渲染模板/返回JSON                    │
│  不包含业务逻辑，仅做路由转发和数据格式化                        │
└───────────────────────────────┬────────────────────────────────┘
                                │
┌───────────────────────────────┴────────────────────────────────┐
│                       services/ (服务层)                        │
│  业务逻辑：风险计算、预测算法、外部API调用                      │
│  尽量减少对 Flask request/session 的直接依赖                     │
└───────────────────────────────┬────────────────────────────────┘
                                │
┌───────────────────────────────┴────────────────────────────────┐
│                      core/db_models.py (模型层)                 │
│  SQLAlchemy模型定义，纯数据结构                                 │
└───────────────────────────────┬────────────────────────────────┘
                                │
┌───────────────────────────────┴────────────────────────────────┐
│                        utils/ (工具层)                          │
│  通用工具函数，无状态，可复用                                   │
│  验证、解析、格式化、缓存等                                     │
└────────────────────────────────────────────────────────────────┘
```

### 2.3 短期改进清单（1-2 周）

| 项目 | 状态 | 说明 |
|------|------|------|
| 架构文档与概览对齐 | ✅ | 更新 `docs/ARCHITECTURE.md` 与 `PROJECT_OVERVIEW.md` |
| 清理废弃模块 | ⏳ | 标记废弃服务迁移计划，移除未被引用代码 |
| API 规范化 | ⏳ | 增加 OpenAPI 草案 + 基础请求校验 |
| 测试补强 | ⏳ | 覆盖关键服务与 API 的边界用例 |

---

## 3. 数据模型

### 3.1 ER图

```
User 1──────────────n HealthRiskAssessment
  │                        
  │ 1                      
  ├──n FamilyMember 1──1 FamilyMemberProfile
  │        │
  │        └──n HealthDiary
  │        └──n MedicationReminder
  │
  └──n Notification
  
Community 1──────────n MedicalRecord
         1──────────n WeatherData (按location)

WeatherCache (天气缓存，按location)
ForecastCache (预报缓存，按location+days)
WeatherAlert (天气预警)
AuditLog (审计日志)
```

### 3.2 模型清单

| 模型 | 用途 | 关联 |
|------|------|------|
| User | 用户账户 | → FamilyMember, HealthRiskAssessment, Notification |
| GuestUser | 游客(内存) | - |
| MedicalRecord | 病历记录 | → Community |
| WeatherData | 天气数据 | 按date+location |
| WeatherCache | 天气缓存 | 按location |
| ForecastCache | 预报缓存 | 按location+days |
| Community | 社区信息 | - |
| HealthRiskAssessment | 健康评估 | → User |
| WeatherAlert | 天气预警 | - |
| FamilyMember | 家庭成员 | → User |
| FamilyMemberProfile | 成员画像 | → FamilyMember |
| HealthDiary | 健康日记 | → User, FamilyMember |
| MedicationReminder | 用药提醒 | → User, FamilyMember |
| Notification | 站内通知 | → User |
| AuditLog | 审计日志 | - |

---

## 4. API端点

### 4.1 页面路由

| 路由 | 方法 | 权限 | 说明 |
|------|------|------|------|
| `/` | GET | 公开 | 首页 |
| `/login` | GET/POST | 公开 | 登录 |
| `/register` | GET/POST | 公开 | 注册 |
| `/guest` | GET | 公开 | 游客入口 |
| `/dashboard` | GET | 登录 | 用户仪表板 |
| `/health-assessment` | GET/POST | 登录 | 健康评估 |
| `/community-risk` | GET | 登录 | 社区风险 |
| `/profile` | GET/POST | 登录 | 个人设置 |
| `/family-members` | GET/POST | 登录 | 家庭成员 |
| `/health-diary` | GET/POST | 登录 | 健康日记 |
| `/ai-qa` | GET | 登录 | AI问答 |
| `/forecast-7day` | GET | 登录 | 7天预测 |
| `/chronic-risk` | GET | 登录 | 慢病风险 |
| `/ml-prediction` | GET | 登录 | ML预测 |
| `/admin/*` | GET/POST | 管理员 | 管理后台 |
| `/analysis/*` | GET/POST | 登录 | 数据分析 |

### 4.2 REST API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/weather/current` | GET | 当前天气 |
| `/api/community/list` | GET | 社区列表 |
| `/api/community/risk-map` | GET | 风险地图 |
| `/api/forecast/7day` | POST | 7天预测 |
| `/api/chronic/individual` | POST | 个体慢病风险 |
| `/api/chronic/rules-version` | GET | 规则版本 |
| `/api/ml/predict` | POST | ML预测 |
| `/api/ml/status` | GET | ML状态 |
| `/api/dlnm/risk` | POST | DLNM风险 |
| `/api/ai/ask` | POST | AI问答 |
| `/api/alert/comprehensive` | POST | 综合预警 |

---

## 5. 技术栈

| 层级 | 技术 |
|------|------|
| Web框架 | Flask 2.3.3 |
| ORM | SQLAlchemy 2.0.23 (Flask-SQLAlchemy) |
| 认证 | Flask-Login 0.6.3 |
| 限流 | Flask-Limiter 3.7.0 |
| 数据库 | SQLite (开发), 可切换PostgreSQL |
| 模板 | Jinja2 3.1.2 |
| 前端 | Bootstrap + Chart.js + 高德地图 |
| ML | scikit-learn 1.3+ |
| 数据 | pandas 2.1.3, numpy 1.26.4 |
| 报告 | ReportLab (PDF), openpyxl (Excel) |

---

## 6. 部署架构

```
┌─────────────────────────────────────────┐
│              Nginx (反向代理)            │
│              :80 / :443                 │
└────────────────────┬────────────────────┘
                     │
┌────────────────────┴────────────────────┐
│           Gunicorn (WSGI服务器)          │
│              :5000 (内部)               │
│           workers = 4                   │
└────────────────────┬────────────────────┘
                     │
┌────────────────────┴────────────────────┐
│           Flask Application             │
│              app.py                     │
└────────────────────┬────────────────────┘
                     │
┌────────────────────┴────────────────────┐
│           SQLite / PostgreSQL           │
│          instance/health_weather.db     │
└─────────────────────────────────────────┘
```

### 6.1 小程序运行与发布控制面

| 链路 | 当前边界 |
|------|----------|
| 公共天气 | 小程序只读取服务端持久化的都昌县快照；Web 当前天气、七日预报和小时降水也只读取周期缓存。bootstrap timer 在部署或开机后完整等待 30 分钟并直接触发缓存服务，首次同步无论成功或失败都通过 `OnSuccess`/`OnFailure` 启动 recurring timer；客户端公共快照缓存 30 分钟。网络闸门在正式服务启动前设置，请求上下文中的 QWeather 预算预占会直接拒绝，普通页面、风险预计算和 `/healthz` 不触发额外上游请求。 |
| 微信登录 | 正式 AppID、AppSecret 和隐私版本由本机私密发布表单进入受控服务器环境。AppSecret、OpenID pepper 和会话密钥不进入小程序包；pepper 与会话密钥在服务器内生成。 |
| 分享与换号 | 只有“分享给家人”按钮生成固定 `from=family_share`。普通分享不带归因，个人页回退到公开首页；登录成功后一次性消费来源，退出和账号注销清理本机会话与来源上下文。 |
| 匿名分析 | 公开浏览使用微信公众平台聚合统计。自有事件只接受固定枚举和最小账号级维度，原始事件保留 30 天，管理看板与 CSV 只输出聚合结果并排除配置的测试账号。地区聚合只使用社区编码，`ANALYTICS_MIN_LOCATION_COUNT` 在生产环境最小强制为 3。 |
| 推送 | WxPusher 默认关闭且需要用户明确同意。投递先以数据库唯一占位保证单次发送；超时、失败和结果不明确的记录进入管理员人工复核队列，复核前禁止自动重发。 |
| 发布门禁 | `.env.wechat-release` 只存在于本机且权限为 `0600`。正式发布要求个人主体类目证据、运营者资料、AppID、隐私版本、`WECHAT_CATEGORY_CONFIRMED=1` 和 `WECHAT_FORM_READY=1` 全部成立。 |

个人主体类目截图和运营者认证资料属于私有发布证据，只放本机私有目录或私有 ops。公开仓库只记录字段、门禁逻辑和复核流程。详细步骤见 `docs/miniprogram/WECHAT_RELEASE_HANDOFF.md` 与 `docs/miniprogram/RELEASE_CHECKLIST.md`。

---

## 7. 服务器运维指南

### 7.1 服务器信息

| 项目 | 值 |
|------|------|
| 服务器IP | `<managed-in-private-ops>` |
| 操作系统 | Debian 12 |
| 持久化状态目录 | `/opt/your-app` |
| 不可变发布根目录 | `/opt/your-app-deploy` |
| 当前版本入口 | `/opt/your-app-deploy/current` |
| Python版本 | 3.11.2 |
| 虚拟环境 | 每个 release 独立的 `venv/` |
| 数据库 | /opt/your-app/instance/health_weather.db |
| 服务名称 | case-weather.service |
| 内部端口 | `127.0.0.1:5000`，只允许 Nginx 反向代理访问 |

### 7.2 服务管理命令

```bash
# 启动服务
systemctl start case-weather

# 停止服务
systemctl stop case-weather

# 重启服务
systemctl restart case-weather

# 查看服务状态
systemctl status case-weather

# 查看实时日志
journalctl -u case-weather -f

# 查看最近50条日志
journalctl -u case-weather --no-pager -n 50
```

### 7.3 代码部署

```bash
# 首次正式发布先从模板建立被忽略的私密表单。
cp .env.wechat-release.example .env.wechat-release
chmod 600 .env.wechat-release
git check-ignore .env.wechat-release

# 首次执行前，先人工核对服务器指纹并写入 ~/.ssh/known_hosts。
# 正式发布只使用这个入口；sync.sh 会进入同一流程。
ENV_FILE=/path/to/private-release.env \
WECHAT_RELEASE_FORM_FILE=/absolute/path/to/.env.wechat-release \
./scripts/deploy.sh
```

正式发布时，私密部署配置中的 `DEPLOY_REQUIRE_WECHAT_READY=1` 会要求发布表单通过权限、个人主体、类目、运营资料、AppID、AppSecret 与隐私版本校验。`WECHAT_CATEGORY_CONFIRMED` 只能在发布当天保存正式个人主体类目截图并人工复核后设为 `1`；`WECHAT_FORM_READY` 必须最后开启。

`deploy.sh` 会创建不可变 release、独立虚拟环境和候选配置，上传时排除所有 `.env*` 与 `project.private.config.json`，先跑全量测试，再在本机隔离端口验活。激活事务负责备份数据库与环境、执行 Alembic、原子切换 `current`、替换 systemd 单元、设置 30 分钟 QWeather 网络闸门并启动 bootstrap timer；服务、两阶段 timer、缓存服务的 `OnSuccess`/`OnFailure`、单调时钟剩余窗口、`current` 链接、暂存环境清理和公网健康检查全部通过后才写入 `COMMITTED`。进入向前提交阶段后的复核失败会写入 `POST_COMMIT_ATTENTION.txt`，阻断下一次激活并保留新数据库。禁止把代码 rsync 到持久化状态目录后手工重启，这会绕过预检、迁移校验和回滚边界。

### 7.4 数据库备份策略

#### 备份机制

| 项目 | 详情 |
|------|------|
| 备份位置 | `/opt/your-app/backups/` |
| 备份频率 | 每天凌晨3点自动执行 (Cron) |
| 保留天数 | 30天（自动删除旧备份） |
| 备份格式 | `.db.gz` (SQLite + gzip压缩) |

#### 备份脚本

```bash
PROJECT_DIR=/opt/your-app \
ENV_FILE=/opt/your-app/.env \
BACKUP_DIR=/opt/your-app/backups \
/opt/your-app-deploy/current/app/scripts/backup.sh
```

#### Cron定时任务

```bash
# 查看定时任务
crontab -l

# 当前配置（每天凌晨3点执行）
0 3 * * * PROJECT_DIR=/opt/your-app ENV_FILE=/opt/your-app/.env BACKUP_DIR=/opt/your-app/backups /opt/your-app-deploy/current/app/scripts/backup.sh >> /opt/your-app/backups/backup.log 2>&1
```

#### 手动备份

```bash
# 在服务器上执行当前 release 中的受控脚本
PROJECT_DIR=/opt/your-app ENV_FILE=/opt/your-app/.env \
  /opt/your-app-deploy/current/app/scripts/backup.sh

# 查看备份列表
ls -lh /opt/your-app/backups/
```

#### 下载备份到本地

```bash
# 建议在私有 ops 文档或私有脚本目录中执行下载流程
# 公开仓库只保留手动示例
scp deploy-user@your-server-host:/opt/your-app/backups/*.gz ./backups/
```

#### 恢复数据库

激活事务失败时由 `activate_release.sh` 自动恢复同一事务内的数据库、环境、release 链接和 systemd 状态。公网服务一旦尝试启动，流程进入只向前修复区间，避免覆盖已经确认的用户写入。跨日期备份恢复属于破坏性运维，只能在维护窗口依据私有 ops 手册执行，先核对目标备份、当前事务目录、`PRAGMA quick_check` 与 `PRAGMA foreign_key_check`。

### 7.5 环境变量配置

环境变量存储在 `/opt/your-app/.env` 文件中（权限 600）：

```bash
# 本地私密发布表单，完整字段见 .env.example。
DEPLOY_SERVER=your-server-host
DEPLOY_USER=deploy-user
DEPLOY_PROJECT_DIR=/opt/your-app
DEPLOY_LOCAL_DIR=/path/to/your-local-repo
WECHAT_RELEASE_FORM_FILE=/absolute/path/to/.env.wechat-release
PUBLIC_BASE_URL=https://your-production-domain.example
QWEATHER_AUTH_MODE=api_key
QWEATHER_API_BASE=https://your-qweather-host.example
QWEATHER_KEY=<server-only>
WX_MINIPROGRAM_APPID=<认证后填写>
WX_MINIPROGRAM_SECRET=<server-only>
DEPLOY_REQUIRE_WECHAT_READY=1
```

普通 `.env` 保存部署与服务器运行参数；运营者姓名、类目门禁和正式微信凭据以 `.env.wechat-release` 为唯一交接表单。`scripts/deploy.sh` 会读取私密表单，并在服务器内生成微信 OpenID pepper 与会话密钥。正式发布要求 HTTPS、有效天气配置和完整微信登录凭据；显式的降级预览不会被误标成正式可上架版本。公开仓库只记录 SSH Key 方式，其他运维细节放到私有 ops 文档。

### 7.6 发布事务与人工恢复确认

发布前记录当前线上小程序版本、平台当时可用的回退版本、目标 commit、后端 `current` release、数据库备份与事务状态。用户确认发布和回滚目标后再点击正式发布。

激活事务在公网切换前失败时自动恢复数据库、旧 release 与 systemd 状态。公网服务已经尝试启动后只进行向前修复，并写入 `POST_COMMIT_ATTENTION.txt`，以保护可能已经确认的用户写入。发现 `ROLLBACK_REQUIRED.txt` 或 `POST_COMMIT_ATTENTION.txt` 时，新部署保持阻塞；管理员核对数据库、`current` 链接、systemd 单元和精确事务目录后，才通过 `DEPLOY_RECOVERY_ACKNOWLEDGED_TRANSACTION` 登记恢复确认。

小程序客户端出现严重问题时，优先使用平台当时可用的版本管理能力回退客户端，Web 公共服务继续运行。后台没有可用回退版本时先停止发布，准备修复包与用户告知方案。

### 7.7 常见问题排查

#### 1. 服务启动失败

```bash
# 查看详细错误
journalctl -u case-weather --no-pager -n 100 | grep -E "(ERROR|Exception)"

# 手动测试应用
cd /opt/your-app-deploy/current/app
/opt/your-app-deploy/current/venv/bin/python -c "from app import app; print('OK')"
```

#### 2. 数据库列缺失错误

```bash
# 只读检查表结构与迁移版本
sqlite3 /opt/your-app/instance/health_weather.db "PRAGMA table_info(表名);"
cd /opt/your-app-deploy/current/app
/opt/your-app-deploy/current/venv/bin/python -m alembic current
```

不要手工执行 `ALTER TABLE` 或在生产库运行 `db.create_all()` 修表。重新走不可变发布事务，由 Alembic、单一 head 校验、小程序关键列校验和外键检查决定能否切换。

#### 3. 天气API返回模拟数据

先检查候选配置 readiness 与 timer 日志。普通页面访问、健康检查和回归测试均不得直接触发 QWeather。

```bash
python3 /opt/your-app-deploy/current/app/scripts/validate_release_env.py \
  --file /opt/your-app/.env --require-wechat 1
systemctl status case-weather-cache-bootstrap.timer --no-pager
systemctl status case-weather-cache.timer --no-pager
journalctl -u case-weather-cache.service --no-pager -n 50
```

部署或开机后由 bootstrap timer 完整等待 30 分钟，再直接执行首次 `case-weather-cache.service` 同步；单次周期会持久化实况、七日预报、预警、小程序快照和供 Web 时间轴读取的 24 小时 Open-Meteo 降水缓存，同步无论成功或失败都通过缓存服务的 `OnSuccess`/`OnFailure` 启动 recurring timer。正式凭据就绪后的唯一一次受控真实联调先完成运行用户 JWT 离线签名和 Redis 预算前值读取，成功后记录调用前后预算差值；普通部署与健康检查不得消耗该次数。

---
