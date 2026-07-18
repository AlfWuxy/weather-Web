# -*- coding: utf-8 -*-
"""Sync minute-level weather cache for all locations."""
import argparse
import json
import logging
import math
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
from core.db_models import ForecastCache, WeatherCache, WeatherData  # noqa: E402
from core.extensions import db  # noqa: E402
from core.weather import normalize_location_name  # noqa: E402
from core.time_utils import today_local, utcnow  # noqa: E402
from services.miniprogram_service import (  # noqa: E402
    CANONICAL_LOCATION_NAME,
    latest_snapshot_record,
    qweather_runtime_configured,
    refresh_snapshot_from_cycle,
    snapshot_payload,
)
from services.weather_service import WeatherService  # noqa: E402

logger = logging.getLogger(__name__)
app = create_app(register_blueprints=False)
NOWCAST_CACHE_LOCATION = f"nowcast:{CANONICAL_LOCATION_NAME}"
NOWCAST_CACHE_HOURS = 24


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


def _valid_nowcast(nowcast):
    """只缓存来源明确、时间连续且数值可信的小时预报。"""
    if not isinstance(nowcast, dict):
        return False
    if (
        not nowcast.get('available')
        or nowcast.get('source') != 'Open-Meteo'
        or nowcast.get('is_mock')
        or nowcast.get('is_demo')
    ):
        return False
    timeline = nowcast.get('timeline')
    if not isinstance(timeline, list) or not 1 <= len(timeline) <= NOWCAST_CACHE_HOURS:
        return False

    parsed_times = []
    for item in timeline:
        if not isinstance(item, dict):
            return False
        try:
            item_time = datetime.fromisoformat(str(item.get('time') or ''))
            probability = float(item.get('precipitation_probability'))
            precipitation = float(item.get('precipitation_mm'))
            temperature = float(item.get('temperature'))
        except (TypeError, ValueError, OverflowError):
            return False
        if item_time.tzinfo is not None:
            return False
        if (
            not math.isfinite(probability)
            or not 0 <= probability <= 100
            or not math.isfinite(precipitation)
            or not 0 <= precipitation <= 500
            or not math.isfinite(temperature)
            or not -100 <= temperature <= 100
        ):
            return False
        expected_risk = '高' if probability >= 70 else '中' if probability >= 40 else '低'
        if item.get('risk_level') != expected_risk:
            return False
        if not str(item.get('condition') or '').strip():
            return False
        parsed_times.append(item_time)

    return parsed_times == sorted(parsed_times) and len(set(parsed_times)) == len(parsed_times)


def _upsert_nowcast(nowcast, fetched_at):
    """复用 ForecastCache 保存后台周期取得的小时预报。"""
    if not _valid_nowcast(nowcast):
        return False
    record = ForecastCache.query.filter_by(
        location=NOWCAST_CACHE_LOCATION,
        days=NOWCAST_CACHE_HOURS,
    ).order_by(ForecastCache.fetched_at.desc(), ForecastCache.id.desc()).first()
    payload = json.dumps(nowcast, ensure_ascii=False)
    if record is None:
        record = ForecastCache(
            location=NOWCAST_CACHE_LOCATION,
            days=NOWCAST_CACHE_HOURS,
            fetched_at=fetched_at,
            payload=payload,
            is_mock=False,
        )
        db.session.add(record)
    else:
        record.fetched_at = fetched_at
        record.payload = payload
        record.is_mock = False
    return True


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
        previous_snapshot = latest_snapshot_record()
        previous_snapshot_id = previous_snapshot.snapshot_id if previous_snapshot else None
        fetched_at = utcnow()
        target_date = today_local()
        updated = 0
        nowcast_updated = 0
        nowcast = {}

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
            try:
                nowcast = weather_service.get_short_term_nowcast(
                    CANONICAL_LOCATION_NAME,
                    hours=NOWCAST_CACHE_HOURS,
                )
            except Exception as exc:
                logger.warning("Nowcast sync failed for %s: %s", CANONICAL_LOCATION_NAME, exc)
                nowcast = {}
        else:
            # QWeather 未配置时由快照层只读旧缓存并继承原始 fetched_at。
            # 这里不回写 WeatherCache，避免旧数据被洗成刚抓取。
            weather_data = {}

        weather_is_mock = bool(
            isinstance(weather_data, dict)
            and (weather_data.get('is_mock') or weather_data.get('is_demo'))
        )
        try:
            if weather_data and not weather_is_mock:
                _upsert_cache(CANONICAL_LOCATION_NAME, weather_data, fetched_at)
                if update_daily:
                    _upsert_daily(CANONICAL_LOCATION_NAME, weather_data, target_date)
                updated = 1
            if _upsert_nowcast(nowcast, fetched_at):
                nowcast_updated = 1
            if updated or nowcast_updated:
                db.session.commit()
        except Exception as exc:
            logger.exception("Weather cache upsert failed for %s: %s", CANONICAL_LOCATION_NAME, exc)
            db.session.rollback()
            updated = 0
            nowcast_updated = 0

        if not weather_data or weather_is_mock:
            logger.warning(
                "No trustworthy persisted weather data available for %s",
                CANONICAL_LOCATION_NAME,
            )

        snapshot = refresh_snapshot_from_cycle(
            {} if weather_is_mock else weather_data,
            weather_service=weather_service,
            fetched_at=fetched_at,
        )
        persisted = snapshot_payload(snapshot)
        snapshot_ready = bool(
            updated == 1
            and persisted.get('snapshot_id')
            and persisted.get('snapshot_id') != previous_snapshot_id
            and persisted.get('available')
            and not persisted.get('stale', True)
            and not (persisted.get('current') or {}).get('is_mock', False)
        )
        return {
            'locations': 1,
            'requested_locations': len(requested_locations),
            'canonical_location': CANONICAL_LOCATION_NAME,
            'updated': updated,
            'nowcast_updated': nowcast_updated,
            'update_daily': update_daily,
            'snapshot_id': snapshot.snapshot_id if snapshot else None,
            'snapshot_ready': snapshot_ready,
            'snapshot_stale': bool(persisted.get('stale', True)),
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Sync minute-level weather cache.')
    parser.add_argument('--location', action='append', dest='locations', help='Location label override')
    parser.add_argument('--no-daily', action='store_true', help='Skip WeatherData daily upsert')
    args = parser.parse_args(argv)

    result = sync_weather_cache(
        locations=args.locations,
        update_daily=not args.no_daily
    )
    print(f"Cache sync: {result}")
    # systemd 只会在新鲜快照形成后通过 OnSuccess 触发推送。
    return 0 if result.get('snapshot_ready') else 2


if __name__ == '__main__':
    raise SystemExit(main())
