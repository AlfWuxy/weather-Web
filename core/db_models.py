# -*- coding: utf-8 -*-
"""Database models.

时区处理策略：
- 数据库中的时间戳统一使用 UTC（timezone-aware）
- 推荐使用 datetime.now(timezone.utc) 或 core.time_utils.utcnow()
- 显示给用户时，使用 core.time_utils 中的本地时区转换函数
- 避免使用已废弃的 lambda: datetime.now(timezone.utc)()（返回 naive datetime）
"""
from datetime import datetime, timezone
from flask_login import UserMixin
from sqlalchemy import Index, text
from werkzeug.security import generate_password_hash, check_password_hash

from core.extensions import db
from core.time_utils import today_local, utcnow, ensure_utc_aware


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

    # 个人健康信息
    age = db.Column(db.Integer)
    gender = db.Column(db.String(10))
    community = db.Column(db.String(100))  # 所属社区 / 定位（可自改）
    # 运营 ACL：社区角色管辖村，仅 admin 可写；缺失时 fail closed，由迁移或管理员补齐
    authorized_community = db.Column(db.String(100))
    has_chronic_disease = db.Column(db.Boolean, default=False)
    chronic_diseases = db.Column(db.Text)  # JSON格式存储多个慢性病

    # 试点推送设置（子女端）
    wxpusher_uid = db.Column(db.String(80))
    push_enabled = db.Column(db.Boolean, default=False)
    # 账号删除：置位后立即收回家庭授权与小程序会话
    deleted_at = db.Column(db.DateTime)
    # 健康敏感信息单独同意，不能由一般隐私同意替代。
    health_sensitive_consent_version = db.Column(db.String(64))
    health_sensitive_consented_at = db.Column(db.DateTime)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_id(self):
        """会话身份含密码戳：改密后旧 session/remember 自动失效。

        格式：{user_id}:{password_hash 的短摘要}。无需额外 DB 列。
        """
        import hashlib
        stamp = hashlib.sha256((self.password_hash or '').encode('utf-8')).hexdigest()[:16]
        return f'{self.id}:{stamp}'


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
    data_source = db.Column(db.String(32), nullable=True)  # 旧行保持 NULL，不伪造来源
    observed_at = db.Column(db.DateTime(timezone=True), nullable=True)  # 上游观测时刻（UTC）
    air_observed_at = db.Column(db.DateTime(timezone=True), nullable=True)  # 空气质量独立观测时刻（UTC）
    quality_version = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    air_quality_available = db.Column(db.Boolean, nullable=False, default=False, server_default='0')
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
    member_id = db.Column(db.Integer, db.ForeignKey('family_members.id'))
    assessment_date = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    weather_condition = db.Column(db.String(100))
    risk_score = db.Column(db.Float)  # 风险评分
    risk_level = db.Column(db.String(20))  # 风险等级
    disease_risks = db.Column(db.Text)  # JSON格式：各类疾病风险
    recommendations = db.Column(db.Text)  # 健康建议
    explain = db.Column(db.Text)  # JSON格式：可解释输出


