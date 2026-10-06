# -*- coding: utf-8 -*-
"""仪表盘不得重新展示未授权的遗留健康资料。"""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask_login import login_user

from core.db_models import FamilyMember, FamilyMemberProfile, HealthRiskAssessment, MedicationReminder, User
from core.time_utils import utcnow
from services.account_service import grant_health_consent


@pytest.fixture
def dashboard_capture(app, db_session, monkeypatch):
    from services.user import dashboard_service as service
    triggered = []
    weather = {'temperature': 32, 'temperature_max': 35, 'temperature_min': 27,
               'humidity': 65, 'pressure': 1000, 'wind_speed': 1, 'weather_condition': '晴',
               'aqi': 70, 'pm25': 20, 'data_source': 'QWeather', 'is_mock': False,
               'observed_at': utcnow().isoformat(), 'quality_version': 1}
    monkeypatch.setattr(service, 'ensure_user_location_valid', lambda:'都昌县')
    monkeypatch.setattr(service, 'get_weather_with_cache', lambda _: (weather, True))
    monkeypatch.setattr(service, '_dashboard_forecast_days', lambda *a:[])
    monkeypatch.setattr(service, '_dashboard_visible_alerts', lambda *a, **kw:[])
    monkeypatch.setattr(service, 'get_consecutive_hot_days', lambda *a, **kw:1)
    monkeypatch.setattr('services.weather_service.WeatherService.identify_extreme_weather',
                        lambda *a:{'is_extreme':False,'conditions':[]})
    monkeypatch.setattr(service, 'reminder_triggered',
                        lambda row, _: (triggered.append(row.medicine_name) is None, '天气提醒'))
    monkeypatch.setattr(service, 'render_template', lambda name, **context: context)
    app.config['FEATURE_NOTIFICATIONS'] = False
    def render(user, elder=False):
        with app.test_request_context('/elder-mode' if elder else '/dashboard'):
            login_user(user)
            return service.user_dashboard(force_elder=elder)
    return render, triggered


def _legacy_records(db_session, *, self_consent=False, member_consent=False, old_version=False):
    user=User(username='legacy-dashboard-owner',role='user',has_chronic_disease=True,chronic_diseases='高血压')
    user.set_password('test-password')
    db_session.add(user);db_session.flush()
    member=FamilyMember(user_id=user.id,name='遗留成员',relation='父亲',age=78,chronic_diseases='高血压')
    db_session.add(member);db_session.flush()
    if self_consent: grant_health_consent(user,user)
    if member_consent: grant_health_consent(member,user)
    if old_version:
        user.health_sensitive_consent_version='expired-privacy-version'
        member.health_sensitive_consent_version='expired-privacy-version'
    profile=FamilyMemberProfile(member_id=member.id,
        metrics=json.dumps({'blood_pressure':'191/102'}),
        contact_prefs=json.dumps({'emergency_name':'私有联系人','emergency_phone':'13800138000'}))
    db_session.add(profile)
    own=HealthRiskAssessment(user_id=user.id,risk_score=99,risk_level='高风险',
        assessment_date=utcnow()-timedelta(hours=2),recommendations=json.dumps([{'advice':'私有本人建议'}]))
    relative=HealthRiskAssessment(user_id=user.id,member_id=member.id,risk_score=98,risk_level='高风险',
        assessment_date=utcnow()-timedelta(hours=1),recommendations=json.dumps([{'advice':'私有成员建议'}]))
    self_med=MedicationReminder(user_id=user.id,medicine_name='本人私有药',is_active=True)
    member_med=MedicationReminder(user_id=user.id,member_id=member.id,medicine_name='成员私有药',is_active=True)
    db_session.add_all([own,relative,self_med,member_med]);db_session.commit()
    return user,member,own,relative,self_med,member_med


@pytest.mark.parametrize('old_version',[False,True])
def test_no_current_consent_hides_old_assessments_and_never_processes_medications(db_session,dashboard_capture,old_version):
    user,_,_,_,self_med,member_med=_legacy_records(db_session,self_consent=old_version,member_consent=old_version,old_version=old_version)
    render,triggered=dashboard_capture
    data=render(user)
    assert data['assessment'] is None and data['assessment_explain']=={}
    assert data['dashboard_metric_cards']==[] and data['reminders']==[]
    elder=render(user,elder=True)
    assert elder['assessment'] is None and elder['elder_actions']==[] and elder['emergency_contact'] is None
    assert triggered==[]
    assert self_med.last_notified_at is None and member_med.last_notified_at is None


@pytest.mark.parametrize('self_consent,member_consent',[(True,False),(False,True),(True,True)])
def test_consent_is_per_subject_not_inherited_from_owner(db_session,dashboard_capture,self_consent,member_consent):
    user,member,own,relative,self_med,member_med=_legacy_records(db_session,self_consent=self_consent,member_consent=member_consent)
    render,triggered=dashboard_capture
    data=render(user)
    expected=relative if member_consent else own
    assert data['assessment'].id==expected.id
    names={row['medicine_name'] for row in data['reminders']}
    assert ('本人私有药' in names)==self_consent
    assert ('成员私有药' in names)==member_consent
    assert bool(data['dashboard_metric_cards'])==member_consent
    assert (self_med.last_notified_at is not None)==self_consent
    assert (member_med.last_notified_at is not None)==member_consent
    elder=render(user,elder=True)
    assert bool(elder['emergency_contact'])==member_consent


def test_owner_consent_does_not_authorize_another_owners_member(db_session,dashboard_capture):
    user,member,own,_,_,_=_legacy_records(db_session,self_consent=True)
    other=User(username='other-dashboard-owner',role='user');other.set_password('test-password')
    db_session.add(other);db_session.flush()
    member.user_id=other.id
    grant_health_consent(member,other)
    db_session.commit()
    render,triggered=dashboard_capture
    data=render(user)
    assert data['assessment'].id==own.id
    assert [row['medicine_name'] for row in data['reminders']]==['本人私有药']
    assert data['dashboard_metric_cards']==[]


def test_guest_keeps_isolated_example_assessment(db_session,dashboard_capture,monkeypatch):
    from core.guest import GuestUser
    _legacy_records(db_session)
    example=SimpleNamespace(explain=None,recommendations='[]')
    monkeypatch.setattr('services.user.dashboard_service.get_guest_assessment',lambda:example)
    render,triggered=dashboard_capture
    data=render(GuestUser('guest_consent_example',{}))
    assert data['assessment'] is example
    assert data['dashboard_metric_cards']==[] and data['reminders']==[]
    assert triggered==[]
