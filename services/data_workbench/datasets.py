"""机构日聚合、报送日历与不可变训练快照。"""
import csv
import hashlib
import io
import json
import zipfile
from datetime import timedelta
from collections import Counter
from sqlalchemy import func, update
from core.extensions import db
from core.pilot_models import PilotInstitution, PilotCoverage, PilotEncounter, PilotImportBatch, PilotWeatherDay, PilotDatasetSnapshot
from core.time_utils import utcnow
from .ingestion import parse_date
from .storage import store_bytes, read_bytes
from .weather import PRODUCT, SOURCE

DAILY_FIELDS = ['date', 'cases_60plus', 'coverage_status', 'tmean', 'rh_mean', 'precipitation', 'weather_source', 'weather_product']
DICTIONARY = {
    'date': '机构当地日历日，Asia/Shanghai',
    'cases_60plus': '年龄>=60岁、有效就诊记录人次；仅完整报送日有值，完整且无就诊为0；不是独立患者数或发病率',
    'coverage_status': 'complete=完整报送；partial=部分报送；closed=确认停诊；unreported=未报送',
    'tmean': 'ERA5近地面日平均气温，摄氏度', 'rh_mean': 'ERA5近地面日平均相对湿度，百分比',
    'precipitation': 'ERA5日累计降水，毫米', 'weather_source': '天气来源；缺失时为空',
    'weather_product': '固定再分析产品，不等同于当时收到的预报',
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _csv(rows, fields):
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fields, lineterminator='\n')
    writer.writeheader()
    for row in rows:
        # 即使未来增加文本列，也不能在 Excel 打开时执行公式。
        safe = {key: ("'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value)
                for key, value in row.items() if key in fields}
        writer.writerow(safe)
    return buffer.getvalue().encode('utf-8')


def _range(inst, start=None, end=None):
    first, last = db.session.query(func.min(PilotCoverage.date), func.max(PilotCoverage.date)).filter(PilotCoverage.institution_id == inst.id).one()
    if start is not None:
        first = parse_date(start)
    if end is not None:
        last = parse_date(end)
    if first is None or last is None:
        return None, None
    if last < first or (last - first).days > 3660:
        raise ValueError('聚合日期范围无效')
    return first, last


def overview(inst, start=None, end=None):
    first, last = _range(inst, start, end)
    latest = PilotImportBatch.query.filter_by(institution_id=inst.id, status='confirmed').order_by(PilotImportBatch.confirmed_at.desc()).first()
    if first is None:
        return {'institution_id': inst.id, 'date_start': None, 'date_end': None, 'daily': [], 'months': [], 'summary': {}, 'latest_reported_at': None}
    coverage = {r.date: r for r in PilotCoverage.query.filter(PilotCoverage.institution_id == inst.id, PilotCoverage.date >= first, PilotCoverage.date <= last).all()}
    weather = {r.date: r for r in PilotWeatherDay.query.filter(PilotWeatherDay.institution_id == inst.id, PilotWeatherDay.product == PRODUCT, PilotWeatherDay.date >= first, PilotWeatherDay.date <= last).all()}
    counts = dict(db.session.query(PilotEncounter.encounter_date, func.count(PilotEncounter.id)).filter(
        PilotEncounter.institution_id == inst.id, PilotEncounter.active.is_(True), PilotEncounter.age >= 60,
        PilotEncounter.encounter_date >= first, PilotEncounter.encounter_date <= last).group_by(PilotEncounter.encounter_date).all())
    daily, months, statuses = [], {}, Counter()
    day = first
    while day <= last:
        cov, met = coverage.get(day), weather.get(day)
        status = cov.status if cov else 'unreported'
        count = counts.get(day, 0) if status == 'complete' else None
        row = {'date': day.isoformat(), 'cases_60plus': count, 'coverage_status': status,
               'observed_rows_60plus': counts.get(day, 0) if cov else None,
               'tmean': met.tmean if met else None, 'rh_mean': met.rh_mean if met else None,
               'precipitation': met.precipitation if met else None, 'weather_source': met.source if met else None,
               'weather_product': met.product if met else None, 'batch_id': cov.batch_id if cov else None}
        daily.append(row)
        statuses[status] += 1
        month = months.setdefault(day.strftime('%Y-%m'), {'month': day.strftime('%Y-%m'), 'complete': 0, 'partial': 0, 'closed': 0, 'unreported': 0, 'confirmed_zero': 0, 'missing_weather': 0, 'cases_60plus': 0})
        month[status] += 1
        month['confirmed_zero'] += int(count == 0)
        month['cases_60plus'] += count or 0
        month['missing_weather'] += int(any(row[k] is None for k in ('tmean', 'rh_mean', 'precipitation')))
        day += timedelta(days=1)
    return {'institution_id': inst.id, 'date_start': first.isoformat(), 'date_end': last.isoformat(),
            'daily': daily, 'months': list(months.values()), 'summary': dict(statuses),
            'latest_reported_at': latest.confirmed_at.isoformat() if latest else None,
            'weather_source': SOURCE, 'weather_product': PRODUCT}


def create_snapshot(inst, user_id, cutoff=None, snapshot_id=None):
    # 无值变化的写锁与确认批次使用同一机构锁，避免并发修订撕裂冻结内容。
    db.session.execute(update(PilotInstitution).where(PilotInstitution.id == inst.id).values(enabled=PilotInstitution.enabled))
    if snapshot_id is not None:
        existing = db.session.get(PilotDatasetSnapshot, snapshot_id)
        if existing:
            if existing.institution_id != inst.id or existing.created_by != user_id or (cutoff is not None and existing.manifest.get('cutoff_date') != parse_date(cutoff).isoformat()):
                db.session.rollback()
                raise ValueError('预分配快照与机构、创建者或截止日期不符')
            db.session.commit()
            return existing
    first, last = _range(inst)
    if cutoff is not None:
        cutoff = parse_date(cutoff)
        if first is None or not first <= cutoff <= last:
            db.session.rollback()
            raise ValueError('截止日期必须位于已报送数据范围内')
    # 首先序列化当前聚合内容，任务重试只使用这一版本，不重新查询病例。
    data = overview(inst, end=cutoff)
    if not data['daily']:
        raise ValueError('尚无已确认报送数据，不能冻结训练包')
    frozen_at = utcnow().isoformat()
    snapshot = PilotDatasetSnapshot(institution_id=inst.id, created_by=user_id, status='frozen')
    if snapshot_id is not None:
        snapshot.id = snapshot_id
    db.session.add(snapshot)
    db.session.flush()
    batches = PilotImportBatch.query.filter_by(institution_id=inst.id, status='confirmed').all()
    manifest = {'schema_version': 'pilot.dataset.v1', 'dataset_id': snapshot.id, 'institution_id': inst.id,
                'region_code': inst.region_code, 'outcome': 'daily_encounters_60plus', 'age_min': 60,
                'feature_version': 'pilot.calendar_weather.v1', 'cutoff_date': data['date_end'],
                'date_start': data['date_start'], 'date_end': data['date_end'], 'timezone': 'Asia/Shanghai',
                'weather_product': PRODUCT, 'weather_source': SOURCE, 'frozen_at': frozen_at, 'generated_at': frozen_at,
                'batch_versions': [{'id': b.id, 'sha256': b.sha256, 'confirmed_at': b.confirmed_at.isoformat()} for b in sorted(batches, key=lambda b: b.id)],
                'missing_weather_days': sum(any(r[k] is None for k in ('tmean', 'rh_mean', 'precipitation')) for r in data['daily']),
                'reporting_summary': data['summary']}
    frozen = {'manifest': manifest, 'daily': data['daily']}
    snapshot.storage_path = store_bytes(_json(frozen), kind='snapshot')
    snapshot.manifest = manifest
    db.session.commit()
    return snapshot


def build_snapshot(snapshot_id):
    snapshot = db.session.get(PilotDatasetSnapshot, snapshot_id)
    if not snapshot:
        raise ValueError('数据快照不存在')
    if snapshot.status == 'ready':
        return snapshot.manifest
    frozen = json.loads(read_bytes(snapshot.storage_path))
    manifest, daily = frozen['manifest'], frozen['daily']
    coverage = [{'date': r['date'], 'coverage_status': r['coverage_status'], 'batch_id': r['batch_id']} for r in daily]
    files = {'daily.csv': _csv(daily, DAILY_FIELDS), 'coverage.csv': _csv(coverage, ['date', 'coverage_status', 'batch_id']), 'dictionary.json': _json(DICTIONARY)}
    manifest['files'] = {name: {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)} for name, content in files.items()}
    files['manifest.json'] = _json(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            # 固定 ZIP 元数据，使相同冻结包可以重现完全相同的文件哈希。
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content)
    content = buffer.getvalue()
    snapshot.storage_path = store_bytes(content, kind='snapshot')
    snapshot.sha256, snapshot.manifest, snapshot.status = hashlib.sha256(content).hexdigest(), manifest, 'ready'
    db.session.commit()
    return manifest


def export_snapshot(snapshot):
    if snapshot.status != 'ready':
        raise ValueError('训练包仍在准备中')
    content = read_bytes(snapshot.storage_path)
    if hashlib.sha256(content).hexdigest() != snapshot.sha256:
        raise ValueError('训练包完整性验证失败')
    return content
