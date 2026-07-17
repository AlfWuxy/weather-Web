# -*- coding: utf-8 -*-
"""Database models.

时区处理策略：
- 数据库中的时间戳统一使用 UTC（timezone-aware）
- 推荐使用 datetime.now(timezone.utc) 或 core.time_utils.utcnow()
- 显示给用户时，使用 core.time_utils 中的本地时区转换函数
- 避免使用已废弃的 lambda: datetime.now(timezone.utc)()（返回 naive datetime）
"""
import sqlite3
from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.security import generate_password_hash, check_password_hash

from core.extensions import db
from core.time_utils import today_local, utcnow, ensure_utc_aware


@event.listens_for(Engine, 'connect')
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
    """SQLite 每条连接都显式开启外键和级联约束。"""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute('PRAGMA foreign_keys=ON')
    finally:
        cursor.close()


class User(UserMixin, db.Model):
    """用户表"""
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(120), unique=True)
    role = db.Column(db.String(20), default='user')  # admin/user/caregiver/community
    # 使用 timezone-aware UTC 时间戳（推荐做法）
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_login = db.Column(db.DateTime)
    # 注销墓碑用于阻断并发中的旧会话和后续写入。
    deleted_at = db.Column(db.DateTime)

    # 个人健康信息
    age = db.Column(db.Integer)
    gender = db.Column(db.String(10))
    community = db.Column(db.String(100))  # 所属社区
    has_chronic_disease = db.Column(db.Boolean, default=False)
    chronic_diseases = db.Column(db.Text)  # JSON格式存储多个慢性病

    # 试点推送设置（子女端）
    wxpusher_uid = db.Column(db.String(80))
    push_enabled = db.Column(db.Boolean, default=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class MedicalRecord(db.Model):
    """病历记录表"""
    __tablename__ = 'medical_records'
    id = db.Column(db.Integer, primary_key=True)
    patient_name = db.Column(db.String(100))
    gender = db.Column(db.String(10))
    age = db.Column(db.Integer)
    visit_time = db.Column(db.DateTime)
    department = db.Column(db.String(50))
    doctor = db.Column(db.String(100))
    disease_category = db.Column(db.String(100))
    diagnosis = db.Column(db.String(200))
    chief_complaint = db.Column(db.Text)
    medical_history = db.Column(db.Text)
    insurance_type = db.Column(db.String(50))
    temperature = db.Column(db.Float)  # 体温
    heart_rate = db.Column(db.Float)   # 心率
    blood_pressure = db.Column(db.String(20))  # 血压
    community = db.Column(db.String(100))  # 所属社区


class WeatherData(db.Model):
    """天气数据表"""
    __tablename__ = 'weather_data'
    __table_args__ = (
        db.UniqueConstraint('date', 'location', name='uq_weather_data_date_location'),
    )
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    location = db.Column(db.String(100), nullable=False)
    temperature = db.Column(db.Float)  # 温度
    temperature_max = db.Column(db.Float)  # 最高温
    temperature_min = db.Column(db.Float)  # 最低温
    humidity = db.Column(db.Float)  # 湿度
    pressure = db.Column(db.Float)  # 气压
    weather_condition = db.Column(db.String(50))  # 天气状况
    wind_speed = db.Column(db.Float)  # 风速
    pm25 = db.Column(db.Float)  # PM2.5
    aqi = db.Column(db.Integer)  # 空气质量指数
    is_extreme = db.Column(db.Boolean, default=False)  # 是否极端天气
    extreme_type = db.Column(db.String(50))  # 极端天气类型


class WeatherCache(db.Model):
    """天气缓存（分钟级）"""
    __tablename__ = 'weather_cache'
    __table_args__ = (
        db.UniqueConstraint('location', name='uq_weather_cache_location'),
    )
    id = db.Column(db.Integer, primary_key=True)
    location = db.Column(db.String(100), nullable=False)
    fetched_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    payload = db.Column(db.Text)
    is_mock = db.Column(db.Boolean, default=False)


class ForecastCache(db.Model):
    """天气预报缓存"""
    __tablename__ = 'forecast_cache'
    id = db.Column(db.Integer, primary_key=True)
    location = db.Column(db.String(100), nullable=False)
    days = db.Column(db.Integer, default=7)
    fetched_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    payload = db.Column(db.Text)
    is_mock = db.Column(db.Boolean, default=False)


class Community(db.Model):
    """社区信息表"""
    __tablename__ = 'communities'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    location = db.Column(db.String(200))  # 地理位置
    latitude = db.Column(db.Float)  # 纬度
    longitude = db.Column(db.Float)  # 经度
    population = db.Column(db.Integer)  # 人口数量
    elderly_ratio = db.Column(db.Float)  # 老年人比例
    chronic_disease_ratio = db.Column(db.Float)  # 慢性病患者比例
    vulnerability_index = db.Column(db.Float)  # 脆弱性指数
    risk_level = db.Column(db.String(20))  # 风险等级: 低/中/高


class HealthRiskAssessment(db.Model):
    """健康风险评估记录"""
    __tablename__ = 'health_risk_assessments'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    # 小程序端可为本人或已绑定老人提交评估，始终由 user_id 做 owner scope。
    member_id = db.Column(db.Integer, db.ForeignKey('family_members.id'))
    assessment_date = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    weather_condition = db.Column(db.String(100))
    risk_score = db.Column(db.Float)  # 风险评分
    risk_level = db.Column(db.String(20))  # 风险等级
    disease_risks = db.Column(db.Text)  # JSON格式：各类疾病风险
    recommendations = db.Column(db.Text)  # 健康建议
    explain = db.Column(db.Text)  # JSON格式：可解释输出


class WeatherAlert(db.Model):
    """天气预警记录"""
    __tablename__ = 'weather_alerts'
    id = db.Column(db.Integer, primary_key=True)
    alert_date = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    location = db.Column(db.String(100))
    alert_type = db.Column(db.String(50))  # 预警类型
    alert_level = db.Column(db.String(20))  # 预警等级
    description = db.Column(db.Text)
    affected_communities = db.Column(db.Text)  # JSON格式：受影响社区
    disease_correlation = db.Column(db.Text)  # JSON格式：疾病相关性分析


class FamilyMember(db.Model):
    """家庭成员"""
    __tablename__ = 'family_members'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    relation = db.Column(db.String(20))  # 关系：父母/配偶/子女等
    age = db.Column(db.Integer)
    gender = db.Column(db.String(10))
    chronic_diseases = db.Column(db.Text)  # JSON
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class FamilyMemberProfile(db.Model):
    """家庭成员扩展画像"""
    __tablename__ = 'family_member_profiles'
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('family_members.id'), unique=True, nullable=False)
    allergies = db.Column(db.Text)
    medications = db.Column(db.Text)
    metrics = db.Column(db.Text)  # JSON
    risk_tags = db.Column(db.Text)  # JSON
    weather_thresholds = db.Column(db.Text)  # JSON
    contact_prefs = db.Column(db.Text)  # JSON
    privacy_level = db.Column(db.String(20), default='family')
    share_with_doctor = db.Column(db.Boolean, default=False)
    share_with_community = db.Column(db.Boolean, default=False)
    alert_enabled = db.Column(db.Boolean, default=True)
    quiet_hours = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class HealthDiary(db.Model):
    """健康日记"""
    __tablename__ = 'health_diary'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('family_members.id'))
    entry_date = db.Column(db.Date, default=today_local)
    symptoms = db.Column(db.Text)
    severity = db.Column(db.String(20))  # 轻微/中等/严重
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class MedicationReminder(db.Model):
    """用药提醒"""
    __tablename__ = 'medication_reminders'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('family_members.id'))
    medicine_name = db.Column(db.String(100), nullable=False)
    dosage = db.Column(db.String(100))
    frequency = db.Column(db.String(20), default='daily')  # daily/weekly
    time_of_day = db.Column(db.String(10))  # HH:MM
    weather_triggers = db.Column(db.Text)  # JSON
    is_active = db.Column(db.Boolean, default=True)
    last_notified_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class Notification(db.Model):
    """站内通知"""
    __tablename__ = 'notifications'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('family_members.id'))
    category = db.Column(db.String(50), default='general')
    title = db.Column(db.String(120))
    message = db.Column(db.Text)
    level = db.Column(db.String(20), default='info')
    action_url = db.Column(db.String(200))
    meta = db.Column(db.Text)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class AuditLog(db.Model):
    """审计日志"""
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer)
    actor_role = db.Column(db.String(20))
    action = db.Column(db.String(80), nullable=False)
    resource_type = db.Column(db.String(80))
    resource_id = db.Column(db.String(80))
    extra_data = db.Column(db.Text)  # renamed from 'metadata' which is reserved
    ip_address = db.Column(db.String(64))
    user_agent = db.Column(db.String(200))
    request_id = db.Column(db.String(40))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class PairLink(db.Model):
    """绑定短码（临时）"""
    __tablename__ = 'pair_links'
    id = db.Column(db.Integer, primary_key=True)
    caregiver_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    short_code = db.Column(db.String(12), unique=True, nullable=False)
    short_code_hash = db.Column(db.String(64))
    token_hash = db.Column(db.String(128), nullable=False)
    community_code = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='active')  # active/redeemed/expired
    expires_at = db.Column(db.DateTime)
    redeemed_at = db.Column(db.DateTime)
    pair_id = db.Column(db.Integer, db.ForeignKey('pairs.id'))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.Index('ix_pair_links_caregiver_id', 'caregiver_id'),
        db.Index('ix_pair_links_expires_at', 'expires_at'),
        db.Index('ix_pair_links_short_code_hash', 'short_code_hash'),
    )

    @property
    def is_expired(self):
        if self.status == 'expired':
            return True
        if self.expires_at:
            # 确保从数据库读取的 datetime 是 UTC aware 的
            return ensure_utc_aware(self.expires_at) < utcnow()
        return False

    @property
    def is_active(self):
        return self.status == 'active' and not self.is_expired


