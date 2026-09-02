# 系统架构文档

> 版本: 2026-09-02
> 项目: 宜老天气通（天气变化与社区居民健康风险预测系统）

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

本轮已修的用户路径问题：登录用户名大小写与锁定桶对齐；用药提醒按 `time_of_day` 到点出现（不再要求慢病+在线天气）；健康评估筛查回填；家庭档案「慢阻肺」与 COPD 同义；社区缺测坐标标为估算；天气 API 不再把 Open-Meteo 的 AQI=0 当成实测；照护页区分「20:00 前确认」与「2 小时未确认转备用联系人」，卡片同时显示两套倒计时；`/entry` 老人入口改为照护码确认，首页仍进极简模式。慢病评估改为 PRG，刷新保留上次结果；和风实况写入热浪/寒潮连续天数；试点 CSV 改为带 CSRF 的 POST；个人设置可撤销 API 凭证。7 天健康预测在滞后观测不足或门诊基线缺失时不再编造就诊负担百分比；无当天污染物预报时，7 天暴露分只按气温、湿度和热夜计分，不把当前实况 PM 或默认 AQI 50 算进「风险 N」。健康评估在社区 VI/负担为代理值或缺测 AQI/湿度时不把这些数字编进个人风险分；未填写年龄或五项筛查未答完时不评估。无模型时 `/ml-prediction` 不提供生成入口；有模型时也不再用 65 岁填年龄。慢病评估同样要求个人设置已有年龄。Open-Meteo 缺湿度不再写成 60。API 层慢病/ML 在 JSON 和资料都缺年龄时返回 400，不再用 40/50 岁顶上。ML 个人/社区预测在未传天气时走和风实况，mock/demo 返回 503，不再用 20°C、湿度 70、AQI 50 顶上；传入气温但未传湿度/AQI/风速时保留缺测，不填 70/50/2.5。未传日照时长不再填 20000 秒；社区预测未传人口/老龄率不再填 100/0.2。健康评估服务缺年龄或缺气温时停止计算，不再用 45 岁或 20°C 顶上。Open-Meteo 日预报缺最高/最低气温时跳过该日，不再填 25/15°C；短临小时缺气温时跳过，不再填 0°C。7 天健康预测缺某日气温时停止就诊负担计算，不再填 15°C。今日页无行动提示时不再编造 1.5L 补水或测血压/降压药。Open-Meteo 未知天气代码不再写成「多云」或缺测代码写成「晴」。极端天气识别不再把缺测气温/湿度/风速/AQI 当成 0。ML 预测服务缺年龄或缺气温时停止计算，天气特征向量不再用 15°C/湿度 70 等训练集缺省值顶上。7 天复合暴露缺气温时停止计分，不再填 20°C；缺夜间最低气温不再用「白天减 4°C」编造热夜，缺湿度不再填 60。社区档案缺人口时保留空值，不再填 100 人进入排名。社区绿地、热岛和医疗可达性缺测时不计入脆弱性指数和 SVI，不再写入稳定代理值。小程序新建老人与网页一样校验性别。今日页主按钮排出照护工作台 → 记录今天是否做到 → 附近避暑点 → 7 天趋势。避暑页温度建议和页脚从 `cooling_page.json` 的 `bands`/`footer` 读取。和风实况缺天气状况不再写成「晴」，日预报缺风速不再填 3.0。热风险缺夜间最低温不再复用当前气温。老人模式无行动清单时不再编造「身边放一杯水」。照护话术只列出已登记慢病名称，不再追加感冒/胸闷临床句。DLNM `calculate_rr` 缺气温时停止计算，不再填 20°C。求助文案区分是否开通推送。每日行动提示已抽到 `data/content/daily_action_tips.json`；照护提醒话术已抽到 `data/content/caregiver_tip_scripts.json`；避暑页导语和温度分档已抽到 `data/content/cooling_page.json`；指标说明已抽到 `data/content/metric_explanations.json`。今日页/长者页标题和空清单已抽到 `data/content/dashboard_copy.json`；7 天「本周建议」已抽到 `data/content/forecast_week_tips.json`，改为照护口吻，不再写增派医护。社区风险页「管控建议」已改为「优先行动」，文案从 `data/content/community_action_tips.json` 读取，不再写增派医疗资源。未知热风险等级不再套用低风险动作清单，页面显示「风险未知」。社区地图缺测 `risk_index` 不再填 55 或画成 0 分低风险。多源预报缺最高/最低气温时不再用 8°C 温差编造昼夜气温。和风与 Open-Meteo 都失败时 7 天预报返回空列表，不再把未标记的模拟天当实测缓存。社区工作台入口不再混用「社区看板」称呼。

