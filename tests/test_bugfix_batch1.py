# -*- coding: utf-8 -*-
"""
验收测试 — 第一批热修 (#2/#4/#6/#9/#14)

每个修复至少 正向 + 负向 两条测试。
运行: pytest tests/test_bugfix_batch1.py -v
"""
import json
import pytest


# ── helpers ──────────────────────────────────────────────────────────


def _make_user(db_session, username, password, role='user', age=30, gender='男性'):
    from core.db_models import User
    u = User(username=username, role=role, age=age, gender=gender)
    u.set_password(password)
    db_session.add(u)
    db_session.commit()
    return u


def _login(client, username, password):
    """登录并返回 csrf_token."""
    csrf = 'test-csrf'
    with client.session_transaction() as s:
        s['_csrf_token'] = csrf
    resp = client.post('/login', data={
        'username': username,
        'password': password,
        'csrf_token': csrf,
    }, follow_redirects=True)
    return csrf, resp


def _csrf(client, token='test-csrf'):
    """注入 CSRF token 到 session."""
    with client.session_transaction() as s:
        s['_csrf_token'] = token
    return token


# ====================================================================
# #2  修改密码需校验旧密码
# ====================================================================


class TestPasswordChangeRequiresOldPassword:
    """Bug #2: profile_service 修改密码必须先验证旧密码。"""

    def test_wrong_old_password_rejected(self, app, client):
        """负向: 旧密码错误 → 密码不变、有错误提示。"""
        from core.extensions import db
        with app.app_context():
            db.create_all()
            user = _make_user(db.session, 'pwuser', 'OldPass123!')
            original_hash = user.password_hash
            csrf, _ = _login(client, 'pwuser', 'OldPass123!')

            resp = client.post('/profile', data={
                'form_id': 'password',
                'old_password': 'WrongOldPass!',
                'new_password': 'NewPass456!',
                'csrf_token': csrf,
            }, follow_redirects=True)

            db.session.refresh(user)
            assert user.password_hash == original_hash, "旧密码错误时密码哈希不应改变"

    def test_correct_old_password_accepted(self, app, client):
        """正向: 旧密码正确 + 新密码合规 → 密码更新。"""
        from core.extensions import db
        with app.app_context():
            db.create_all()
            user = _make_user(db.session, 'pwuser2', 'OldPass123!')
            original_hash = user.password_hash
            csrf, _ = _login(client, 'pwuser2', 'OldPass123!')

            resp = client.post('/profile', data={
                'form_id': 'password',
                'old_password': 'OldPass123!',
                'new_password': 'NewPass456!',
                'csrf_token': csrf,
            }, follow_redirects=True)

            db.session.refresh(user)
            assert user.password_hash != original_hash, "密码哈希应当改变"
            assert user.check_password('NewPass456!'), "应当能用新密码验证"

    def test_missing_old_password_rejected(self, app, client):
        """负向: 不提交旧密码 → 拒绝。"""
        from core.extensions import db
        with app.app_context():
            db.create_all()
            user = _make_user(db.session, 'pwuser3', 'OldPass123!')
            original_hash = user.password_hash
            csrf, _ = _login(client, 'pwuser3', 'OldPass123!')

            resp = client.post('/profile', data={
                'form_id': 'password',
                'old_password': '',
                'new_password': 'NewPass456!',
                'csrf_token': csrf,
            }, follow_redirects=True)

            db.session.refresh(user)
            assert user.password_hash == original_hash, "未提供旧密码时密码不应改变"


# ====================================================================
# 登录锁定兜底（Redis 不可用时）
# ====================================================================


class TestLoginLockoutFallback:
    def test_login_lockout_uses_db_when_redis_unavailable(self, app, client):
        from unittest.mock import patch
        from core.extensions import db
        from core.db_models import ShortCodeAttempt
        from core.security import hash_identifier

        with app.app_context():
            db.create_all()
            _make_user(db.session, 'lockdbuser', 'RightPass123!')
            app.config['RATE_LIMIT_LOGIN'] = '100 per minute'
            app.config['LOGIN_MAX_FAILURES'] = 2
            app.config['LOGIN_LOCKOUT_SECONDS'] = 300

        csrf = _csrf(client, token='lock-csrf')
        with patch('core.weather._get_redis_client', return_value=None):
            # 两次失败触发锁定
            for _ in range(2):
                client.post('/login', data={
                    'username': 'lockdbuser',
                    'password': 'WrongPass!',
                    'csrf_token': csrf,
                }, follow_redirects=False)

            # 即便密码正确，也应被锁定
            resp = client.post('/login', data={
                'username': 'lockdbuser',
                'password': 'RightPass123!',
                'csrf_token': csrf,
            }, follow_redirects=False)

        assert resp.status_code == 200
        assert '登录失败次数过多' in resp.get_data(as_text=True)
        with client.session_transaction() as sess:
            assert not sess.get('_user_id'), "锁定期间不应写入登录态"

        with app.app_context():
            key_hash = hash_identifier('login:lockdbuser')
            row = ShortCodeAttempt.query.filter_by(key_hash=key_hash).first()
            assert row is not None
            assert (row.failed_count or 0) >= 2


