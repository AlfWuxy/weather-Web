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
    if env_updates.get('DEBUG') == 'false':
        assert 'WECHAT_FORMAL_RUNTIME' in env_updates
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
    from core.extensions import db
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

    refreshed = db.session.get(PairLink, link.id)
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
        'WECHAT_FORMAL_RUNTIME': '0',
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
        'WECHAT_FORMAL_RUNTIME': '0',
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
        'WECHAT_FORMAL_RUNTIME': '0',
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
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod.db'}",
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


@pytest.mark.parametrize(
    'pepper',
    [
        'x',
        'dev-pepper-value-123456789012345678901234567890',
        'your-pair-token-pepper-here',
    ],
)
def test_validate_production_config_rejects_weak_pair_token_pepper(tmp_path, pepper):
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': pepper,
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_weak_pepper.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match='PAIR_TOKEN_PEPPER'):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


@pytest.mark.parametrize(
    ('first_key', 'second_key'),
    [
        ('PAIR_TOKEN_PEPPER', 'SECRET_KEY'),
        ('PAIR_TOKEN_PEPPER', 'WX_MINIPROGRAM_OPENID_PEPPER'),
        ('PAIR_TOKEN_PEPPER', 'WX_MINIPROGRAM_SESSION_SECRET'),
        ('SECRET_KEY', 'WX_MINIPROGRAM_OPENID_PEPPER'),
        ('SECRET_KEY', 'WX_MINIPROGRAM_SESSION_SECRET'),
        ('WX_MINIPROGRAM_OPENID_PEPPER', 'WX_MINIPROGRAM_SESSION_SECRET'),
        ('ACCOUNT_LINK_CODE_PEPPER', 'PAIR_TOKEN_PEPPER'),
        ('ACCOUNT_LINK_CODE_PEPPER', 'SECRET_KEY'),
        ('ACCOUNT_LINK_CODE_PEPPER', 'WX_MINIPROGRAM_OPENID_PEPPER'),
        ('ACCOUNT_LINK_CODE_PEPPER', 'WX_MINIPROGRAM_SESSION_SECRET'),
        ('WXPUSHER_BINDING_PEPPER', 'PAIR_TOKEN_PEPPER'),
        ('WXPUSHER_BINDING_PEPPER', 'SECRET_KEY'),
        ('WXPUSHER_BINDING_PEPPER', 'ACCOUNT_LINK_CODE_PEPPER'),
        ('WXPUSHER_BINDING_PEPPER', 'WX_MINIPROGRAM_OPENID_PEPPER'),
        ('WXPUSHER_BINDING_PEPPER', 'WX_MINIPROGRAM_SESSION_SECRET'),
    ],
)
def test_validate_production_config_requires_pairwise_independent_secrets(
    tmp_path,
    first_key,
    second_key,
):
    env = {
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'pairpepper1234567890abcdefghijklmn',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '1',
        'WEB_PRIVATE_FEATURES_ENABLED': '1',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_duplicate_pepper.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'WX_MINIPROGRAM_APPID': 'wx-production-appid',
        'WX_MINIPROGRAM_SECRET': 'wx-production-secret-value',
        'WX_MINIPROGRAM_OPENID_PEPPER': 'openidpepper1234567890abcdefghijklm',
        'WX_MINIPROGRAM_SESSION_SECRET': 'sessionvalue1234567890abcdefghijkl',
        'ACCOUNT_LINK_CODE_PEPPER': 'accountlink1234567890abcdefghijkl',
        'PUBLIC_BASE_URL': 'https://yilaoweather.org',
        'WXPUSHER_APP_TOKEN': 'AT_abcdefghijklmnop',
        'WXPUSHER_API_BASE': 'https://wxpusher.zjiecode.com/api',
        'FEATURE_WXPUSHER_BINDING': '1',
        'WXPUSHER_BINDING_PEPPER': 'bindvalue93847502938475029384750293847',
        'DISPATCH_LOCK_PATH': str(tmp_path / 'dispatch.lock'),
        'ALLOW_INSECURE_PUBLIC_BASE_URL': None,
        'QWEATHER_AUTH_MODE': 'disabled',
    }
    env[second_key] = env[first_key]
    original = _set_env(env)
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError) as exc_info:
            config.validate_production_config()
        error = str(exc_info.value)
        assert first_key in error
        assert second_key in error
        assert env[first_key] not in error
    finally:
        _restore_env(original)
        _reload_config()


