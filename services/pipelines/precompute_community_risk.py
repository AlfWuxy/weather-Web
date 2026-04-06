# -*- coding: utf-8 -*-
"""周期性预热社区风险分析缓存。"""
import argparse
import logging
import os

from core.app import create_app
from core.constants import DEFAULT_CITY_LABEL  # noqa: E402
from core.time_utils import today_local  # noqa: E402
from core.weather import get_weather_with_cache, normalize_location_name  # noqa: E402
from services.community_risk_cache import get_or_build_community_risk_result  # noqa: E402
from services.community_risk_service import get_community_service  # noqa: E402

logger = logging.getLogger(__name__)


def _weather_signature(weather_data):
    """提取会影响社区风险结果的关键天气字段。"""
    if not isinstance(weather_data, dict):
        return {}

    signature = {}
    for key in (
        'temperature',
        'temperature_max',
        'temperature_min',
        'humidity',
        'aqi',
        'weather_condition',
        'wind_speed',
    ):
        value = weather_data.get(key)
        if isinstance(value, float):
            signature[key] = round(value, 3)
        elif value is not None:
            signature[key] = value
    return signature


def _normalize_disease_filter(value):
    value = (value or '').strip()
    return '' if value in ('', 'all', '全部') else value


def _parse_csv_items(raw):
    if not raw:
        return []
    return [item.strip() for item in str(raw).split(',') if item and item.strip()]


def _resolve_locations():
    env_locations = _parse_csv_items(os.getenv('COMMUNITY_RISK_PRECOMPUTE_LOCATIONS', '').strip())
    if env_locations:
        raw_locations = env_locations
    else:
        default_city = os.getenv('DEFAULT_CITY', DEFAULT_CITY_LABEL) or DEFAULT_CITY_LABEL
        raw_locations = [default_city]

    locations = []
    seen = set()
    for location in raw_locations:
        normalized = normalize_location_name(location)
        if normalized in seen:
            continue
        seen.add(normalized)
        locations.append(normalized)
    return locations or [normalize_location_name(DEFAULT_CITY_LABEL)]


def _resolve_window_days_list(window_days_list=None):
    if window_days_list:
        raw_values = window_days_list
    else:
        raw_values = _parse_csv_items(os.getenv('COMMUNITY_RISK_PRECOMPUTE_WINDOW_DAYS', '30'))

    result = []
    for value in raw_values:
        try:
            parsed = max(7, min(int(value), 120))
        except (TypeError, ValueError):
            continue
        if parsed not in result:
            result.append(parsed)
    return result or [30]


def _resolve_disease_filters(disease_filters=None):
    raw_values = disease_filters if disease_filters is not None else _parse_csv_items(
        os.getenv('COMMUNITY_RISK_PRECOMPUTE_DISEASES', '')
    )

    result = []
    if not raw_values:
        return ['']
    for value in raw_values:
        normalized = _normalize_disease_filter(value)
        if normalized not in result:
            result.append(normalized)
    return result or ['']


def precompute_community_risk(app=None, locations=None, window_days_list=None, disease_filters=None, analysis_date=None):
    """预热社区风险默认结果缓存。"""
    app = app or create_app(register_blueprints=False)

    with app.app_context():
        target_date = analysis_date or today_local()
        locations = locations or _resolve_locations()
        window_days_list = _resolve_window_days_list(window_days_list)
        disease_filters = _resolve_disease_filters(disease_filters)
        community_service = get_community_service()

        summary = {
            'analysis_date': str(target_date),
            'locations': locations,
            'window_days_list': window_days_list,
            'disease_filters': disease_filters,
            'weather_cache_hits': 0,
            'risk_cache_hits': 0,
            'computed': 0,
            'combinations': 0,
        }

        for location in locations:
            weather_data, weather_from_cache = get_weather_with_cache(location)
            if weather_from_cache:
                summary['weather_cache_hits'] += 1

            cache_weather = _weather_signature(weather_data)
            for window_days in window_days_list:
                for disease_filter in disease_filters:
                    disease_filter = _normalize_disease_filter(disease_filter)
                    cache_params = {
                        'version': 1,
                        'analysis_date': target_date.isoformat(),
                        'window_days': int(window_days),
                        'disease_filter': disease_filter,
                        'weather': cache_weather,
                    }

                    def _build_result():
                        return community_service.generate_community_risk_map(
                            weather_data,
                            target_date=target_date,
                            window_days=window_days,
                            disease_filter=disease_filter
                        )

                    _payload, cache_hit = get_or_build_community_risk_result(cache_params, _build_result)
                    summary['combinations'] += 1
                    if cache_hit:
                        summary['risk_cache_hits'] += 1
                    else:
                        summary['computed'] += 1

        return summary


def main():
    parser = argparse.ArgumentParser(description='Precompute community risk cache.')
    parser.add_argument('--location', action='append', dest='locations', help='Location override')
    parser.add_argument('--window-days', action='append', dest='window_days_list', help='Window days override')
    parser.add_argument('--disease', action='append', dest='disease_filters', help='Disease filter override')
    args = parser.parse_args()

    result = precompute_community_risk(
        locations=args.locations,
        window_days_list=args.window_days_list,
        disease_filters=args.disease_filters,
    )
    print(f"Community risk precompute: {result}")


if __name__ == '__main__':
    main()