class WeatherAlert(db.Model):
    """天气提醒记录；只有带来源与有效期的记录才能标为官方预警。"""
    __tablename__ = 'weather_alerts'
    id = db.Column(db.Integer, primary_key=True)
    alert_date = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    location = db.Column(db.String(100))
    alert_type = db.Column(db.String(50))  # 预警类型
    alert_level = db.Column(db.String(20))  # 预警等级
    description = db.Column(db.Text)
    source = db.Column(db.String(50), nullable=True)  # QWeather / AppThreshold / Legacy
    is_official = db.Column(db.Boolean, nullable=False, default=False, server_default='0')
    starts_at = db.Column(db.DateTime(timezone=True), nullable=True)
    ends_at = db.Column(db.DateTime(timezone=True), nullable=True)
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
    family_space_id = db.Column(db.Integer, db.ForeignKey('family_spaces.id'))
    # 原始自由输入的地点（如“九江某乡镇”），用于地理编码与多地区支持
    location_query = db.Column(db.String(200))
    elder_code = db.Column(db.String(40), unique=True, nullable=False)
    short_code = db.Column(db.String(12), unique=True, nullable=False)
    short_code_hash = db.Column(db.String(64))
    short_code_expires_at = db.Column(db.DateTime)
    status = db.Column(db.String(20), default='active')  # active/inactive
    is_test = db.Column(db.Boolean, default=False, nullable=False, server_default='0')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_active_at = db.Column(db.DateTime)

    __table_args__ = (
        db.Index('ix_pairs_caregiver_id', 'caregiver_id'),
        db.Index('ix_pairs_community_code', 'community_code'),
        db.Index('ix_pairs_short_code_hash', 'short_code_hash'),
        db.Index('ix_pairs_member_id', 'member_id'),
        db.Index('ix_pairs_family_space_id', 'family_space_id'),
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
    confirmed_at = db.Column(db.DateTime)  # 语义 = self_reported_at，仅 self_reported 时写入
    understood_at = db.Column(db.DateTime)
    verified_at = db.Column(db.DateTime)
    help_acknowledged_at = db.Column(db.DateTime)
    closed_at = db.Column(db.DateTime)
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
    confirm_rate = db.Column(db.Float, default=0)  # deprecated = self_report_rate
    understood_rate = db.Column(db.Float, default=0)
    self_report_rate = db.Column(db.Float, default=0)
    verified_rate = db.Column(db.Float, default=0)
    open_help_count = db.Column(db.Integer, default=0)
    unknown_count = db.Column(db.Integer, default=0)
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
    last_verified_at = db.Column(db.DateTime)
    verified_by_role = db.Column(db.String(16))
    verify_method = db.Column(db.String(16))
    open_during_alert = db.Column(db.String(16))
    alert_open_note_code = db.Column(db.String(32))
    amenities_json = db.Column(db.Text)
    transport_need = db.Column(db.String(16))
    verify_status = db.Column(db.String(16))

    __table_args__ = (
        db.Index('ix_cooling_resources_community', 'community_code'),
    )


class CoolingFeedback(db.Model):
    """避暑资源反馈（append-only，只存封闭码，不存自由文本）。"""
    __tablename__ = 'cooling_feedback'
    id = db.Column(db.Integer, primary_key=True)
    resource_id = db.Column(db.Integer, db.ForeignKey('cooling_resources.id'), nullable=False)
    pair_id = db.Column(db.Integer, db.ForeignKey('pairs.id'))
    code = db.Column(db.String(16), nullable=False)
    channel = db.Column(db.String(24))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.Index('ix_cooling_feedback_resource_id', 'resource_id'),
        db.Index('ix_cooling_feedback_pair_id', 'pair_id'),
        db.Index('ix_cooling_feedback_code', 'code'),
        db.Index('ix_cooling_feedback_created_at', 'created_at'),
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
    last_used_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)

    __table_args__ = (
        db.Index('ix_api_tokens_user_id', 'user_id'),
        db.Index('ix_api_tokens_token_hash', 'token_hash'),
    )


class ActionEvent(db.Model):
    """老人当日行动链（append-only，无 update/delete 路由）。"""
    __tablename__ = 'action_events'
    id = db.Column(db.Integer, primary_key=True)
    pair_id = db.Column(db.Integer, db.ForeignKey('pairs.id'), nullable=False, index=True)
    local_date = db.Column(db.Date, nullable=False, index=True)
    stage = db.Column(db.String(32), nullable=False, index=True)
    actor_role = db.Column(db.String(16), nullable=False)
    channel = db.Column(db.String(24), nullable=False)
    script_version = db.Column(db.String(16))
    action_id = db.Column(db.String(32))
    alert_id = db.Column(db.Integer, db.ForeignKey('weather_alerts.id'))
    delivery_id = db.Column(db.Integer, db.ForeignKey('alert_deliveries.id'))
    meta_json = db.Column(db.Text)
    help_request_id = db.Column(db.Integer, db.ForeignKey('help_requests.id'))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        db.Index('ix_action_events_pair_date_stage', 'pair_id', 'local_date', 'stage'),
        db.Index('ix_action_events_help_request_id', 'help_request_id'),
    )


