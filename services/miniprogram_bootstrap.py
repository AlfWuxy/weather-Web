# -*- coding: utf-8 -*-
"""小程序公共启动数据：只读共享天气缓存，不触发供应商请求。"""
from __future__ import annotations

from datetime import timedelta

from flask import current_app

from core.db_models import WeatherCache
from core.time_utils import ensure_utc_aware, utcnow
from services.content_scripts import script_catalog
from services.help_request_service import capabilities
from services.miniprogram_auth import current_privacy_version
from utils.parsers import safe_json_loads

SNAPSHOT_TTL_SECONDS = 1800
CANONICAL_LOCATION_NAME = '都昌县'


def get_bootstrap_payload():
    """与 v1.1 客户端字段对齐；缺失时标记 unavailable/stale，不伪造低风险。"""
    record = (
        WeatherCache.query.filter_by(location=CANONICAL_LOCATION_NAME)
        .order_by(WeatherCache.fetched_at.desc(), WeatherCache.id.desc())
        .first()
    )
    current = safe_json_loads(record.payload, {}) if record else {}
    if not isinstance(current, dict):
        current = {}
    fetched_at = ensure_utc_aware(record.fetched_at) if record else None
    now = utcnow()
    ttl = timedelta(seconds=SNAPSHOT_TTL_SECONDS)
    stale = True
    available = False
    if fetched_at is not None:
        stale = now > fetched_at + ttl
        available = bool(current) and current.get('is_mock') is not True and current.get('temperature') is not None
    if current.get('stale') is True:
        stale = True
    if current.get('available') is False:
        available = False
    expires_at = (fetched_at + ttl) if fetched_at else None
    payload = capabilities()
    payload.update({
        'snapshot_id': f'cache:{record.id}' if record else None,
        'location': {'name': CANONICAL_LOCATION_NAME, 'code': '116.20,29.27', 'scope': 'county'},
        'fetched_at': fetched_at.isoformat() if fetched_at else None,
        'observed_at': fetched_at.isoformat() if fetched_at else None,
        'expires_at': expires_at.isoformat() if expires_at else None,
        'ttl_seconds': SNAPSHOT_TTL_SECONDS,
        'available': available,
        'stale': stale,
        'current': current or {'is_mock': True},
        'forecast': [],
        'warnings': [],
        'risk': {
            'level': '未知',
            'score': None,
            'summary': '天气快照过期或不可用' if stale or not available else '请按行动清单执行',
            'reasons': ['天气快照过期或不可用'] if stale or not available else [],
            'disclaimer': '仅作天气健康行动提醒，不提供医疗诊断。过期天气不等于低风险。',
        },
        'actions': [] if stale or not available else [
            {'id': 'general-water', 'title': '少量多次补水', 'detail': '不要等到明显口渴才喝水。'},
            {'id': 'general-room', 'title': '检查室内温度和通风', 'detail': '高温时拉上遮光帘，合理使用风扇或空调。'},
            {'id': 'general-contact', 'title': '和家人确认一次状态', 'detail': '问清是否头晕、胸闷、乏力。'},
        ],
        'source_status': {
            'mode': 'shared_weather_cache',
            'status': 'fresh' if available and not stale else ('stale' if stale else 'missing'),
            'refresh_interval_seconds': SNAPSHOT_TTL_SECONDS,
            'canonical_location_only': True,
        },
        'required_privacy_consent_version': current_privacy_version(),
        'scripts': script_catalog(),
        'schema_version': payload.get('schema_version'),
    })
    payload['features'] = dict(payload.get('features') or {})
    payload['features']['help_requests'] = True
    return payload
