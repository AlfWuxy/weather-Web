# -*- coding: utf-8 -*-
"""权限与降级修复回归测试。"""


def test_analysis_route_redirects_non_admin(authenticated_client):
    response = authenticated_client.get('/analysis/heatmap', follow_redirects=False)

    assert response.status_code == 302
    assert '/dashboard' in response.headers.get('Location', '')


def test_logout_requires_post(authenticated_client):
    response = authenticated_client.get('/logout', follow_redirects=False)

    assert response.status_code == 405


def test_forecast_api_rejects_incomplete_forecast_temps(authenticated_client):
    with authenticated_client.session_transaction() as session:
        session['_csrf_token'] = 'forecast-csrf'

    response = authenticated_client.post(
        '/api/forecast/7day',
        json={'forecast_temps': [10, 11, 12]},
        headers={'X-CSRF-Token': 'forecast-csrf'},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload['error'] == 'invalid_forecast_temps_length'


def test_resolve_location_fallback_uses_default_city_name(app):
    from services.location_resolver import resolve_location

    with app.app_context():
        resolved = resolve_location('南昌西湖区')

    assert resolved['provider'] == 'fallback'
    assert resolved['display_name'] == app.config['DEFAULT_CITY']