class ShortCodeAttempt(db.Model):
    """短码失败计数（防枚举）"""
    __tablename__ = 'short_code_attempts'
    id = db.Column(db.Integer, primary_key=True)
    key_hash = db.Column(db.String(64), nullable=False)
    failed_count = db.Column(db.Integer, default=0)
    first_failed_at = db.Column(db.DateTime)
    last_failed_at = db.Column(db.DateTime)
    locked_until = db.Column(db.DateTime)

    __table_args__ = (
        db.Index('ix_short_code_attempts_key_hash', 'key_hash'),
    )


class Pair(db.Model):
    """照护关系（不含个人敏感信息）"""
    __tablename__ = 'pairs'
    id = db.Column(db.Integer, primary_key=True)
    caregiver_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    community_code = db.Column(db.String(100), nullable=False)
    # 关联到家庭成员（老人档案，可选）
    member_id = db.Column(db.Integer, db.ForeignKey('family_members.id'))
    # 原始自由输入的地点（如“九江某乡镇”），用于地理编码与多地区支持
    location_query = db.Column(db.String(200))
    elder_code = db.Column(db.String(40), unique=True, nullable=False)
    short_code = db.Column(db.String(12), unique=True, nullable=False)
    short_code_hash = db.Column(db.String(64))
    short_code_expires_at = db.Column(db.DateTime)
    status = db.Column(db.String(20), default='active')  # active/inactive
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_active_at = db.Column(db.DateTime)

    __table_args__ = (
        db.Index('ix_pairs_caregiver_id', 'caregiver_id'),
        db.Index('ix_pairs_community_code', 'community_code'),
        db.Index('ix_pairs_short_code_hash', 'short_code_hash'),
        db.Index('ix_pairs_member_id', 'member_id'),
    )

    @property
    def is_expired(self):
        return False

    @property
    def is_active(self):
        return self.status == 'active'