# ====================================================================
# #6  三个 v1 POST 端点补 @login_required
# ====================================================================


class TestV1EndpointsRequireAuth:
    """Bug #6: forecast/daily, chronic/population, alert/comprehensive 需登录。"""

    @pytest.mark.parametrize('path', [
        '/api/v1/forecast/daily',
        '/api/v1/chronic/population',
        '/api/v1/alert/comprehensive',
    ])
    def test_unauthenticated_post_rejected(self, app, client, path):
        """负向: 未登录 POST 被拒绝 (CSRF 400 或 login 302/401)。"""
        from core.extensions import db
        with app.app_context():
            db.create_all()
            # 带上 CSRF token 以测试 login_required 而非 CSRF 拦截
            csrf = _csrf(client)
            resp = client.post(path, data=json.dumps({}),
                               content_type='application/json',
                               headers={'X-CSRF-Token': csrf})
            # 未登录用户应被拒绝: CSRF 400, or login redirect 302, or 401
            assert resp.status_code in (302, 400, 401, 403), \
                f"{path}: 未登录应被拒绝, 实际 {resp.status_code}"
            # 确保不是 200 (成功响应)
            assert resp.status_code != 200

    def test_authenticated_post_not_auth_rejected(self, app, client):
        """正向: 登录后 POST 不被认证层拒绝。"""
        from core.extensions import db
        with app.app_context():
            db.create_all()
            _make_user(db.session, 'apiuser', 'ApiPass123!')
            csrf, _ = _login(client, 'apiuser', 'ApiPass123!')

            resp = client.post('/api/v1/forecast/daily',
                               data=json.dumps({}),
                               content_type='application/json',
                               headers={'X-CSRF-Token': csrf})
            # 登录后不应被 302 重定向到登录页
            assert resp.status_code != 302 or '/login' not in (resp.headers.get('Location') or ''), \
                f"登录后不应被重定向到登录页, 实际 {resp.status_code}"


# ====================================================================
# #9  member_id 归属校验
# ====================================================================


class TestMemberIdOwnership:
    """Bug #9: health diary / medication reminder 的 member_id 必须属于当前用户。"""

    def test_diary_with_foreign_member_rejected(self, app, client):
        """负向: 用户 A 提交用户 B 的 member_id → 拒绝。"""
        from core.extensions import db
        from core.db_models import FamilyMember, HealthDiary
        with app.app_context():
            db.create_all()
            user_a = _make_user(db.session, 'userA', 'PassA123!')
            user_b = _make_user(db.session, 'userB', 'PassB123!')
            member_b = FamilyMember(user_id=user_b.id, name='老人B', relation='父亲')
            db.session.add(member_b)
            db.session.commit()

            csrf, _ = _login(client, 'userA', 'PassA123!')
            client.post('/health-diary', data={
                'member_id': str(member_b.id),
                'symptoms': '头痛',
                'severity': 'mild',
                'csrf_token': csrf,
            })

            diary = HealthDiary.query.filter_by(user_id=user_a.id, member_id=member_b.id).first()
            assert diary is None, "不应允许关联其他用户的 member_id"

    def test_diary_with_own_member_accepted(self, app, client):
        """正向: 用户提交自己的 member_id → 成功。"""
        from core.extensions import db
        from core.db_models import FamilyMember, HealthDiary
        with app.app_context():
            db.create_all()
            user = _make_user(db.session, 'userC', 'PassC123!')
            member = FamilyMember(user_id=user.id, name='老人C', relation='母亲')
            db.session.add(member)
            db.session.commit()

            csrf, _ = _login(client, 'userC', 'PassC123!')
            client.post('/health-diary', data={
                'member_id': str(member.id),
                'symptoms': '正常',
                'severity': 'none',
                'csrf_token': csrf,
            })

            diary = HealthDiary.query.filter_by(user_id=user.id, member_id=member.id).first()
            assert diary is not None, "应允许关联自己的 member_id"


# ====================================================================
# #14  小程序创建老人原子事务
# ====================================================================