class FamilySpace(db.Model):
    """家庭空间：网页与小程序共享的授权边界。"""
    __tablename__ = 'family_spaces'
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(32), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    is_test = db.Column(db.Boolean, default=False, nullable=False, server_default='0')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.Index('ix_family_spaces_created_by', 'created_by_user_id'),
    )


class FamilyMembership(db.Model):
    """家庭成员授权。角色由服务端邀请决定，客户端不可伪造。"""
    __tablename__ = 'family_memberships'
    id = db.Column(db.Integer, primary_key=True)
    family_space_id = db.Column(db.Integer, db.ForeignKey('family_spaces.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    role = db.Column(db.String(32), nullable=False)
    status = db.Column(db.String(16), nullable=False, default='active')
    invited_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    revoked_at = db.Column(db.DateTime)

    __table_args__ = (
        db.Index('ix_family_memberships_user_id', 'user_id'),
        db.Index('ix_family_memberships_space_id', 'family_space_id'),
        Index(
            'uq_family_memberships_active_user',
            'family_space_id',
            'user_id',
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )


class FamilyInvite(db.Model):
    """家庭邀请。只存哈希；GET 预览不消费。"""
    __tablename__ = 'family_invites'
    id = db.Column(db.Integer, primary_key=True)
    family_space_id = db.Column(db.Integer, db.ForeignKey('family_spaces.id'), nullable=False)
    code_hash = db.Column(db.String(64), unique=True, nullable=False)
    role = db.Column(db.String(32), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    max_uses = db.Column(db.Integer, nullable=False, default=1)
    use_count = db.Column(db.Integer, nullable=False, default=0)
    revoked_at = db.Column(db.DateTime)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_consumed_at = db.Column(db.DateTime)

    __table_args__ = (
        db.Index('ix_family_invites_space_id', 'family_space_id'),
        db.Index('ix_family_invites_expires_at', 'expires_at'),
    )


class HelpRequest(db.Model):
    """跨天持续存在的求助工单；同一对象最多一条未结。"""
    __tablename__ = 'help_requests'
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(32), unique=True, nullable=False)
    family_space_id = db.Column(db.Integer, db.ForeignKey('family_spaces.id'), nullable=False)
    pair_id = db.Column(db.Integer, db.ForeignKey('pairs.id'), nullable=False)
    status = db.Column(db.String(24), nullable=False)
    origin_channel = db.Column(db.String(24), nullable=False)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    actor_role = db.Column(db.String(24), nullable=False)
    is_proxy = db.Column(db.Boolean, nullable=False, default=False)
    category = db.Column(db.String(32), nullable=False, default='cannot_complete')
    version = db.Column(db.Integer, nullable=False, default=1)
    acknowledged_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    acknowledged_at = db.Column(db.DateTime)
    started_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    started_at = db.Column(db.DateTime)
    resolved_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    resolved_at = db.Column(db.DateTime)
    resolution_code = db.Column(db.String(32))
    cancelled_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    cancelled_at = db.Column(db.DateTime)
    cancel_reason_code = db.Column(db.String(32))
    is_test = db.Column(db.Boolean, nullable=False, default=False, server_default='0')
    legacy_source = db.Column(db.String(32))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.Index('ix_help_requests_pair_id', 'pair_id'),
        db.Index('ix_help_requests_space_status', 'family_space_id', 'status'),
        db.Index('ix_help_requests_updated_at', 'updated_at'),
        Index(
            'uq_help_requests_open_pair',
            'pair_id',
            unique=True,
            sqlite_where=text(
                "status IN ('pending_ack', 'acknowledged', 'in_progress')"
            ),
            postgresql_where=text(
                "status IN ('pending_ack', 'acknowledged', 'in_progress')"
            ),
        ),
    )


class HelpRequestEvent(db.Model):
    """求助生命周期事件，与行动证据 ActionEvent 分离。"""
    __tablename__ = 'help_request_events'
    id = db.Column(db.Integer, primary_key=True)
    help_request_id = db.Column(db.Integer, db.ForeignKey('help_requests.id'), nullable=False)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    actor_role = db.Column(db.String(24), nullable=False)
    from_status = db.Column(db.String(24))
    to_status = db.Column(db.String(24), nullable=False)
    event_type = db.Column(db.String(32), nullable=False)
    channel = db.Column(db.String(24), nullable=False)
    meta_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        db.Index('ix_help_request_events_request_id', 'help_request_id'),
    )


class NotificationOutbox(db.Model):
    """与求助同事务写入的待投递通知；不含凭据。"""
    __tablename__ = 'notification_outbox'
    id = db.Column(db.Integer, primary_key=True)
    help_request_id = db.Column(db.Integer, db.ForeignKey('help_requests.id'))
    help_event_id = db.Column(db.Integer, db.ForeignKey('help_request_events.id'))
    recipient_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    channel = db.Column(db.String(24), nullable=False)
    event_type = db.Column(db.String(32), nullable=False)
    dedupe_key = db.Column(db.String(160), unique=True, nullable=False)
    status = db.Column(db.String(16), nullable=False, default='pending')
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    next_attempt_at = db.Column(db.DateTime)
    last_error_type = db.Column(db.String(64))
    provider_accepted_at = db.Column(db.DateTime)
    opened_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.Index('ix_notification_outbox_status_next', 'status', 'next_attempt_at'),
        db.Index('ix_notification_outbox_recipient', 'recipient_user_id'),
    )