class PairActionToken(db.Model):
    """行动链接令牌（只保存哈希）"""
    __tablename__ = 'pair_action_tokens'
    id = db.Column(db.Integer, primary_key=True)
    pair_id = db.Column(db.Integer, db.ForeignKey('pairs.id'), nullable=False)
    token_hash = db.Column(db.String(128), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    used_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)

    __table_args__ = (
        db.Index('ix_pair_action_tokens_pair_id', 'pair_id'),
        db.Index('ix_pair_action_tokens_token_hash', 'token_hash'),
        db.Index('ix_pair_action_tokens_expires_at', 'expires_at'),
    )

    @property
    def is_active(self):
        if self.revoked_at:
            return False
        return ensure_utc_aware(self.expires_at) >= utcnow()


class DailyStatus(db.Model):
    """日度行动状态"""
    __tablename__ = 'daily_status'
    id = db.Column(db.Integer, primary_key=True)
    pair_id = db.Column(db.Integer, db.ForeignKey('pairs.id'), nullable=False)
    status_date = db.Column(db.Date, nullable=False)
    community_code = db.Column(db.String(100), nullable=False)
    risk_level = db.Column(db.String(20))  # 低风险/中风险/高风险/极高
    confirmed_at = db.Column(db.DateTime)
    help_flag = db.Column(db.Boolean, default=False)
    actions_done_count = db.Column(db.Integer, default=0)
    relay_stage = db.Column(db.String(20), default='none')
    debrief_optin = db.Column(db.Boolean, default=False)
    caregiver_actions = db.Column(db.Text)  # JSON
    caregiver_note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('pair_id', 'status_date', name='uq_daily_status_pair_date'),
        db.Index('ix_daily_status_pair_date', 'pair_id', 'status_date'),
        db.Index('ix_daily_status_community_date', 'community_code', 'status_date'),
    )


