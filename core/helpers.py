# -*- coding: utf-8 -*-
"""Compatibility re-exports for helper utilities."""
from core.analytics import get_high_risk_streak, pearson_corr
from core.audit import log_audit
from core.health_profiles import (
    _build_member_profile_form_payload,
    _parse_chronic_diseases_from_form,
    compute_member_risk,
    compute_profile_completion,
    member_weather_triggered,
    profile_to_context,
    reminder_triggered
)
from core.notifications import _notification_daily_count, create_notification
from core.security import csrf_failure_response, generate_csrf_token, rate_limit_key
from core.weather import (
    ensure_user_location_valid,
    get_fallback_weather_data,
    get_forecast_with_cache,
    get_location_options,
    get_user_location_value,
    get_weather_with_cache,
    normalize_location_name,
    resolve_weather_city_label
)

__all__ = [
    '_build_member_profile_form_payload',
    '_notification_daily_count',
    '_parse_chronic_diseases_from_form',
    'compute_member_risk',
    'compute_profile_completion',
    'create_notification',
    'csrf_failure_response',
    'ensure_user_location_valid',
    'generate_csrf_token',
    'get_fallback_weather_data',
    'get_forecast_with_cache',
    'get_high_risk_streak',
    'get_location_options',
    'get_user_location_value',
    'get_weather_with_cache',
    'log_audit',
    'member_weather_triggered',
    'normalize_location_name',
    'pearson_corr',
    'profile_to_context',
    'rate_limit_key',
    'reminder_triggered',
    'resolve_weather_city_label'
]
