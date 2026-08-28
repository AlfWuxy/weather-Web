# -*- coding: utf-8 -*-
"""照护端 confirmed_at 的 UTC 到应用时区显示回归测试。"""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask

from core import time_utils
from core.db_models import DailyStatus, Pair, User
from core.security import hash_short_code
from core.time_utils import today_local


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXED_UTC_NAIVE = datetime(2026, 8, 9, 4, 0)


def _login_as(client, user_id):
    with client.session_transaction() as session:
        session['_user_id'] = f'{user_id}:1'
        session['_fresh'] = True
        session['_csrf_token'] = 'test-csrf-token'


def _create_confirmed_pair(db_session, suffix, confirmed_at=FIXED_UTC_NAIVE):
    user = User(username=f'timezone_caregiver_{suffix}', role='caregiver')
    user.set_password('timezone-display-test-password')
    db_session.add(user)
    db_session.flush()

    short_code = f'86420{suffix:03d}'
    pair = Pair(
        caregiver_id=user.id,
        community_code='都昌',
        location_query='都昌',
        elder_code=f'timezone-elder-{suffix}',
        short_code=short_code,
        short_code_hash=hash_short_code(short_code),
        status='active',
        created_at=datetime(2026, 8, 9, 3, 15),
        last_active_at=datetime(2026, 8, 9, 3, 30),
    )
    db_session.add(pair)
    db_session.flush()
    db_session.add(DailyStatus(
        pair_id=pair.id,
        status_date=today_local(),
        community_code='都昌',
        confirmed_at=confirmed_at,
        actions_done_count=1,
    ))
    db_session.commit()
    return user, pair


def _patch_caregiver_weather(monkeypatch):
    from services.user import caregiver_service

    monkeypatch.setattr(
        caregiver_service,
        'resolve_location',
        lambda _label: {
            'location_code': '101240201',
            'display_name': '都昌',
        },
    )
    monkeypatch.setattr(
        caregiver_service,
        'get_weather_with_cache',
        lambda _location: ({}, False),
    )


@pytest.mark.parametrize(
    'source',
    (
        FIXED_UTC_NAIVE,
        FIXED_UTC_NAIVE.replace(tzinfo=timezone.utc),
    ),
)
def test_utc_to_local_datetime_accepts_naive_and_aware_utc(source):
    """数据库返回 naive 或 aware UTC 时都应显示为应用本地时间。"""
    app = Flask(__name__)
    app.config['APP_TIMEZONE'] = 'Asia/Shanghai'

    with app.app_context():
        converted = time_utils.utc_to_local_datetime(source)

    assert converted == datetime(2026, 8, 9, 12, 0)
    assert converted.tzinfo is None


def test_utc_to_local_datetime_honors_non_default_app_timezone():
    """转换必须读取 APP_TIMEZONE，不能把默认东八区写死。"""
    app = Flask(__name__)
    app.config['APP_TIMEZONE'] = 'Asia/Tokyo'

    with app.app_context():
        converted = time_utils.utc_to_local_datetime(
            FIXED_UTC_NAIVE.replace(tzinfo=timezone.utc)
        )

    assert converted == datetime(2026, 8, 9, 13, 0)


def test_utc_to_local_datetime_crosses_into_next_local_day():
    """UTC 当日下午应能正确跨入东八区次日。"""
    app = Flask(__name__)
    app.config['APP_TIMEZONE'] = 'Asia/Shanghai'

    with app.app_context():
        converted = time_utils.utc_to_local_datetime(
            datetime(2026, 8, 9, 16, 30, tzinfo=timezone.utc)
        )

    assert converted == datetime(2026, 8, 10, 0, 30)


@pytest.mark.parametrize(
    ('confirmed_at', 'expected_display'),
    (
        (FIXED_UTC_NAIVE, '12:00'),
        (datetime(2026, 8, 9, 16, 30), '00:30'),
    ),
    ids=('same-local-day', 'next-local-day'),
)
def test_caregiver_dashboard_displays_confirmed_at_in_app_timezone(
    app,
    client,
    db_session,
    monkeypatch,
    confirmed_at,
    expected_display,
):
    """照护列表应把数据库 UTC 确认时间显示为 APP_TIMEZONE。"""
    app.config['APP_TIMEZONE'] = 'Asia/Shanghai'
    user, _pair = _create_confirmed_pair(
        db_session,
        1,
        confirmed_at=confirmed_at,
    )
    _login_as(client, user.id)
    _patch_caregiver_weather(monkeypatch)

    response = client.get('/caregiver', follow_redirects=True)

    assert response.status_code == 200
    assert response.history
    assert response.history[0].headers['Location'].endswith('/pairs')
    assert (
        f'<div class="text-muted small">{expected_display}</div>'
        in response.get_data(as_text=True)
    )


@pytest.mark.parametrize(
    ('confirmed_at', 'expected_display'),
    (
        (FIXED_UTC_NAIVE, '12:00'),
        (datetime(2026, 8, 9, 16, 30), '00:30'),
    ),
    ids=('same-local-day', 'next-local-day'),
)
def test_caregiver_pair_detail_displays_confirmed_at_in_app_timezone(
    app,
    client,
    db_session,
    confirmed_at,
    expected_display,
):
    """照护详情应把数据库 UTC 确认时间显示为 APP_TIMEZONE。"""
    app.config['APP_TIMEZONE'] = 'Asia/Shanghai'
    user, pair = _create_confirmed_pair(
        db_session,
        2,
        confirmed_at=confirmed_at,
    )
    _login_as(client, user.id)

    response = client.get(f'/caregiver/pair/{pair.id}')

    assert response.status_code == 200
    assert (
        f'确认时间：</span>{expected_display}</div>'
        in response.get_data(as_text=True)
    )


def test_confirmed_at_templates_never_format_database_utc_directly():
    """模板只消费服务层准备好的本地显示值。"""
    pair_management = (
        PROJECT_ROOT / 'templates' / 'pair_management.html'
    ).read_text(encoding='utf-8')
    caregiver_detail = (
        PROJECT_ROOT / 'templates' / 'caregiver_pair_detail.html'
    ).read_text(encoding='utf-8')

    assert "status.confirmed_at.strftime('%H:%M')" not in pair_management
    assert "status_today.confirmed_at.strftime('%H:%M')" not in caregiver_detail
    assert 'item.confirmed_at_local_display' in pair_management
    assert 'confirmed_at_local_display' in caregiver_detail


def test_profile_displays_account_times_in_app_timezone(
    app,
    client,
    db_session,
):
    """个人资料页的注册与最后登录时间都应由 UTC 转成应用时区。"""
    app.config['APP_TIMEZONE'] = 'Asia/Shanghai'
    user = User(
        username='profile_timezone_user',
        role='user',
        created_at=datetime(2026, 8, 9, 16, 30),
        last_login=datetime(2026, 8, 9, 4, 0),
    )
    user.set_password('profile-timezone-test-password')
    db_session.add(user)
    db_session.commit()
    _login_as(client, user.id)

    response = client.get('/profile')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '2026-08-10' in body
    assert '2026-08-09 12:00' in body


def test_profile_template_never_formats_database_utc_directly():
    """个人资料模板只消费服务层准备好的本地时间。"""
    profile_template = (
        PROJECT_ROOT / 'templates' / 'profile.html'
    ).read_text(encoding='utf-8')

    assert 'current_user.created_at.strftime' not in profile_template
    assert 'current_user.last_login.strftime' not in profile_template
    assert 'created_at_local.strftime' in profile_template
    assert 'last_login_local.strftime' in profile_template
