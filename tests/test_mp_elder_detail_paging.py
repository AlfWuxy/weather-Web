"""分页后详情定位与归属边界回归，禁止调用真实天气或地理服务。"""
import pytest


@pytest.fixture
def elder_details(app, db_session, monkeypatch):
    from core.db_models import FamilyMember, Pair, User
    from core.usage import create_api_token
    owner = User(username='paging-owner', password_hash='unused')
    other = User(username='paging-other', password_hash='unused')
    db_session.add_all([owner, other])
    db_session.flush()
    pairs = []
    for number in range(27):
        caregiver = other if number == 25 else owner
        member = FamilyMember(user_id=caregiver.id, name=f'家人{number}')
        db_session.add(member)
        db_session.flush()
        pair = Pair(caregiver_id=caregiver.id, member_id=member.id, community_code='九江',
                    elder_code=f'detail-{number}', short_code=f'd{number}',
                    status='inactive' if number == 26 else 'active')
        db_session.add(pair)
        pairs.append(pair)
    db_session.commit()
    calls = []
    monkeypatch.setattr('blueprints.mp_api.resolve_location', lambda label: calls.append(label) or {'location_code': 'test-only'})
    monkeypatch.setattr('blueprints.mp_api.get_weather_with_cache', lambda _location: ({'is_mock': True}, True))
    app.config['PAIR_LIST_PAGE_SIZE'] = 20
    return {'pairs': [pair.id for pair in pairs], 'calls': calls,
            'headers': {'Authorization': f'Bearer {create_api_token(owner.id)}'}}


def test_detail_lookup_reaches_historical_pair_beyond_first_twenty(client, elder_details):
    headers = elder_details['headers']
    target = elder_details['pairs'][0]
    first = client.get('/mp/api/v1/elders', headers=headers).get_json()
    assert len(first['data']) == 20 and first['has_more'] is True
    assert target not in {row['pair_id'] for row in first['data']}
    second = client.get('/mp/api/v1/elders?page=2', headers=headers).get_json()
    assert len(second['data']) == 5 and second['has_more'] is False
    elder_details['calls'].clear()
    detail = client.get(f'/mp/api/v1/elders?pair_id={target}&page=2', headers=headers)
    assert detail.status_code == 200
    body = detail.get_json()
    assert [row['pair_id'] for row in body['data']] == [target]
    assert body['data'][0]['member']['name'] == '家人0'
    assert body['page'] == 1 and body['has_more'] is False
    assert len(elder_details['calls']) == 1


@pytest.mark.parametrize('target_index', [25, 26, None])
def test_detail_lookup_cannot_read_foreign_inactive_or_missing_pair(client, elder_details, target_index):
    target = elder_details['pairs'][target_index] if target_index is not None else 9223372036854775807
    response = client.get(f'/mp/api/v1/elders?pair_id={target}', headers=elder_details['headers'])
    assert response.status_code == 200
    assert response.get_json()['data'] == []
    assert response.get_json()['has_more'] is False
    assert elder_details['calls'] == []
    assert client.get(f'/mp/api/v1/elders?pair_id={target}').status_code == 401


@pytest.mark.parametrize('pair_id', ['', '0', '-1', 'text', '1.5', '9223372036854775808', '1' * 100])
def test_detail_lookup_rejects_invalid_ids_before_provider_work(client, elder_details, pair_id):
    response = client.get('/mp/api/v1/elders', query_string={'pair_id': pair_id}, headers=elder_details['headers'])
    assert response.status_code == 400
    assert response.get_json()['error'] == 'invalid_pair_id'
    assert elder_details['calls'] == []
