# -*- coding: utf-8 -*-
"""Resolve free-form location text to a QWeather-compatible location code.

Priority:
1) Static CITY_LOCATION_MAP (village/city aliases)
2) Raw lon,lat or numeric location id
3) AMap geocode (address -> lon,lat)

Results are cached in DB table `location_cache` for ~30 days.
"""

import json
import logging
from datetime import timedelta

import requests
from flask import current_app

from core.db_models import LocationCache
from core.extensions import db
from core.time_utils import utcnow, ensure_utc_aware
from utils.validators import sanitize_input

logger = logging.getLogger(__name__)


def _is_lon_lat(value):
    if not value or ',' not in str(value):
        return False
    parts = [p.strip() for p in str(value).split(',')]
    if len(parts) != 2:
        return False
    try:
        lon = float(parts[0])
        lat = float(parts[1])
    except (TypeError, ValueError):
        return False
    return -180 <= lon <= 180 and -90 <= lat <= 90


def _fresh(updated_at, ttl_days=30):
    if not updated_at:
        return False
    try:
        return utcnow() - ensure_utc_aware(updated_at) <= timedelta(days=ttl_days)
    except Exception:
        return False


def resolve_location(query, ttl_days=30):
    """Resolve query -> (location_code, provider, display_name, raw_json)."""
    query = sanitize_input(query, max_length=200) if query else None
    query = query.strip() if isinstance(query, str) else ''
    if not query:
        default_location = current_app.config.get('DEFAULT_LOCATION', '116.20,29.27')
        return {
            'location_code': default_location,
            'provider': 'default',
            'display_name': current_app.config.get('DEFAULT_CITY', '都昌'),
            'raw_json': None
        }

    # 1) DB cache (exact match)
    try:
        record = LocationCache.query.filter_by(query_text=query).order_by(
            LocationCache.updated_at.desc(),
            LocationCache.id.desc()
        ).first()
        if record and _fresh(record.updated_at, ttl_days=ttl_days):
            return {
                'location_code': record.location_code,
                'provider': record.provider or 'cache',
                'display_name': query,
                'raw_json': record.raw_json
            }
    except Exception as exc:
        logger.debug("location cache read failed: %s", exc)
        db.session.rollback()

    # 2) Static map
    city_map = current_app.config.get('CITY_LOCATION_MAP', {}) or {}
    if query in city_map:
        code = city_map[query]
        _upsert_cache(query, code, provider='map', raw_json=None)
        return {
            'location_code': code,
            'provider': 'map',
            'display_name': query,
            'raw_json': None
        }

    # 3) Raw forms
    if query.isdigit() or _is_lon_lat(query):
        _upsert_cache(query, query, provider='raw', raw_json=None)
        return {
            'location_code': query,
            'provider': 'raw',
            'display_name': query,
            'raw_json': None
        }

    # 4) AMap geocode
    amap_key = current_app.config.get('AMAP_KEY') or ''
    if not amap_key:
        default_location = current_app.config.get('DEFAULT_LOCATION', '116.20,29.27')
        default_city = current_app.config.get('DEFAULT_CITY', '都昌')
        return {
            'location_code': default_location,
            'provider': 'fallback',
            'display_name': default_city,
            'raw_json': None
        }

    url = 'https://restapi.amap.com/v3/geocode/geo'
    try:
        resp = requests.get(url, params={'address': query, 'key': amap_key}, timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"amap http {resp.status_code}")
        data = resp.json()
    except Exception as exc:
        logger.warning("AMap geocode failed for %s: %s", query, exc)
        default_location = current_app.config.get('DEFAULT_LOCATION', '116.20,29.27')
        default_city = current_app.config.get('DEFAULT_CITY', '都昌')
        return {
            'location_code': default_location,
            'provider': 'fallback',
            'display_name': default_city,
            'raw_json': None
        }

    try:
        if str(data.get('status')) != '1':
            raise RuntimeError(f"amap status {data.get('status')}")
        geocodes = data.get('geocodes') or []
        first = geocodes[0] if geocodes else None
        location = first.get('location') if isinstance(first, dict) else None
        if not location or not _is_lon_lat(location):
            raise RuntimeError("amap no lonlat")
        display_name = first.get('formatted_address') or query
        raw_json = json.dumps(data, ensure_ascii=False)
        _upsert_cache(query, location, provider='amap', raw_json=raw_json)
        return {
            'location_code': location,
            'provider': 'amap',
            'display_name': display_name,
            'raw_json': raw_json
        }
    except Exception as exc:
        logger.warning("AMap geocode parse failed for %s: %s", query, exc)
        default_location = current_app.config.get('DEFAULT_LOCATION', '116.20,29.27')
        default_city = current_app.config.get('DEFAULT_CITY', '都昌')
        return {
            'location_code': default_location,
            'provider': 'fallback',
            'display_name': default_city,
            'raw_json': None
        }


def _upsert_cache(query, location_code, provider='cache', raw_json=None):
    try:
        now = utcnow()
        record = LocationCache.query.filter_by(query_text=query).first()
        if record:
            record.location_code = location_code
            record.provider = provider
            record.raw_json = raw_json
            record.updated_at = now
        else:
            record = LocationCache(
                query_text=query,
                location_code=location_code,
                provider=provider,
                raw_json=raw_json,
                created_at=now,
                updated_at=now
            )
            db.session.add(record)
        db.session.commit()
    except Exception as exc:
        logger.debug("location cache upsert failed: %s", exc)
        db.session.rollback()
