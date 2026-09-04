# -*- coding: utf-8 -*-
"""活天气必须带上热浪/寒潮连续天数，且不得污染非和风数据。"""
from datetime import timedelta

from core.constants import DEFAULT_CITY_LABEL


def _seed_daily(db_session, location, *, temperature_max, temperature_min, days=3):
    from core.db_models import WeatherData
    from core.time_utils import today_local

    today = today_local()
    rows = [
        WeatherData(
            date=today - timedelta(days=offset),
            location=location,
            temperature=temperature_min + 2,
            temperature_max=temperature_max,
            temperature_min=temperature_min,
        )
        for offset in range(1, days + 1)
    ]
    db_session.add_all(rows)
    db_session.commit()
    return today


class _LiveFetcher:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get_current_weather(self, location):
        self.calls += 1
        return dict(self.payload)


def test_consecutive_cold_days_counts_minima_at_or_below_five(app, db_session):
    from core.weather import get_consecutive_cold_days

    location = DEFAULT_CITY_LABEL
    with app.app_context():
        app.config['DEMO_MODE'] = False
        _seed_daily(db_session, location, temperature_max=8, temperature_min=2, days=3)
        count = get_consecutive_cold_days(location, today_min=3, threshold=5, max_days=7)

    assert count == 4


def test_qweather_live_weather_attaches_heat_and_cold_wave_days(app, db_session):
    from core.weather import get_weather_with_cache, register_weather_fetcher

    location = DEFAULT_CITY_LABEL
    payload = {
        'temperature': 36,
        'temperature_max': 36,
        'temperature_min': 3,
        'humidity': 70,
        'data_source': 'QWeather',
        'is_mock': False,
    }
    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.extensions['redis_client'] = None
        _seed_daily(db_session, location, temperature_max=36, temperature_min=1, days=2)
        register_weather_fetcher(_LiveFetcher(payload))
        weather, _from_cache = get_weather_with_cache(location)

    assert weather['heat_wave_days'] == 3
    assert weather['cold_wave_days'] == 3
    assert weather['data_source'] == 'QWeather'


def test_open_meteo_weather_does_not_attach_wave_days(app, db_session):
    from core.weather import get_weather_with_cache, register_weather_fetcher

    location = DEFAULT_CITY_LABEL
    payload = {
        'temperature': 36,
        'temperature_max': 36,
        'temperature_min': 3,
        'humidity': 70,
        'data_source': 'Open-Meteo',
        'is_mock': False,
    }
    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.extensions['redis_client'] = None
        _seed_daily(db_session, location, temperature_max=36, temperature_min=1, days=2)
        register_weather_fetcher(_LiveFetcher(payload))
        weather, _from_cache = get_weather_with_cache(location)

    assert 'heat_wave_days' not in weather
    assert 'cold_wave_days' not in weather