def test_validate_production_config_locks_web_only_wxpusher_origin(tmp_path):
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'peppervalue1234567890abcdefghijkl',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_web_push.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'disabled',
        'WX_MINIPROGRAM_APPID': None,
        'WX_MINIPROGRAM_SECRET': None,
        'WX_MINIPROGRAM_OPENID_PEPPER': None,
        'WX_MINIPROGRAM_SESSION_SECRET': None,
        'PUBLIC_BASE_URL': 'http://attacker.example',
        'WXPUSHER_APP_TOKEN': 'AT_abcdefghijklmnop',
        'WXPUSHER_API_BASE': 'https://wxpusher.zjiecode.com/api',
        'DISPATCH_LOCK_PATH': str(tmp_path / 'dispatch.lock'),
        'ALLOW_INSECURE_PUBLIC_BASE_URL': None,
        'QWEATHER_AUTH_MODE': 'disabled',
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match='PUBLIC_BASE_URL'):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_validate_production_config_accepts_locked_web_only_wxpusher(tmp_path):
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'peppervalue1234567890abcdefghijkl',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_web_push_ok.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'disabled',
        'WX_MINIPROGRAM_APPID': None,
        'WX_MINIPROGRAM_SECRET': None,
        'WX_MINIPROGRAM_OPENID_PEPPER': None,
        'WX_MINIPROGRAM_SESSION_SECRET': None,
        'PUBLIC_BASE_URL': 'https://yilaoweather.org',
        'WXPUSHER_APP_TOKEN': 'AT_abcdefghijklmnop',
        'WXPUSHER_API_BASE': 'https://wxpusher.zjiecode.com/api',
        'FEATURE_WXPUSHER_BINDING': '0',
        # 绑定入口关闭时，预配置值不得破坏纯推送部署。
        'WXPUSHER_BINDING_PEPPER': 'strongkey1234567890strongkey123456',
        'DISPATCH_LOCK_PATH': str(tmp_path / 'dispatch.lock'),
        'ALLOW_INSECURE_PUBLIC_BASE_URL': None,
    })
    try:
        config = _reload_config()
        config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_validate_production_config_requires_pepper_only_for_wxpusher_binding(
    tmp_path,
):
    base_env = {
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'peppervalue1234567890abcdefghijkl',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_web_binding.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'disabled',
        'WX_MINIPROGRAM_APPID': None,
        'WX_MINIPROGRAM_SECRET': None,
        'WX_MINIPROGRAM_OPENID_PEPPER': None,
        'WX_MINIPROGRAM_SESSION_SECRET': None,
        'PUBLIC_BASE_URL': 'https://yilaoweather.org',
        'FEATURE_WXPUSHER': '1',
        'WXPUSHER_APP_TOKEN': 'AT_abcdefghijklmnop',
        'WXPUSHER_API_BASE': 'https://wxpusher.zjiecode.com/api',
        'FEATURE_WXPUSHER_BINDING': '1',
        'WXPUSHER_BINDING_PEPPER': None,
        'DISPATCH_LOCK_PATH': str(tmp_path / 'dispatch.lock'),
        'ALLOW_INSECURE_PUBLIC_BASE_URL': None,
    }
    original = _set_env(base_env)
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match='WXPUSHER_BINDING_PEPPER'):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_validate_production_config_accepts_enabled_wxpusher_binding(tmp_path):
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'peppervalue1234567890abcdefghijkl',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_web_binding_ok.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'disabled',
        'WX_MINIPROGRAM_APPID': None,
        'WX_MINIPROGRAM_SECRET': None,
        'WX_MINIPROGRAM_OPENID_PEPPER': None,
        'WX_MINIPROGRAM_SESSION_SECRET': None,
        'PUBLIC_BASE_URL': 'https://yilaoweather.org',
        'FEATURE_WXPUSHER': '1',
        'WXPUSHER_APP_TOKEN': 'AT_abcdefghijklmnop',
        'WXPUSHER_API_BASE': 'https://wxpusher.zjiecode.com/api',
        'FEATURE_WXPUSHER_BINDING': '1',
        'WXPUSHER_BINDING_PEPPER': 'bindvalue93847502938475029384750293847',
        'DISPATCH_LOCK_PATH': str(tmp_path / 'dispatch.lock'),
        'ALLOW_INSECURE_PUBLIC_BASE_URL': None,
    })
    try:
        config = _reload_config()
        config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_validate_production_config_rejects_binding_without_wxpusher(tmp_path):
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'peppervalue1234567890abcdefghijkl',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_binding_without_push.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'disabled',
        'WX_MINIPROGRAM_APPID': None,
        'WX_MINIPROGRAM_SECRET': None,
        'WX_MINIPROGRAM_OPENID_PEPPER': None,
        'WX_MINIPROGRAM_SESSION_SECRET': None,
        'FEATURE_WXPUSHER': '0',
        'WXPUSHER_APP_TOKEN': None,
        'FEATURE_WXPUSHER_BINDING': '1',
        'WXPUSHER_BINDING_PEPPER': 'bindvalue93847502938475029384750293847',
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match='FEATURE_WXPUSHER_BINDING'):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_validate_production_config_rejects_nonbinary_binding_flag(tmp_path):
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'peppervalue1234567890abcdefghijkl',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_invalid_binding_flag.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'disabled',
        'WX_MINIPROGRAM_APPID': None,
        'WX_MINIPROGRAM_SECRET': None,
        'WX_MINIPROGRAM_OPENID_PEPPER': None,
        'WX_MINIPROGRAM_SESSION_SECRET': None,
        'FEATURE_WXPUSHER': '0',
        'WXPUSHER_APP_TOKEN': None,
        'FEATURE_WXPUSHER_BINDING': 'yes',
        'WXPUSHER_BINDING_PEPPER': None,
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match='FEATURE_WXPUSHER_BINDING'):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


