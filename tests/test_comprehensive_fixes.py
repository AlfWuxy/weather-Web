# -*- coding: utf-8 -*-
"""Comprehensive regression tests for security fixes."""
import importlib
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest


def _reload_config():
    if 'core.config' in sys.modules:
        return importlib.reload(sys.modules['core.config'])
    import core.config as config
    return config


def _set_env(env_updates):
    original = {key: os.environ.get(key) for key in env_updates}
    for key, value in env_updates.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return original


def _restore_env(original):
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _nested_dict(levels):
    data = {}
    current = data
    for _ in range(max(levels - 1, 0)):
        current['child'] = {}
        current = current['child']
    return data


def test_db_models_no_datetime_utcnow():
    content = Path('core/db_models.py').read_text(encoding='utf-8')
    assert 'datetime.utcnow' not in content
    assert 'datetime.now(timezone.utc)' in content


def test_utcnow_naive_returns_naive():
    from core.time_utils import utcnow_naive

    now = utcnow_naive()
    assert now.tzinfo is None


def test_from_json_filter_size_limit(app):
    filter_fn = app.jinja_env.filters['from_json']
    payload = json.dumps({'data': 'a' * (10 * 1024 + 1)})
    assert filter_fn(payload) == []


def test_from_json_filter_depth_limit(app):
    filter_fn = app.jinja_env.filters['from_json']
    payload = json.dumps(_nested_dict(6))
    assert filter_fn(payload) == []


def test_from_json_filter_valid_depth(app):
    filter_fn = app.jinja_env.filters['from_json']
    payload = json.dumps(_nested_dict(5))
    parsed = filter_fn(payload)
    assert isinstance(parsed, dict)
    assert 'child' in parsed


def test_api_post_requires_csrf(authenticated_client):
    response = authenticated_client.post(
        '/api/forecast/7day',
        json={'forecast_temps': [10, 11, 12, 13, 14, 15, 16]},
    )
    assert response.status_code == 400

    with authenticated_client.session_transaction() as session:
        session['_csrf_token'] = 'csrf-token'
    response = authenticated_client.post(
        '/api/forecast/7day',
        json={'forecast_temps': [10, 11, 12, 13, 14, 15, 16]},
        headers={'X-CSRF-Token': 'csrf-token'},
    )
    assert response.status_code == 200


def test_short_code_length_8(db_session):
    from services.user._common import _generate_short_code

    code = _generate_short_code()
    assert len(code) == 8
    assert code.isdigit()


def test_redeemed_at_only_set_once(db_session):
    from core.db_models import PairLink, User
    from core.security import hash_pair_token, hash_short_code
    from core.time_utils import utcnow_naive
    from services.public_service import _resolve_pair

    caregiver = User(username='caregiver')
    caregiver.set_password('password123')
    db_session.add(caregiver)
    db_session.commit()

    short_code = '12345678'
    link = PairLink(
        caregiver_id=caregiver.id,
        short_code=short_code,
        short_code_hash=hash_short_code(short_code),
        token_hash=hash_pair_token('token'),
        community_code='test',
        status='active',
        redeemed_at=utcnow_naive() - timedelta(days=1)
    )
    db_session.add(link)
    db_session.commit()

    original_redeemed_at = link.redeemed_at
    pair, error = _resolve_pair(short_code, 'token')
    assert pair is None
    assert error is not None

    refreshed = PairLink.query.get(link.id)
    assert refreshed.redeemed_at == original_redeemed_at


def test_atomic_transaction_rolls_back(db_session):
    from core.db_models import User
    from utils.database import atomic_transaction

    with pytest.raises(RuntimeError):
        with atomic_transaction():
            user = User(username='rollback_user')
            user.set_password('password123')
            db_session.add(user)
            raise RuntimeError('force rollback')

    assert User.query.filter_by(username='rollback_user').first() is None


def test_pairlink_is_expired_property(db_session):
    from core.db_models import PairLink, User
    from core.time_utils import utcnow_naive

    caregiver = User(username='caregiver2')
    caregiver.set_password('password123')
    db_session.add(caregiver)
    db_session.commit()

    link = PairLink(
        caregiver_id=caregiver.id,
        short_code='87654321',
        short_code_hash='hash',
        token_hash='hash',
        community_code='test',
        status='active',
        expires_at=utcnow_naive() - timedelta(days=1)
    )
    db_session.add(link)
    db_session.commit()

    assert link.is_expired is True
    assert link.is_active is False


def test_pair_is_active_property(db_session):
    from core.db_models import Pair, User

    caregiver = User(username='caregiver3')
    caregiver.set_password('password123')
    db_session.add(caregiver)
    db_session.commit()

    pair = Pair(
        caregiver_id=caregiver.id,
        community_code='test',
        elder_code='elder123',
        short_code='11223344',
        short_code_hash='hash',
        status='active'
    )
    db_session.add(pair)
    db_session.commit()

    assert pair.is_active is True
    assert pair.is_expired is False


def test_validate_production_config_missing_secret_key(tmp_path):
    original = _set_env({
        'SECRET_KEY': None,
        'PAIR_TOKEN_PEPPER': 'pepper-1234567890pepper-1234567890',
        'DEBUG': 'false',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod.db'}",
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_validate_production_config_short_secret_key(tmp_path):
    original = _set_env({
        'SECRET_KEY': 'short-key',
        'PAIR_TOKEN_PEPPER': 'pepper-1234567890pepper-1234567890',
        'DEBUG': 'false',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod.db'}",
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_validate_production_config_weak_secret_key(tmp_path):
    original = _set_env({
        'SECRET_KEY': 'dev-secret-key-should-fail-1234567890',
        'PAIR_TOKEN_PEPPER': 'pepper-1234567890pepper-1234567890',
        'DEBUG': 'false',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod.db'}",
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_validate_production_config_missing_pepper(tmp_path):
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': None,
        'DEBUG': 'false',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod.db'}",
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_sqlalchemy_engine_options_sqlite(tmp_path):
    original = _set_env({
        'DATABASE_URI': f"sqlite:///{tmp_path/'engine.db'}",
    })
    try:
        config = _reload_config()
        database_uri = config.resolve_database_uri()
        assert config.resolve_engine_options(database_uri) == {}
    finally:
        _restore_env(original)
        _reload_config()


def test_error_handler_classification():
    from utils.error_handlers import classify_exception

    status, message = classify_exception(ValueError('bad'))
    assert status == 400
    assert message
