# -*- coding: utf-8 -*-
"""生产公开产品边界：未知、分灾种、服务核验与坐标核验。"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from core.time_utils import utcnow


def test_public_snapshot_is_preserved_and_hazards_are_separate(client, db_session, monkeypatch):
    monkeypatch.setattr('services.public_service.get_bootstrap_payload', lambda: {
        'location': {'name':'都昌县'}, 'risk': {'available':False},
        'family_reminder': {'date':'2026-10-06', 'message':'保留生产提醒', 'follow_up_question':'今天准备好了吗？'}})
    response = client.get('/risk?location=北京市')
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '保留生产提醒' in body
    assert body.index('data-hazard-card="official"') < body.index('data-hazard-card="heat"')
    for hazard in ['official','heat','rain','cold']:
        assert body.count('data-hazard-card="'+hazard+'"') == 1
    assert '这不表示当前没有预警' in body
    assert '时段雨量预报：未知' in body
    assert '寒冷 · 研究中' in body
    assert '当前风险：低' not in body
    assert 'value="都昌县"' in body
    assert 'value="北京市"' not in body


def test_anon_elders_public_renderer_never_reads_health(app, db_session, monkeypatch):
    from services.public_service import render_public_risk_page
    monkeypatch.setattr('services.public_service.get_bootstrap_payload', lambda: {'location':{'name':'都昌县'},'risk':{'available':False}})
    with app.test_request_context('/elder-mode'):
        body = render_public_risk_page(None, elder_mode=True)
    assert 'public-elder' in body and '大字版' in body
    assert 'data-rain-card' in body


@pytest.mark.parametrize('days,method,expected', [(0,'phone','verified'),(31,'phone','stale'),(-1,'phone','unverified'),(0,None,'unverified')])
def test_resource_service_verification_boundaries(monkeypatch, days, method, expected):
    from services import cooling_service
    monkeypatch.setattr(cooling_service, '_closed_pair_ids_after_verification', lambda _:set())
    now=utcnow()
    resource=SimpleNamespace(last_verified_at=now-timedelta(days=days),verify_method=method)
    assert cooling_service.compute_verify_status(resource,now)==expected


def test_two_closed_reports_override_fresh_verification(monkeypatch):
    from services import cooling_service
    monkeypatch.setattr(cooling_service, '_closed_pair_ids_after_verification', lambda _:{1,2})
    resource=SimpleNamespace(last_verified_at=utcnow(),verify_method='onsite')
    assert cooling_service.compute_verify_status(resource)=='closed_reported'


@pytest.mark.parametrize('status,coordinate_system,expected', [('stale','GCJ-02',False),('unverified','GCJ-02',False),('closed_reported','GCJ-02',False),('verified','WGS84',False),('verified','GCJ-02',True)])
def test_map_requires_both_service_and_coordinate_verification(app, monkeypatch, status, coordinate_system, expected):
    from services import public_service
    monkeypatch.setattr(public_service,'compute_verify_status',lambda _:status)
    resource=SimpleNamespace(id=1,name='核验场所',community_code='都昌',resource_type='社区',address_hint='公共地址',
        open_hours='9–17',has_ac=None,is_accessible=None,latitude=29.27,longitude=116.20,
        coordinate_verified_at=utcnow(),coordinate_system=coordinate_system,coordinate_source='manual')
    with app.app_context(): point=public_service._verified_cooling_map_point(resource)
    assert bool(point) is expected


def test_home_claims_only_heat_and_keeps_guest_care_path(client, db_session):
    body=client.get('/').get_data(as_text=True)
    assert '寒潮、气压变化' not in body
    assert '寒冷健康评估仍在研究中' in body
    assert '免登录查看今日天气' in body
    assert '/guest' in body


def test_cooling_map_excludes_expired_unknown_and_closed_service(client, db_session, monkeypatch):
    import json
    import re
    from core.db_models import CoolingResource, CoolingFeedback, Pair, User
    monkeypatch.setattr('services.public_service.get_weather_with_cache',lambda _:({},False))
    now=utcnow()
    rows=[]
    for name,days,method in [('有效场所',1,'onsite'),('过期场所',31,'onsite'),('仅坐标核验',None,None),('关闭反馈',1,'phone')]:
        row=CoolingResource(community_code='都昌',name=name,is_active=True,latitude=29.27,longitude=116.20,
            coordinate_system='GCJ-02',coordinate_source='manual',coordinate_verified_at=now,
            last_verified_at=now-timedelta(days=days) if days is not None else None,verify_method=method)
        db_session.add(row);rows.append(row)
    owner=User(username='cooling-feedback-owner',role='caregiver');owner.set_password('cooling-test-password');db_session.add(owner);db_session.flush()
    for i in range(2):
        pair=Pair(caregiver_id=owner.id,community_code='都昌',location_query='都昌',elder_code=f'cooling-{i}',short_code=f'7777777{i}',short_code_hash=f'test-hash-{i}',status='active')
        db_session.add(pair);db_session.flush()
        db_session.add(CoolingFeedback(resource_id=rows[-1].id,pair_id=pair.id,code='closed',channel='web',created_at=now))
    db_session.commit()
    response=client.get('/cooling')
    body=response.get_data(as_text=True)
    assert response.status_code==200
    points=json.loads(re.search(r'<script id="coolingMapData" type="application/json">(.*?)</script>',body,re.S).group(1))
    assert [point['name'] for point in points]==['有效场所']
    pending=body.split('data-resource-status="pending"',1)[1]
    for name in ['过期场所','仅坐标核验','关闭反馈']:
        assert name in pending
    assert '核验已超过 30 天' in pending
    assert '有用户反馈已关闭，待复核' in pending


def test_production_agriculture_nav_and_inline_nonce_survive_port(app, db_session):
    from flask import render_template
    from flask_login import login_user
    from core.db_models import User
    if 'yilao_community_workbench.page' not in app.view_functions:
        app.add_url_rule('/agriculture', endpoint='yilao_community_workbench.page', view_func=lambda:'test')
    app.config['YILAO_AGRICULTURE_WORKBENCH_ENABLED']=True
    actor=User(username='agriculture-port-actor',role='caregiver');actor.set_password('test-password')
    db_session.add(actor);db_session.commit()
    with app.test_request_context('/risk'):
        login_user(actor)
        body=render_template('base.html',agriculture_nonce='test-agriculture-nonce')
    assert body.count('data-nav-key="agriculture"')==2
    assert '<script nonce="test-agriculture-nonce">' in body
    assert '<style nonce="test-agriculture-nonce">' in body
    assert '机器学习分类 · 已停用' not in body or 'href="/ml-prediction"' not in body