@pytest.mark.parametrize('formal_value', [None, '', 'true', 'yes', '2'])
def test_production_requires_explicit_binary_wechat_runtime(tmp_path, formal_value):
    """生产环境必须明确选择正式小程序态或 Web-only 态。"""
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'peppervalue1234567890abcdefghijkl',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': formal_value,
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_formal_flag.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'disabled',
        'WX_MINIPROGRAM_APPID': None,
        'WX_MINIPROGRAM_SECRET': None,
        'WX_MINIPROGRAM_OPENID_PEPPER': None,
        'WX_MINIPROGRAM_SESSION_SECRET': None,
        'WXPUSHER_APP_TOKEN': None,
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match='WECHAT_FORMAL_RUNTIME'):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


@pytest.mark.parametrize('web_private_value', ['true', 'yes', '2'])
def test_runtime_rejects_non_binary_web_private_flag(
    tmp_path,
    web_private_value,
):
    """网页私密功能开关只能使用可审计的 0 或 1。"""
    original = _set_env({
        'DEBUG': 'true',
        'WECHAT_FORMAL_RUNTIME': '0',
        'WEB_PRIVATE_FEATURES_ENABLED': web_private_value,
        'DATABASE_URI': f"sqlite:///{tmp_path/'invalid_web_private_flag.db'}",
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match='WEB_PRIVATE_FEATURES_ENABLED'):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_formal_production_requires_explicit_web_private_flag(tmp_path):
    """正式微信生产态缺少双端开关时必须拒绝启动。"""
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'peppervalue1234567890abcdefghijkl',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '1',
        'WEB_PRIVATE_FEATURES_ENABLED': None,
        'DATABASE_URI': f"sqlite:///{tmp_path/'missing_web_private_flag.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'disabled',
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match='WEB_PRIVATE_FEATURES_ENABLED'):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_formal_wechat_runtime_rejects_debug_and_incomplete_credentials(tmp_path):
    """正式小程序态必须关闭 DEBUG 并同时具备五项微信服务端配置。"""
    debug_env = {
        'WECHAT_FORMAL_RUNTIME': '1',
        'WEB_PRIVATE_FEATURES_ENABLED': '1',
        'WX_MINIPROGRAM_APPID': 'wx-production-appid',
        'WX_MINIPROGRAM_SECRET': 'wx-production-secret-value',
        'WX_MINIPROGRAM_OPENID_PEPPER': 'openidpepper1234567890abcdefghijklm',
        'WX_MINIPROGRAM_SESSION_SECRET': 'sessionvalue1234567890abcdefghijkl',
        'ACCOUNT_LINK_CODE_PEPPER': 'accountlink1234567890abcdefghijkl',
    }
    for debug_setting in ('true', None):
        original = _set_env({**debug_env, 'DEBUG': debug_setting})
        try:
            config = _reload_config()
            with pytest.raises(RuntimeError, match='DEBUG=false'):
                config.validate_production_config()
        finally:
            _restore_env(original)
            _reload_config()

    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'peppervalue1234567890abcdefghijkl',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '1',
        'WEB_PRIVATE_FEATURES_ENABLED': '1',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_formal_missing.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'disabled',
        'WX_MINIPROGRAM_APPID': None,
        'WX_MINIPROGRAM_SECRET': None,
        'WX_MINIPROGRAM_OPENID_PEPPER': None,
        'WX_MINIPROGRAM_SESSION_SECRET': None,
        'ACCOUNT_LINK_CODE_PEPPER': None,
        'WXPUSHER_APP_TOKEN': None,
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match='五项微信服务端配置'):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


