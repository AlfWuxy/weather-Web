"""固定 ERA5 历史天气；与线上既有天气服务隔离。"""
import math
from datetime import timedelta
import requests
from sqlalchemy.exc import IntegrityError
from core.extensions import db
from core.pilot_models import PilotWeatherDay
from core.time_utils import utcnow, today_local
from .ingestion import parse_date

PRODUCT = 'era5'
SOURCE = 'Open-Meteo / ERA5 reanalysis'
HISTORY_URL = 'https://archive-api.open-meteo.com/v1/archive'


def _number(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        return None
    return float(value)


def sync_history(inst, start, end):
    start, requested_end = parse_date(start), parse_date(end)
    if requested_end < start or (requested_end - start).days > 3660:
        raise ValueError('天气日期范围无效')
    if not (-90 <= inst.latitude <= 90 and -180 <= inst.longitude <= 180):
        raise ValueError('机构坐标无效')
    # ERA5 约延迟五日，不使用其他产品填补最近日期。
    end = min(requested_end, today_local() - timedelta(days=5))
    saved = 0
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=365), end)
        cached = {r.date for r in PilotWeatherDay.query.filter(
            PilotWeatherDay.institution_id == inst.id, PilotWeatherDay.product == PRODUCT,
            PilotWeatherDay.date >= cursor, PilotWeatherDay.date <= chunk_end,
            PilotWeatherDay.tmean.is_not(None), PilotWeatherDay.rh_mean.is_not(None),
            PilotWeatherDay.precipitation.is_not(None)).all()}
        if len(cached) == (chunk_end - cursor).days + 1:
            cursor = chunk_end + timedelta(days=1)
            continue
        response = requests.get(HISTORY_URL, params={
            'latitude': inst.latitude, 'longitude': inst.longitude,
            'start_date': cursor.isoformat(), 'end_date': chunk_end.isoformat(), 'models': PRODUCT,
            'daily': 'temperature_2m_mean,relative_humidity_2m_mean,precipitation_sum',
            'timezone': 'Asia/Shanghai', 'temperature_unit': 'celsius', 'precipitation_unit': 'mm',
        }, timeout=(5, 45), allow_redirects=False)
        response.raise_for_status()
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError('天气响应超过上限')
        payload = response.json()
        daily = payload.get('daily', {})
        names = ['time', 'temperature_2m_mean', 'relative_humidity_2m_mean', 'precipitation_sum']
        if not all(isinstance(daily.get(name), list) for name in names) or len({len(daily[n]) for n in names}) != 1 or len(daily['time']) > 366:
            raise ValueError('天气响应字段或长度异常')
        parsed, seen = [], set()
        for day, temp, rh, rain in zip(*(daily[name] for name in names)):
            day = parse_date(day)
            if day in seen or not cursor <= day <= chunk_end:
                raise ValueError('天气日期重复或越界')
            seen.add(day)
            parsed.append((day, _number(temp, -90, 65), _number(rh, 0, 100), _number(rain, 0, 3000)))
        try:
            for day, temp, rh, rain in parsed:
                record = PilotWeatherDay.query.filter_by(institution_id=inst.id, date=day, product=PRODUCT).first()
                if not record:
                    record = PilotWeatherDay(institution_id=inst.id, date=day, product=PRODUCT, source=SOURCE)
                    db.session.add(record)
                record.tmean, record.rh_mean, record.precipitation = temp, rh, rain
                record.fetched_at = utcnow()
                saved += 1
            db.session.commit()
        except IntegrityError:
            # 重叠工作任务由数据库唯一约束兜底；已提交日期可在重试时复用。
            db.session.rollback()
            raise ValueError('天气缓存正在更新，请重试') from None
        cursor = chunk_end + timedelta(days=1)
    return {'saved_days': saved, 'source': SOURCE, 'product': PRODUCT,
            'requested_end': requested_end.isoformat(), 'available_through': end.isoformat(),
            'recent_days_pending': max(0, (requested_end - max(end, start - timedelta(days=1))).days)}
