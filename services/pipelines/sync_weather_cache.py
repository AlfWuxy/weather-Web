# -*- coding: utf-8 -*-
"""Sync minute-level weather cache for all locations."""
import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    # 兼容旧定时任务和手工命令从任意目录直接执行脚本。
    sys.path.insert(0, str(ROOT_DIR))

from core.app import create_app  # noqa: E402
from core.constants import DEFAULT_CITY_LABEL  # noqa: E402
from core.db_models import WeatherCache, WeatherData  # noqa: E402
from core.extensions import db  # noqa: E402
from core.weather import normalize_location_name  # noqa: E402
from core.time_utils import today_local, utcnow  # noqa: E402
from services.miniprogram_service import (  # noqa: E402
    CANONICAL_LOCATION_NAME,
    qweather_runtime_configured,
    refresh_snapshot_from_cycle,
)
from services.weather_service import WeatherService  # noqa: E402

logger = logging.getLogger(__name__)
app = create_app(register_blueprints=False)


def _dedupe_locations(locations):
    seen = set()
    for loc in locations:
        if not loc:
            continue
        normalized = normalize_location_name(loc)
        if normalized in seen:
            continue
        seen.add(normalized)
        yield normalized


def _upsert_cache(location, weather_data, fetched_at):
    payload = json.dumps(weather_data, ensure_ascii=False)
    cache = WeatherCache.query.filter_by(location=location).first()
    if cache:
        cache.payload = payload
        cache.fetched_at = fetched_at
        cache.is_mock = bool(weather_data.get('is_mock'))
        return cache
    cache = WeatherCache(
        location=location,
        fetched_at=fetched_at,
        payload=payload,
        is_mock=bool(weather_data.get('is_mock'))
    )
    db.session.add(cache)
    return cache


def _upsert_daily(location, weather_data, target_date):
    record = WeatherData.query.filter_by(date=target_date, location=location).first()
    if record is None:
        record = WeatherData(date=target_date, location=location)
        db.session.add(record)

    record.temperature = weather_data.get('temperature')
    record.temperature_max = weather_data.get('temperature_max')
    record.temperature_min = weather_data.get('temperature_min')
    record.humidity = weather_data.get('humidity')
    record.pressure = weather_data.get('pressure')
    record.weather_condition = weather_data.get('weather_condition')
    record.wind_speed = weather_data.get('wind_speed')
    record.pm25 = weather_data.get('pm25')
    record.aqi = weather_data.get('aqi')
    return record


def _resolve_locations(locations):
    if locations:
        return locations
    env_locations = os.getenv('WEATHER_SYNC_LOCATIONS', '').strip()
    if env_locations:
        return [item.strip() for item in env_locations.split(',') if item.strip()]
    # 后台默认只预热都昌县。村庄与页面选项共享县级缓存，禁止把 UI 选项批量当成 API 任务。
    return [DEFAULT_CITY_LABEL]


def sync_weather_cache(locations=None, update_daily=True):
    with app.app_context():
        # 小程序与 Web 共用唯一都昌县周期。即使旧命令传入多个地点，也不会形成 API fan-out。
        requested_locations = list(_dedupe_locations(_resolve_locations(locations)))
        locations = [CANONICAL_LOCATION_NAME]
        weather_service = WeatherService() if qweather_runtime_configured() else None
        fetched_at = utcnow()
        target_date = today_local()
        updated = 0

        if weather_service is not None:
            try:
                weather_data = weather_service.get_current_weather(
                    CANONICAL_LOCATION_NAME,
                    include_enrichment=False,
                )
            except Exception as exc:
                logger.exception("Weather sync failed for %s: %s", CANONICAL_LOCATION_NAME, exc)
                db.session.rollback()
                weather_data = {}
        else:
            # QWeather 未配置时由快照层只读旧缓存并继承原始 fetched_at。
            # 这里不回写 WeatherCache，避免旧数据被洗成刚抓取。
            weather_data = {}

        if weather_data:
            try:
                _upsert_cache(CANONICAL_LOCATION_NAME, weather_data, fetched_at)
                if update_daily:
                    _upsert_daily(CANONICAL_LOCATION_NAME, weather_data, target_date)
                db.session.commit()
            except Exception as exc:
                logger.exception("Weather cache upsert failed for %s: %s", CANONICAL_LOCATION_NAME, exc)
                db.session.rollback()
            else:
                updated = 1
        else:
            logger.warning("No persisted weather data available for %s", CANONICAL_LOCATION_NAME)

        snapshot = refresh_snapshot_from_cycle(
            weather_data,
            weather_service=weather_service,
            fetched_at=fetched_at,
        )
        return {
            'locations': 1,
            'requested_locations': len(requested_locations),
            'canonical_location': CANONICAL_LOCATION_NAME,
            'updated': updated,
            'update_daily': update_daily,
            'snapshot_id': snapshot.snapshot_id if snapshot else None,
        }


def main():
    parser = argparse.ArgumentParser(description='Sync minute-level weather cache.')
    parser.add_argument('--location', action='append', dest='locations', help='Location label override')
    parser.add_argument('--no-daily', action='store_true', help='Skip WeatherData daily upsert')
    args = parser.parse_args()

    result = sync_weather_cache(
        locations=args.locations,
        update_daily=not args.no_daily
    )
    print(f"Cache sync: {result}")


if __name__ == '__main__':
    main()