@pytest.mark.parametrize(
    'missing_name',
    [
        'WX_MINIPROGRAM_APPID',
        'WX_MINIPROGRAM_SECRET',
        'WX_MINIPROGRAM_OPENID_PEPPER',
        'WX_MINIPROGRAM_SESSION_SECRET',
    ],
)
def test_formal_wechat_runtime_rejects_each_missing_wechat_credential(
    tmp_path,
    missing_name,
):
    """绑定码 pepper 已配置时，任一微信凭据缺失也必须阻止启动。"""
    env = {
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'peppervalue1234567890abcdefghijkl',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '1',
        'WEB_PRIVATE_FEATURES_ENABLED': '1',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_formal_partial.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'disabled',
        'WX_MINIPROGRAM_APPID': 'wx-production-appid',
        'WX_MINIPROGRAM_SECRET': 'wx-production-secret-value',
        'WX_MINIPROGRAM_OPENID_PEPPER': 'openidpepper1234567890abcdefghijklm',
        'WX_MINIPROGRAM_SESSION_SECRET': 'sessionvalue1234567890abcdefghijkl',
        'ACCOUNT_LINK_CODE_PEPPER': 'accountlink1234567890abcdefghijkl',
        'WXPUSHER_APP_TOKEN': None,
    }
    env[missing_name] = None
    original = _set_env(env)
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match=missing_name):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_web_only_runtime_rejects_wechat_credentials(tmp_path):
    """Web-only 运行态不得悄悄加载微信正式凭据。"""
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'peppervalue1234567890abcdefghijkl',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_web_with_wechat.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'disabled',
        'WX_MINIPROGRAM_APPID': 'wx-production-appid',
        'WX_MINIPROGRAM_SECRET': 'wx-production-secret-value',
        'WX_MINIPROGRAM_OPENID_PEPPER': 'openidpepper1234567890abcdefghijklm',
        'WX_MINIPROGRAM_SESSION_SECRET': 'sessionvalue1234567890abcdefghijkl',
        'ACCOUNT_LINK_CODE_PEPPER': 'accountlink1234567890abcdefghijkl',
        'WXPUSHER_APP_TOKEN': None,
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match='Web-only'):
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