class TestMpApiAtomicCreate:
    """Bug #14: POST /mp/api/v1/elders 应为原子操作。"""

    def test_successful_create_has_both_member_and_pair(self, app, client):
        """正向: 成功创建 → 同时存在 FamilyMember 和 Pair。"""
        from core.extensions import db
        from core.db_models import FamilyMember, Pair
        from core.usage import create_api_token
        with app.app_context():
            db.create_all()
            user = _make_user(db.session, 'mpuser', 'MpPass123!')
            token = create_api_token(user.id, name='test')

            resp = client.post('/mp/api/v1/elders',
                               data=json.dumps({
                                   'name': '测试老人',
                                   'relation': '父亲',
                                   'location_query': '北京市',
                               }),
                               content_type='application/json',
                               headers={'Authorization': f'Bearer {token}'})
            data = resp.get_json()
            assert data['success'] is True

            member = FamilyMember.query.filter_by(user_id=user.id).first()
            pair = Pair.query.filter_by(caregiver_id=user.id).first()
            assert member is not None, "应创建 FamilyMember"
            assert pair is not None, "应创建 Pair"
            assert pair.member_id == member.id

    def test_missing_fields_rejected(self, app, client):
        """负向: 缺少必填字段 → 400, 无残留记录。"""
        from core.extensions import db
        from core.db_models import FamilyMember
        from core.usage import create_api_token
        with app.app_context():
            db.create_all()
            user = _make_user(db.session, 'mpuser2', 'MpPass123!')
            token = create_api_token(user.id, name='test')

            resp = client.post('/mp/api/v1/elders',
                               data=json.dumps({'name': ''}),
                               content_type='application/json',
                               headers={'Authorization': f'Bearer {token}'})
            assert resp.status_code == 400

            count = FamilyMember.query.filter_by(user_id=user.id).count()
            assert count == 0, "校验失败不应留下残留记录"


# ====================================================================
# #4  Open-Meteo 回退 AQI 标记修正
# ====================================================================