class ApiIdempotencyKey(db.Model):
    """写操作幂等键；同 key 不同载荷拒绝。"""
    __tablename__ = 'api_idempotency_keys'
    id = db.Column(db.Integer, primary_key=True)
    scope = db.Column(db.String(80), nullable=False)
    key = db.Column(db.String(80), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    resource_type = db.Column(db.String(32), nullable=False)
    resource_public_id = db.Column(db.String(32))
    response_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('scope', 'key', name='uq_api_idempotency_scope_key'),
    )


class MiniProgramIdentity(db.Model):
    """微信身份映射，仅保存带独立 pepper 的 OpenID 哈希。"""
    __tablename__ = 'miniprogram_identities'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    openid_hash = db.Column(db.String(64), nullable=False, unique=True)
    privacy_consent_version = db.Column(db.String(64), nullable=False)
    privacy_consented_at = db.Column(db.DateTime, nullable=False)
    acquisition_source = db.Column(db.String(20), nullable=False, default='direct')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint('id', 'user_id', name='uq_miniprogram_identities_id_user_id'),
        db.Index('ix_miniprogram_identities_user_id', 'user_id'),
    )


class MiniProgramSession(db.Model):
    """可撤销、可过期的小程序会话，仅保存 token 哈希。"""
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
        ),
        db.Index('ix_miniprogram_sessions_user_id', 'user_id'),
        db.Index('ix_miniprogram_sessions_expires_at', 'expires_at'),
    )


class UsageEvent(db.Model):
    """试点埋点事件（用于打开率/触发/反馈等指标）"""
    __tablename__ = 'usage_events'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    pair_id = db.Column(db.Integer, db.ForeignKey('pairs.id'))
    member_id = db.Column(db.Integer, db.ForeignKey('family_members.id'))
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
