# -*- coding: utf-8 -*-
"""Service initialization helpers."""


def init_services(app):
    """Initialize services and register dependency injection hooks."""
    from services.weather_service import WeatherService
    from core.weather import register_weather_fetcher

    with app.app_context():
        weather_service = WeatherService()
        register_weather_fetcher(weather_service)
    return weather_service
