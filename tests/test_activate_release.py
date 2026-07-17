# -*- coding: utf-8 -*-
"""不可变发布激活事务的行为级回归测试。"""

import os
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
if command in {'daemon-reload', 'status'}:
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
        'FAKE_SYSTEMCTL_STATE': str(fake_state),
        'HEALTH_ATTEMPTS': '1',
        'HEALTH_SLEEP_SECONDS': '0',
        'FAKE_CANDIDATE_HEALTH_OK': '1' if candidate_health_ok else '0',
        'FAKE_PUBLIC_HEALTH_OK': '1' if public_health_ok else '0',
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