class TestOpenMeteoAqiFlag:
    """Bug #4: Open-Meteo 回退时 AQI/PM2.5 不应标记为真实数据。"""

    def test_openmeteo_returns_estimated_aqi(self, app):
        """正向: Open-Meteo 回退返回 aqi=0, pm25=0, aqi_estimated=True（非伪造的高值）。"""
        from unittest.mock import patch, MagicMock
        with app.app_context():
            from services.weather_service import WeatherService
            ws = WeatherService()

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                'current': {
                    'temperature_2m': 28,
                    'relative_humidity_2m': 65,
                    'surface_pressure': 1010,
                    'weather_code': 1,
                    'wind_speed_10m': 5,
                },
                'daily': {
                    'temperature_2m_max': [36],
                    'temperature_2m_min': [18],
                },
            }
            with patch.object(ws, '_get_location', return_value='120.0,30.0'), \
                 patch.object(ws, '_parse_lon_lat', return_value=('120.0', '30.0')), \
                 patch('requests.get', return_value=mock_resp):
                result = ws._get_openmeteo_weather('测试城市')

            assert result is not None
            # AQI/PM2.5 应为 0（安全占位），而非之前硬编码的 75/50
            assert result['aqi'] == 0, "AQI 应为 0（未知），而非硬编码虚假值"
            assert result['pm25'] == 0, "PM2.5 应为 0（未知），而非硬编码虚假值"
            assert result.get('aqi_estimated') is True, "应标记为估算数据"
            assert result['temperature_max'] == 36
            assert result['temperature_min'] == 18
            assert result.get('temperature_estimated') is False, "应优先采用 daily 的真实高低温"
            assert result['is_mock'] is False, "Open-Meteo 是真实来源，非 mock"

    def test_qweather_realtime_uses_daily_extremes(self, app):
        """正向: QWeather 实况应优先使用 daily 的当日最高/最低温。"""
        from unittest.mock import patch, MagicMock
        with app.app_context():
            from services.weather_service import WeatherService
            ws = WeatherService()
            ws.qweather_key = 'test_key'
            ws.api_base_url = 'https://test.api'

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                'code': '200',
                'now': {
                    'temp': '30',
                    'humidity': '60',
                    'pressure': '1013',
                    'text': '晴',
                    'windSpeed': '5',
                    'feelsLike': '32',
                }
            }
            # 空气质量请求 mock
            mock_air = MagicMock()
            mock_air.status_code = 200
            mock_air.json.return_value = {'code': '404'}

            # 当日预报请求 mock
            mock_daily = MagicMock()
            mock_daily.status_code = 200
            mock_daily.json.return_value = {
                'code': '200',
                'daily': [{'tempMax': '37', 'tempMin': '19'}]
            }

            with patch('requests.get', side_effect=[mock_resp, mock_daily, mock_air]):
                result = ws.get_current_weather('测试城市')

            assert result is not None
            assert result['temperature_max'] == 37.0
            assert result['temperature_min'] == 19.0
            assert result.get('temperature_estimated') is False

    def test_openmeteo_uses_hourly_when_daily_missing(self, app):
        """负向: Open-Meteo 缺失 daily 时应回退 hourly 推导，不使用固定 ±3。"""
        from unittest.mock import patch, MagicMock
        with app.app_context():
            from services.weather_service import WeatherService
            ws = WeatherService()

            mock_now = MagicMock()
            mock_now.status_code = 200
            mock_now.json.return_value = {
                'current': {
                    'temperature_2m': 28,
                    'relative_humidity_2m': 65,
                    'surface_pressure': 1010,
                    'weather_code': 1,
                    'wind_speed_10m': 5,
                },
                'daily': {
                    'temperature_2m_max': [],
                    'temperature_2m_min': [],
                },
            }
            mock_hourly = MagicMock()
            mock_hourly.status_code = 200
            mock_hourly.json.return_value = {
                'hourly': {
                    'time': [
                        '2026-02-17T00:00',
                        '2026-02-17T06:00',
                        '2026-02-17T12:00',
                        '2026-02-17T18:00',
                    ],
                    'temperature_2m': [18, 22, 35, 25],
                }
            }
            with patch.object(ws, '_get_location', return_value='120.0,30.0'), \
                 patch.object(ws, '_parse_lon_lat', return_value=('120.0', '30.0')), \
                 patch('requests.get', side_effect=[mock_now, mock_hourly]):
                result = ws._get_openmeteo_weather('测试城市')

            assert result['temperature_max'] == 35.0
            assert result['temperature_min'] == 18.0
            assert result.get('temperature_estimated') is True
            assert result.get('temperature_range_source') == 'hourly'

    def test_qweather_realtime_fallback_estimated_when_daily_unavailable(self, app):
        """负向: daily 失败时回退 hourly 推导，不再使用固定 temp±3。"""
        from unittest.mock import patch, MagicMock
        with app.app_context():
            from services.weather_service import WeatherService
            ws = WeatherService()
            ws.qweather_key = 'test_key'
            ws.api_base_url = 'https://test.api'

            mock_now = MagicMock()
            mock_now.status_code = 200
            mock_now.json.return_value = {
                'code': '200',
                'now': {'temp': '30', 'humidity': '60', 'pressure': '1013', 'text': '晴', 'windSpeed': '5'}
            }
            mock_daily_fail = MagicMock()
            mock_daily_fail.status_code = 500
            mock_daily_fail.json.return_value = {}
            mock_hourly = MagicMock()
            mock_hourly.status_code = 200
            mock_hourly.json.return_value = {
                'code': '200',
                'hourly': [
                    {'temp': '18'},
                    {'temp': '22'},
                    {'temp': '30'},
                    {'temp': '35'},
                    {'temp': '26'},
                    {'temp': '20'},
                ]
            }
            mock_air = MagicMock()
            mock_air.status_code = 200
            mock_air.json.return_value = {'code': '404'}

            with patch('requests.get', side_effect=[mock_now, mock_daily_fail, mock_hourly, mock_air]):
                result = ws.get_current_weather('测试城市')

            assert result['temperature_max'] == 35.0
            assert result['temperature_min'] == 18.0
            assert result.get('temperature_estimated') is True
            assert result.get('temperature_range_source') == 'hourly'

    def test_qweather_realtime_marks_unavailable_when_no_daily_and_hourly(self, app):
        """负向: daily+hourly 均失败时，温差字段应明确 unavailable，不返回伪造值。"""
        from unittest.mock import patch, MagicMock
        with app.app_context():
            from services.weather_service import WeatherService
            ws = WeatherService()
            ws.qweather_key = 'test_key'
            ws.api_base_url = 'https://test.api'

            mock_now = MagicMock()
            mock_now.status_code = 200
            mock_now.json.return_value = {
                'code': '200',
                'now': {'temp': '30', 'humidity': '60', 'pressure': '1013', 'text': '晴', 'windSpeed': '5'}
            }
            mock_daily_fail = MagicMock()
            mock_daily_fail.status_code = 500
            mock_daily_fail.json.return_value = {}
            mock_hourly_fail = MagicMock()
            mock_hourly_fail.status_code = 500
            mock_hourly_fail.json.return_value = {}
            mock_air = MagicMock()
            mock_air.status_code = 200
            mock_air.json.return_value = {'code': '404'}

            with patch('requests.get', side_effect=[mock_now, mock_daily_fail, mock_hourly_fail, mock_air]):
                result = ws.get_current_weather('测试城市')

            assert result['temperature_max'] is None
            assert result['temperature_min'] is None
            assert result.get('temperature_range_source') == 'unavailable'
            assert result.get('temperature_range_confidence') == 'none'

    def test_extreme_weather_can_detect_large_diurnal_range(self, app):
        """正向: 当日温差>15°C 时，极端天气识别应触发温差预警。"""
        with app.app_context():
            from services.weather_service import WeatherService
            ws = WeatherService()
            result = ws.identify_extreme_weather({
                'temperature': 27,
                'temperature_max': 38,
                'temperature_min': 20,
                'humidity': 60,
                'wind_speed': 2,
                'aqi': 50
            })
            types = [item['type'] for item in result.get('conditions', [])]
            assert '温差过大' in types


