"""地点解析：静态地点、坐标、带预算和有界缓存的服务端地理编码。"""
import json
import logging
import time
from datetime import timedelta

import requests
from flask import current_app
from sqlalchemy import delete, select, update

from core.db_models import LocationCache
from core.extensions import db
from core.resource_budget import (
    acquire_lease, release_lease, reserve, setting,
    ResourceLimitError, ResourceBudgetUnavailable,
)
from core.time_utils import utcnow, ensure_utc_aware
from utils.validators import sanitize_input

logger = logging.getLogger(__name__)


def _is_lon_lat(value):
    if not value or len(str(value)) > 100 or ',' not in str(value):
        return False
    parts = str(value).split(',')
    if len(parts) != 2:
        return False
    try:
        lon, lat = (float(part) for part in parts)
        return -180 <= lon <= 180 and -90 <= lat <= 90
    except (TypeError, ValueError):
        return False


def _fallback():
    return {'location_code': current_app.config.get('DEFAULT_LOCATION', '116.20,29.27'),
            'provider': 'fallback', 'display_name': current_app.config.get('DEFAULT_CITY', '都昌'),
            'raw_json': None}


def _read_cache(query, ttl_days):
    # 独立短连接避免回滚或提交调用方正在处理的业务事务。
    with db.engine.connect() as conn:
        row = conn.execute(select(LocationCache.location_code, LocationCache.provider, LocationCache.updated_at).where(
            LocationCache.query_text == query).order_by(LocationCache.updated_at.desc()).limit(1)).mappings().first()
    if not row or not row['updated_at']:
        return None
    ttl = timedelta(hours=1) if row['provider'] == 'negative' else timedelta(days=ttl_days)
    if utcnow() - ensure_utc_aware(row['updated_at']) > ttl:
        return None
    if row['provider'] == 'negative':
        return _fallback()
    return {'location_code': row['location_code'], 'provider': row['provider'] or 'cache',
            'display_name': query, 'raw_json': None}


def _upsert_cache(query, location_code, provider='cache', raw_json=None):
    """缓存写入串行化后淘汰旧数据；不保存供应商完整地址响应。"""
    from core.resource_budget import ResourceLease, _insert_ignore
    table = LocationCache.__table__
    now = utcnow()
    with db.engine.begin() as conn:
        # 所有缓存写入共用一行写锁，保证多 worker 总量不会越界。
        locks = ResourceLease.__table__
        _insert_ignore(conn, locks, {'key': 'location-cache', 'owner': '', 'expires_at': now})
        conn.execute(update(locks).where(locks.c.key == 'location-cache').values(expires_at=now))
        conn.execute(delete(table).where(table.c.updated_at < now - timedelta(days=30)))
        conn.execute(delete(table).where(table.c.query == query))
        cap = setting('LOCATION_CACHE_MAX_ROWS', 1000)
        if cap:
            conn.execute(table.insert().values({'query': query, 'location_code': location_code,
                         'provider': provider, 'raw_json': None, 'created_at': now, 'updated_at': now}))
        keep = select(table.c.id).order_by(table.c.updated_at.desc(), table.c.id.desc()).limit(cap)
        conn.execute(delete(table).where(table.c.id.not_in(keep)))


def _response_json(response):
    maximum = min(65536, setting('GEOCODE_RESPONSE_MAX_BYTES', 65536))
    raw = bytearray()
    for chunk in response.iter_content(chunk_size=4096):
        raw.extend(chunk)
        if len(raw) > maximum:
            raise ValueError('geocode response too large')
    return json.loads(raw)


def resolve_location(query, ttl_days=30, user_id=None):
    query = (sanitize_input(query, max_length=200) or '').strip()
    if not query:
        result = _fallback()
        result['provider'] = 'default'
        return result
    ttl_days = min(30, max(1, ttl_days))
    try:
        cached = _read_cache(query, ttl_days)
        if cached:
            return cached
        city_map = current_app.config.get('CITY_LOCATION_MAP', {}) or {}
        if query in city_map or (query.isdigit() and len(query) <= 100) or _is_lon_lat(query):
            code = city_map.get(query, query)
            provider = 'map' if query in city_map else 'raw'
            _upsert_cache(query, code, provider)
            return {'location_code': code, 'provider': provider, 'display_name': query, 'raw_json': None}
        key = current_app.config.get('AMAP_WEB_SERVICE_KEY') or ''
        if not key:
            return _fallback()
        lease = acquire_lease(query)
        if not lease:
            # 同一 miss 的其他调用等待短时缓存；超时降级，不重复购买请求。
            for _ in range(10):
                time.sleep(0.1)
                cached = _read_cache(query, ttl_days)
                if cached:
                    return cached
            return _fallback()
        try:
            cached = _read_cache(query, ttl_days)
            if cached:
                return cached
            reserve('GEOCODE', user_id=user_id)
            response = None
            try:
                response = requests.get('https://restapi.amap.com/v3/geocode/geo',
                                        params={'address': query, 'key': key}, timeout=(3, 10), stream=True)
                if response.status_code != 200:
                    raise ValueError('geocode unavailable')
                data = _response_json(response)
                geocodes = data.get('geocodes') or []
                first = geocodes[0] if geocodes else {}
                location = first.get('location')
                if str(data.get('status')) != '1' or not _is_lon_lat(location):
                    raise ValueError('geocode no result')
                _upsert_cache(query, location, 'amap')
                return {'location_code': location, 'provider': 'amap', 'display_name': query, 'raw_json': None}
            except (requests.RequestException, ValueError, TypeError, AttributeError, RecursionError):
                # 不写入地址、供应商 URL 或异常文本，它们可能携带 key 和精确坐标。
                logger.warning('地理编码失败，已使用短期负缓存')
                _upsert_cache(query, _fallback()['location_code'], 'negative')
                return _fallback()
            finally:
                if response is not None:
                    response.close()
        finally:
            release_lease(lease)
    except (ResourceLimitError, ResourceBudgetUnavailable):
        logger.warning('地理编码预算不可用，已停止外部调用')
    except Exception:
        logger.warning('地点缓存不可用，已停止外部调用')
    return _fallback()