class CommunityDaily(db.Model):
    """社区日度聚合"""
    __tablename__ = 'community_daily'
    id = db.Column(db.Integer, primary_key=True)
    community_code = db.Column(db.String(100), nullable=False)
    date = db.Column(db.Date, nullable=False)
    total_people = db.Column(db.Integer, default=0)
    confirm_rate = db.Column(db.Float, default=0)
    escalation_rate = db.Column(db.Float, default=0)
    risk_distribution = db.Column(db.Text)
    outreach_summary = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('community_code', 'date', name='uq_community_daily_code_date'),
        db.Index('ix_community_daily_code_date', 'community_code', 'date'),
    )


class CoolingResource(db.Model):
    """避暑点资源"""
    __tablename__ = 'cooling_resources'
    id = db.Column(db.Integer, primary_key=True)
    community_code = db.Column(db.String(100), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    resource_type = db.Column(db.String(50))
    address_hint = db.Column(db.String(200))
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    open_hours = db.Column(db.String(100))
    has_ac = db.Column(db.Boolean, default=False)
    is_accessible = db.Column(db.Boolean, default=False)
    contact_hint = db.Column(db.String(100))
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.Index('ix_cooling_resources_community', 'community_code'),
    )


class Debrief(db.Model):
    """行动复盘"""
    __tablename__ = 'debriefs'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    community_code = db.Column(db.String(100), nullable=False)
    pair_id = db.Column(db.Integer, db.ForeignKey('pairs.id'))
    question_1 = db.Column(db.String(200))
    question_2 = db.Column(db.String(200))
    question_3 = db.Column(db.String(200))
    difficulty = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.Index('ix_debriefs_community_date', 'community_code', 'date'),
        db.Index('ix_debriefs_pair_date', 'pair_id', 'date'),
    )


class ApiToken(db.Model):
    """API Token（用于小程序绑定；仅存哈希，明文仅展示一次）"""
    __tablename__ = 'api_tokens'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(80))
    token_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = db.Column(db.DateTime)
    scopes = db.Column(db.String(200))
    privacy_consent_version = db.Column(db.String(64))
    last_used_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)

    __table_args__ = (
        db.Index('ix_api_tokens_user_id', 'user_id'),
        db.Index('ix_api_tokens_token_hash', 'token_hash'),
        db.Index('ix_api_tokens_expires_at', 'expires_at'),
    )


class UsageEvent(db.Model):
    """试点埋点事件（用于打开率/触发/反馈等指标）"""
    __tablename__ = 'usage_events'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    pair_id = db.Column(db.Integer, db.ForeignKey('pairs.id', ondelete='SET NULL'))
    member_id = db.Column(
        db.Integer,
        db.ForeignKey('family_members.id', ondelete='SET NULL'),
    )
    event_type = db.Column(db.String(50), nullable=False)
    meta_json = db.Column(db.Text)
    source = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.Index('ix_usage_events_user_id', 'user_id'),
        db.Index('ix_usage_events_event_type', 'event_type'),
        db.Index('ix_usage_events_created_at', 'created_at'),
    )