| 问题 | 严重性 | 说明 |
|------|--------|------|
| 巨型模块仍在 | 🟡 中 | `blueprints/analysis.py`（约 3300 行）、`community_risk_service.py`、`weather_service.py`、`community_risk.html` 仍过重，后续应按页面/用例拆分 |
| 样式层叠过多 | 🟡 中 | 每页同时加载 `style.css`、`yilao.css`、`apple-polish.css` 与多套 motion/data-fx，长期应收敛成一套主题 |
| 内容写死在代码里 | 🟡 中 | 每日提示、照护话术、避暑分档、指标解释、今日/长者标题、7 天照护建议、社区优先行动已在 `data/content/`。慢病/ML/健康评估以及 7 天 API 里的角色行动卡仍有代码内规则。 |
| 研究页仍在主路径 | 🟡 中 | 「AI 疾病预测」在模型未加载时已从导航和今日主页拿掉；`/ml-prediction` 直链仍在。仓库没有 `models/*.pkl` |
| 小程序发布配置 | 🟡 中 | 公开仓库里 `miniprogram/config.js` 的 `API_BASE_URL` 必须留空；发布前在本地填写真实域名 |
| 服务层依赖框架上下文 | 🟡 中 | 部分服务仍依赖 request/current_app，影响可测性 |
| API 规范化不足 | 🟢 低 | 缺少统一的 OpenAPI 文档与请求校验 |
| 可观测性不足 | 🟢 低 | 仅日志+可选 Sentry，缺少指标/Tracing |

### 1.3 产品简洁化方向（下一步，不在本轮一次做完）

面向长期维护、可持续更新内容的网站 + 小程序，建议按这个顺序收敛，而不是继续加研究页：

1. **一条主路径**：今天风险 → 提醒家人 → 记录是否做到 → 附近避暑。Web 主导航保持「今天 / 照护 / 7 天 / 社区风险 / 老人模式」，研究分析只留在管理员。小程序保持薄客户端：档案、预警、话术；打卡和避暑在网页完成。
2. **内容与代码分离**：每日行动提示已进入 `data/content/daily_action_tips.json`；照护提醒话术已进入 `data/content/caregiver_tip_scripts.json`（小程序同文案副本在 `miniprogram/content/`，改一处必须同步另一处）；避暑页导语、页脚和温度分档已进入 `data/content/cooling_page.json`；指标说明已进入 `data/content/metric_explanations.json`；今日页/长者页标题和空清单已进入 `data/content/dashboard_copy.json`；7 天「本周建议」已进入 `data/content/forecast_week_tips.json`；社区风险页「优先行动」已进入 `data/content/community_action_tips.json`。改 JSON 后需重启 Flask（loader 有 `lru_cache`）。下一步再抽慢病/ML/健康评估规则。`docs/REFACTOR_PLAN.md` 是 2026-01 历史稿，以本节为准。
3. **样式一套**：合并 `style.css` / `yilao.css` / `apple-polish.css`，motion 按需加载，去掉对每页都生效的研究仪表盘装饰。
4. **拆巨型文件**：`analysis.py` 按 heatmap/lag/history/alerts/reports 拆蓝图；`community_risk.html` 拆成地图组件 + 说明组件。
5. **双端同一领域模型**：Web 家庭成员与小程序 elders 共用同一套档案字段与删除/解绑语义（本轮已对齐 PATCH 与删除解绑）。
6. **训练脚本不进主路径**：`services/pipelines/train_*.py` 保持离线工具，不再复制 `parse_age`。

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
│   ├── parsers.py            # parse_age / parse_int 等
│   ├── validators.py         # 输入验证
│   ├── error_handlers.py
│   └── audit_log.py
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
| 清理废弃模块 | ✅ | 已删除无引用预测服务；`health_risk_service` 废弃空方法已移除 |
| API 规范化 | ⏳ | 增加 OpenAPI 草案 + 基础请求校验 |
| 测试补强 | ⏳ | 覆盖关键服务与 API 的边界用例 |
| 产品简洁化 | 📋 | 见上文 1.3：主路径、内容分离、样式一套、拆巨型文件 |

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

---

## 7. 服务器运维指南

### 7.1 服务器信息

| 项目 | 值 |
|------|------|
| 服务器IP | `<managed-in-private-ops>` |
| 操作系统 | Debian 12 |
| 项目路径 | /opt/your-app |
| Python版本 | 3.11.2 |
| 虚拟环境 | /opt/your-app/venv |
| 数据库 | /opt/your-app/instance/health_weather.db |
| 服务名称 | case-weather.service |
| 端口 | 5000 |

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
# 从本地同步代码到服务器（排除数据库和敏感文件）
rsync -avz \
  --exclude "venv" \
  --exclude ".venv" \
  --exclude "__pycache__" \
  --exclude ".git" \
  --exclude ".DS_Store" \
  --exclude ".env" \
  --exclude "instance/health_weather.db" \
  --exclude "storage" \
  /path/to/your-local-repo/ \
  deploy-user@your-server-host:/opt/your-app/

