# -*- coding: utf-8 -*-
"""Thin entrypoint that re-exports the Flask app and models."""
from core.app import create_app, db, init_db, main
from core.db_models import (
    AuditLog,
    Community,
    CommunityDaily,
    CoolingResource,
    DailyStatus,
    Debrief,
    FamilyMember,
    FamilyMemberProfile,
    ForecastCache,
    HealthDiary,
    HealthRiskAssessment,
    MedicalRecord,
    MedicationReminder,
    Notification,
    Pair,
    PairLink,
    ShortCodeAttempt,
    User,
    WeatherAlert,
    WeatherCache,
    WeatherData,
)

app = create_app()

__all__ = [
    'app',
    'db',
    'init_db',
    'main',
    'AuditLog',
    'Community',
    'CommunityDaily',
    'CoolingResource',
    'DailyStatus',
    'Debrief',
    'FamilyMember',
    'FamilyMemberProfile',
    'ForecastCache',
    'HealthDiary',
    'HealthRiskAssessment',
    'MedicalRecord',
    'MedicationReminder',
    'Notification',
    'Pair',
    'PairLink',
    'ShortCodeAttempt',
    'User',
    'WeatherAlert',
    'WeatherCache',
    'WeatherData',
]


if __name__ == '__main__':
    main()