class AlertDelivery(db.Model):
    """预警投递记录（推送发送/点击追踪）"""
    __tablename__ = 'alert_deliveries'
    id = db.Column(db.Integer, primary_key=True)
    alert_id = db.Column(db.Integer, db.ForeignKey('weather_alerts.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    pair_id = db.Column(db.Integer, db.ForeignKey('pairs.id'))
    channel = db.Column(db.String(20))
    status = db.Column(db.String(20))  # sent/failed
    error = db.Column(db.Text)
    delivery_token = db.Column(db.String(64), unique=True, nullable=False)
    sent_at = db.Column(db.DateTime)
    clicked_at = db.Column(db.DateTime)

    __table_args__ = (
        db.Index('ix_alert_deliveries_alert_user', 'alert_id', 'user_id'),
        db.Index('ix_alert_deliveries_delivery_token', 'delivery_token'),
    )


class LocationCache(db.Model):
    """地点解析缓存（输入->location_code，经纬度/城市ID）"""
    __tablename__ = 'location_cache'
    id = db.Column(db.Integer, primary_key=True)
    # NOTE: cannot name the Python attribute `query` (conflicts with Flask-SQLAlchemy Model.query).
    query_text = db.Column('query', db.String(200), nullable=False)
    location_code = db.Column(db.String(100), nullable=False)
    provider = db.Column(db.String(20))
    raw_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.Index('ix_location_cache_query', query_text),
        db.Index('ix_location_cache_updated_at', 'updated_at'),
    )


class MiniProgramSnapshot(db.Model):
    """小程序只读天气快照，由 30 分钟后台任务统一写入。"""
    __tablename__ = 'miniprogram_snapshots'
    id = db.Column(db.Integer, primary_key=True)
    snapshot_id = db.Column(db.String(36), nullable=False, unique=True)
    location_name = db.Column(db.String(100), nullable=False, default='都昌县')
    location_code = db.Column(db.String(100), nullable=False, default='116.20,29.27')
    fetched_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    available = db.Column(db.Boolean, nullable=False, default=False)
    current_json = db.Column(db.Text)
    forecast_json = db.Column(db.Text)
    warnings_json = db.Column(db.Text)
    risk_json = db.Column(db.Text)
    actions_json = db.Column(db.Text)
    source_status_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.Index('ix_miniprogram_snapshots_fetched_at', 'fetched_at'),
        db.Index('ix_miniprogram_snapshots_expires_at', 'expires_at'),
    )


class MiniProgramIdentity(db.Model):
    """微信身份映射，仅保存带独立 pepper 的 OpenID 哈希。"""
    __tablename__ = 'miniprogram_identities'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
    )
    openid_hash = db.Column(db.String(64), nullable=False, unique=True)
    privacy_consent_version = db.Column(db.String(64), nullable=False)
    privacy_consented_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint(
            'id',
            'user_id',
            name='uq_miniprogram_identities_id_user_id',
        ),
        db.Index('ix_miniprogram_identities_user_id', 'user_id'),
        db.Index('ix_miniprogram_identities_openid_hash', 'openid_hash'),
    )


class MiniProgramSession(db.Model):
    """可撤销、可过期的小程序签名会话，仅保存 token 哈希。"""
    __tablename__ = 'miniprogram_sessions'
    id = db.Column(db.Integer, primary_key=True)
    identity_id = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    privacy_consent_version = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ['identity_id', 'user_id'],
            ['miniprogram_identities.id', 'miniprogram_identities.user_id'],
            name='fk_miniprogram_sessions_identity_owner',
            ondelete='CASCADE',
        ),
        db.Index('ix_miniprogram_sessions_identity_id', 'identity_id'),
        db.Index('ix_miniprogram_sessions_user_id', 'user_id'),
        db.Index('ix_miniprogram_sessions_token_hash', 'token_hash'),
        db.Index('ix_miniprogram_sessions_expires_at', 'expires_at'),
    )
