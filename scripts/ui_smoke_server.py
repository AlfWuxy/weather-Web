#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为真实浏览器回归提供隔离的本机 Flask 服务，仅使用合成数据。"""

import argparse
import os
from pathlib import Path
import secrets
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
ROLES = ('user', 'caregiver', 'community', 'admin')
# 该密码只用于临时合成账号；真实登录表单和本机快捷入口均可用于回归。
FIXTURE_PASSWORD = 'UiSmoke-LocalOnly!'


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8770, help='本机监听端口，默认 8770')
    parser.add_argument(
        '--state-dir',
        type=Path,
        help='仓库外的绝对空目录；默认自动创建临时目录',
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('--port 必须在 1024–65535 之间')
    if args.state_dir is None:
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if temporary_root == REPO_ROOT or REPO_ROOT in temporary_root.parents:
            parser.error('系统临时目录位于仓库内，请用 --state-dir 指定仓库外目录')
        args.state_dir = Path(tempfile.mkdtemp(prefix='yilao-ui-smoke-')).resolve()
    else:
        if not args.state_dir.is_absolute():
            parser.error('--state-dir 必须是绝对路径')
        args.state_dir = args.state_dir.resolve()
        if args.state_dir == REPO_ROOT or REPO_ROOT in args.state_dir.parents:
            parser.error('--state-dir 必须位于仓库外，避免产生业务或测试数据')
        if args.state_dir.exists() and (
            not args.state_dir.is_dir() or any(args.state_dir.iterdir())
        ):
            parser.error('--state-dir 必须为空，不允许读取或覆盖已有数据')
        args.state_dir.mkdir(parents=True, exist_ok=True)
    return args


def isolate_environment(state_dir, port):
    """导入应用前切断宿主机配置，所有可写状态留在独立目录。"""
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO_ROOT))
    basic_env = {
        key: os.environ[key]
        for key in ('PATH', 'LANG', 'LC_ALL', 'TZ', 'TMPDIR')
        if key in os.environ
    }
    os.environ.clear()
    os.environ.update(basic_env)
    os.environ.update({
        'DATABASE_URI': f'sqlite:///{state_dir / "fixture.sqlite"}',
        'SECRET_KEY': secrets.token_urlsafe(48),
        'PAIR_TOKEN_PEPPER': secrets.token_urlsafe(48),
        'DEBUG': 'true',
        'DEMO_MODE': '1',
        'WEB_PRIVATE_FEATURES_ENABLED': '1',
        'WECHAT_FORMAL_RUNTIME': '0',
        'FEATURE_ELDER_MODE': '1',
        'FEATURE_HEAT_EXPOSURE_GIS': '1',
        'FEATURE_WEB_AI': '0',
        'FEATURE_WXPUSHER': '0',
        'FEATURE_WXPUSHER_BINDING': '0',
        'QWEATHER_AUTH_MODE': 'disabled',
        'QWEATHER_KEY': '',
        'QWEATHER_API_BASE': '',
        'QWEATHER_REQUIRE_PERSISTENT_BUDGET': '0',
        'REDIS_URL': '',
        'WEATHER_CACHE_REDIS_URL': '',
        'RATE_LIMIT_STORAGE_URI': 'memory://',
        'PUBLIC_BASE_URL': f'http://127.0.0.1:{port}',
        'DISPATCH_LOCK_PATH': str(state_dir / 'dispatch.lock'),
        'MPLCONFIGDIR': str(state_dir / 'matplotlib'),
        'XDG_CACHE_HOME': str(state_dir / 'cache'),
        'PYTHONDONTWRITEBYTECODE': '1',
    })

    # core.app 在导入时会调用 load_dotenv，必须在它之前禁用配置文件加载。
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False

    def require_isolated_path(path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return
        target = Path(os.fsdecode(path)).resolve()
        if target != state_dir and state_dir not in target.parents:
            raise OSError(f'UI 回归禁止修改隔离目录之外: {target}')

    def audit_isolation(event, audit_args):
        # 在 DNS 解析和连接前拒绝出站访问；本机监听仍正常工作。
        if event in ('socket.connect', 'socket.sendto'):
            raise OSError('本机 UI 回归已禁止出站连接')
        if event in ('socket.getaddrinfo', 'socket.gethostbyname'):
            if audit_args[0] not in ('127.0.0.1', 'localhost', '::1'):
                raise OSError('本机 UI 回归已禁止外部域名解析')
        if event in ('subprocess.Popen', 'os.system', 'os.posix_spawn'):
            raise OSError('本机 UI 回归已禁止创建外部进程')
        if event == 'open':
            path, mode, flags = audit_args
            writing = (
                isinstance(mode, str) and any(char in mode for char in 'wax+')
            ) or (
                isinstance(flags, int)
                and flags & (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
                )
            )
            if writing and path != os.devnull:
                require_isolated_path(path)
        if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod'):
            require_isolated_path(audit_args[0])
        if event in ('os.rename', 'os.link', 'os.symlink'):
            require_isolated_path(audit_args[0])
            require_isolated_path(audit_args[1])

    sys.addaudithook(audit_isolation)
    os.chdir(state_dir)


def build_fixture_app():
    """复用真实模板、路由和权限逻辑，仅新增本机合成账号入口。"""
    from flask import abort, redirect, request, session
    from flask_login import login_user, logout_user
    from core.app import create_app
    from core.db_models import (
        Community,
        CoolingResource,
        FamilyMember,
        FamilyMemberProfile,
        User,
    )
    from core.extensions import db
    from services.weather_service import WeatherService

    # 演示天气或明确缺省状态可用；网络天气备用服务始终关闭。
    original_weather_init = WeatherService.__init__

    def offline_weather_init(self):
        original_weather_init(self)
        self.use_openmeteo_fallback = False

    WeatherService.__init__ = offline_weather_init
    app = create_app()
    app.config.update(
        TESTING=False,
        TEMPLATES_AUTO_RELOAD=True,
        SESSION_COOKIE_SECURE=False,
        # 多浏览器会重复登录同一组合成账号；此配置仅属于隔离服务。
        RATE_LIMIT_LOGIN='1000 per minute',
    )

    with app.app_context():
        db.create_all()
        users = {}
        for role in ROLES:
            username = f'uiqa_{role}'
            user = User(
                username=username,
                role=role,
                email=f'{username}@example.invalid',
                age=68 if role == 'user' else 42,
                community='都昌镇',
                authorized_community='都昌镇' if role in ('community', 'admin') else None,
                push_enabled=False,
            )
            user.set_password(FIXTURE_PASSWORD)
            db.session.add(user)
            users[role] = user
        db.session.add(Community(
            name='都昌镇',
            location='本机 UI 合成社区',
            latitude=29.27,
            longitude=116.20,
            population=100,
            elderly_ratio=0.25,
            chronic_disease_ratio=0.2,
            vulnerability_index=0.4,
            risk_level='低',
        ))
        db.session.flush()
        for role in ('user', 'caregiver'):
            member = FamilyMember(
                user_id=users[role].id,
                name='测试家人（合成）',
                relation='父亲',
                age=72,
                gender='男',
                chronic_diseases='[]',
            )
            db.session.add(member)
            db.session.flush()
            db.session.add(FamilyMemberProfile(
                member_id=member.id,
                alert_enabled=False,
                share_with_doctor=False,
                share_with_community=False,
            ))
        db.session.add(CoolingResource(
            community_code='都昌镇',
            name='本机测试避暑点（合成）',
            resource_type='社区服务中心',
            address_hint='合成地址，不可用于真实导航',
            open_hours='09:00–17:00',
            has_ac=True,
            is_accessible=True,
            is_active=True,
            notes='仅用于验证卡片、筛选与移动端布局。',
        ))
        db.session.commit()

    @app.route('/__uiqa/login/<role>')
    def fixture_login(role):
        if (
            request.remote_addr not in ('127.0.0.1', '::1')
            or request.host.split(':')[0] not in ('127.0.0.1', 'localhost')
        ):
            abort(403)
        if role == 'anonymous':
            logout_user()
            session.clear()
            return redirect('/')
        if role not in ROLES:
            abort(404)
        session.clear()
        login_user(User.query.filter_by(username=f'uiqa_{role}').one())
        destinations = {'community': '/community', 'caregiver': '/pairs'}
        return redirect(destinations.get(role, '/dashboard'))

    return app


def main():
    args = parse_args()
    isolate_environment(args.state_dir, args.port)
    app = build_fixture_app()
    print(f'UI_SMOKE_STATE_DIR={args.state_dir}', flush=True)
    print(f'UI_SMOKE_URL=http://127.0.0.1:{args.port}', flush=True)
    app.run(
        host='127.0.0.1',
        port=args.port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == '__main__':
    main()
