# -*- coding: utf-8 -*-
"""不可变发布激活事务的行为级回归测试。"""

import hashlib
import os
import grp
import pwd
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTIVATE_SCRIPT = ROOT / 'scripts' / 'activate_release.sh'
START_TIMER_UNITS = (
    'case-weather-cache-bootstrap.timer',
    'case-weather-risk-precompute.timer',
    'case-weather-usage-cleanup.timer',
)
DEFERRED_TIMER_UNITS = ('case-weather-cache.timer',)
MANAGED_TIMER_UNITS = START_TIMER_UNITS + DEFERRED_TIMER_UNITS
LEGACY_UNITS = ('case-weather-dispatch.timer',)
SERVICE_UNITS = (
    'case-weather-cache-bootstrap.service',
    'case-weather-cache.service',
    'case-weather-dispatch.service',
    'case-weather-risk-precompute.service',
    'case-weather-usage-cleanup.service',
    'case-weather.service',
)
INSTALL_UNITS = MANAGED_TIMER_UNITS + SERVICE_UNITS
ALL_UNITS = INSTALL_UNITS + LEGACY_UNITS
FORMAL_COMMIT = 'a' * 40
# 保持运行时格式真实，同时避免测试夹具被静态扫描识别为正式 AppID。
TEST_MINIPROGRAM_APPID = ''.join(('w', 'x', '1234567890abcdef'))
ROTATED_TEST_MINIPROGRAM_APPID = ''.join(('w', 'x', 'abcdef1234567890'))


def _write_executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    path.chmod(0o755)