class TestSunshineInputNormalization:
    """#5 协议级修复：统一日照输入单位为秒，兼容小时输入。"""

    def test_sunshine_duration_hours_is_converted_to_seconds(self):
        from services.api_service import _normalize_sunshine_seconds
        seconds = _normalize_sunshine_seconds({'sunshine_duration_hours': 6})
        assert seconds == 21600.0

    def test_legacy_small_sunshine_hours_is_treated_as_hours(self):
        from services.api_service import _normalize_sunshine_seconds
        seconds = _normalize_sunshine_seconds({'sunshine_hours': 5})
        assert seconds == 18000.0

    def test_sunshine_duration_seconds_takes_precedence(self):
        from services.api_service import _normalize_sunshine_seconds
        seconds = _normalize_sunshine_seconds({
            'sunshine_duration_seconds': 12345,
            'sunshine_hours': 6
        })
        assert seconds == 12345.0

    def test_legacy_ambiguous_sunshine_hours_rejected(self):
        from services.api_service import _normalize_sunshine_seconds
        with pytest.raises(ValueError):
            _normalize_sunshine_seconds({'sunshine_hours': 30})

    def test_ml_predict_api_rejects_ambiguous_legacy_sunshine_hours(self, app, client):
        from core.extensions import db
        with app.app_context():
            db.create_all()
            _make_user(db.session, 'sunapiuser', 'SunPass123!')
            csrf, _ = _login(client, 'sunapiuser', 'SunPass123!')

            resp = client.post(
                '/api/v1/ml/predict',
                data=json.dumps({'sunshine_hours': 30}),
                content_type='application/json',
                headers={'X-CSRF-Token': csrf}
            )
            assert resp.status_code == 400


# ====================================================================
# 审计IP：受信代理边界 + 隐私保护
# ====================================================================


class TestAuditIpTrustBoundary:
    def test_untrusted_remote_does_not_trust_xff(self, app):
        from core.audit import _get_client_ip
        with app.test_request_context(
            '/audit',
            headers={'X-Forwarded-For': '8.8.8.8'},
            environ_base={'REMOTE_ADDR': '203.0.113.10'}
        ):
            assert _get_client_ip() == '203.0.113.10'

    def test_trusted_proxy_parses_xff_right_to_left(self, app):
        from core.audit import _get_client_ip
        app.config['TRUSTED_PROXY_CIDRS'] = '127.0.0.1/32,::1/128'
        with app.test_request_context(
            '/audit',
            headers={'X-Forwarded-For': '8.8.8.8, 198.51.100.23'},
            environ_base={'REMOTE_ADDR': '127.0.0.1'}
        ):
            # $proxy_add_x_forwarded_for 场景下，右端更接近真实客户端
            assert _get_client_ip() == '198.51.100.23'

    def test_log_security_event_persists_hashed_ip_only(self, app):
        from core.extensions import db
        from core.db_models import AuditLog
        from core.security import hash_identifier
        from utils.audit_log import log_security_event

        with app.app_context():
            app.config['FEATURE_AUDIT_LOGS'] = True
            app.config['TRUSTED_PROXY_CIDRS'] = '127.0.0.1/32,::1/128'
            db.create_all()

            with app.test_request_context(
                '/audit',
                headers={'X-Forwarded-For': '9.9.9.9, 198.51.100.23', 'User-Agent': 'pytest'},
                environ_base={'REMOTE_ADDR': '127.0.0.1'}
            ):
                log_security_event('ip_privacy_test')

            row = AuditLog.query.filter_by(action='ip_privacy_test').first()
            assert row is not None
            assert row.ip_address == hash_identifier('198.51.100.23')
            assert row.ip_address != '198.51.100.23'
