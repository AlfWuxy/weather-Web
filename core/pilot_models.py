"""机构试点独立数据表；不改变线上既有病历与风险模型。"""
from uuid import uuid4
from core.extensions import db
from core.time_utils import utcnow


def new_id():
    return str(uuid4())


class PilotInstitution(db.Model):
    __tablename__ = 'pilot_institutions'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    name = db.Column(db.String(160), nullable=False)
    region_code = db.Column(db.String(32), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    raw_storage_approved = db.Column(db.Boolean, nullable=False, default=False)
    retention_days = db.Column(db.Integer, nullable=False, default=365)
    field_mapping = db.Column(db.JSON, nullable=False, default=dict)
    current_model_id = db.Column(db.String(36))


class PilotMembership(db.Model):
    __tablename__ = 'pilot_memberships'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    institution_id = db.Column(db.String(36), db.ForeignKey('pilot_institutions.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='uploader')
    active = db.Column(db.Boolean, nullable=False, default=True)
    __table_args__ = (db.UniqueConstraint('institution_id', 'user_id', name='uq_pilot_member'),)


class PilotImportBatch(db.Model):
    __tablename__ = 'pilot_import_batches'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    institution_id = db.Column(db.String(36), db.ForeignKey('pilot_institutions.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    idempotency_key = db.Column(db.String(64), nullable=False)
    filename = db.Column(db.String(200), nullable=False)
    storage_path = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(24), nullable=False, default='queued')
    revision_of = db.Column(db.String(36), db.ForeignKey('pilot_import_batches.id'))
    coverage_start = db.Column(db.Date, nullable=False)
    coverage_end = db.Column(db.Date, nullable=False)
    coverage_mode = db.Column(db.String(20), nullable=False)
    closed_dates = db.Column(db.JSON, nullable=False, default=list)
    mapping = db.Column(db.JSON, nullable=False, default=dict)
    report = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    confirmed_at = db.Column(db.DateTime(timezone=True))
    __table_args__ = (db.UniqueConstraint('institution_id', 'idempotency_key', name='uq_pilot_import_idempotency'),)


class PilotEncounter(db.Model):
    __tablename__ = 'pilot_encounters'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    institution_id = db.Column(db.String(36), db.ForeignKey('pilot_institutions.id'), nullable=False, index=True)
    batch_id = db.Column(db.String(36), db.ForeignKey('pilot_import_batches.id'), nullable=False)
    encounter_date = db.Column(db.Date, nullable=False, index=True)
    age = db.Column(db.Integer, nullable=False)
    source_key = db.Column(db.String(64), index=True)
    fingerprint = db.Column(db.String(64), nullable=False, index=True)
    suspected_key = db.Column(db.String(64), index=True)
    diagnosis_category = db.Column(db.String(32), nullable=False)
    residence_region_code = db.Column(db.String(12))
    active = db.Column(db.Boolean, nullable=False, default=True)
    supersedes_id = db.Column(db.String(36), db.ForeignKey('pilot_encounters.id'))
    __table_args__ = (db.Index('uq_pilot_active_source', 'institution_id', 'source_key', unique=True,
                              sqlite_where=db.text('active = 1 AND source_key IS NOT NULL'),
                              postgresql_where=db.text('active AND source_key IS NOT NULL')),)


class PilotCoverage(db.Model):
    __tablename__ = 'pilot_coverage'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    institution_id = db.Column(db.String(36), db.ForeignKey('pilot_institutions.id'), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False)
    batch_id = db.Column(db.String(36), db.ForeignKey('pilot_import_batches.id'), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    __table_args__ = (db.UniqueConstraint('institution_id', 'date', name='uq_pilot_coverage_day'),)


class PilotWeatherDay(db.Model):
    __tablename__ = 'pilot_weather_days'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    institution_id = db.Column(db.String(36), db.ForeignKey('pilot_institutions.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    tmean = db.Column(db.Float)
    rh_mean = db.Column(db.Float)
    precipitation = db.Column(db.Float)
    source = db.Column(db.String(80), nullable=False)
    product = db.Column(db.String(80), nullable=False)
    fetched_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (db.UniqueConstraint('institution_id', 'date', 'product', name='uq_pilot_weather_day'),)


class PilotDatasetSnapshot(db.Model):
    __tablename__ = 'pilot_dataset_snapshots'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    institution_id = db.Column(db.String(36), db.ForeignKey('pilot_institutions.id'), nullable=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    status = db.Column(db.String(24), nullable=False, default='frozen')
    manifest = db.Column(db.JSON, nullable=False, default=dict)
    sha256 = db.Column(db.String(64))
    storage_path = db.Column(db.String(200))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class PilotModelVersion(db.Model):
    __tablename__ = 'pilot_model_versions'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    institution_id = db.Column(db.String(36), db.ForeignKey('pilot_institutions.id'), nullable=False, index=True)
    dataset_id = db.Column(db.String(36), db.ForeignKey('pilot_dataset_snapshots.id'), nullable=False)
    name = db.Column(db.String(160), nullable=False)
    family = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(24), nullable=False, default='candidate')
    payload = db.Column(db.JSON, nullable=False, default=dict)
    sha256 = db.Column(db.String(64), nullable=False)
    metrics = db.Column(db.JSON, nullable=False, default=dict)
    validation = db.Column(db.JSON, nullable=False, default=dict)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (db.UniqueConstraint('institution_id', 'sha256', name='uq_pilot_model_hash'),)


class PilotModelActivation(db.Model):
    __tablename__ = 'pilot_model_activations'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    institution_id = db.Column(db.String(36), db.ForeignKey('pilot_institutions.id'), nullable=False)
    model_id = db.Column(db.String(36), db.ForeignKey('pilot_model_versions.id'), nullable=False)
    previous_model_id = db.Column(db.String(36))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class PilotForecastReceipt(db.Model):
    __tablename__ = 'pilot_forecast_receipts'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    institution_id = db.Column(db.String(36), db.ForeignKey('pilot_institutions.id'), nullable=False, index=True)
    issued_at = db.Column(db.DateTime(timezone=True), nullable=False)
    received_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    provider = db.Column(db.String(80), nullable=False)
    product = db.Column(db.String(80), nullable=False)
    payload = db.Column(db.JSON, nullable=False, default=dict)
    sha256 = db.Column(db.String(64), nullable=False)


class PilotForecastRun(db.Model):
    __tablename__ = 'pilot_forecast_runs'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    institution_id = db.Column(db.String(36), db.ForeignKey('pilot_institutions.id'), nullable=False, index=True)
    model_id = db.Column(db.String(36), db.ForeignKey('pilot_model_versions.id'), nullable=False)
    receipt_id = db.Column(db.String(36), db.ForeignKey('pilot_forecast_receipts.id'), nullable=False)
    issued_at = db.Column(db.DateTime(timezone=True), nullable=False)
    payload = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (db.UniqueConstraint('model_id', 'receipt_id', name='uq_pilot_forecast_run'),
                      db.UniqueConstraint('institution_id', 'issued_at', name='uq_pilot_forecast_issue'))


class PilotJob(db.Model):
    __tablename__ = 'pilot_jobs'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    institution_id = db.Column(db.String(36), db.ForeignKey('pilot_institutions.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    parameters = db.Column(db.JSON, nullable=False, default=dict)
    kind = db.Column(db.String(32), nullable=False)
    resource_id = db.Column(db.String(36), nullable=False)
    status = db.Column(db.String(24), nullable=False, default='queued')
    progress = db.Column(db.Integer, nullable=False, default=0)
    error = db.Column(db.String(200))
    attempts = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    started_at = db.Column(db.DateTime(timezone=True))
    finished_at = db.Column(db.DateTime(timezone=True))