# 重启服务使更新生效
ssh deploy-user@your-server-host "systemctl restart case-weather"
```

**重要**：`.env` 被排除，不会被覆盖。数据库默认位于 `/opt/your-app/instance/health_weather.db`；若本地存在 `instance/` 或 `storage/` 目录也会被排除。

### 7.4 数据库备份策略

#### 备份机制

| 项目 | 详情 |
|------|------|
| 备份位置 | `/opt/your-app/backups/` |
| 备份频率 | 每天凌晨3点自动执行 (Cron) |
| 保留天数 | 30天（自动删除旧备份） |
| 备份格式 | `.db.gz` (SQLite + gzip压缩) |

#### 备份脚本 (`/opt/your-app/scripts/backup.sh`)

```bash
#!/bin/bash
BACKUP_DIR=/opt/your-app/backups
DB_FILE=/opt/your-app/instance/health_weather.db
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE=$BACKUP_DIR/health_weather_$DATE.db

mkdir -p $BACKUP_DIR
sqlite3 $DB_FILE ".backup $BACKUP_FILE"
gzip $BACKUP_FILE
find $BACKUP_DIR -name "*.gz" -mtime +30 -delete
```

#### Cron定时任务

```bash
# 查看定时任务
crontab -l

# 当前配置（每天凌晨3点执行）
0 3 * * * /opt/your-app/scripts/backup.sh >> /opt/your-app/backups/backup.log 2>&1
```

#### 手动备份

```bash
# 在服务器上执行
/opt/your-app/scripts/backup.sh

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

```bash
# 1. 停止服务
systemctl stop case-weather

# 2. 解压备份
cd /opt/your-app/backups
gunzip health_weather_YYYYMMDD_HHMMSS.db.gz

# 3. 备份当前数据库（以防万一）
cp /opt/your-app/instance/health_weather.db /opt/your-app/instance/health_weather.db.old

# 4. 恢复数据库
cp health_weather_YYYYMMDD_HHMMSS.db /opt/your-app/instance/health_weather.db

# 5. 重启服务
systemctl restart case-weather
```

### 7.5 环境变量配置

环境变量存储在 `/opt/your-app/.env` 文件中（权限 600）：

```bash
# Flask配置
FLASK_ENV=production
SECRET_KEY=your_secret_key

# 和风天气API（主来源）
QWEATHER_KEY=YOUR_QWEATHER_KEY
QWEATHER_API_BASE=<在本地或私有 ops 中显式配置的 QWeather Host>

# Open-Meteo（兜底，无需 Key）
# 无需配置，系统会在 QWeather 失败时自动启用

# 高德地图API
AMAP_KEY=your_amap_key

# SiliconFlow AI
SILICONFLOW_API_KEY=your_siliconflow_key

# 本地部署/同步（不上传服务器）
DEPLOY_SERVER=your-server-host
DEPLOY_USER=deploy-user
DEPLOY_PROJECT_DIR=/opt/your-app
DEPLOY_LOCAL_DIR=/path/to/your-local-repo
# 推荐使用 SSH Key；不要把密码式运维流程写进公开仓库
```

`scripts/deploy.sh` / `scripts/sync.sh` 会读取本地 `.env` 中的部署变量。公开仓库只记录 SSH Key 方式；其他运维细节放到私有 ops 文档。

### 7.6 常见问题排查

#### 1. 服务启动失败

```bash
# 查看详细错误
journalctl -u case-weather --no-pager -n 100 | grep -E "(ERROR|Exception)"

# 手动测试应用
cd /opt/your-app
/opt/your-app/venv/bin/python -c "from app import app; print('OK')"
```

#### 2. 数据库列缺失错误

```bash
# 检查表结构
sqlite3 /opt/your-app/instance/health_weather.db "PRAGMA table_info(表名);"

# 添加缺失列
sqlite3 /opt/your-app/instance/health_weather.db "ALTER TABLE 表名 ADD COLUMN 列名 TEXT;"

# 或使用 db.create_all() 同步所有表
cd /opt/your-app
/opt/your-app/venv/bin/python -c "from app import app, db;
with app.app_context(): db.create_all(); print('Done')"
```

#### 3. 天气API返回模拟数据

检查 `.env` 文件中的 `QWEATHER_KEY` 和 `QWEATHER_API_BASE` 是否正确配置；若未配置 QWeather，系统会自动回退 Open-Meteo。

```bash
# 先在当前 shell 中提供真实 Host，再测试 API
export QWEATHER_API_BASE="https://your-real-qweather-host/v7"
curl -s --compressed "${QWEATHER_API_BASE}/weather/now?key=YOUR_KEY&location=116.20,29.27"
```

---