def test_validate_production_config_rejects_memory_rate_limit(tmp_path):
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'pepper-1234567890pepper-1234567890',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_rate_limit.db'}",
        'RATE_LIMIT_STORAGE_URI': 'memory://',
        'REDIS_URL': '',
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match='memory://'):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_validate_production_config_accepts_redis_rate_limit(tmp_path):
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'pepper-1234567890pepper-1234567890',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_rate_limit_ok.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'disabled',
    })
    try:
        config = _reload_config()
        config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_validate_production_config_requires_qweather_persistent_redis(tmp_path):
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'pepper-1234567890pepper-1234567890',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_qweather_budget.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'api_key',
        'QWEATHER_KEY': 'server-weather-key',
        'QWEATHER_API_BASE': 'https://unit-test.qweatherapi.com/v7',
        'QWEATHER_REQUIRE_PERSISTENT_BUDGET': '1',
        'REDIS_URL': None,
        'WEATHER_CACHE_REDIS_URL': None,
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match='REDIS_URL'):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_validate_production_config_rejects_disabled_qweather_persistent_budget(
    tmp_path,
):
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'pepper-1234567890pepper-1234567890',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_qweather_flag.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'api_key',
        'QWEATHER_KEY': 'server-weather-key',
        'QWEATHER_API_BASE': 'https://unit-test.qweatherapi.com/v7',
        'QWEATHER_REQUIRE_PERSISTENT_BUDGET': '0',
        'REDIS_URL': 'redis://localhost:6379/0',
        'WEATHER_CACHE_REDIS_URL': None,
    })
    try:
        config = _reload_config()
        with pytest.raises(RuntimeError, match='QWEATHER_REQUIRE_PERSISTENT_BUDGET'):
            config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_validate_production_config_accepts_qweather_persistent_budget(tmp_path):
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'pepper-1234567890pepper-1234567890',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_qweather_ok.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'QWEATHER_AUTH_MODE': 'api_key',
        'QWEATHER_KEY': 'server-weather-key',
        'QWEATHER_API_BASE': 'https://unit-test.qweatherapi.com/v7',
        'QWEATHER_REQUIRE_PERSISTENT_BUDGET': '1',
        'REDIS_URL': None,
        'WEATHER_CACHE_REDIS_URL': 'rediss://cache.example:6380/1',
    })
    try:
        config = _reload_config()
        config.validate_production_config()
    finally:
        _restore_env(original)
        _reload_config()


def test_configure_app_sets_secure_cookie_defaults_for_production(tmp_path):
    original = _set_env({
        'SECRET_KEY': 'strongkey1234567890strongkey123456',
        'PAIR_TOKEN_PEPPER': 'pepper-1234567890pepper-1234567890',
        'DEBUG': 'false',
        'WECHAT_FORMAL_RUNTIME': '0',
        'DATABASE_URI': f"sqlite:///{tmp_path/'prod_cookie.db'}",
        'RATE_LIMIT_STORAGE_URI': 'redis://localhost:6379/0',
        'PUBLIC_BASE_URL': 'https://yilaoweather.org',
        'QWEATHER_AUTH_MODE': 'disabled',
    })
    try:
        from flask import Flask
        import logging

        config = _reload_config()
        app = Flask(__name__)
        config.configure_app(app, logging.getLogger(__name__))

        assert app.config['SESSION_COOKIE_SECURE'] is True
        assert app.config['REMEMBER_COOKIE_SECURE'] is True
        assert app.config['SESSION_COOKIE_HTTPONLY'] is True
        assert app.config['REMEMBER_COOKIE_HTTPONLY'] is True
        assert app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'
        assert app.config['REMEMBER_COOKIE_SAMESITE'] == 'Lax'
        assert app.config['PREFERRED_URL_SCHEME'] == 'https'
        assert app.config['WECHAT_FORMAL_RUNTIME'] is False
        assert app.config['WEB_PRIVATE_FEATURES_ENABLED'] is False
    finally:
        _restore_env(original)
        _reload_config()


def test_error_handler_classification():
    from utils.error_handlers import classify_exception

    status, message = classify_exception(ValueError('bad'))
    assert status == 400
    assert message
