# -*- coding: utf-8 -*-
"""Forecast service regression tests."""


def test_normalize_forecast_entry_preserves_zero_p50():
    from services.forecast_service import ForecastService

    service = ForecastService()
    normalized = service._normalize_forecast_entry({
        'temperature_ensemble_p50': 0,
        'temperature_ensemble_mean': 5,
        'temperature': 8,
    })

    assert normalized['temperature_p50'] == 0.0
    assert normalized['temp'] == 0.0
