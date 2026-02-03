# -*- coding: utf-8 -*-
"""Sync minute-level weather cache for all locations."""
import argparse
import json
import logging
import os
from datetime import datetime

from core.app import create_app
from core.constants import DEFAULT_CITY_LABEL  # noqa: E402
from core.db_models import WeatherCache, WeatherData  # noqa: E402
from core.extensions import db  # noqa: E402
from core.weather import get_location_options, normalize_location_name  # noqa: E402
from core.time_utils import today_local, utcnow  # noqa: E402
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
    options = get_location_options()
    if options:
        return options
    return [DEFAULT_CITY_LABEL]


def sync_weather_cache(locations=None, update_daily=True):
    with app.app_context():
        locations = list(_dedupe_locations(_resolve_locations(locations)))
        if not locations:
            locations = list(_dedupe_locations([DEFAULT_CITY_LABEL]))

        weather_service = WeatherService()
        fetched_at = utcnow()
        target_date = today_local()
        updated = 0

        for location in locations:
            try:
                weather_data = weather_service.get_current_weather(location)
            except Exception as exc:
                logger.exception("Weather sync failed for %s: %s", location, exc)
                db.session.rollback()
                continue
            if not weather_data:
                logger.warning("No weather data returned for %s", location)
                continue
            try:
                _upsert_cache(location, weather_data, fetched_at)
                if update_daily:
                    _upsert_daily(location, weather_data, target_date)
                db.session.commit()
            except Exception as exc:
                logger.exception("Weather cache upsert failed for %s: %s", location, exc)
                db.session.rollback()
                continue
            updated += 1
        return {
            'locations': len(locations),
            'updated': updated,
            'update_daily': update_daily
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
