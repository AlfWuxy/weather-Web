# -*- coding: utf-8 -*-
"""历史分析页的日期与缺失天气回归测试。"""
from datetime import date

from core.db_models import WeatherData


def test_history_swaps_inverted_dates_and_keeps_metric_missing_values_independent(
    admin_client,
    db_session,
    monkeypatch,
):
    location = '历史缺失天气社区'
    db_session.add_all([
        WeatherData(
            date=date(2025, 1, 1),
            location=location,
            temperature=30.0,
            humidity=None,
        ),
        WeatherData(
            date=date(2025, 1, 2),
            location=location,
            temperature=None,
            humidity=50.0,
        ),
        WeatherData(
            date=date(2025, 1, 3),
            location=location,
            temperature=None,
            humidity=None,
        ),
    ])
    db_session.commit()

    captured = {}

    def capture_template(template_name, **context):
        captured['template_name'] = template_name
        captured.update(context)
        return 'ok'

    monkeypatch.setattr('blueprints.analysis.render_template', capture_template)

    response = admin_client.post(
        '/analysis/history',
        data={
            'start_date': '2025-01-03',
            'end_date': '2025-01-01',
            'community': location,
            'csrf_token': 'test-csrf-token',
        },
    )

    assert response.status_code == 200
    assert captured['template_name'] == 'analysis_history.html'
    assert captured['start_date'] == '2025-01-01'
    assert captured['end_date'] == '2025-01-03'
    assert captured['temperatures'] == [30.0, None, None]
    assert captured['humidities'] == [None, 50.0, None]
    assert captured['temp_n'] == 1
    assert captured['hum_n'] == 1
    assert captured['data_summary']['weather_days'] == 2
