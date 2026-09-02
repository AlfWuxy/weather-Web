# -*- coding: utf-8 -*-
from datetime import date

from core.db_models import Community, FamilyMember, HealthDiary, User, WeatherData


def _seed_health_diary_data(db_session, *, with_weather):
    db_session.add(Community(
        name='测试社区',
        population=1200,
        elderly_ratio=0.31,
        chronic_disease_ratio=0.18,
        vulnerability_index=55.0,
        risk_level='中'
    ))

    user = User.query.filter_by(username='testuser').first()
    user.community = '测试社区'
    db_session.flush()

    member = FamilyMember(user_id=user.id, name='李奶奶', relation='母亲')
    db_session.add(member)
    db_session.flush()

    entry_date = date(2026, 3, 28)
    db_session.add(HealthDiary(
        user_id=user.id,
        member_id=member.id,
        entry_date=entry_date,
        symptoms='头晕',
        severity='轻微'
    ))

    if with_weather:
        db_session.add(WeatherData(
            date=entry_date,
            location='测试社区',
            temperature=31.2,
            humidity=72,
            weather_condition='晴'
        ))

    db_session.commit()


def test_health_diary_page_renders_member_and_weather(authenticated_client, db_session):
    _seed_health_diary_data(db_session, with_weather=True)

    response = authenticated_client.get('/health-diary')
    assert response.status_code == 200

    html = response.get_data(as_text=True)
    assert '健康日记' in html
    assert '李奶奶' in html
    assert '31.2' in html
    assert '72' in html
    assert '头晕' in html


def test_health_diary_page_handles_missing_weather(authenticated_client, db_session):
    _seed_health_diary_data(db_session, with_weather=False)

    response = authenticated_client.get('/health-diary')
    assert response.status_code == 200

    html = response.get_data(as_text=True)
    assert '健康日记' in html
    assert '李奶奶' in html
    assert '头晕' in html
    assert '°C /' not in html


def test_health_diary_rejects_empty_entry(authenticated_client, db_session):
    from core.db_models import HealthDiary

    _seed_health_diary_data(db_session, with_weather=False)
    before = HealthDiary.query.count()

    response = authenticated_client.post(
        '/health-diary',
        data={
            'entry_date': '2026-03-29',
            'csrf_token': 'test-csrf-token',
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '请填写症状、严重程度或备注' in html
    assert HealthDiary.query.count() == before


def test_health_diary_lists_notes(authenticated_client, db_session):
    from core.db_models import HealthDiary, User

    _seed_health_diary_data(db_session, with_weather=False)
    user = User.query.filter_by(username='testuser').first()
    db_session.add(HealthDiary(
        user_id=user.id,
        entry_date=date(2026, 3, 29),
        notes='午睡后头晕好转',
    ))
    db_session.commit()

    response = authenticated_client.get('/health-diary')
    html = response.get_data(as_text=True)
    assert '午睡后头晕好转' in html
    assert '>备注<' in html
