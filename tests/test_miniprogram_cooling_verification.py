"""小程序地图需要服务与坐标双重核验，不新增公开字段。"""
from datetime import datetime, timedelta, timezone

import pytest

from core.db_models import CoolingFeedback, CoolingResource, Pair, User

NOW = datetime(2026, 10, 6, 8, tzinfo=timezone.utc)


def resource(**overrides):
    values = dict(community_code='都昌', name='公共纳凉点', is_active=True,
                  latitude=29.27, longitude=116.20, coordinate_system='GCJ-02',
                  coordinate_source='现场坐标回执', coordinate_verified_at=NOW,
                  last_verified_at=NOW, verify_method='onsite',
                  verified_by_role='admin', verify_status='verified')
    values.update(overrides)
    return CoolingResource(**values)


@pytest.mark.parametrize('verified_at,method,visible', [
    (NOW, 'phone', True),
    (NOW - timedelta(days=30), 'official_doc', True),
    ((NOW - timedelta(days=1)).replace(tzinfo=None), 'onsite', True),
    (NOW - timedelta(days=30, seconds=1), 'onsite', False),
    (NOW + timedelta(seconds=1), 'onsite', False),
    (None, 'onsite', False),
    (NOW, None, False),
    (NOW, 'unreviewed', False),
])
def test_service_status_is_recomputed_before_coordinates_are_public(
        client, db_session, monkeypatch, verified_at, method, visible):
    monkeypatch.setattr('services.miniprogram_service.utcnow', lambda: NOW)
    db_session.add(resource(last_verified_at=verified_at, verify_method=method))
    db_session.commit()
    response = client.get('/mp/api/v1/public/cooling-resources')
    assert response.status_code == 200
    item = response.get_json()['data']['items'][0]
    assert item['name'] == '公共纳凉点'
    assert (item['latitude'] is not None) is visible
    assert (item['longitude'] is not None) is visible
    assert item['coordinate_system'] == ('GCJ-02' if visible else None)
    for private_field in ('coordinate_source', 'verify_method', 'verified_by_role', 'pair_id'):
        assert private_field not in item


def test_two_independent_closed_reports_hide_previously_verified_map_point(
        client, db_session, monkeypatch):
    monkeypatch.setattr('services.miniprogram_service.utcnow', lambda: NOW)
    row = resource(last_verified_at=NOW - timedelta(days=1))
    owner = User(username='mini-cooling-feedback', role='caregiver')
    owner.set_password('test-cooling-password')
    db_session.add_all([row, owner])
    db_session.flush()
    for index in range(2):
        pair = Pair(caregiver_id=owner.id, community_code='都昌',
                    elder_code=f'mini-cooling-{index}', short_code=f'93821{index}', status='active')
        db_session.add(pair)
        db_session.flush()
        db_session.add(CoolingFeedback(resource_id=row.id, pair_id=pair.id,
                                       code='closed', channel='web', created_at=NOW))
    db_session.commit()
    response = client.get('/mp/api/v1/public/cooling-resources')
    assert response.status_code == 200
    item = response.get_json()['data']['items'][0]
    assert item['latitude'] is None and item['longitude'] is None
    assert item['coordinate_system'] is None