def _make_fake_systemctl(path):
    _write_executable(
        path,
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

state = Path(os.environ['FAKE_SYSTEMCTL_STATE'])
unit_dir = Path(os.environ['UNIT_DIR'])
args = sys.argv[1:]
command = args[0]
unit = next((value for value in reversed(args[1:]) if not value.startswith('-')), '')

def marker(kind):
    return state / f'{unit}.{kind}'

if command == 'cat':
    raise SystemExit(0 if marker('exists').exists() or (unit_dir / unit).exists() else 1)
if command == 'show':
    if os.environ.get('FAKE_BAD_ON_SUCCESS_UNIT') == unit:
        print('')
        raise SystemExit(0)
    if unit == 'case-weather-cache.service':
        print('case-weather-dispatch.service')
        raise SystemExit(0)
    if unit == 'case-weather-cache-bootstrap.service':
        print('case-weather-cache.timer')
        raise SystemExit(0)
    print('')
    raise SystemExit(0)
if command == 'is-enabled':
    if marker('enabled-runtime').exists():
        print('enabled-runtime')
        raise SystemExit(0)
    if marker('enabled').exists():
        print('enabled')
        raise SystemExit(0)
    print('disabled')
    raise SystemExit(1)
if command == 'is-active':
    active_state = next(
        (value for value in ('active', 'activating', 'reloading') if marker(value).exists()),
        '',
    )
    if active_state:
        stop_on_check = os.environ.get('FAKE_STOP_BOOTSTRAP_ON_ACTIVE_CHECK', '')
        if (
            unit == 'case-weather-cache-bootstrap.timer'
            and '--quiet' in args
            and stop_on_check
        ):
            counter = state / 'bootstrap-active-check-count'
            count = int(counter.read_text(encoding='utf-8')) + 1 if counter.exists() else 1
            counter.write_text(str(count), encoding='utf-8')
            if count == int(stop_on_check):
                marker('active').unlink(missing_ok=True)
                raise SystemExit(3)
        if '--quiet' not in args:
            print(active_state)
        raise SystemExit(0)
    if '--quiet' not in args:
        print('inactive')
    raise SystemExit(3)
if command == 'stop':
    if os.environ.get('FAKE_FAIL_STOP_UNIT') == unit:
        raise SystemExit(9)
    for value in ('active', 'activating', 'reloading'):
        marker(value).unlink(missing_ok=True)
    raise SystemExit(0)
if command in {'start', 'restart'}:
    failure_marker = state / 'start-failure-consumed'
    should_fail_once = os.environ.get('FAKE_FAIL_START_UNIT') == unit and not failure_marker.exists()
    should_fail_always = os.environ.get('FAKE_FAIL_START_ALWAYS') == unit
    if should_fail_once or should_fail_always:
        failure_marker.touch()
        raise SystemExit(9)
    marker('active').touch()
    raise SystemExit(0)
if command == 'enable':
    if '--runtime' in args:
        marker('enabled-runtime').touch()
    else:
        marker('enabled').touch()
    if '--now' in args:
        marker('active').touch()
    raise SystemExit(0)
if command == 'disable':
    marker('enabled').unlink(missing_ok=True)
    marker('enabled-runtime').unlink(missing_ok=True)
    raise SystemExit(0)
if command == 'daemon-reload':
    for exists_marker in state.glob('*.exists'):
        current_unit = exists_marker.name[:-len('.exists')]
        if not (unit_dir / current_unit).exists():
            exists_marker.unlink(missing_ok=True)
    for unit_file in unit_dir.iterdir():
        if unit_file.is_file():
            (state / f'{unit_file.name}.exists').touch()
    raise SystemExit(0)
if command == 'status':
    raise SystemExit(0)
raise SystemExit(f'unsupported fake systemctl call: {args}')
""",
    )


def _database_value(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute('SELECT value FROM release_state').fetchone()[0]
    finally:
        connection.close()


def _prepare_transaction(
    tmp_path,
    *,
    migration_exit=0,
    candidate_health_ok=True,
    public_health_ok=True,
    weather_timer_phase='recurring',
):
    if weather_timer_phase not in {'recurring', 'bootstrap', 'writer'}:
        raise ValueError(f'unsupported weather timer phase: {weather_timer_phase}')

    state_dir = tmp_path / 'state'
    release_root = tmp_path / 'deploy'
    old_release = release_root / 'releases' / 'old'
    new_release = release_root / 'releases' / 'new'
    current_link = release_root / 'current'
    unit_dir = tmp_path / 'units'
    fake_state = tmp_path / 'systemctl-state'
    fake_bin = tmp_path / 'fake-bin'
    database_file = state_dir / 'instance' / 'health_weather.db'

    for directory in (
        state_dir / 'instance',
        state_dir / 'backups',
        old_release,
        new_release / 'app' / 'scripts',
        new_release / 'private-metadata',
        new_release / 'venv' / 'bin',
        new_release / 'systemd',
        unit_dir,
        fake_state,
        fake_bin,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (state_dir / '.env').write_text('DEBUG=true\nRELEASE_VALUE=old\n', encoding='utf-8')
    (new_release / 'staged.env').write_text(
        'DEBUG=true\nRELEASE_VALUE=new\n',
        encoding='utf-8',
    )
    current_link.symlink_to(old_release)

    requirements_lock = (ROOT / 'requirements.lock').read_bytes()
    (new_release / 'app' / 'requirements.lock').write_bytes(requirements_lock)
    (new_release / 'private-metadata' / 'requirements-lock.sha256').write_text(
        hashlib.sha256(requirements_lock).hexdigest() + '\n',
        encoding='utf-8',
    )
    (new_release / 'private-metadata' / 'python-version.txt').write_text(
        sys.version + '\n',
        encoding='utf-8',
    )
    (new_release / 'private-metadata' / 'pip-inspect.json').write_text(
        '{}\n',
        encoding='utf-8',
    )

    connection = sqlite3.connect(database_file)
    connection.execute('CREATE TABLE release_state (value TEXT NOT NULL)')
    connection.execute("INSERT INTO release_state(value) VALUES ('old')")
    connection.commit()
    connection.close()

    migration = f"""#!/bin/bash
set -euo pipefail
/usr/bin/sqlite3 "$DATABASE_FILE" "UPDATE release_state SET value='new';"
exit {migration_exit}
"""
    _write_executable(new_release / 'app' / 'scripts' / 'server_migrate.sh', migration)
    (new_release / 'app' / 'scripts' / 'update_env_value.py').write_text(
        (ROOT / 'scripts' / 'update_env_value.py').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    (new_release / 'venv' / 'bin' / 'python').symlink_to(Path(sys.executable))
    _write_executable(
        new_release / 'venv' / 'bin' / 'gunicorn',
        '#!/bin/sh\ntrap "exit 0" TERM INT\nwhile :; do sleep 1; done\n',
    )

    for unit in INSTALL_UNITS:
        (new_release / 'systemd' / unit).write_text(f'new unit {unit}\n', encoding='utf-8')
    for unit in ALL_UNITS:
        (unit_dir / unit).write_text(f'old unit {unit}\n', encoding='utf-8')
        (fake_state / f'{unit}.exists').touch()
        (fake_state / f'{unit}.enabled').touch()
    for unit in (
        'case-weather.service',
        'case-weather-risk-precompute.timer',
        'case-weather-usage-cleanup.timer',
    ) + LEGACY_UNITS:
        (fake_state / f'{unit}.active').touch()
    if weather_timer_phase == 'recurring':
        (fake_state / 'case-weather-cache.timer.active').touch()
        (fake_state / 'case-weather-cache-bootstrap.timer.enabled').unlink(missing_ok=True)
    elif weather_timer_phase == 'bootstrap':
        (fake_state / 'case-weather-cache-bootstrap.timer.active').touch()
        (fake_state / 'case-weather-cache.timer.enabled').unlink(missing_ok=True)
    else:
        (fake_state / 'case-weather-cache.timer.enabled').unlink(missing_ok=True)
        (fake_state / 'case-weather-cache.service.activating').touch()

    fake_systemctl = fake_bin / 'systemctl'
    _make_fake_systemctl(fake_systemctl)
    fake_curl = fake_bin / 'curl'
    _write_executable(
        fake_curl,
        """#!/usr/bin/env python3
import os
import sys

url = sys.argv[-1]
if ':5001/' in url:
    healthy = os.environ.get('FAKE_CANDIDATE_HEALTH_OK') == '1'
else:
    healthy = os.environ.get('FAKE_PUBLIC_HEALTH_OK') == '1'
print('{"status":"ok"}' if healthy else '{"status":"unavailable"}')
""",
    )
    fake_busctl = fake_bin / 'busctl'
    _write_executable(
        fake_busctl,
        """#!/bin/sh
printf 't 2000000000\n'
""",
    )
    uptime_file = tmp_path / 'uptime'
    uptime_file.write_text('200.00 100.00\n', encoding='utf-8')

    environment = os.environ.copy()
    environment.update({
        'STATE_DIR': str(state_dir),
        'RELEASE_ROOT': str(release_root),
        'NEW_RELEASE': str(new_release),
        'CURRENT_LINK': str(current_link),
        'ENV_FILE': str(state_dir / '.env'),
        'STAGED_ENV_FILE': str(new_release / 'staged.env'),
        'UNIT_DIR': str(unit_dir),
        'DATABASE_FILE': str(database_file),
        'SYSTEMCTL_BIN': str(fake_systemctl),
        'SQLITE3_BIN': '/usr/bin/sqlite3',
        'CURL_BIN': str(fake_curl),
        'FLOCK_BIN': '/usr/bin/true',
        'BUSCTL_BIN': str(fake_busctl),
        'UPTIME_FILE': str(uptime_file),
        'FAKE_SYSTEMCTL_STATE': str(fake_state),
        'HEALTH_ATTEMPTS': '1',
        'HEALTH_SLEEP_SECONDS': '0',
        'POST_COMMIT_STABILITY_SECONDS': '0',
        'POST_COMMIT_STABILITY_INTERVAL_SECONDS': '1',
        'FAKE_CANDIDATE_HEALTH_OK': '1' if candidate_health_ok else '0',
        'FAKE_PUBLIC_HEALTH_OK': '1' if public_health_ok else '0',
        'RUNTIME_USER': pwd.getpwuid(os.getuid()).pw_name,
        'RUNTIME_GROUP': grp.getgrgid(os.getgid()).gr_name,
        'CHOWN_BIN': '/usr/bin/true',
        'CONTROL_OWNER_UID': str(os.getuid()),
        'CONTROL_OWNER_GID': str(os.getgid()),
    })
    return {
        'env': environment,
        'state_dir': state_dir,
        'release_root': release_root,
        'old_release': old_release,
        'new_release': new_release,
        'current_link': current_link,
        'unit_dir': unit_dir,
        'fake_state': fake_state,
        'database_file': database_file,
    }


def _run_activation(transaction):
    return subprocess.run(
        ['bash', str(ACTIVATE_SCRIPT)],
        cwd=ROOT,
        env=transaction['env'],
        text=True,
        capture_output=True,
        check=False,
    )


def _configure_formal_smoke(transaction, *, provider='QWeather'):
    """为激活事务准备完全离线的正式天气烟测桩。"""
    staged_text = f"""DEBUG=true
RELEASE_VALUE=new
QWEATHER_AUTH_MODE=api_key
QWEATHER_KEY=test-qweather-key
QWEATHER_API_BASE=https://example.invalid
QWEATHER_CANONICAL_LOCATION=116.20,29.27
QWEATHER_MONTHLY_REQUEST_LIMIT=40000
QWEATHER_BUDGET_FAIL_CLOSED=1
QWEATHER_REQUIRE_PERSISTENT_BUDGET=1
ALLOW_WEATHER_UNAVAILABLE=0
WEATHER_CACHE_TTL_MINUTES=30
FORECAST_CACHE_TTL_MINUTES=30
QWEATHER_WARNING_CACHE_TTL_MINUTES=30
WEATHER_SYNC_LOCATIONS=都昌县
WXPUSHER_APP_TOKEN=test-wxpusher-token
FEATURE_HEAT_EXPOSURE_GIS=1
WX_MINIPROGRAM_APPID={TEST_MINIPROGRAM_APPID}
WX_MINIPROGRAM_SECRET=test-miniprogram-secret
WX_MINIPROGRAM_PRIVACY_VERSION=2026-07-18
PUBLIC_BASE_URL=https://yilaoweather.org
"""
    (transaction['new_release'] / 'staged.env').write_text(staged_text, encoding='utf-8')
    (transaction['new_release'] / 'private-metadata' / 'source-commit.txt').write_text(
        FORMAL_COMMIT + '\n',
        encoding='utf-8',
    )
    transaction['env']['REQUIRE_WECHAT_READY'] = '1'
    transaction['env']['EXPECTED_RELEASE_COMMIT'] = FORMAL_COMMIT
    counter_file = transaction['state_dir'] / 'formal-smoke-request-count'
    weather_sync = transaction['new_release'] / 'app' / 'scripts' / 'weather_cache_sync.sh'
    _write_executable(
        weather_sync,
        f"""#!/bin/bash
set -euo pipefail
if [ "$#" -ne 1 ] || [ "$1" != "--skip-nowcast" ]; then
    echo '正式烟测必须显式跳过 nowcast' >&2
    exit 91
fi
"$VENV_PY" - "$DATABASE_FILE" "{counter_file}" "{provider}" <<'PY'
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

database, counter_path, provider = sys.argv[1:]
counter = Path(counter_path)
count = int(counter.read_text(encoding='utf-8')) + 1 if counter.exists() else 1
counter.write_text(str(count), encoding='utf-8')
now = datetime.now(timezone.utc)
snapshot_id = f'formal-snapshot-{{count}}'
current = {{'temperature': 31, 'humidity': 70, 'data_source': provider, 'is_mock': False}}
forecast = [{{
    'date': now.date().isoformat(),
    'temperature_max': 34,
    'temperature_min': 27,
    'data_source': provider,
    'is_mock': False,
}}]
source_status = {{
    'weather': {{'available': True, 'provider': provider, 'is_mock': False}},
    'forecast': {{
        'available': True,
        'providers': [provider],
        'meta': {{'source': provider}},
    }},
    'warnings': {{'available': True, 'count': 0, 'status': 'ok'}},
}}
connection = sqlite3.connect(database)
try:
    connection.execute(
        '''
        INSERT INTO miniprogram_snapshots(
            snapshot_id, fetched_at, expires_at, available,
            current_json, forecast_json, source_status_json
        ) VALUES (?, ?, ?, 1, ?, ?, ?)
        ''',
        (
            snapshot_id,
            now.isoformat(),
            (now + timedelta(hours=1)).isoformat(),
            json.dumps(current),
            json.dumps(forecast),
            json.dumps(source_status),
        ),
    )
    connection.commit()
finally:
    connection.close()
PY
""",
    )
    connection = sqlite3.connect(transaction['database_file'])
    try:
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS miniprogram_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id TEXT NOT NULL UNIQUE,
                fetched_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                available INTEGER NOT NULL,
                current_json TEXT,
                forecast_json TEXT,
                source_status_json TEXT
            )
            '''
        )
        connection.commit()
    finally:
        connection.close()
    return staged_text, counter_file


def _configure_formal_jwt_smoke(transaction, private_key, *, provider='QWeather'):
    staged_text, counter_file = _configure_formal_smoke(
        transaction,
        provider=provider,
    )
    jwt_text = staged_text.replace(
        "QWEATHER_AUTH_MODE=api_key\nQWEATHER_KEY=test-qweather-key\n",
        "QWEATHER_AUTH_MODE=jwt\n"
        "QWEATHER_KEY=\n"
        "QWEATHER_JWT_KID=test-kid\n"
        "QWEATHER_JWT_PROJECT_ID=test-project\n"
        f"QWEATHER_JWT_PRIVATE_KEY_PATH={private_key}\n",
    )
    assert jwt_text != staged_text
    (transaction['new_release'] / 'staged.env').write_text(
        jwt_text,
        encoding='utf-8',
    )
    return jwt_text, counter_file


def test_success_switches_release_only_after_migration_and_health(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    started_at = int(time.time())

    result = _run_activation(transaction)

    assert result.returncode == 0, result.stderr
    assert transaction['current_link'].resolve() == transaction['new_release'].resolve()
    assert _database_value(transaction['database_file']) == 'new'
    assert (transaction['state_dir'] / 'deployments' / 'current-release').read_text(
        encoding='utf-8'
    ).strip() == str(transaction['new_release'])
    assert 'RELEASE_VALUE=new' in (transaction['state_dir'] / '.env').read_text(
        encoding='utf-8'
    )
    assert not (transaction['new_release'] / 'staged.env').exists()
    for unit in INSTALL_UNITS:
        assert (transaction['unit_dir'] / unit).read_text(encoding='utf-8') == f'new unit {unit}\n'
    for unit in LEGACY_UNITS:
        assert not (transaction['unit_dir'] / unit).exists()
        assert not (transaction['fake_state'] / f'{unit}.enabled').exists()
        assert not (transaction['fake_state'] / f'{unit}.active').exists()
    for unit in ('case-weather.service',) + START_TIMER_UNITS:
        assert (transaction['fake_state'] / f'{unit}.active').exists()
    for unit in DEFERRED_TIMER_UNITS:
        assert not (transaction['fake_state'] / f'{unit}.active').exists()
        assert not (transaction['fake_state'] / f'{unit}.enabled').exists()
    env_text = (transaction['state_dir'] / '.env').read_text(encoding='utf-8')
    gate_line = next(
        line for line in env_text.splitlines()
        if line.startswith('QWEATHER_NETWORK_NOT_BEFORE_EPOCH=')
    )
    assert int(gate_line.split('=', 1)[1]) >= started_at + 1800
    committed_markers = list(
        (transaction['state_dir'] / 'backups').rglob('COMMITTED')
    )
    assert len(committed_markers) == 1


def test_stability_window_detects_post_activation_timer_cleanup(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction['env']['POST_COMMIT_STABILITY_SECONDS'] = '1'
    transaction['env']['FAKE_STOP_BOOTSTRAP_ON_ACTIVE_CHECK'] = '3'

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '发布后单元未处于 active: case-weather-cache-bootstrap.timer' in result.stderr
    markers = list(
        (transaction['state_dir'] / 'backups').rglob('POST_COMMIT_ATTENTION.txt')
    )
    assert len(markers) == 1
    assert not list((transaction['state_dir'] / 'backups').rglob('COMMITTED'))
    assert not (
        transaction['fake_state'] / 'case-weather-cache-bootstrap.timer.active'
    ).exists()


def test_migration_failure_restores_database_release_and_unit_state(tmp_path):
    transaction = _prepare_transaction(tmp_path, migration_exit=23)

    result = _run_activation(transaction)

    assert result.returncode == 23
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'
    assert 'RELEASE_VALUE=old' in (transaction['state_dir'] / '.env').read_text(encoding='utf-8')
    for unit in ALL_UNITS:
        assert (transaction['unit_dir'] / unit).read_text(encoding='utf-8') == f'old unit {unit}\n'
    for unit in (
        'case-weather.service',
        'case-weather-cache.timer',
        'case-weather-risk-precompute.timer',
        'case-weather-usage-cleanup.timer',
    ) + LEGACY_UNITS:
        assert (transaction['fake_state'] / f'{unit}.active').exists()
    assert not (
        transaction['fake_state'] / 'case-weather-cache-bootstrap.timer.active'
    ).exists()
    assert (transaction['fake_state'] / 'case-weather-cache.timer.enabled').exists()
    assert not (
        transaction['fake_state'] / 'case-weather-cache-bootstrap.timer.enabled'
    ).exists()
    assert not list((transaction['state_dir'] / 'backups').rglob('ROLLBACK_REQUIRED.txt'))
    assert len(list((transaction['state_dir'] / 'backups').rglob('ROLLED_BACK'))) == 1


def test_migration_failure_restores_runtime_enable_without_persisting_it(tmp_path):
    transaction = _prepare_transaction(tmp_path, migration_exit=23)
    unit = 'case-weather-cache.timer'
    (transaction['fake_state'] / f'{unit}.enabled').unlink()
    (transaction['fake_state'] / f'{unit}.enabled-runtime').touch()

    result = _run_activation(transaction)

    assert result.returncode == 23
    assert (transaction['fake_state'] / f'{unit}.enabled-runtime').exists()
    assert not (transaction['fake_state'] / f'{unit}.enabled').exists()


def test_migration_failure_restores_reloading_public_service(tmp_path):
    transaction = _prepare_transaction(tmp_path, migration_exit=23)
    unit = 'case-weather.service'
    (transaction['fake_state'] / f'{unit}.active').unlink()
    (transaction['fake_state'] / f'{unit}.reloading').touch()

    result = _run_activation(transaction)

    assert result.returncode == 23
    assert (transaction['fake_state'] / f'{unit}.active').exists()
    assert not (transaction['fake_state'] / f'{unit}.reloading').exists()


def test_migration_failure_restores_bootstrap_wait_state_without_recurring_timer(tmp_path):
    transaction = _prepare_transaction(
        tmp_path,
        migration_exit=23,
        weather_timer_phase='bootstrap',
    )

    result = _run_activation(transaction)

    assert result.returncode == 23
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'
    assert (
        transaction['fake_state'] / 'case-weather-cache-bootstrap.timer.active'
    ).exists()
    assert (
        transaction['fake_state'] / 'case-weather-cache-bootstrap.timer.enabled'
    ).exists()
    assert not (transaction['fake_state'] / 'case-weather-cache.timer.active').exists()
    assert not (transaction['fake_state'] / 'case-weather-cache.timer.enabled').exists()
    assert not (transaction['fake_state'] / 'case-weather-cache.service.active').exists()


def test_migration_failure_rearms_bootstrap_after_interrupting_active_writer(tmp_path):
    transaction = _prepare_transaction(
        tmp_path,
        migration_exit=23,
        weather_timer_phase='writer',
    )

    result = _run_activation(transaction)

    assert result.returncode == 23
    assert (
        transaction['fake_state'] / 'case-weather-cache-bootstrap.timer.active'
    ).exists()
    assert not (transaction['fake_state'] / 'case-weather-cache.timer.active').exists()
    assert not (
        transaction['fake_state'] / 'case-weather-cache.service.active'
    ).exists()
    assert not (
        transaction['fake_state'] / 'case-weather-cache.service.activating'
    ).exists()


def test_first_release_prepare_failure_removes_new_bootstrap_enable_link(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    for unit in (
        'case-weather-cache-bootstrap.timer',
        'case-weather-cache-bootstrap.service',
    ):
        (transaction['unit_dir'] / unit).unlink()
        (transaction['fake_state'] / f'{unit}.exists').unlink()
        (transaction['fake_state'] / f'{unit}.enabled').unlink(missing_ok=True)
    (transaction['new_release'] / 'app' / 'scripts' / 'update_env_value.py').write_text(
        'raise SystemExit(31)\n',
        encoding='utf-8',
    )

    result = _run_activation(transaction)

    assert result.returncode == 31
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert not (
        transaction['unit_dir'] / 'case-weather-cache-bootstrap.timer'
    ).exists()
    assert not (
        transaction['unit_dir'] / 'case-weather-cache-bootstrap.service'
    ).exists()
    assert not (
        transaction['fake_state'] / 'case-weather-cache-bootstrap.timer.enabled'
    ).exists()


def test_candidate_health_failure_rolls_back_new_code_database_and_units(tmp_path):
    transaction = _prepare_transaction(tmp_path, candidate_health_ok=False)

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'
    assert 'RELEASE_VALUE=old' in (transaction['state_dir'] / '.env').read_text(encoding='utf-8')
    for unit in ALL_UNITS:
        assert (transaction['unit_dir'] / unit).read_text(encoding='utf-8') == f'old unit {unit}\n'


def test_public_health_failure_keeps_forward_migrated_database(tmp_path):
    transaction = _prepare_transaction(tmp_path, public_health_ok=False)

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert transaction['current_link'].resolve() == transaction['new_release'].resolve()
    assert _database_value(transaction['database_file']) == 'new'
    assert 'RELEASE_VALUE=new' in (transaction['state_dir'] / '.env').read_text(
        encoding='utf-8'
    )
    markers = list((transaction['state_dir'] / 'backups').rglob('POST_COMMIT_ATTENTION.txt'))
    assert len(markers) == 1
    assert '可能已有用户写入' in markers[0].read_text(encoding='utf-8')
    assert (transaction['fake_state'] / 'case-weather.service.active').exists()
    for unit in START_TIMER_UNITS:
        assert (transaction['fake_state'] / f'{unit}.enabled').exists()
        assert not (transaction['fake_state'] / f'{unit}.active').exists()
    for unit in DEFERRED_TIMER_UNITS:
        assert not (transaction['fake_state'] / f'{unit}.enabled').exists()
        assert not (transaction['fake_state'] / f'{unit}.active').exists()


def test_timer_start_failure_keeps_committed_release_and_user_writes(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction['env']['FAKE_FAIL_START_UNIT'] = 'case-weather-risk-precompute.timer'

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert transaction['current_link'].resolve() == transaction['new_release'].resolve()
    assert _database_value(transaction['database_file']) == 'new'
    assert 'RELEASE_VALUE=new' in (transaction['state_dir'] / '.env').read_text(encoding='utf-8')
    markers = list((transaction['state_dir'] / 'backups').rglob('POST_COMMIT_ATTENTION.txt'))
    assert len(markers) == 1
    assert '不会回滚数据库' in markers[0].read_text(encoding='utf-8')
    assert (transaction['fake_state'] / 'case-weather.service.active').exists()
    assert not (transaction['fake_state'] / 'case-weather-dispatch.timer.active').exists()


def test_post_start_verification_failure_persists_blocking_marker(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction['env']['FAKE_BAD_ON_SUCCESS_UNIT'] = (
        'case-weather-cache-bootstrap.service'
    )

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert transaction['current_link'].resolve() == transaction['new_release'].resolve()
    assert _database_value(transaction['database_file']) == 'new'
    markers = list(
        (transaction['state_dir'] / 'backups').rglob('POST_COMMIT_ATTENTION.txt')
    )
    assert len(markers) == 1
    transaction_dir = markers[0].parent
    assert not (transaction_dir / 'COMMITTED').exists()
    (transaction['new_release'] / 'staged.env').write_text(
        'DEBUG=true\nRELEASE_VALUE=new\n',
        encoding='utf-8',
    )

    blocked = _run_activation(transaction)

    assert blocked.returncode != 0
    assert '尚未人工确认' in blocked.stderr
    assert transaction['current_link'].resolve() == transaction['new_release'].resolve()
    assert _database_value(transaction['database_file']) == 'new'


def test_rollback_failure_is_loud_and_leaves_all_units_stopped(tmp_path):
    transaction = _prepare_transaction(tmp_path, migration_exit=23)
    transaction['env']['FAKE_FAIL_START_ALWAYS'] = 'case-weather-risk-precompute.timer'

    result = _run_activation(transaction)

    assert result.returncode == 70
    markers = list((transaction['state_dir'] / 'backups').rglob('ROLLBACK_REQUIRED.txt'))
    assert len(markers) == 1
    assert '人工核对数据库' in markers[0].read_text(encoding='utf-8')
    for unit in ALL_UNITS:
        assert not (transaction['fake_state'] / f'{unit}.active').exists()


def test_unfinished_previous_transaction_blocks_new_mutation(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    unfinished = (
        transaction['state_dir']
        / 'backups'
        / 'deploy-transactions'
        / 'interrupted-release'
    )
    unfinished.mkdir(parents=True)
    (unfinished / 'ACTIVATION_STARTED').write_text('old-release\n', encoding='utf-8')

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '未完成事务' in result.stderr
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'
    assert 'RELEASE_VALUE=old' in (transaction['state_dir'] / '.env').read_text(
        encoding='utf-8'
    )


def test_rollback_required_blocks_until_exact_transaction_is_acknowledged(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    failed = (
        transaction['state_dir']
        / 'backups'
        / 'deploy-transactions'
        / 'failed-release'
    )
    failed.mkdir(parents=True)
    (failed / 'ACTIVATION_STARTED').write_text('old-release\n', encoding='utf-8')
    (failed / 'ROLLBACK_REQUIRED.txt').write_text('manual review required\n', encoding='utf-8')

    blocked = _run_activation(transaction)

    assert blocked.returncode != 0
    assert '尚未人工确认' in blocked.stderr
    assert not (failed / 'RECOVERY_CONFIRMED').exists()
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'

    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(failed)
    confirmed = _run_activation(transaction)

    assert confirmed.returncode == 0, confirmed.stderr
    assert (failed / 'RECOVERY_CONFIRMED').is_file()
    assert transaction['current_link'].resolve() == transaction['new_release'].resolve()


def test_recovery_ack_rejects_symlink_escape_and_symlinked_marker(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction_root = (
        transaction['state_dir'] / 'backups' / 'deploy-transactions'
    )
    transaction_root.mkdir()
    outside = tmp_path / 'outside-transaction'
    outside.mkdir()
    (outside / 'ROLLBACK_REQUIRED.txt').write_text(
        'manual review required\n',
        encoding='utf-8',
    )
    escaped = transaction_root / 'linked-transaction'
    escaped.symlink_to(outside, target_is_directory=True)
    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(escaped)

    escaped_result = _run_activation(transaction)

    assert escaped_result.returncode != 0
    assert 'realpath' in escaped_result.stderr
    assert not (outside / 'RECOVERY_CONFIRMED').exists()
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()

    escaped.unlink()
    direct = transaction_root / 'direct-transaction'
    direct.mkdir()
    marker_target = tmp_path / 'outside-marker.txt'
    marker_target.write_text('manual review required\n', encoding='utf-8')
    (direct / 'ROLLBACK_REQUIRED.txt').symlink_to(marker_target)
    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(direct)

    marker_result = _run_activation(transaction)

    assert marker_result.returncode != 0
    assert '故障标记不得为符号链接' in marker_result.stderr
    assert not (direct / 'RECOVERY_CONFIRMED').exists()

    canonical = transaction_root / 'canonical-transaction'
    canonical.mkdir()
    (canonical / 'ROLLBACK_REQUIRED.txt').write_text(
        'manual review required\n',
        encoding='utf-8',
    )
    nested = transaction_root / 'nested'
    nested.mkdir()
    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = (
        f"{nested}/../{canonical.name}"
    )

    dotdot_result = _run_activation(transaction)

    assert dotdot_result.returncode != 0
    assert 'realpath' in dotdot_result.stderr
    assert not (canonical / 'RECOVERY_CONFIRMED').exists()


def test_control_directories_are_private_and_owner_asserted(tmp_path):
    transaction = _prepare_transaction(tmp_path)

    result = _run_activation(transaction)

    assert result.returncode == 0, result.stderr
    for directory in (
        transaction['state_dir'] / 'backups',
        transaction['state_dir'] / 'deployments',
        transaction['state_dir'] / 'backups' / 'deploy-transactions',
    ):
        file_stat = directory.stat()
        assert file_stat.st_mode & 0o777 == 0o700
        assert file_stat.st_uid == os.getuid()
        assert file_stat.st_gid == os.getgid()


def test_committed_transaction_with_post_commit_attention_still_blocks(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    failed = (
        transaction['state_dir']
        / 'backups'
        / 'deploy-transactions'
        / 'committed-but-unverified'
    )
    failed.mkdir(parents=True)
    (failed / 'ACTIVATION_STARTED').write_text('old-release\n', encoding='utf-8')
    (failed / 'COMMITTED').write_text('success\n', encoding='utf-8')
    (failed / 'POST_COMMIT_ATTENTION.txt').write_text(
        'manual review required\n',
        encoding='utf-8',
    )

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '尚未人工确认' in result.stderr
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'


def test_formal_activation_rejects_release_commit_metadata_mismatch_before_mutation(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    (transaction['new_release'] / 'private-metadata' / 'source-commit.txt').write_text(
        ('b' * 40) + '\n',
        encoding='utf-8',
    )

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '上传 release 与冻结 commit 票据不一致' in result.stderr
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'
    assert not counter_file.exists()


def test_formal_qweather_smoke_writes_completed_receipt_and_reuses_without_request(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)

    first = _run_activation(transaction)

    assert first.returncode == 0, first.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'
    receipt_dirs = list(
        (transaction['state_dir'] / 'deployments' / 'formal-cache-smokes').iterdir()
    )
    assert len(receipt_dirs) == 1
    receipt = receipt_dirs[0]
    assert (receipt / 'started').is_file()
    assert (receipt / 'completed').is_file()
    assert 'snapshot_id=formal-snapshot-1' in (receipt / 'completed').read_text(
        encoding='utf-8'
    )

    # 动态网络闸门和非天气发布字段轮换都不能获得第二次自动烟测机会。
    rotated_config = (transaction['state_dir'] / '.env').read_text(encoding='utf-8')
    for old, new in (
        ('WXPUSHER_APP_TOKEN=test-wxpusher-token', 'WXPUSHER_APP_TOKEN=rotated-token'),
        ('FEATURE_HEAT_EXPOSURE_GIS=1', 'FEATURE_HEAT_EXPOSURE_GIS=0'),
        (
            f'WX_MINIPROGRAM_APPID={TEST_MINIPROGRAM_APPID}',
            f'WX_MINIPROGRAM_APPID={ROTATED_TEST_MINIPROGRAM_APPID}',
        ),
        ('WX_MINIPROGRAM_SECRET=test-miniprogram-secret', 'WX_MINIPROGRAM_SECRET=rotated-secret'),
        ('WX_MINIPROGRAM_PRIVACY_VERSION=2026-07-18', 'WX_MINIPROGRAM_PRIVACY_VERSION=2026-07-19'),
        ('PUBLIC_BASE_URL=https://yilaoweather.org', 'PUBLIC_BASE_URL=https://preview.example.invalid'),
    ):
        assert old in rotated_config
        rotated_config = rotated_config.replace(old, new)
    (transaction['new_release'] / 'staged.env').write_text(
        rotated_config,
        encoding='utf-8',
    )
    second = _run_activation(transaction)

    assert second.returncode == 0, second.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'
    assert '未再次请求上游' in second.stdout
    assert list(
        (transaction['state_dir'] / 'deployments' / 'formal-cache-smokes').iterdir()
    ) == [receipt]


def test_qweather_key_change_creates_new_weather_fingerprint(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    first = _run_activation(transaction)
    assert first.returncode == 0, first.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'

    weather_config = (transaction['state_dir'] / '.env').read_text(encoding='utf-8')
    assert 'QWEATHER_KEY=test-qweather-key' in weather_config
    weather_config = weather_config.replace(
        'QWEATHER_KEY=test-qweather-key',
        'QWEATHER_KEY=rotated-qweather-key',
    )
    (transaction['new_release'] / 'staged.env').write_text(
        weather_config,
        encoding='utf-8',
    )
    second = _run_activation(transaction)

    assert second.returncode == 0, second.stderr
    assert counter_file.read_text(encoding='utf-8') == '2'
    receipt_dirs = list(
        (transaction['state_dir'] / 'deployments' / 'formal-cache-smokes').iterdir()
    )
    assert len(receipt_dirs) == 2
    assert all((receipt / 'completed').is_file() for receipt in receipt_dirs)


def test_jwt_private_key_content_change_creates_new_weather_fingerprint(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    private_key = tmp_path / 'qweather-private.pem'
    private_key.write_bytes(b'private-key-version-one')
    private_key.chmod(0o600)
    _staged_text, counter_file = _configure_formal_jwt_smoke(
        transaction,
        private_key,
    )

    first = _run_activation(transaction)

    assert first.returncode == 0, first.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'

    private_key.write_bytes(b'private-key-version-two')
    private_key.chmod(0o600)
    (transaction['new_release'] / 'staged.env').write_text(
        (transaction['state_dir'] / '.env').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    second = _run_activation(transaction)

    assert second.returncode == 0, second.stderr
    assert counter_file.read_text(encoding='utf-8') == '2'
    receipt_dirs = list(
        (transaction['state_dir'] / 'deployments' / 'formal-cache-smokes').iterdir()
    )
    assert len(receipt_dirs) == 2


def test_jwt_private_key_symlink_fails_before_weather_request(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    private_key = tmp_path / 'qweather-private.pem'
    private_key.write_bytes(b'private-key-material')
    private_key.chmod(0o600)
    linked_key = tmp_path / 'linked-private.pem'
    linked_key.symlink_to(private_key)
    _staged_text, counter_file = _configure_formal_jwt_smoke(
        transaction,
        linked_key,
    )

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '正式 JWT 私钥安全校验失败' in result.stderr
    assert str(private_key) not in result.stderr
    assert not counter_file.exists()


def test_formal_fallback_snapshot_is_rejected_and_started_receipt_blocks_retry(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    staged_text, counter_file = _configure_formal_smoke(
        transaction,
        provider='Open-Meteo',
    )

    first = _run_activation(transaction)

    assert first.returncode != 0
    assert '实况来源不是 QWeather 官方数据' in first.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'
    receipt_dirs = list(
        (transaction['state_dir'] / 'deployments' / 'formal-cache-smokes').iterdir()
    )
    assert len(receipt_dirs) == 1
    assert (receipt_dirs[0] / 'started').is_file()
    assert not (receipt_dirs[0] / 'completed').exists()

    # 即使下一事务的桩能生成 QWeather 数据，同一绑定也必须在请求前关闭。
    _configure_formal_smoke(transaction, provider='QWeather')
    (transaction['new_release'] / 'staged.env').write_text(staged_text, encoding='utf-8')
    second = _run_activation(transaction)

    assert second.returncode != 0
    assert '禁止自动重试' in second.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'


def test_completed_receipt_with_expired_snapshot_fails_closed_without_request(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    first = _run_activation(transaction)
    assert first.returncode == 0, first.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'

    connection = sqlite3.connect(transaction['database_file'])
    try:
        connection.execute(
            "UPDATE miniprogram_snapshots SET expires_at = '2000-01-01T00:00:00+00:00'"
        )
        connection.commit()
    finally:
        connection.close()
    (transaction['new_release'] / 'staged.env').write_text(
        (transaction['state_dir'] / '.env').read_text(encoding='utf-8'),
        encoding='utf-8',
    )

    second = _run_activation(transaction)

    assert second.returncode != 0
    assert '持久化快照不可用或已经过期' in second.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'
