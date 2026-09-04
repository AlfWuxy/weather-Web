# -*- coding: utf-8 -*-
from datetime import datetime, timezone
from types import SimpleNamespace


def _reminder(**kwargs):
    defaults = {
        'medicine_name': '氨氯地平',
        'dosage': '1片',
        'time_of_day': None,
        'weather_triggers': None,
        'last_notified_at': None,
        'is_active': True,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_reminder_is_due_at_scheduled_time_without_weather():
    from core.health_profiles import reminder_is_due

    reminder = _reminder(time_of_day='08:00')
    due, reason = reminder_is_due(
        reminder,
        weather=None,
        now_local=datetime(2026, 6, 1, 8, 0),
    )
    assert due is True
    assert '08:00' in reason


def test_reminder_is_not_due_before_scheduled_time():
    from core.health_profiles import reminder_is_due

    reminder = _reminder(time_of_day='08:00')
    due, reason = reminder_is_due(
        reminder,
        weather=None,
        now_local=datetime(2026, 6, 1, 7, 59),
    )
    assert due is False
    assert reason is None


def test_weather_trigger_still_fires_without_schedule():
    from core.health_profiles import reminder_is_due

    reminder = _reminder(weather_triggers='{"high_temp": 32}')
    weather = SimpleNamespace(temperature=35, humidity=50, aqi=40)
    due, reason = reminder_is_due(
        reminder,
        weather=weather,
        now_local=datetime(2026, 6, 1, 7, 0),
    )
    assert due is True
    assert '高温' in reason


def test_last_notified_uses_local_date_not_utc_date(app):
    from core.health_profiles import reminder_notified_on_local_date

    notified_utc = datetime(2026, 6, 1, 16, 30, tzinfo=timezone.utc)  # 次日 00:30 上海
    reminder = _reminder(last_notified_at=notified_utc)
    with app.app_context():
        assert reminder_notified_on_local_date(reminder, datetime(2026, 6, 2).date()) is True
        assert reminder_notified_on_local_date(reminder, datetime(2026, 6, 1).date()) is False


def test_dashboard_shows_due_medication_even_without_chronic_or_weather(
    authenticated_client,
    db_session,
    monkeypatch,
):
    from core.db_models import MedicationReminder, User

    user = User.query.filter_by(username='testuser').one()
    user.has_chronic_disease = False
    db_session.add(MedicationReminder(
        user_id=user.id,
        medicine_name='氨氯地平',
        dosage='1片',
        time_of_day='08:00',
        is_active=True,
    ))
    db_session.commit()

    fixed = datetime(2026, 6, 1, 9, 15)
    monkeypatch.setattr('services.user.dashboard_service.now_local', lambda: fixed)
    monkeypatch.setattr('services.user.dashboard_service.today_local', lambda: fixed.date())

    response = authenticated_client.get('/dashboard')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '氨氯地平' in html
    assert '08:00' in html
