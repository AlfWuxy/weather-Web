# -*- coding: utf-8 -*-
"""不可变发布激活事务的行为级回归测试。"""

import hashlib
import gzip
import json
import os
import grp
import pwd
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
ACTIVATE_SCRIPT = ROOT / 'scripts' / 'activate_release.sh'
START_TIMER_UNITS = (
    'case-weather-backup.timer',
    'case-weather-cache-bootstrap.timer',
    'case-weather-risk-precompute.timer',
    'case-weather-usage-cleanup.timer',
)
DEFERRED_TIMER_UNITS = ('case-weather-cache.timer',)
MANAGED_TIMER_UNITS = START_TIMER_UNITS + DEFERRED_TIMER_UNITS
LEGACY_TIMER_UNITS = (
    'case-weather-dispatch.timer',
    'case-weather-sync.timer',
)
LEGACY_SERVICE_UNITS = ('case-weather-sync.service',)
LEGACY_UNITS = LEGACY_TIMER_UNITS + LEGACY_SERVICE_UNITS
SERVICE_UNITS = (
    'case-weather-backup.service',
    'case-weather-cache.service',
    'case-weather-dispatch.service',
    'case-weather-risk-precompute.service',
    'case-weather-usage-cleanup.service',
    'case-weather.service',
)
RETIRED_BOOTSTRAP_UNITS = (
    'case-weather-cache-bootstrap.service',
)
INSTALL_UNITS = MANAGED_TIMER_UNITS + SERVICE_UNITS
ALL_UNITS = INSTALL_UNITS + LEGACY_UNITS + RETIRED_BOOTSTRAP_UNITS
FORMAL_COMMIT = 'a' * 40
# 保持运行时格式真实，同时避免测试夹具被静态扫描识别为正式 AppID。
TEST_MINIPROGRAM_APPID = ''.join(('w', 'x', '1234567890abcdef'))
ROTATED_TEST_MINIPROGRAM_APPID = ''.join(('w', 'x', 'abcdef1234567890'))


def _legacy_cron_lines(state_dir):
    return (
        f'0 3 * * * {state_dir}/backup.sh >> {state_dir}/backups/backup.log 2>&1',
        (
            f'0 6 * * * TZ=Asia/Shanghai {state_dir}/venv/bin/python3 '
            f'{state_dir}/services/pipelines/sync_weather_data.py --daily '
            f'>> {state_dir}/logs/weather_sync.log 2>&1'
        ),
    )


def _release_backup_cron_line(state_dir, release_root):
    return (
        f'0 3 * * * PROJECT_DIR={state_dir} ENV_FILE={state_dir}/.env '
        f'BACKUP_DIR={state_dir}/backups '
        f'{release_root}/current/app/scripts/backup.sh '
        f'>> {state_dir}/backups/backup.log 2>&1'
    )


def _write_executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    path.chmod(0o755)


def _write_test_ed25519_private_key(path, *, mode=0o600):
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(mode)
    return path


def _make_fake_sync(path):
    _write_executable(
        path,
        """#!/usr/bin/env python3
import json
import os
import signal
import sqlite3
from pathlib import Path

state_dir = Path(os.environ['STATE_DIR'])
count_file = Path(os.environ['FAKE_SYNC_COUNT_FILE'])
audit_file = Path(os.environ['FAKE_SYNC_AUDIT_FILE'])
count = int(count_file.read_text(encoding='utf-8')) + 1 if count_file.exists() else 1
count_file.write_text(str(count), encoding='utf-8')
transactions = sorted((state_dir / 'backups' / 'deploy-transactions').iterdir())
transaction = transactions[-1]
actions_path = Path(os.environ['FAKE_SYSTEMCTL_LOG'])
actions = actions_path.read_text(encoding='utf-8').splitlines() if actions_path.exists() else []
database = Path(os.environ['FAKE_DURABILITY_DATABASE'])
database_value = ''
if database.exists():
    connection = sqlite3.connect(database)
    try:
        row = connection.execute('SELECT value FROM release_state').fetchone()
        database_value = row[0] if row else ''
    finally:
        connection.close()
receipt_root = state_dir / 'deployments' / 'formal-cache-smokes'
started_receipts = list(receipt_root.glob('*/started')) if receipt_root.exists() else []
completed_receipts = list(receipt_root.glob('*/completed')) if receipt_root.exists() else []
live_env = (state_dir / '.env').read_text(encoding='utf-8')
event = {
    'call': count,
    'capture_checkpoint': (transaction / 'CAPTURED_STATE_DURABLE').is_file(),
    'recovery_checkpoint': (transaction / 'RECOVERY_MATERIALS_DURABLE').is_file(),
    'env_backup': (transaction / 'environment-before.env').is_file(),
    'backup_env_backup': (transaction / 'backup-runtime-before.env').is_file(),
    'db_backup': (transaction / 'database-before.db').is_file(),
    'guard': (state_dir / 'deployments' / 'activation-in-progress').is_file(),
    'stop_seen': any(action.startswith('stop ') for action in actions),
    'live_release_new': 'RELEASE_VALUE=new' in live_env,
    'network_gate_high': 'QWEATHER_NETWORK_NOT_BEFORE_EPOCH=4102444800' in live_env,
    'database_value': database_value,
    'started_receipt': len(started_receipts) == 1,
    'completed_receipt': len(completed_receipts) == 1,
    'weather_count_exists': Path(os.environ['FAKE_WEATHER_COUNT_FILE']).exists(),
}
with audit_file.open('a', encoding='utf-8') as stream:
    stream.write(json.dumps(event, sort_keys=True) + '\\n')
if count == int(os.environ.get('FAKE_SYNC_FAIL_ON', '0')):
    raise SystemExit(23)
if count == int(os.environ.get('FAKE_SYNC_KILL_ON', '0')):
    os.kill(os.getppid(), signal.SIGKILL)
""",
    )


def _make_fake_systemctl(path):
    _write_executable(
        path,
        """#!/usr/bin/env python3
import gzip
import json
import os
import signal
import sqlite3
import sys
from pathlib import Path

state = Path(os.environ['FAKE_SYSTEMCTL_STATE'])
unit_dir = Path(os.environ['UNIT_DIR'])
args = sys.argv[1:]
command = args[0]
unit = next((value for value in reversed(args[1:]) if not value.startswith('-')), '')
action_log = os.environ.get('FAKE_SYSTEMCTL_LOG')
if action_log:
    with open(action_log, 'a', encoding='utf-8') as stream:
        stream.write(' '.join(args) + '\\n')

key_audit = os.environ.get('FAKE_QWEATHER_KEY_STOP_AUDIT', '')
if command == 'stop' and key_audit and not Path(key_audit).exists():
    pending = Path(os.environ['FAKE_QWEATHER_PENDING_KEY'])
    final = Path(os.environ['FAKE_QWEATHER_FINAL_KEY'])
    pending_stat = pending.lstat()
    Path(key_audit).write_text(json.dumps({
        'unit': unit,
        'pending_mode': oct(pending_stat.st_mode & 0o777),
        'pending_regular': pending.is_file() and not pending.is_symlink(),
        'final_exists': final.exists() or final.is_symlink(),
    }, sort_keys=True), encoding='utf-8')
    if os.environ.get('FAKE_REPLACE_QWEATHER_PRIVATE_DIR_ON_STOP') == '1':
        private_dir = pending.parent
        original_dir = private_dir.with_name(private_dir.name + '.original')
        private_dir.rename(original_dir)
        private_dir.mkdir(mode=0o700)
        replacement = private_dir / pending.name
        replacement.write_bytes((original_dir / pending.name).read_bytes())
        replacement.chmod(0o600)

def marker(kind):
    return state / f'{unit}.{kind}'

if command == 'cat':
    unit_file = unit_dir / unit
    if not marker('exists').exists() and not unit_file.exists():
        raise SystemExit(1)
    if unit_file.is_file():
        print(f'# {unit_file}')
        print(unit_file.read_text(encoding='utf-8'), end='')
    dropin_dir = unit_dir / f'{unit}.d'
    if dropin_dir.is_dir():
        for dropin in sorted(dropin_dir.glob('*.conf')):
            print(f'# {dropin}')
            print(dropin.read_text(encoding='utf-8'), end='')
    raise SystemExit(0)
if command == 'show':
    load_state = os.environ.get('FAKE_BACKUP_LOAD_STATE', '') or (
        'loaded'
        if marker('exists').exists() or (unit_dir / unit).exists()
        else 'not-found'
    )
    if '--property=LoadState' in args and '--property=ActiveState' in args:
        state_query_counter = state / 'backup-state-query-count'
        state_query_count = (
            int(state_query_counter.read_text(encoding='utf-8')) + 1
            if state_query_counter.exists()
            else 1
        )
        state_query_counter.write_text(str(state_query_count), encoding='utf-8')
        if os.environ.get('FAKE_FAIL_BACKUP_STATE_QUERY_ON') == str(state_query_count):
            raise SystemExit(9)
        if os.environ.get('FAKE_FAIL_BACKUP_STATE_QUERY') == '1':
            raise SystemExit(9)
        combined_load_state = (
            os.environ.get('FAKE_COMBINED_BACKUP_LOAD_STATE', '') or load_state
        )
        active_state = next(
            (
                value
                for value in ('active', 'activating', 'reloading', 'deactivating')
                if marker(value).exists()
            ),
            'inactive',
        )
        active_state = os.environ.get('FAKE_BACKUP_ACTIVE_STATE', '') or active_state
        if unit == 'case-weather-backup.service' and active_state in {
            'active', 'activating', 'reloading', 'deactivating'
        }:
            finish_on = os.environ.get('FAKE_BACKUP_FINISH_ON_ACTIVE_CHECK', '')
            if finish_on:
                counter = state / 'backup-active-check-count'
                count = int(counter.read_text(encoding='utf-8')) + 1 if counter.exists() else 1
                counter.write_text(str(count), encoding='utf-8')
                if count >= int(finish_on):
                    for value in ('active', 'activating', 'reloading', 'deactivating'):
                        marker(value).unlink(missing_ok=True)
                    active_state = 'inactive'
        print(f'LoadState={combined_load_state}')
        print(f'ActiveState={active_state}')
        raise SystemExit(0)
    if '--property=LoadState' in args:
        query_counter = state / f'{unit}.load-state-query-count'
        query_count = (
            int(query_counter.read_text(encoding='utf-8')) + 1
            if query_counter.exists()
            else 1
        )
        query_counter.write_text(str(query_count), encoding='utf-8')
        fail_on = os.environ.get('FAKE_FAIL_LOAD_STATE_QUERY_ON', '')
        if fail_on == f'{unit}:{query_count}':
            raise SystemExit(9)
        if os.environ.get('FAKE_FAIL_LOAD_STATE_QUERY') == unit:
            raise SystemExit(9)
        print(load_state)
        raise SystemExit(0)
    if '--property=NeedDaemonReload' in args:
        print(os.environ.get('FAKE_NEED_DAEMON_RELOAD_UNIT') == unit and 'yes' or 'no')
        raise SystemExit(0)
    if '--property=FragmentPath' in args:
        print(unit_dir / unit)
        raise SystemExit(0)
    if '--property=Result' in args:
        print('success')
        raise SystemExit(0)
    if '--property=ExecMainStatus' in args:
        print('0')
        raise SystemExit(0)
    if '--property=OnSuccess' in args:
        if os.environ.get('FAKE_BAD_ON_SUCCESS_UNIT') == unit:
            print('')
            raise SystemExit(0)
        if unit == 'case-weather-cache.service':
            print('case-weather-dispatch.service case-weather-cache.timer')
            raise SystemExit(0)
    if '--property=OnFailure' in args:
        if os.environ.get('FAKE_BAD_ON_FAILURE_UNIT') == unit:
            print('')
            raise SystemExit(0)
        if unit == 'case-weather-cache.service':
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
        (
            value
            for value in ('active', 'activating', 'reloading', 'deactivating')
            if marker(value).exists()
        ),
        '',
    )
    if active_state:
        if unit == 'case-weather-backup.service' and '--quiet' in args:
            finish_on = os.environ.get('FAKE_BACKUP_FINISH_ON_ACTIVE_CHECK', '')
            if finish_on:
                counter = state / 'backup-active-check-count'
                count = int(counter.read_text(encoding='utf-8')) + 1 if counter.exists() else 1
                counter.write_text(str(count), encoding='utf-8')
                if count >= int(finish_on):
                    marker('active').unlink(missing_ok=True)
                    raise SystemExit(3)
        stop_on_check = os.environ.get('FAKE_STOP_CACHE_ON_ACTIVE_CHECK', '')
        if (
            unit in {'case-weather-cache.timer', 'case-weather-cache-bootstrap.timer'}
            and '--quiet' in args
            and stop_on_check
        ):
            counter = state / f'{unit}.active-check-count'
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
    for value in ('active', 'activating', 'reloading', 'deactivating'):
        marker(value).unlink(missing_ok=True)
    if (
        unit == 'case-weather-backup.timer'
        and os.environ.get('FAKE_START_BACKUP_AFTER_TIMER_STOP') == '1'
    ):
        (state / 'case-weather-backup.service.activating').touch()
    raise SystemExit(0)
if command in {'start', 'restart'}:
    failure_marker = state / 'start-failure-consumed'
    should_fail_once = os.environ.get('FAKE_FAIL_START_UNIT') == unit and not failure_marker.exists()
    should_fail_always = os.environ.get('FAKE_FAIL_START_ALWAYS') == unit
    if should_fail_once or should_fail_always:
        failure_marker.touch()
        raise SystemExit(9)
    if unit == 'case-weather-backup.service':
        if os.environ.get('FAKE_FAIL_ACTUAL_BACKUP_SERVICE') == '1':
            raise SystemExit(9)
        runtime_env = Path(os.environ['STATE_DIR']) / 'backups' / 'backup-runtime.env'
        values = {}
        for line in runtime_env.read_text(encoding='utf-8').splitlines():
            key, value = line.split('=', 1)
            values[key] = value
        source_database = Path(values['BACKUP_DATABASE_FILE'])
        destination = Path(os.environ['STATE_DIR']) / 'backups' / 'daily'
        destination.mkdir(parents=True, exist_ok=True)
        counter = state / 'actual-backup-count'
        count = int(counter.read_text(encoding='utf-8')) + 1 if counter.exists() else 1
        counter.write_text(str(count), encoding='utf-8')
        raw_database = destination / f'health_weather_actual_{count}.db'
        source = sqlite3.connect(f'file:{source_database}?mode=ro', uri=True)
        target = sqlite3.connect(raw_database)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        with raw_database.open('rb') as input_stream, gzip.open(
            destination / f'health_weather_actual_{count}.db.gz',
            'wb',
        ) as output_stream:
            output_stream.write(input_stream.read())
        raw_database.unlink()
        marker('active').unlink(missing_ok=True)
        raise SystemExit(0)
    marker('active').touch()
    if (
        command == 'restart'
        and unit == 'case-weather.service'
        and os.environ.get('FAKE_REPLACE_QWEATHER_FINAL_AFTER_RESTART') == '1'
    ):
        final_key = Path(os.environ['FAKE_QWEATHER_FINAL_KEY'])
        payload = final_key.read_bytes()
        final_key.unlink()
        final_key.write_bytes(payload)
        final_key.chmod(0o640)
    if os.environ.get('FAKE_KILL_PARENT_AFTER_RESTART_UNIT') == unit and command == 'restart':
        os.kill(os.getppid(), signal.SIGKILL)
    cache_result = os.environ.get('FAKE_CACHE_RESULT', '')
    if unit == 'case-weather-cache.service' and cache_result in {'success', 'failure'}:
        hook_name = 'OnSuccess' if cache_result == 'success' else 'OnFailure'
        hook_units = []
        for line in (unit_dir / unit).read_text(encoding='utf-8').splitlines():
            key, separator, value = line.partition('=')
            if separator and key.strip() == hook_name:
                hook_units.extend(value.split())
        marker('active').unlink(missing_ok=True)
        for hook_unit in hook_units:
            (state / f'{hook_unit}.active').touch()
        raise SystemExit(0 if cache_result == 'success' else 1)
    if (
        command == 'restart'
        and unit == 'case-weather-backup.timer'
        and os.environ.get('FAKE_START_BACKUP_AFTER_TIMER_RESTART') == '1'
    ):
        (state / 'case-weather-backup.service.activating').touch()
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


def _make_fake_crontab(path):
    _write_executable(
        path,
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

store = Path(os.environ['FAKE_ROOT_CRONTAB'])
state = Path(os.environ['FAKE_SYSTEMCTL_STATE'])
args = sys.argv[1:]
if '-l' in args:
    if not store.exists():
        print('no crontab for root', file=sys.stderr)
        raise SystemExit(1)
    sys.stdout.buffer.write(store.read_bytes())
    read_counter = state / 'crontab-read-count'
    read_count = int(read_counter.read_text(encoding='ascii')) + 1 if read_counter.exists() else 1
    read_counter.write_text(str(read_count), encoding='ascii')
    append_after_read = os.environ.get('FAKE_CRONTAB_APPEND_AFTER_READ_COUNT', '')
    append_before_install = os.environ.get('FAKE_CRONTAB_APPEND_BEFORE_INSTALL', '')
    if append_after_read and read_count == int(append_after_read) and append_before_install:
        with store.open('ab') as stream:
            stream.write(append_before_install.encode('utf-8'))
    counter = state / 'crontab-install-count'
    appended_marker = state / 'concurrent-cron-appended'
    appended = os.environ.get('FAKE_CRONTAB_APPEND_AFTER_REMOVE', '')
    if counter.exists() and counter.read_text(encoding='ascii') == '1' and appended and not appended_marker.exists():
        with store.open('ab') as stream:
            stream.write(appended.encode('utf-8'))
        appended_marker.touch()
    raise SystemExit(0)

source = Path(args[-1])
store.write_bytes(source.read_bytes())
counter = state / 'crontab-install-count'
count = int(counter.read_text(encoding='ascii')) + 1 if counter.exists() else 1
counter.write_text(str(count), encoding='ascii')
if os.environ.get('FAKE_START_BACKUP_AFTER_CRONTAB_INSTALL') == '1':
    (state / 'case-weather-backup.service.activating').touch()
""",
    )


def _make_fake_pgrep(path):
    _write_executable(
        path,
        """#!/bin/sh
if [ -f \"$FAKE_SYSTEMCTL_STATE/legacy-process-running\" ]; then
    printf '%s\\n' 4321
    exit 0
fi
case "$*" in
    *"-u $RUNTIME_USER"*)
        if [ -f "$FAKE_SYSTEMCTL_STATE/runtime-user-process-running" ]; then
            printf '%s\\n' 4999
            exit 0
        fi
        ;;
    *'/current/app/scripts/backup.sh'*)
        for state in active activating reloading deactivating; do
            if [ -f "$FAKE_SYSTEMCTL_STATE/case-weather-backup.service.$state" ]; then
                printf '%s\\n' 4322
                exit 0
            fi
        done
        ;;
esac
exit 1
""",
    )


def _make_fake_systemd_run(path):
    _write_executable(
        path,
        """#!/usr/bin/env python3
import gzip
import os
import sqlite3
import sys
from pathlib import Path

args = sys.argv[1:]
action_log = os.environ.get('FAKE_SYSTEMCTL_LOG')
if action_log:
    with open(action_log, 'a', encoding='utf-8') as stream:
        stream.write('systemd-run ' + ' '.join(args) + '\\n')
if os.environ.get('FAKE_FAIL_SYSTEMD_RUN') == '1':
    raise SystemExit(9)
values = {}
for value in args:
    if value.startswith('--setenv='):
        key, item = value[len('--setenv='):].split('=', 1)
        values[key] = item
destination = Path(values['BACKUP_DIR'])
destination.mkdir(parents=True, exist_ok=True)
if os.environ.get('FAKE_INVALID_SQLITE_BACKUP') == '1':
    with gzip.open(destination / 'health_weather_fake.db.gz', 'wb') as stream:
        stream.write(b'not a sqlite database')
    raise SystemExit(0)
raw_database = destination / 'health_weather_fake.db'
connection = sqlite3.connect(raw_database)
try:
    connection.execute('CREATE TABLE validation_state (value TEXT NOT NULL)')
    connection.execute("INSERT INTO validation_state(value) VALUES ('ok')")
    connection.commit()
finally:
    connection.close()
with raw_database.open('rb') as source, gzip.open(
    destination / 'health_weather_fake.db.gz', 'wb'
) as stream:
    stream.write(source.read())
raw_database.unlink()
raise SystemExit(0)
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
    root_crontab = tmp_path / 'root-crontab'
    systemctl_log = tmp_path / 'systemctl-actions.log'
    database_file = state_dir / 'instance' / 'health_weather.db'
    runtime_guard_dir = tmp_path / 'runtime-boot-guard'
    qweather_private_dir = state_dir / 'private'

    for directory in (
        state_dir / 'instance',
        state_dir / 'backups',
        qweather_private_dir,
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
    qweather_private_dir.chmod(0o700)
    database_uri = f'sqlite:///{database_file.as_posix()}'
    (state_dir / '.env').write_text(
        f'DEBUG=true\nRELEASE_VALUE=old\nDATABASE_URI={database_uri}\n',
        encoding='utf-8',
    )
    (new_release / 'staged.env').write_text(
        f'DEBUG=true\nRELEASE_VALUE=new\nDATABASE_URI={database_uri}\n',
        encoding='utf-8',
    )
    current_link.symlink_to(old_release)
    root_crontab.write_bytes(
        (
            'MAILTO=ops@example.invalid\n'
            '@reboot /usr/local/sbin/unrelated-health-check'
        ).encode('utf-8')
    )

    requirements_lock = (ROOT / 'requirements.lock').read_bytes()
    (new_release / 'app' / 'requirements.lock').write_bytes(requirements_lock)
    (new_release / 'app' / 'scripts' / 'backup.sh').write_text(
        (ROOT / 'scripts' / 'backup.sh').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    (new_release / 'app' / 'scripts' / 'backup.sh').chmod(0o755)
    core_dir = new_release / 'app' / 'core'
    core_dir.mkdir()
    (core_dir / '__init__.py').write_text('', encoding='utf-8')
    (core_dir / 'app.py').write_text(
        """from contextlib import nullcontext
from pathlib import Path
import os


class _ConfigApp:
    def __init__(self, values):
        self.config = dict(values)
        self.config['SQLALCHEMY_DATABASE_URI'] = values['DATABASE_URI']
        self.instance_path = str(Path.cwd() / 'instance')

    def app_context(self):
        return nullcontext()


def create_app(register_blueprints=False):
    env_file = Path(os.environ['CASE_WEATHER_ENV_FILE'])
    values = {}
    for line in env_file.read_text(encoding='utf-8').splitlines():
        key, separator, value = line.partition('=')
        if separator:
            values[key] = value
    return _ConfigApp(values)
""",
        encoding='utf-8',
    )
    (core_dir / 'config.py').write_text(
        """from pathlib import Path


def resolve_sqlite_db_path(uri, *, repo_root, instance_dir):
    if not uri.startswith('sqlite:///') or uri == 'sqlite:///:memory:':
        return None
    raw = uri[len('sqlite:///'):]
    path = Path(raw)
    if not path.is_absolute():
        path = Path(instance_dir) / path
    return path.resolve(strict=False)
""",
        encoding='utf-8',
    )
    services_dir = new_release / 'app' / 'services'
    services_dir.mkdir()
    (services_dir / '__init__.py').write_text('', encoding='utf-8')
    (services_dir / 'qweather_auth.py').write_text(
        (ROOT / 'services' / 'qweather_auth.py').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
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
    _write_executable(
        new_release / 'venv' / 'bin' / 'python',
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n',
    )
    _write_executable(
        new_release / 'venv' / 'bin' / 'gunicorn',
        '#!/bin/sh\ntrap "exit 0" TERM INT\nwhile :; do sleep 1; done\n',
    )

    for unit in INSTALL_UNITS:
        if unit == 'case-weather-backup.service':
            unit_text = f"""[Unit]
Description=Case Weather test backup
ConditionPathExists=|!{state_dir}/deployments/activation-in-progress
ConditionPathExists=|{runtime_guard_dir}/activation-permit

[Service]
Type=oneshot
User=root
Group=root
PrivateNetwork=true
ProtectSystem=strict
EnvironmentFile={state_dir}/backups/backup-runtime.env
ExecStart=/bin/bash {current_link}/app/scripts/backup.sh
TimeoutStartSec=15min
"""
        else:
            unit_text = f'new unit {unit}\n'
        (new_release / 'systemd' / unit).write_text(unit_text, encoding='utf-8')
    for unit in ALL_UNITS:
        (unit_dir / unit).write_text(f'old unit {unit}\n', encoding='utf-8')
        dropin_dir = unit_dir / f'{unit}.d'
        dropin_dir.mkdir()
        (dropin_dir / '10-case-weather-activation-guard.conf').write_text(
            '[Unit]\n'
            f'ConditionPathExists=|!{state_dir}/deployments/activation-in-progress\n'
            f'ConditionPathExists=|{runtime_guard_dir}/activation-permit\n',
            encoding='utf-8',
        )
        (dropin_dir / '10-case-weather-activation-guard.conf').chmod(0o644)
        (fake_state / f'{unit}.exists').touch()
        (fake_state / f'{unit}.enabled').touch()
    for unit in (
        'case-weather.service',
        'case-weather-backup.timer',
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
    fake_crontab = fake_bin / 'crontab'
    _make_fake_crontab(fake_crontab)
    fake_pgrep = fake_bin / 'pgrep'
    _make_fake_pgrep(fake_pgrep)
    fake_systemd_run = fake_bin / 'systemd-run'
    _make_fake_systemd_run(fake_systemd_run)
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
    environment.pop('DATABASE_FILE', None)
    environment.pop('DATABASE_URI', None)
    environment.update({
        'STATE_DIR': str(state_dir),
        'RELEASE_ROOT': str(release_root),
        'NEW_RELEASE': str(new_release),
        'CURRENT_LINK': str(current_link),
        'ENV_FILE': str(state_dir / '.env'),
        'STAGED_ENV_FILE': str(new_release / 'staged.env'),
        'UNIT_DIR': str(unit_dir),
        'SYSTEMCTL_BIN': str(fake_systemctl),
        'SYSTEMD_RUN_BIN': str(fake_systemd_run),
        'CRONTAB_BIN': str(fake_crontab),
        'PGREP_BIN': str(fake_pgrep),
        'SQLITE3_BIN': '/usr/bin/sqlite3',
        'CURL_BIN': str(fake_curl),
        'FLOCK_BIN': '/usr/bin/true',
        'BUSCTL_BIN': str(fake_busctl),
        'UPTIME_FILE': str(uptime_file),
        'FAKE_SYSTEMCTL_STATE': str(fake_state),
        'FAKE_SYSTEMCTL_LOG': str(systemctl_log),
        'FAKE_ROOT_CRONTAB': str(root_crontab),
        'HEALTH_ATTEMPTS': '1',
        'HEALTH_SLEEP_SECONDS': '0',
        'POST_COMMIT_STABILITY_SECONDS': '0',
        'POST_COMMIT_STABILITY_INTERVAL_SECONDS': '1',
        'FAKE_CANDIDATE_HEALTH_OK': '1' if candidate_health_ok else '0',
        'FAKE_PUBLIC_HEALTH_OK': '1' if public_health_ok else '0',
        'RUNTIME_USER': pwd.getpwuid(os.getuid()).pw_name,
        'RUNTIME_GROUP': grp.getgrgid(os.getgid()).gr_name,
        'RUNTIME_BOOT_GUARD_DIR': str(runtime_guard_dir),
        'ALLOW_NONROOT_TEST_RUNTIME_GUARD': '1',
        'SYNC_BIN': '/usr/bin/true',
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
        'root_crontab': root_crontab,
        'systemctl_log': systemctl_log,
        'database_file': database_file,
        'qweather_private_dir': qweather_private_dir,
    }


def _run_activation(transaction):
    pending_raw = transaction['env'].get('QWEATHER_PENDING_KEY_PATH', '')
    if pending_raw and transaction.get('auto_stage_qweather_pending', True):
        pending = Path(pending_raw)
        if not pending.exists() and not pending.is_symlink():
            staged_values = {}
            staged_env = transaction['new_release'] / 'staged.env'
            if staged_env.exists():
                for line in staged_env.read_text(encoding='utf-8').splitlines():
                    key, separator, value = line.partition('=')
                    if separator:
                        staged_values[key] = value
            final = Path(staged_values.get('QWEATHER_JWT_PRIVATE_KEY_PATH', ''))
            if final.is_file() and not final.is_symlink():
                pending.write_bytes(final.read_bytes())
                pending.chmod(0o600)
    return subprocess.run(
        ['bash', str(ACTIVATE_SCRIPT)],
        cwd=ROOT,
        env=transaction['env'],
        text=True,
        capture_output=True,
        check=False,
    )


def _seed_interrupted_activation_guard(
    transaction,
    name,
    *,
    terminal=None,
    include_runtime_permit=True,
):
    transaction_dir = (
        transaction['state_dir']
        / 'backups'
        / 'deploy-transactions'
        / name
    )
    transaction_dir.mkdir(parents=True)
    (transaction_dir / 'ACTIVATION_STARTED').write_text(
        'interrupted-release\n',
        encoding='utf-8',
    )
    if terminal:
        (transaction_dir / terminal).write_text('success\n', encoding='utf-8')
        (transaction_dir / terminal).chmod(0o600)
    deployments = transaction['state_dir'] / 'deployments'
    deployments.mkdir(parents=True, exist_ok=True)
    persistent_guard = deployments / 'activation-in-progress'
    persistent_guard.write_text(
        'release_id=interrupted-release\n'
        f'transaction={transaction_dir}\n'
        'started_at=2026-07-18T00:00:00Z\n',
        encoding='utf-8',
    )
    persistent_guard.chmod(0o600)
    runtime_guard = Path(transaction['env']['RUNTIME_BOOT_GUARD_DIR'])
    if include_runtime_permit:
        runtime_guard.mkdir(parents=True)
        permit = runtime_guard / 'activation-permit'
        permit.write_text(
            'release_id=interrupted-release\n'
            f'transaction={transaction_dir}\n',
            encoding='utf-8',
        )
        permit.chmod(0o600)
    return transaction_dir


def _configure_formal_smoke(transaction, *, provider='QWeather'):
    """为激活事务准备完全离线的正式天气烟测桩。"""
    private_key = transaction['qweather_private_dir'] / 'qweather-formal-current.pem'
    pending_key = (
        transaction['qweather_private_dir']
        / f'.qweather-jwt.pending-{transaction["new_release"].name}'
    )
    if not pending_key.exists() and not pending_key.is_symlink():
        if private_key.is_file() and not private_key.is_symlink():
            pending_key.write_bytes(private_key.read_bytes())
            pending_key.chmod(0o600)
        else:
            _write_test_ed25519_private_key(pending_key, mode=0o600)
    transaction['env']['QWEATHER_PENDING_KEY_PATH'] = str(pending_key)
    transaction['env']['FAKE_QWEATHER_PENDING_KEY'] = str(pending_key)
    transaction['env']['FAKE_QWEATHER_FINAL_KEY'] = str(private_key)
    transaction['env']['FAKE_QWEATHER_KEY_STOP_AUDIT'] = str(
        transaction['state_dir'] / 'qweather-key-stop-audit.json'
    )
    transaction['qweather_pending_key'] = pending_key
    transaction['qweather_final_key'] = private_key
    staged_text = f"""DEBUG=true
RELEASE_VALUE=new
QWEATHER_AUTH_MODE=jwt
DATABASE_URI=sqlite:///{transaction['database_file'].as_posix()}
REDIS_URL=redis://127.0.0.1:6379/0
QWEATHER_KEY=
QWEATHER_API_BASE=https://unit-test.qweatherapi.com/v7
QWEATHER_JWT_KID=test-kid
QWEATHER_JWT_PROJECT_ID=test-project
QWEATHER_JWT_PRIVATE_KEY_PATH={private_key}
QWEATHER_EXPECTED_KID=test-kid
QWEATHER_EXPECTED_PROJECT_ID=test-project
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
    budget_mode_file = transaction['state_dir'] / 'formal-smoke-budget-mode'
    budget_helper = transaction['state_dir'] / 'formal-smoke-budget-snapshot'
    _write_executable(
        budget_helper,
        f"""#!/usr/bin/env python3
import json
from pathlib import Path

counter = Path({str(counter_file)!r})
mode_file = Path({str(budget_mode_file)!r})
count = int(counter.read_text(encoding='utf-8')) if counter.exists() else 0
mode = mode_file.read_text(encoding='utf-8').strip() if mode_file.exists() else 'valid'
if mode == 'duplicate':
    used = 10 + (count * 4)
    endpoints = {{
        'weather_now': count * 2,
        'weather_7d_forecast': count,
        'weatheralert_v1_current': count,
    }} if count else {{}}
elif mode == 'zero':
    used = 10
    endpoints = {{}}
elif mode == 'unexpected':
    used = 10 + (count * 3)
    endpoints = {{
        'weather_now': count,
        'weather_7d_forecast': count,
        'airquality_v1_current': count,
    }} if count else {{}}
else:
    used = 10 + (count * 3)
    endpoints = {{
        'weather_now': count,
        'weather_7d_forecast': count,
        'weatheralert_v1_current': count,
    }} if count else {{}}
print(json.dumps({{
    'backend': 'redis',
    'month': '2026-07',
    'used': used,
    'endpoints': endpoints,
}}, sort_keys=True, separators=(',', ':')))
""",
    )
    transaction['env']['QWEATHER_BUDGET_SNAPSHOT_HELPER'] = str(budget_helper)
    lease_token_file = transaction['state_dir'] / 'formal-smoke-lease-token'
    lease_mode_file = transaction['state_dir'] / 'formal-smoke-lease-mode'
    lease_helper = transaction['state_dir'] / 'formal-smoke-lease-reserve'
    _write_executable(
        lease_helper,
        f"""#!/usr/bin/env python3
import os
from pathlib import Path

token = os.environ.get('CASE_WEATHER_FORMAL_SMOKE_LEASE_TOKEN', '')
if len(token) != 64 or any(char not in '0123456789abcdef' for char in token):
    raise SystemExit(92)
mode_file = Path({str(lease_mode_file)!r})
if mode_file.exists() and mode_file.read_text(encoding='utf-8').strip() == 'busy':
    raise SystemExit(75)
Path({str(lease_token_file)!r}).write_text(token, encoding='ascii')
""",
    )
    transaction['env']['FORMAL_SMOKE_LEASE_HELPER'] = str(lease_helper)
    weather_sync = transaction['new_release'] / 'app' / 'scripts' / 'weather_cache_sync.sh'
    _write_executable(
        weather_sync,
        f"""#!/bin/bash
set -euo pipefail
if [ "$#" -ne 1 ] || [ "$1" != "--skip-nowcast" ]; then
    echo '正式烟测必须显式跳过 nowcast' >&2
    exit 91
fi
if [ -z "${{CASE_WEATHER_FORMAL_SMOKE_LEASE_TOKEN:-}}" ] \
    || [ "$(cat {str(lease_token_file)!r})" != "$CASE_WEATHER_FORMAL_SMOKE_LEASE_TOKEN" ]; then
    echo '正式烟测缺少预占的全局租约' >&2
    exit 92
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


def _retarget_formal_retry(transaction, suffix='retry'):
    """用新的 release ID 重试同一冻结 commit，避免复用旧 pending 路径。"""
    source_release = transaction['new_release']
    retry_release = source_release.parent / f'{source_release.name}-{suffix}'
    shutil.copytree(source_release, retry_release, symlinks=True)
    pending = (
        transaction['qweather_private_dir']
        / f'.qweather-jwt.pending-{retry_release.name}'
    )
    transaction['new_release'] = retry_release
    transaction['qweather_pending_key'] = pending
    transaction['env']['NEW_RELEASE'] = str(retry_release)
    transaction['env']['STAGED_ENV_FILE'] = str(retry_release / 'staged.env')
    transaction['env']['QWEATHER_PENDING_KEY_PATH'] = str(pending)
    transaction['env']['FAKE_QWEATHER_PENDING_KEY'] = str(pending)
    return retry_release


def _configure_formal_jwt_smoke(transaction, private_key, *, provider='QWeather'):
    staged_text, counter_file = _configure_formal_smoke(
        transaction,
        provider=provider,
    )
    pending = transaction['qweather_pending_key']
    pending.unlink(missing_ok=True)
    if private_key.is_symlink():
        pending.symlink_to(private_key)
    else:
        pending.write_bytes(private_key.read_bytes())
        pending.chmod(0o600)
    return staged_text, counter_file


def test_success_switches_release_only_after_migration_and_health(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    started_at = int(time.time())
    original_crontab = transaction['root_crontab'].read_bytes()

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
        installed = (transaction['unit_dir'] / unit).read_text(encoding='utf-8')
        if unit == 'case-weather-backup.service':
            assert 'EnvironmentFile=' in installed
            assert 'ExecStart=/bin/bash ' in installed
            assert 'TimeoutStartSec=15min' in installed
        else:
            assert installed == f'new unit {unit}\n'
    for unit in LEGACY_UNITS + RETIRED_BOOTSTRAP_UNITS:
        assert not (transaction['unit_dir'] / unit).exists()
        assert not (transaction['fake_state'] / f'{unit}.enabled').exists()
        assert not (transaction['fake_state'] / f'{unit}.active').exists()
    for unit in ('case-weather.service',) + START_TIMER_UNITS:
        assert (transaction['fake_state'] / f'{unit}.active').exists()
    for unit in DEFERRED_TIMER_UNITS:
        assert not (transaction['fake_state'] / f'{unit}.active').exists()
        assert not (transaction['fake_state'] / f'{unit}.enabled').exists()
    cron_bytes = transaction['root_crontab'].read_bytes()
    assert cron_bytes == original_crontab
    assert not (transaction['fake_state'] / 'crontab-install-count').exists()
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
    transaction_dir = committed_markers[0].parent
    assert (transaction_dir / 'root-crontab.before').read_bytes() == original_crontab
    assert (transaction_dir / 'root-crontab.before.sha256').read_text(
        encoding='ascii'
    ).strip() == hashlib.sha256(original_crontab).hexdigest()
    validation_archives = list(
        (transaction_dir / 'managed-backup-validation').glob('*.db.gz')
    )
    assert len(validation_archives) == 1
    assert (transaction_dir / 'managed-backup-validation.db').is_file()
    assert (transaction_dir / 'managed-daily-backup-validation.db').is_file()
    assert (transaction_dir / 'ACTUAL_BACKUP_UNIT_VERIFIED').is_file()
    assert not (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).exists()
    assert not (
        Path(transaction['env']['RUNTIME_BOOT_GUARD_DIR']) / 'activation-permit'
    ).exists()
    backup_runtime = (
        transaction['state_dir'] / 'backups' / 'backup-runtime.env'
    ).read_text(encoding='utf-8')
    assert f'BACKUP_DATABASE_FILE={transaction["database_file"]}' in backup_runtime
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    transient_index = next(
        index for index, action in enumerate(actions)
        if action.startswith('systemd-run ')
    )
    transient_action = actions[transient_index]
    assert '--property=TimeoutStartSec=15min' in transient_action
    assert '--if-present' not in transient_action
    assert f'--setenv=BACKUP_DATABASE_FILE={transaction["database_file"]}' in transient_action
    actual_backup_index = actions.index('start case-weather-backup.service')
    assert transient_index < actions.index('restart case-weather.service')
    assert transient_index < actual_backup_index < actions.index('restart case-weather.service')


def test_crontab_migration_is_idempotent_when_legacy_lines_are_absent(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    unrelated = b'MAILTO=ops@example.invalid\n17 2 * * * /usr/local/bin/unrelated-job'
    transaction['root_crontab'].write_bytes(unrelated)

    result = _run_activation(transaction)

    assert result.returncode == 0, result.stderr
    assert transaction['root_crontab'].read_bytes() == unrelated
    assert not (transaction['fake_state'] / 'crontab-install-count').exists()


def test_second_release_with_no_legacy_cron_still_rolls_back_migration_failure(
    tmp_path,
):
    transaction = _prepare_transaction(tmp_path, migration_exit=23)
    unrelated = b'MAILTO=ops@example.invalid\n17 2 * * * /usr/local/bin/unrelated-job'
    transaction['root_crontab'].write_bytes(unrelated)

    result = _run_activation(transaction)

    assert result.returncode == 23
    assert transaction['root_crontab'].read_bytes() == unrelated
    assert _database_value(transaction['database_file']) == 'old'
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert 'RELEASE_VALUE=old' in (transaction['state_dir'] / '.env').read_text(
        encoding='utf-8'
    )

    assert list((transaction['state_dir'] / 'backups').rglob('ROLLED_BACK'))
    assert (transaction['fake_state'] / 'case-weather.service.active').exists()


def test_crontab_migration_keeps_missing_root_crontab_absent(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction['root_crontab'].unlink()

    result = _run_activation(transaction)

    assert result.returncode == 0, result.stderr
    assert not transaction['root_crontab'].exists()
    assert not (transaction['fake_state'] / 'crontab-install-count').exists()


@pytest.mark.parametrize('backup_style', ('legacy', 'release'))
def test_activation_rejects_legacy_cron_until_controlled_migration(
    tmp_path,
    backup_style,
):
    transaction = _prepare_transaction(tmp_path)
    legacy_backup, sync_cron = _legacy_cron_lines(transaction['state_dir'])
    backup_cron = (
        legacy_backup
        if backup_style == 'legacy'
        else _release_backup_cron_line(
            transaction['state_dir'],
            transaction['release_root'],
        )
    )
    transaction['root_crontab'].write_text(
        f'MAILTO=ops@example.invalid\n{backup_cron}\n{sync_cron}\n',
        encoding='utf-8',
    )
    original = transaction['root_crontab'].read_bytes()

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '受控维护窗口' in result.stderr
    assert transaction['root_crontab'].read_bytes() == original
    assert not (transaction['fake_state'] / 'crontab-install-count').exists()
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    assert not any(
        action.startswith(('stop ', 'start ', 'restart ', 'enable ', 'disable '))
        for action in actions
    )


def test_crontab_concurrent_unrelated_change_is_preserved_without_install(
    tmp_path,
):
    transaction = _prepare_transaction(tmp_path)
    original = transaction['root_crontab'].read_bytes()
    concurrent = '\n15 4 * * * /usr/local/sbin/concurrent-before-install\n'
    transaction['env']['FAKE_CRONTAB_APPEND_AFTER_READ_COUNT'] = '1'
    transaction['env']['FAKE_CRONTAB_APPEND_BEFORE_INSTALL'] = concurrent

    result = _run_activation(transaction)

    assert result.returncode == 0, result.stderr
    assert transaction['root_crontab'].read_bytes() == original + concurrent.encode('utf-8')
    assert not (transaction['fake_state'] / 'crontab-install-count').exists()


@pytest.mark.parametrize('cron_state', ('partial', 'duplicate', 'drift'))
def test_crontab_preflight_rejects_partial_duplicate_or_drift_before_mutation(
    tmp_path,
    cron_state,
):
    transaction = _prepare_transaction(tmp_path)
    backup_cron, sync_cron = _legacy_cron_lines(transaction['state_dir'])
    if cron_state == 'partial':
        cron_text = f'MAILTO=ops@example.invalid\n{backup_cron}\n'
    elif cron_state == 'duplicate':
        cron_text = f'{backup_cron}\n{backup_cron}\n{sync_cron}\n'
    else:
        cron_text = f'5 3 * * * {transaction["state_dir"]}/backup.sh\n{sync_cron}\n'
    original = cron_text.encode('utf-8')
    transaction['root_crontab'].write_bytes(original)

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '尚未修改生产状态' in result.stderr
    assert transaction['root_crontab'].read_bytes() == original
    assert _database_value(transaction['database_file']) == 'old'
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    assert not any(action.startswith(('stop ', 'start ', 'restart ', 'enable ', 'disable ')) for action in actions)
    assert not (transaction['fake_state'] / 'crontab-install-count').exists()


def test_scheduler_stop_order_and_rollback_never_restart_legacy_oneshots(tmp_path):
    transaction = _prepare_transaction(tmp_path, migration_exit=23)

    result = _run_activation(transaction)

    assert result.returncode == 23
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    stoppable_units = tuple(
        unit for unit in ALL_UNITS if unit != 'case-weather-backup.service'
    )
    first_stop = {
        unit: actions.index(f'stop {unit}')
        for unit in stoppable_units
    }
    assert max(first_stop[unit] for unit in MANAGED_TIMER_UNITS + LEGACY_TIMER_UNITS) < min(
        first_stop[unit]
        for unit in SERVICE_UNITS + LEGACY_SERVICE_UNITS
        if unit != 'case-weather-backup.service'
    )
    assert 'stop case-weather-backup.service' not in actions
    for unit in ('case-weather-backup.service',) + LEGACY_SERVICE_UNITS:
        assert f'start {unit}' not in actions
        assert f'restart {unit}' not in actions


def test_rollback_never_rewrites_unrelated_cron(tmp_path):
    transaction = _prepare_transaction(tmp_path, migration_exit=23)
    original = transaction['root_crontab'].read_bytes()

    result = _run_activation(transaction)

    assert result.returncode == 23
    assert transaction['root_crontab'].read_bytes() == original
    assert not (transaction['fake_state'] / 'crontab-install-count').exists()


def test_activation_fails_closed_when_legacy_process_survives_stop(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    (transaction['fake_state'] / 'legacy-process-running').touch()
    original = transaction['root_crontab'].read_bytes()

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '仍在运行的旧调度进程' in result.stderr
    assert transaction['root_crontab'].read_bytes() == original
    assert _database_value(transaction['database_file']) == 'old'


def test_active_daily_backup_blocks_release_before_any_mutation(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    (transaction['fake_state'] / 'case-weather-backup.service.activating').touch()
    original_crontab = transaction['root_crontab'].read_bytes()

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '未中止备份' in result.stderr
    assert transaction['root_crontab'].read_bytes() == original_crontab
    assert _database_value(transaction['database_file']) == 'old'
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    assert 'stop case-weather-backup.service' not in actions
    assert not any(
        action.startswith(('stop ', 'start ', 'restart ', 'enable ', 'disable '))
        for action in actions
    )


@pytest.mark.parametrize(
    ('environment', 'expected_message'),
    (
        ({'FAKE_FAIL_BACKUP_STATE_QUERY': '1'}, 'LoadState/ActiveState'),
        ({'FAKE_BACKUP_ACTIVE_STATE': 'maintenance'}, 'LoadState/ActiveState'),
        (
            {
                'FAKE_COMBINED_BACKUP_LOAD_STATE': 'maintenance',
                'FAKE_BACKUP_ACTIVE_STATE': 'inactive',
            },
            'LoadState/ActiveState',
        ),
    ),
)
def test_backup_state_uncertainty_fails_closed_before_mutation(
    tmp_path,
    environment,
    expected_message,
):
    transaction = _prepare_transaction(tmp_path)
    transaction['env'].update(environment)
    original_crontab = transaction['root_crontab'].read_bytes()

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert expected_message in result.stderr
    assert transaction['root_crontab'].read_bytes() == original_crontab
    assert _database_value(transaction['database_file']) == 'old'
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    assert not any(
        action.startswith(('stop ', 'start ', 'restart ', 'enable ', 'disable '))
        for action in actions
    )


def test_load_state_query_failure_during_capture_is_pre_mutation(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction['env']['FAKE_FAIL_LOAD_STATE_QUERY'] = 'case-weather.service'

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '无法可靠读取 systemd 单元 LoadState' in result.stderr
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'
    assert 'RELEASE_VALUE=old' in (transaction['state_dir'] / '.env').read_text(
        encoding='utf-8'
    )
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    assert not any(
        action.startswith(('stop ', 'start ', 'restart ', 'enable ', 'disable '))
        for action in actions
    )
    for unit in ALL_UNITS:
        assert (transaction['unit_dir'] / unit).read_text(encoding='utf-8') == (
            f'old unit {unit}\n'
        )


def test_load_state_query_failure_during_quiesce_never_reaches_migration(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction['env']['FAKE_FAIL_LOAD_STATE_QUERY_ON'] = 'case-weather.service:3'

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '无法可靠读取 systemd 单元 LoadState' in result.stderr
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'
    assert 'RELEASE_VALUE=old' in (transaction['state_dir'] / '.env').read_text(
        encoding='utf-8'
    )
    assert list((transaction['state_dir'] / 'backups').rglob('ROLLED_BACK'))
    assert not list((transaction['state_dir'] / 'backups').rglob('ROLLBACK_REQUIRED.txt'))


def test_missing_guard_dropin_blocks_before_runtime_mutation(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    target = (
        transaction['unit_dir']
        / 'case-weather.service.d'
        / '10-case-weather-activation-guard.conf'
    )
    target.unlink()
    original_crontab = transaction['root_crontab'].read_bytes()

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert 'drop-in 文件无效' in result.stderr
    assert transaction['root_crontab'].read_bytes() == original_crontab
    assert _database_value(transaction['database_file']) == 'old'
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    assert not any(
        action.startswith(('stop ', 'start ', 'restart ', 'enable ', 'disable '))
        for action in actions
    )


@pytest.mark.parametrize(
    'override',
    (
        '[Unit]\nConditionArchitecture=\n',
        '[Unit]\nConditionKernelCommandLine=|always-allow\n',
        '[Unit]\nConditionPathExists=|/\n',
    ),
)
def test_later_dropin_cannot_reset_or_bypass_activation_guard(
    tmp_path,
    override,
):
    transaction = _prepare_transaction(tmp_path)
    bypass = (
        transaction['unit_dir']
        / 'case-weather.service.d'
        / '99-bypass.conf'
    )
    bypass.write_text(override, encoding='utf-8')
    bypass.chmod(0o644)

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '尚未加载预期的断电保护' in result.stderr
    assert _database_value(transaction['database_file']) == 'old'
    assert not (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).exists()


def test_guard_preflight_rejects_stale_systemd_manager_state(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction['env']['FAKE_NEED_DAEMON_RELOAD_UNIT'] = 'case-weather.service'

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '尚未加载磁盘上的最新断电保护配置' in result.stderr
    assert _database_value(transaction['database_file']) == 'old'


@pytest.mark.parametrize('database_config', ('missing', 'duplicate'))
def test_invalid_backup_database_config_blocks_before_mutation(
    tmp_path,
    database_config,
):
    transaction = _prepare_transaction(tmp_path)
    database_uri = f'sqlite:///{transaction["database_file"].as_posix()}'
    if database_config == 'missing':
        staged = 'DEBUG=true\nRELEASE_VALUE=new\n'
    else:
        staged = (
            'DEBUG=true\nRELEASE_VALUE=new\n'
            f'DATABASE_URI={database_uri}\n'
            f'DATABASE_URI={database_uri}\n'
        )
    (transaction['new_release'] / 'staged.env').write_text(staged, encoding='utf-8')
    original_crontab = transaction['root_crontab'].read_bytes()

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '备份配置不唯一或格式无效' in result.stderr
    assert transaction['root_crontab'].read_bytes() == original_crontab
    assert _database_value(transaction['database_file']) == 'old'
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    assert not any(
        action.startswith(('stop ', 'start ', 'restart ', 'enable ', 'disable '))
        for action in actions
    )


def test_external_database_path_is_rejected_before_runtime_mutation(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    external_database = tmp_path / 'external' / 'live.db'
    (transaction['new_release'] / 'staged.env').write_text(
        f'DEBUG=true\nRELEASE_VALUE=new\n'
        f'DATABASE_URI=sqlite:///{external_database.as_posix()}\n',
        encoding='utf-8',
    )
    original_crontab = transaction['root_crontab'].read_bytes()

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '受控 instance 或 storage' in result.stderr
    assert transaction['root_crontab'].read_bytes() == original_crontab
    assert _database_value(transaction['database_file']) == 'old'
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    assert not any(
        action.startswith(('stop ', 'start ', 'restart ', 'enable ', 'disable '))
        for action in actions
    )


@pytest.mark.parametrize('variable', ('DATABASE_FILE', 'DATABASE_URI'))
def test_inherited_database_override_is_rejected_before_runtime_mutation(
    tmp_path,
    variable,
):
    transaction = _prepare_transaction(tmp_path)
    transaction['env'][variable] = '/tmp/forbidden-override'
    original_crontab = transaction['root_crontab'].read_bytes()

    result = _run_activation(transaction)

    assert result.returncode == 2
    assert '禁止继承 DATABASE_FILE 或 DATABASE_URI' in result.stderr
    assert transaction['root_crontab'].read_bytes() == original_crontab
    assert not transaction['systemctl_log'].exists()


def test_backup_start_race_waits_for_completion_without_killing_backup(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction['env']['FAKE_START_BACKUP_AFTER_TIMER_STOP'] = '1'
    transaction['env']['FAKE_BACKUP_FINISH_ON_ACTIVE_CHECK'] = '2'
    transaction['env']['BACKUP_WAIT_ATTEMPTS'] = '3'
    transaction['env']['BACKUP_WAIT_SLEEP_SECONDS'] = '0'

    result = _run_activation(transaction)

    assert result.returncode == 0, result.stderr
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    assert 'stop case-weather-backup.service' not in actions
    assert not any(
        (
            transaction['fake_state']
            / f'case-weather-backup.service.{state}'
        ).exists()
        for state in ('active', 'activating', 'reloading', 'deactivating')
    )


def test_post_commit_allows_managed_backup_started_by_persistent_timer(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction['env']['FAKE_START_BACKUP_AFTER_TIMER_RESTART'] = '1'
    transaction['env']['FAKE_BACKUP_FINISH_ON_ACTIVE_CHECK'] = '2'
    transaction['env']['BACKUP_WAIT_ATTEMPTS'] = '3'
    transaction['env']['BACKUP_WAIT_SLEEP_SECONDS'] = '0'

    result = _run_activation(transaction)

    assert result.returncode == 0, result.stderr
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    assert 'restart case-weather-backup.timer' in actions
    assert 'stop case-weather-backup.service' not in actions
    assert not list(
        (transaction['state_dir'] / 'backups').rglob('POST_COMMIT_ATTENTION.txt')
    )


def test_stalled_backup_race_restores_only_backup_schedule(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    original_crontab = transaction['root_crontab'].read_bytes()
    transaction['env']['FAKE_START_BACKUP_AFTER_TIMER_STOP'] = '1'
    transaction['env']['BACKUP_WAIT_ATTEMPTS'] = '1'
    transaction['env']['BACKUP_WAIT_SLEEP_SECONDS'] = '0'

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '公网服务保持原状态' in result.stderr
    assert transaction['root_crontab'].read_bytes() == original_crontab
    assert (transaction['fake_state'] / 'case-weather.service.active').exists()
    assert (transaction['fake_state'] / 'case-weather-backup.timer.active').exists()
    assert (
        transaction['fake_state'] / 'case-weather-backup.service.activating'
    ).exists()
    assert list((transaction['state_dir'] / 'backups').rglob('ROLLED_BACK'))
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    assert 'stop case-weather.service' not in actions
    assert 'stop case-weather-backup.service' not in actions


def test_stability_window_detects_post_activation_timer_cleanup(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction['env']['POST_COMMIT_STABILITY_SECONDS'] = '1'
    transaction['env']['FAKE_STOP_CACHE_ON_ACTIVE_CHECK'] = '3'

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '发布后单元未处于 active: case-weather-cache-bootstrap.timer' in result.stderr
    markers = list(
        (transaction['state_dir'] / 'backups').rglob('POST_COMMIT_ATTENTION.txt')
    )
    assert len(markers) == 1
    assert not list((transaction['state_dir'] / 'backups').rglob('COMMITTED'))
    assert (
        transaction['fake_state'] / 'case-weather-cache-bootstrap.timer.active'
    ).exists()
    assert '已逐个补齐并复核' in markers[0].read_text(encoding='utf-8')


def test_migration_failure_restores_database_release_and_unit_state(tmp_path):
    transaction = _prepare_transaction(tmp_path, migration_exit=23)
    original_crontab = transaction['root_crontab'].read_bytes()

    result = _run_activation(transaction)

    assert result.returncode == 23
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'
    assert 'RELEASE_VALUE=old' in (transaction['state_dir'] / '.env').read_text(encoding='utf-8')
    for unit in ALL_UNITS:
        assert (transaction['unit_dir'] / unit).read_text(encoding='utf-8') == f'old unit {unit}\n'
    for unit in (
        'case-weather.service',
        'case-weather-backup.timer',
        'case-weather-cache.timer',
        'case-weather-risk-precompute.timer',
        'case-weather-usage-cleanup.timer',
    ) + LEGACY_TIMER_UNITS:
        assert (transaction['fake_state'] / f'{unit}.active').exists()
    for unit in ('case-weather-backup.service',) + LEGACY_SERVICE_UNITS:
        assert not (transaction['fake_state'] / f'{unit}.active').exists()
    assert transaction['root_crontab'].read_bytes() == original_crontab
    assert not (
        transaction['fake_state'] / 'case-weather-cache-bootstrap.timer.active'
    ).exists()
    assert (transaction['fake_state'] / 'case-weather-cache.timer.enabled').exists()
    assert not (
        transaction['fake_state'] / 'case-weather-cache-bootstrap.timer.enabled'
    ).exists()
    assert not list((transaction['state_dir'] / 'backups').rglob('ROLLBACK_REQUIRED.txt'))
    assert len(list((transaction['state_dir'] / 'backups').rglob('ROLLED_BACK'))) == 1
    assert not (
        transaction['state_dir'] / 'backups' / 'backup-runtime.env'
    ).exists()
    assert len(
        list(
            (transaction['state_dir'] / 'backups').rglob(
                'backup-runtime-from-failed-release.env'
            )
        )
    ) == 1


def test_migration_failure_restores_previous_backup_runtime_environment(tmp_path):
    transaction = _prepare_transaction(tmp_path, migration_exit=23)
    runtime_env = transaction['state_dir'] / 'backups' / 'backup-runtime.env'
    previous = (
        f'BACKUP_DATABASE_FILE={transaction["state_dir"]}/storage/previous.db\n'
        'BACKUP_PRUNE=1\n'
    )
    runtime_env.write_text(previous, encoding='utf-8')
    runtime_env.chmod(0o600)

    result = _run_activation(transaction)

    assert result.returncode == 23
    assert runtime_env.read_text(encoding='utf-8') == previous
    assert runtime_env.stat().st_mode & 0o777 == 0o600


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


@pytest.mark.parametrize(
    ('weather_timer_phase', 'unit', 'captured_state', 'expected_timer'),
    (
        ('recurring', 'case-weather-cache.timer', 'activating', 'case-weather-cache.timer'),
        ('recurring', 'case-weather-cache.timer', 'reloading', 'case-weather-cache.timer'),
        ('recurring', 'case-weather-cache.timer', 'deactivating', 'case-weather-cache.timer'),
        (
            'bootstrap',
            'case-weather-cache-bootstrap.timer',
            'activating',
            'case-weather-cache-bootstrap.timer',
        ),
        (
            'bootstrap',
            'case-weather-cache-bootstrap.timer',
            'reloading',
            'case-weather-cache-bootstrap.timer',
        ),
        (
            'bootstrap',
            'case-weather-cache-bootstrap.timer',
            'deactivating',
            'case-weather-cache-bootstrap.timer',
        ),
        (
            'writer',
            'case-weather-cache.service',
            'deactivating',
            'case-weather-cache-bootstrap.timer',
        ),
    ),
)
def test_migration_failure_restores_transitional_weather_phase(
    tmp_path,
    weather_timer_phase,
    unit,
    captured_state,
    expected_timer,
):
    transaction = _prepare_transaction(
        tmp_path,
        migration_exit=23,
        weather_timer_phase=weather_timer_phase,
    )
    for state in ('active', 'activating', 'reloading', 'deactivating'):
        (transaction['fake_state'] / f'{unit}.{state}').unlink(missing_ok=True)
    (transaction['fake_state'] / f'{unit}.{captured_state}').touch()

    result = _run_activation(transaction)

    assert result.returncode == 23
    assert (
        transaction['fake_state'] / f'{expected_timer}.active'
    ).exists()
    other_timer = (
        'case-weather-cache-bootstrap.timer'
        if expected_timer == 'case-weather-cache.timer'
        else 'case-weather-cache.timer'
    )
    assert not (transaction['fake_state'] / f'{other_timer}.active').exists()
    for state in ('activating', 'reloading', 'deactivating'):
        assert not (transaction['fake_state'] / f'{unit}.{state}').exists()


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


def test_first_release_prepare_failure_removes_new_weather_unit_enable_links(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    for unit in (
        'case-weather-cache-bootstrap.timer',
        'case-weather-cache.timer',
        'case-weather-cache.service',
    ):
        (transaction['unit_dir'] / unit).unlink()
        (transaction['fake_state'] / f'{unit}.exists').unlink()
        (transaction['fake_state'] / f'{unit}.enabled').unlink(missing_ok=True)
        (transaction['fake_state'] / f'{unit}.active').unlink(missing_ok=True)
    (transaction['new_release'] / 'app' / 'scripts' / 'update_env_value.py').write_text(
        'raise SystemExit(31)\n',
        encoding='utf-8',
    )

    result = _run_activation(transaction)

    assert result.returncode == 31
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    for unit in (
        'case-weather-cache-bootstrap.timer',
        'case-weather-cache.timer',
        'case-weather-cache.service',
    ):
        assert not (transaction['unit_dir'] / unit).exists()
        assert not (transaction['fake_state'] / f'{unit}.enabled').exists()


def test_candidate_health_failure_rolls_back_new_code_database_and_units(tmp_path):
    transaction = _prepare_transaction(tmp_path, candidate_health_ok=False)

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'
    assert 'RELEASE_VALUE=old' in (transaction['state_dir'] / '.env').read_text(encoding='utf-8')
    for unit in ALL_UNITS:
        assert (transaction['unit_dir'] / unit).read_text(encoding='utf-8') == f'old unit {unit}\n'


def test_managed_backup_validation_failure_rolls_back_before_public_switch(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction['env']['FAKE_FAIL_SYSTEMD_RUN'] = '1'
    original_crontab = transaction['root_crontab'].read_bytes()

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '备份 transient unit 验证失败' in result.stderr
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'
    assert transaction['root_crontab'].read_bytes() == original_crontab
    assert not list((transaction['state_dir'] / 'backups' / 'daily').glob('*.db.gz'))
    assert 'restart case-weather.service' not in (
        transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    )


def test_managed_backup_non_sqlite_archive_fails_quick_check(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction['env']['FAKE_INVALID_SQLITE_BACKUP'] = '1'
    original_crontab = transaction['root_crontab'].read_bytes()

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '未通过 SQLite quick_check' in result.stderr
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'
    assert transaction['root_crontab'].read_bytes() == original_crontab
    assert 'restart case-weather.service' not in (
        transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    )


def test_actual_installed_backup_unit_failure_rolls_back_before_public_start(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    transaction['env']['FAKE_FAIL_ACTUAL_BACKUP_SERVICE'] = '1'

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '正式日备份 unit 实际执行失败' in result.stderr
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    assert 'start case-weather-backup.service' in actions
    assert 'restart case-weather.service' not in actions


@pytest.mark.parametrize(
    'failure_environment',
    (
        {'FAKE_FAIL_SYSTEMD_RUN': '1'},
        {'FAKE_FAIL_ACTUAL_BACKUP_SERVICE': '1'},
    ),
)
def test_formal_backup_failures_happen_before_the_only_weather_request(
    tmp_path,
    failure_environment,
):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    transaction['env'].update(failure_environment)

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert not counter_file.exists()
    receipt_root = (
        transaction['state_dir'] / 'deployments' / 'formal-cache-smokes'
    )
    assert not receipt_root.exists()
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'
    actions = transaction['systemctl_log'].read_text(encoding='utf-8').splitlines()
    assert 'restart case-weather.service' not in actions


def test_public_health_failure_keeps_forward_migrated_database(tmp_path):
    transaction = _prepare_transaction(tmp_path, public_health_ok=False)
    original_crontab = transaction['root_crontab'].read_bytes()

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert transaction['current_link'].resolve() == transaction['new_release'].resolve()
    assert _database_value(transaction['database_file']) == 'new'
    assert 'RELEASE_VALUE=new' in (transaction['state_dir'] / '.env').read_text(
        encoding='utf-8'
    )
    markers = list((transaction['state_dir'] / 'backups').rglob('POST_COMMIT_ATTENTION.txt'))
    assert len(markers) == 1
    assert '持久开机门保持启用' in markers[0].read_text(encoding='utf-8')
    assert not (transaction['fake_state'] / 'case-weather.service.active').exists()
    for unit in START_TIMER_UNITS:
        assert (transaction['fake_state'] / f'{unit}.enabled').exists()
        assert not (transaction['fake_state'] / f'{unit}.active').exists()
    for unit in DEFERRED_TIMER_UNITS:
        assert not (transaction['fake_state'] / f'{unit}.enabled').exists()
        assert not (transaction['fake_state'] / f'{unit}.active').exists()
    assert transaction['root_crontab'].read_bytes() == original_crontab
    assert (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).is_file()
    assert (
        Path(transaction['env']['RUNTIME_BOOT_GUARD_DIR']) / 'activation-permit'
    ).exists() is False


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
    for unit in START_TIMER_UNITS:
        assert (transaction['fake_state'] / f'{unit}.active').exists()
    marker_text = markers[0].read_text(encoding='utf-8')
    assert '已逐个补齐并复核' in marker_text


@pytest.mark.parametrize(
    'broken_hook',
    ('FAKE_BAD_ON_SUCCESS_UNIT', 'FAKE_BAD_ON_FAILURE_UNIT'),
)
def test_post_start_verification_failure_persists_blocking_marker(
    tmp_path,
    broken_hook,
):
    transaction = _prepare_transaction(tmp_path)
    transaction['env'][broken_hook] = 'case-weather-cache.service'

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
    assert '必须显式确认其精确事务' in blocked.stderr
    assert transaction['current_link'].resolve() == transaction['new_release'].resolve()
    assert _database_value(transaction['database_file']) == 'new'


@pytest.mark.parametrize(
    ('cache_result', 'expected_returncode', 'dispatch_expected'),
    (
        ('success', 0, True),
        ('failure', 1, False),
    ),
)
def test_cache_attempt_transitions_to_recurring_timer(
    tmp_path,
    cache_result,
    expected_returncode,
    dispatch_expected,
):
    transaction = _prepare_transaction(tmp_path)
    cache_unit = transaction['new_release'] / 'systemd' / 'case-weather-cache.service'
    cache_unit.write_text(
        '[Unit]\n'
        'OnSuccess=case-weather-dispatch.service case-weather-cache.timer\n'
        'OnFailure=case-weather-cache.timer\n'
        '[Service]\n'
        'Type=oneshot\n',
        encoding='utf-8',
    )
    activated = _run_activation(transaction)
    assert activated.returncode == 0, activated.stderr
    (transaction['fake_state'] / 'case-weather-cache-bootstrap.timer.active').unlink()
    transaction['env']['FAKE_CACHE_RESULT'] = cache_result

    attempted = subprocess.run(
        [
            transaction['env']['SYSTEMCTL_BIN'],
            'start',
            'case-weather-cache.service',
        ],
        cwd=ROOT,
        env=transaction['env'],
        text=True,
        capture_output=True,
        check=False,
    )

    assert attempted.returncode == expected_returncode
    assert (
        transaction['fake_state'] / 'case-weather-cache.timer.active'
    ).exists()
    assert (
        transaction['fake_state'] / 'case-weather-dispatch.service.active'
    ).exists() is dispatch_expected


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

    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(unfinished)
    confirmed = _run_activation(transaction)

    assert confirmed.returncode == 0, confirmed.stderr
    assert (unfinished / 'RECOVERY_CONFIRMED').is_file()
    assert transaction['current_link'].resolve() == transaction['new_release'].resolve()


def test_interrupted_guard_requires_exact_ack_then_recovers(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    interrupted = _seed_interrupted_activation_guard(
        transaction,
        'interrupted-with-guard',
    )

    blocked = _run_activation(transaction)

    assert blocked.returncode != 0
    assert '必须显式确认其精确事务' in blocked.stderr
    assert (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).is_file()
    assert not (interrupted / 'RECOVERY_CONFIRMED').exists()

    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(interrupted)
    recovered = _run_activation(transaction)

    assert recovered.returncode == 0, recovered.stderr
    assert (interrupted / 'RECOVERY_CONFIRMED').is_file()
    assert (interrupted / 'activation-in-progress.recovered').is_file()
    assert not (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).exists()
    assert not (
        Path(transaction['env']['RUNTIME_BOOT_GUARD_DIR'])
        / 'activation-permit'
    ).exists()


@pytest.mark.parametrize('terminal', ('COMMITTED', 'ROLLED_BACK'))
def test_terminal_transaction_leftover_guard_recovers_automatically(
    tmp_path,
    terminal,
):
    transaction = _prepare_transaction(tmp_path)
    completed = _seed_interrupted_activation_guard(
        transaction,
        f'interrupted-after-{terminal.lower()}',
        terminal=terminal,
        include_runtime_permit=False,
    )

    result = _run_activation(transaction)

    assert result.returncode == 0, result.stderr
    assert (completed / 'activation-in-progress.recovered').is_file()
    assert not (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).exists()


def test_interrupted_guard_rejects_mismatched_acknowledgement(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    guarded = _seed_interrupted_activation_guard(transaction, 'guarded-transaction')
    mismatched = (
        transaction['state_dir']
        / 'backups'
        / 'deploy-transactions'
        / 'different-transaction'
    )
    mismatched.mkdir()
    (mismatched / 'ACTIVATION_STARTED').write_text('other\n', encoding='utf-8')
    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(mismatched)

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '不匹配' in result.stderr
    assert not (mismatched / 'RECOVERY_CONFIRMED').exists()
    assert (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).is_file()
    assert guarded.is_dir()


def test_partial_recovery_cannot_delete_old_guard_and_ack_is_retryable(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    interrupted = _seed_interrupted_activation_guard(
        transaction,
        'retryable-interrupted-transaction',
    )
    other = interrupted.parent / 'other-existing-transaction'
    other.mkdir()
    permit = (
        Path(transaction['env']['RUNTIME_BOOT_GUARD_DIR'])
        / 'activation-permit'
    )
    permit.write_text(
        'release_id=interrupted-release\n'
        f'transaction={other}\n',
        encoding='utf-8',
    )
    permit.chmod(0o600)
    persistent_guard = (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    )
    original_guard = persistent_guard.read_bytes()
    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(interrupted)

    first = _run_activation(transaction)

    assert first.returncode != 0
    assert '运行期开机许可' in first.stderr
    assert (interrupted / 'RECOVERY_CONFIRMED').is_file()
    assert persistent_guard.read_bytes() == original_guard

    transaction['env'].pop('RECOVERY_ACKNOWLEDGED_TRANSACTION')
    second = _run_activation(transaction)

    assert second.returncode != 0
    assert '必须显式确认其精确事务' in second.stderr
    assert persistent_guard.read_bytes() == original_guard

    permit.write_text(
        'release_id=interrupted-release\n'
        f'transaction={interrupted}\n',
        encoding='utf-8',
    )
    permit.chmod(0o600)
    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(interrupted)
    third = _run_activation(transaction)

    assert third.returncode == 0, third.stderr
    assert (interrupted / 'activation-in-progress.recovered').is_file()
    assert not persistent_guard.exists()


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


def test_durable_checkpoints_precede_mutation_and_the_only_weather_request(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    runtime_env = transaction['state_dir'] / 'backups' / 'backup-runtime.env'
    runtime_env.write_text(
        f'BACKUP_DATABASE_FILE={transaction["database_file"]}\nBACKUP_PRUNE=1\n',
        encoding='utf-8',
    )
    staged_text, counter_file = _configure_formal_smoke(transaction)
    (transaction['new_release'] / 'staged.env').write_text(
        staged_text + 'QWEATHER_NETWORK_NOT_BEFORE_EPOCH=4102444800\n',
        encoding='utf-8',
    )
    fake_sync = tmp_path / 'fake-bin' / 'sync'
    _make_fake_sync(fake_sync)
    audit_file = tmp_path / 'durability-audit.jsonl'
    transaction['env'].update({
        'SYNC_BIN': str(fake_sync),
        'FAKE_SYNC_COUNT_FILE': str(tmp_path / 'sync-count'),
        'FAKE_SYNC_AUDIT_FILE': str(audit_file),
        'FAKE_SYNC_KILL_ON': '3',
        'FAKE_DURABILITY_DATABASE': str(transaction['database_file']),
        'FAKE_WEATHER_COUNT_FILE': str(counter_file),
    })

    result = _run_activation(transaction)

    assert result.returncode == -signal.SIGKILL
    events = [json.loads(line) for line in audit_file.read_text(encoding='utf-8').splitlines()]
    assert [event['call'] for event in events] == [1, 2, 3]
    captured, recovery, smoke_started = events
    assert captured == {
        'call': 1,
        'capture_checkpoint': True,
        'recovery_checkpoint': False,
        'env_backup': False,
        'backup_env_backup': False,
        'db_backup': False,
        'guard': False,
        'stop_seen': False,
        'live_release_new': False,
        'network_gate_high': False,
        'database_value': 'old',
        'started_receipt': False,
        'completed_receipt': False,
        'weather_count_exists': False,
    }
    assert recovery['capture_checkpoint'] is True
    assert recovery['recovery_checkpoint'] is True
    assert recovery['env_backup'] is True
    assert recovery['backup_env_backup'] is True
    assert recovery['db_backup'] is True
    assert recovery['guard'] is True
    assert recovery['stop_seen'] is True
    assert recovery['live_release_new'] is False
    assert recovery['database_value'] == 'old'
    assert recovery['started_receipt'] is False
    assert recovery['weather_count_exists'] is False
    assert smoke_started['started_receipt'] is True
    assert smoke_started['completed_receipt'] is False
    assert smoke_started['weather_count_exists'] is False
    assert smoke_started['live_release_new'] is True
    assert smoke_started['network_gate_high'] is True
    assert smoke_started['database_value'] == 'new'
    assert not counter_file.exists()
    receipt_dirs = list(
        (transaction['state_dir'] / 'deployments' / 'formal-cache-smokes').iterdir()
    )
    assert len(receipt_dirs) == 1
    assert (receipt_dirs[0] / 'started').is_file()
    assert not (receipt_dirs[0] / 'completed').exists()


def test_completed_receipt_is_reused_after_pre_public_failure(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    transaction['env']['FAKE_PUBLIC_HEALTH_OK'] = '0'

    first = _run_activation(transaction)

    assert first.returncode != 0
    assert counter_file.read_text(encoding='utf-8') == '1'
    receipt_dirs = list(
        (transaction['state_dir'] / 'deployments' / 'formal-cache-smokes').iterdir()
    )
    assert len(receipt_dirs) == 1
    assert (receipt_dirs[0] / 'completed').is_file()
    assert transaction['current_link'].resolve() == transaction['new_release'].resolve()
    assert _database_value(transaction['database_file']) == 'new'
    connection = sqlite3.connect(transaction['database_file'])
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM miniprogram_snapshots WHERE snapshot_id = 'formal-snapshot-1'"
        ).fetchone()[0] == 1
    finally:
        connection.close()
    markers = list(
        (transaction['state_dir'] / 'backups').rglob('POST_COMMIT_ATTENTION.txt')
    )
    assert len(markers) == 1
    assert (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).is_file()
    assert not (
        Path(transaction['env']['RUNTIME_BOOT_GUARD_DIR']) / 'activation-permit'
    ).exists()
    for unit in ALL_UNITS:
        assert not (transaction['fake_state'] / f'{unit}.active').exists()

    _retarget_formal_retry(transaction)
    (transaction['new_release'] / 'staged.env').write_text(
        (transaction['state_dir'] / '.env').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    transaction['env']['FAKE_PUBLIC_HEALTH_OK'] = '1'
    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(markers[0].parent)

    second = _run_activation(transaction)

    assert second.returncode == 0, second.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'
    assert '未再次请求上游' in second.stdout


def test_quarantine_stops_other_units_when_backup_state_query_fails(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    transaction['env']['FAKE_PUBLIC_HEALTH_OK'] = '0'
    transaction['env']['FAKE_FAIL_BACKUP_STATE_QUERY_ON'] = '4'

    result = _run_activation(transaction)

    assert result.returncode == 70
    assert counter_file.read_text(encoding='utf-8') == '1'
    for unit in ALL_UNITS:
        assert not (transaction['fake_state'] / f'{unit}.active').exists()
        assert not (transaction['fake_state'] / f'{unit}.activating').exists()
    assert (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).is_file()
    assert not (
        Path(transaction['env']['RUNTIME_BOOT_GUARD_DIR']) / 'activation-permit'
    ).exists()
    attention = list(
        (transaction['state_dir'] / 'backups').rglob('POST_COMMIT_ATTENTION.txt')
    )
    assert len(attention) == 1


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
    started_text = (receipt / 'started').read_text(encoding='utf-8')
    assert 'budget_month=2026-07' in started_text
    assert 'budget_used_before=10' in started_text
    assert 'budget_endpoints_before_json={}' in started_text
    assert re.search(r'^formal_smoke_binding=[0-9a-f]{64}$', started_text, re.MULTILINE)
    assert re.search(
        r'^formal_smoke_token_sha256=[0-9a-f]{64}$',
        started_text,
        re.MULTILINE,
    )
    assert re.search(
        r'^formal_smoke_lease_token_sha256=[0-9a-f]{64}$',
        started_text,
        re.MULTILINE,
    )
    assert list((transaction['state_dir'] / 'run').glob('formal-weather-smoke-*.ticket')) == []
    completed_text = (receipt / 'completed').read_text(encoding='utf-8')
    assert 'snapshot_id=formal-snapshot-1' in (receipt / 'completed').read_text(
        encoding='utf-8'
    )
    assert 'budget_used_after=13' in completed_text
    assert 'budget_total_delta=3' in completed_text
    assert (
        'budget_endpoint_deltas_json='
        '{"weather_7d_forecast":1,"weather_now":1,"weatheralert_v1_current":1}'
    ) in completed_text

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


def test_qweather_kid_change_creates_new_weather_fingerprint(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    first = _run_activation(transaction)
    assert first.returncode == 0, first.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'

    weather_config = (transaction['state_dir'] / '.env').read_text(encoding='utf-8')
    assert 'QWEATHER_JWT_KID=test-kid' in weather_config
    weather_config = weather_config.replace(
        'QWEATHER_JWT_KID=test-kid',
        'QWEATHER_JWT_KID=rotated-test-kid',
    ).replace(
        'QWEATHER_EXPECTED_KID=test-kid',
        'QWEATHER_EXPECTED_KID=rotated-test-kid',
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


def test_lost_completed_receipt_never_retries_the_weather_request(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    first = _run_activation(transaction)
    assert first.returncode == 0, first.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'
    receipt_dirs = list(
        (transaction['state_dir'] / 'deployments' / 'formal-cache-smokes').iterdir()
    )
    assert len(receipt_dirs) == 1
    (receipt_dirs[0] / 'completed').unlink()
    (transaction['new_release'] / 'staged.env').write_text(
        (transaction['state_dir'] / '.env').read_text(encoding='utf-8'),
        encoding='utf-8',
    )

    second = _run_activation(transaction)

    assert second.returncode != 0
    assert '禁止自动重试' in second.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'


def test_jwt_private_key_content_change_creates_new_weather_fingerprint(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    private_key = tmp_path / 'qweather-private.pem'
    _write_test_ed25519_private_key(private_key)
    _staged_text, counter_file = _configure_formal_jwt_smoke(
        transaction,
        private_key,
    )

    first = _run_activation(transaction)

    assert first.returncode == 0, first.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'

    _write_test_ed25519_private_key(private_key)
    rotated_final = transaction['qweather_private_dir'] / 'qweather-formal-rotated.pem'
    rotated_config = (transaction['state_dir'] / '.env').read_text(encoding='utf-8')
    rotated_config = rotated_config.replace(
        f'QWEATHER_JWT_PRIVATE_KEY_PATH={transaction["qweather_final_key"]}',
        f'QWEATHER_JWT_PRIVATE_KEY_PATH={rotated_final}',
    )
    (transaction['new_release'] / 'staged.env').write_text(rotated_config, encoding='utf-8')
    transaction['qweather_pending_key'].write_bytes(private_key.read_bytes())
    transaction['qweather_pending_key'].chmod(0o600)
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
    private_key.chmod(0o640)
    linked_key = tmp_path / 'linked-private.pem'
    linked_key.symlink_to(private_key)
    _staged_text, counter_file = _configure_formal_jwt_smoke(
        transaction,
        linked_key,
    )

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert 'QWeather pending/final 私钥或转换计划校验失败' in result.stderr
    assert str(private_key) not in result.stderr
    assert not counter_file.exists()
    assert not (
        transaction['state_dir'] / 'deployments' / 'formal-cache-smokes'
    ).exists()


def test_malformed_jwt_key_fails_offline_before_started_receipt(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    private_key = tmp_path / 'qweather-private.pem'
    private_key.write_bytes(b'malformed-private-key')
    private_key.chmod(0o640)
    _staged_text, counter_file = _configure_formal_jwt_smoke(
        transaction,
        private_key,
    )

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '正式 JWT 运行用户离线签名预检失败' in result.stderr
    assert not counter_file.exists()
    assert not (
        transaction['state_dir'] / 'deployments' / 'formal-cache-smokes'
    ).exists()


def test_jwt_private_key_0600_fails_before_weather_request(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    private_key = tmp_path / 'qweather-private.pem'
    _write_test_ed25519_private_key(private_key)
    private_key.chmod(0o600)
    _staged_text, counter_file = _configure_formal_jwt_smoke(
        transaction,
        private_key,
    )
    transaction['qweather_final_key'].write_bytes(private_key.read_bytes())
    transaction['qweather_final_key'].chmod(0o600)

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert 'QWeather pending/final 私钥或转换计划校验失败' in result.stderr
    assert not counter_file.exists()
    assert not (
        transaction['state_dir'] / 'deployments' / 'formal-cache-smokes'
    ).exists()


def test_formal_jwt_key_checks_root_group_read_only_state():
    content = ACTIVATE_SCRIPT.read_text(encoding='utf-8')

    assert 'stat.S_IMODE(before.st_mode) != 0o640' in content
    assert 'before.st_uid != expected_key_owner_uid' in content
    assert 'before.st_gid != expected_key_group_gid' in content
    assert 'stat.S_IMODE(key_stat.st_mode) != 0o640' in content
    assert 'key_stat.st_uid != expected_key_owner_uid' in content
    assert 'key_stat.st_gid != expected_key_group_gid' in content
    assert content.index(
        'reconcile_qweather_key_plan "$TRANSACTION_DIR" committed'
    ) < content.index('COMMITTED=1')


def test_qweather_key_create_then_reuse_only_after_quiescence(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, _counter_file = _configure_formal_smoke(transaction)

    first = _run_activation(transaction)

    assert first.returncode == 0, first.stderr
    stop_audit = json.loads(
        Path(transaction['env']['FAKE_QWEATHER_KEY_STOP_AUDIT']).read_text(
            encoding='utf-8'
        )
    )
    assert stop_audit == {
        'unit': 'case-weather-backup.timer',
        'pending_mode': '0o600',
        'pending_regular': True,
        'final_exists': False,
    }
    assert transaction['qweather_final_key'].is_file()
    assert transaction['qweather_final_key'].stat().st_mode & 0o777 == 0o640
    assert not transaction['qweather_pending_key'].exists()
    plans = sorted(
        (transaction['state_dir'] / 'backups' / 'deploy-transactions').glob(
            '*/qweather-key-transition.json'
        )
    )
    assert [json.loads(path.read_text(encoding='utf-8'))['action'] for path in plans] == [
        'create'
    ]

    (transaction['new_release'] / 'staged.env').write_text(
        (transaction['state_dir'] / '.env').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    second = _run_activation(transaction)

    assert second.returncode == 0, second.stderr
    plans = sorted(
        (transaction['state_dir'] / 'backups' / 'deploy-transactions').glob(
            '*/qweather-key-transition.json'
        )
    )
    assert [json.loads(path.read_text(encoding='utf-8'))['action'] for path in plans] == [
        'create',
        'reuse',
    ]
    assert not transaction['qweather_pending_key'].exists()


@pytest.mark.parametrize(
    'fault_point',
    ('before-promotion', 'after-link', 'after-permissions', 'after-pending-unlink'),
)
def test_qweather_key_transition_fault_recovers_before_old_units_restart(
    tmp_path,
    fault_point,
):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    transaction['env']['QWEATHER_KEY_TRANSITION_FAIL_AT'] = fault_point

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert not counter_file.exists()
    assert not transaction['qweather_final_key'].exists()
    assert not transaction['qweather_pending_key'].exists()
    transaction_dirs = list(
        (transaction['state_dir'] / 'backups' / 'deploy-transactions').iterdir()
    )
    assert len(transaction_dirs) == 1
    archive = transaction_dirs[0] / 'qweather-key-recovery'
    assert archive.is_dir()
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in archive.iterdir())
    assert all(path.stat().st_nlink == 1 for path in archive.iterdir())
    assert transaction['qweather_private_dir'].stat().st_mode & 0o777 == 0o700
    assert (transaction_dirs[0] / 'ROLLED_BACK').is_file()
    assert not (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).exists()
    assert (transaction['fake_state'] / 'case-weather.service.active').is_file()


@pytest.mark.parametrize(
    ('fault_point', 'pending_exists', 'final_mode', 'final_nlink', 'private_mode'),
    (
        ('after-link-cleanup', True, 0o600, 2, 0o700),
        ('after-pending-unlink-cleanup', False, 0o600, 1, 0o700),
        ('after-permissions-cleanup', False, 0o640, 1, 0o750),
    ),
)
def test_qweather_key_cleanup_failure_keeps_units_stopped_and_guarded(
    tmp_path,
    fault_point,
    pending_exists,
    final_mode,
    final_nlink,
    private_mode,
):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    transaction['env']['QWEATHER_KEY_TRANSITION_FAIL_AT'] = fault_point

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert not counter_file.exists()
    assert (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).is_file()
    assert list(
        (transaction['state_dir'] / 'backups').rglob('ROLLBACK_REQUIRED.txt')
    )
    assert transaction['qweather_pending_key'].exists() is pending_exists
    assert transaction['qweather_final_key'].stat().st_mode & 0o777 == final_mode
    assert transaction['qweather_final_key'].stat().st_nlink == final_nlink
    assert transaction['qweather_private_dir'].stat().st_mode & 0o777 == private_mode
    assert not (
        Path(transaction['env']['RUNTIME_BOOT_GUARD_DIR']) / 'activation-permit'
    ).exists()
    for unit in ALL_UNITS:
        assert not (transaction['fake_state'] / f'{unit}.active').exists()
        assert not (transaction['fake_state'] / f'{unit}.activating').exists()


def test_qweather_different_existing_final_fails_before_mutation(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    _write_test_ed25519_private_key(
        transaction['qweather_final_key'],
        mode=0o640,
    )

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert 'QWeather pending/final 私钥或转换计划校验失败' in result.stderr
    assert not counter_file.exists()
    assert transaction['qweather_pending_key'].stat().st_mode & 0o777 == 0o600
    assert not Path(transaction['env']['FAKE_QWEATHER_KEY_STOP_AUDIT']).exists()
    assert not (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).exists()


def test_qweather_pending_extra_hardlink_fails_before_plan_or_mutation(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    alias = tmp_path / 'pending-alias.pem'
    os.link(transaction['qweather_pending_key'], alias)

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert 'QWeather pending/final 私钥或转换计划校验失败' in result.stderr
    assert transaction['qweather_pending_key'].stat().st_nlink == 2
    assert alias.stat().st_nlink == 2
    assert not counter_file.exists()
    assert not Path(transaction['env']['FAKE_QWEATHER_KEY_STOP_AUDIT']).exists()
    assert (transaction['fake_state'] / 'case-weather.service.active').is_file()


def test_qweather_plan_failure_before_started_recovers_pending_and_can_retry(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    transaction['env']['QWEATHER_KEY_TRANSITION_FAIL_AT'] = 'after-plan'

    first = _run_activation(transaction)

    assert first.returncode != 0
    transaction_dirs = list(
        (transaction['state_dir'] / 'backups' / 'deploy-transactions').iterdir()
    )
    assert len(transaction_dirs) == 1
    old_transaction = transaction_dirs[0]
    assert not (old_transaction / 'ACTIVATION_STARTED').exists()
    assert (old_transaction / 'ROLLED_BACK').is_file()
    recovered_pending = old_transaction / 'qweather-key-recovery' / 'pending.pem'
    assert recovered_pending.is_file()
    assert recovered_pending.stat().st_mode & 0o777 == 0o600
    assert recovered_pending.stat().st_nlink == 1
    assert not transaction['qweather_pending_key'].exists()
    assert not (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).exists()

    transaction['qweather_pending_key'].write_bytes(recovered_pending.read_bytes())
    transaction['qweather_pending_key'].chmod(0o600)
    transaction['env'].pop('QWEATHER_KEY_TRANSITION_FAIL_AT')
    second = _run_activation(transaction)

    assert second.returncode == 0, second.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'


def test_qweather_plan_cleanup_failure_is_registered_and_blocks_retry(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    transaction['env']['QWEATHER_KEY_TRANSITION_FAIL_AT'] = 'after-plan-cleanup'

    first = _run_activation(transaction)

    assert first.returncode == 70
    failures = list(
        (transaction['state_dir'] / 'backups').rglob('ROLLBACK_REQUIRED.txt')
    )
    assert len(failures) == 1
    assert not (failures[0].parent / 'ACTIVATION_STARTED').exists()
    assert transaction['qweather_pending_key'].is_file()
    assert transaction['qweather_pending_key'].stat().st_mode & 0o777 == 0o600
    assert not counter_file.exists()

    transaction['env'].pop('QWEATHER_KEY_TRANSITION_FAIL_AT')
    second = _run_activation(transaction)

    assert second.returncode != 0
    assert '尚未人工确认' in second.stderr
    assert transaction['qweather_pending_key'].is_file()


def test_runtime_uid_process_with_arbitrary_argv_blocks_key_promotion(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    (transaction['fake_state'] / 'runtime-user-process-running').touch()

    result = _run_activation(transaction)

    assert result.returncode == 70
    assert '运行账户仍有未归属进程' in result.stderr
    assert not transaction['qweather_final_key'].exists()
    assert not counter_file.exists()
    assert (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).is_file()
    assert not (
        Path(transaction['env']['RUNTIME_BOOT_GUARD_DIR']) / 'activation-permit'
    ).exists()
    for unit in ALL_UNITS:
        assert not (transaction['fake_state'] / f'{unit}.active').exists()


def test_private_directory_inode_replacement_is_preserved_and_quarantined(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    transaction['env']['FAKE_REPLACE_QWEATHER_PRIVATE_DIR_ON_STOP'] = '1'

    result = _run_activation(transaction)

    assert result.returncode == 70
    replacement_dir = transaction['qweather_private_dir']
    original_dir = replacement_dir.with_name(replacement_dir.name + '.original')
    assert replacement_dir.is_dir()
    assert original_dir.is_dir()
    replacement_pending = replacement_dir / transaction['qweather_pending_key'].name
    original_pending = original_dir / transaction['qweather_pending_key'].name
    assert replacement_pending.is_file()
    assert original_pending.is_file()
    assert replacement_pending.stat().st_ino != original_pending.stat().st_ino
    assert not counter_file.exists()
    assert (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).is_file()
    assert not (
        Path(transaction['env']['RUNTIME_BOOT_GUARD_DIR']) / 'activation-permit'
    ).exists()


def test_private_directory_restore_interruption_can_resume_safely(tmp_path):
    transaction = _prepare_transaction(tmp_path, candidate_health_ok=False)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    transaction['env']['QWEATHER_KEY_TRANSITION_FAIL_AT'] = (
        'during-directory-restore'
    )

    first = _run_activation(transaction)

    assert first.returncode == 70
    failures = list(
        (transaction['state_dir'] / 'backups').rglob('ROLLBACK_REQUIRED.txt')
    )
    assert len(failures) == 1
    old_transaction = failures[0].parent
    assert transaction['qweather_private_dir'].stat().st_mode & 0o777 == 0o700
    archived_keys = list((old_transaction / 'qweather-key-recovery').iterdir())
    assert len(archived_keys) == 1
    assert not (
        Path(transaction['env']['RUNTIME_BOOT_GUARD_DIR']) / 'activation-permit'
    ).exists()

    _retarget_formal_retry(transaction, suffix='directory-restore-retry')
    transaction['qweather_pending_key'].write_bytes(archived_keys[0].read_bytes())
    transaction['qweather_pending_key'].chmod(0o600)
    (transaction['new_release'] / 'staged.env').write_text(
        (transaction['state_dir'] / '.env').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    transaction['env']['FAKE_CANDIDATE_HEALTH_OK'] = '1'
    transaction['env'].pop('QWEATHER_KEY_TRANSITION_FAIL_AT')
    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(old_transaction)
    second = _run_activation(transaction)

    assert second.returncode == 0, second.stderr
    assert (old_transaction / 'RECOVERY_CONFIRMED').is_file()
    assert counter_file.read_text(encoding='utf-8') == '1'


def test_sync_failure_after_started_receipt_uses_durable_forward_marker(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    fake_sync = tmp_path / 'fake-bin' / 'sync'
    _make_fake_sync(fake_sync)
    transaction['env'].update({
        'SYNC_BIN': str(fake_sync),
        'FAKE_SYNC_COUNT_FILE': str(tmp_path / 'sync-count'),
        'FAKE_SYNC_AUDIT_FILE': str(tmp_path / 'sync-audit.jsonl'),
        'FAKE_SYNC_FAIL_ON': '3',
        'FAKE_DURABILITY_DATABASE': str(transaction['database_file']),
        'FAKE_WEATHER_COUNT_FILE': str(counter_file),
    })

    result = _run_activation(transaction)

    assert result.returncode != 0
    forward_markers = list(
        (transaction['state_dir'] / 'backups').rglob('FORWARD_ONLY_REQUIRED')
    )
    assert len(forward_markers) == 1
    assert forward_markers[0].read_text(encoding='utf-8').strip() == (
        'phase=formal-smoke-started'
    )
    assert (forward_markers[0].parent / 'POST_COMMIT_ATTENTION.txt').is_file()
    assert not (forward_markers[0].parent / 'ROLLED_BACK').exists()
    assert transaction['qweather_final_key'].is_file()
    assert transaction['qweather_final_key'].stat().st_mode & 0o777 == 0o640
    assert not counter_file.exists()
    assert (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).is_file()


def test_public_restart_sigkill_ack_preserves_key_and_reuses_receipt(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    transaction['env']['FAKE_KILL_PARENT_AFTER_RESTART_UNIT'] = 'case-weather.service'

    first = _run_activation(transaction)

    assert first.returncode == -signal.SIGKILL
    public_markers = list(
        (transaction['state_dir'] / 'backups').rglob('PUBLIC_START_ATTEMPTED')
    )
    assert len(public_markers) == 1
    old_transaction = public_markers[0].parent
    assert (old_transaction / 'FORWARD_ONLY_REQUIRED').is_file()
    assert not (old_transaction / 'COMMITTED').exists()
    final_identity = (
        transaction['qweather_final_key'].stat().st_dev,
        transaction['qweather_final_key'].stat().st_ino,
    )

    _retarget_formal_retry(transaction, suffix='sigkill-retry')
    (transaction['new_release'] / 'staged.env').write_text(
        (transaction['state_dir'] / '.env').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    transaction['env'].pop('FAKE_KILL_PARENT_AFTER_RESTART_UNIT')
    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(old_transaction)
    second = _run_activation(transaction)

    assert second.returncode == 0, second.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'
    assert (
        transaction['qweather_final_key'].stat().st_dev,
        transaction['qweather_final_key'].stat().st_ino,
    ) == final_identity
    assert (old_transaction / 'RECOVERY_CONFIRMED').is_file()


def test_same_content_final_inode_replacement_blocks_commit_and_ack(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    transaction['env']['FAKE_REPLACE_QWEATHER_FINAL_AFTER_RESTART'] = '1'

    first = _run_activation(transaction)

    assert first.returncode != 0
    attention = list(
        (transaction['state_dir'] / 'backups').rglob('POST_COMMIT_ATTENTION.txt')
    )
    assert len(attention) == 1
    old_transaction = attention[0].parent
    assert not (old_transaction / 'COMMITTED').exists()
    replacement_identity = (
        transaction['qweather_final_key'].stat().st_dev,
        transaction['qweather_final_key'].stat().st_ino,
    )
    assert (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).is_file()
    assert not (
        Path(transaction['env']['RUNTIME_BOOT_GUARD_DIR']) / 'activation-permit'
    ).exists()

    (transaction['fake_state'] / 'case-weather.service.active').touch()
    runtime_guard = Path(transaction['env']['RUNTIME_BOOT_GUARD_DIR'])
    runtime_guard.mkdir(parents=True, exist_ok=True)
    permit = runtime_guard / 'activation-permit'
    unrelated_transaction = old_transaction.parent / 'unrelated-permit-transaction'
    unrelated_transaction.mkdir(mode=0o700)
    permit.write_text(
        'release_id=new\n'
        f'transaction={unrelated_transaction}\n',
        encoding='utf-8',
    )
    permit.chmod(0o600)
    (transaction['new_release'] / 'staged.env').write_text(
        (transaction['state_dir'] / '.env').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    transaction['auto_stage_qweather_pending'] = False
    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(old_transaction)
    second = _run_activation(transaction)

    assert second.returncode != 0
    assert (
        transaction['qweather_final_key'].stat().st_dev,
        transaction['qweather_final_key'].stat().st_ino,
    ) == replacement_identity
    assert not (old_transaction / 'RECOVERY_CONFIRMED').exists()
    assert (
        transaction['state_dir'] / 'deployments' / 'activation-in-progress'
    ).is_file()
    assert not permit.exists(), second.stderr
    assert list(runtime_guard.glob('activation-permit.quarantined.*'))
    for unit in ALL_UNITS:
        assert not (transaction['fake_state'] / f'{unit}.active').exists()


def test_formal_smoke_rejects_duplicate_endpoint_budget_delta(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    (transaction['state_dir'] / 'formal-smoke-budget-mode').write_text(
        'duplicate\n',
        encoding='utf-8',
    )

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '必须由 now、7d 与 weatheralert 三项各增加 1 次' in result.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'
    receipt_dirs = list(
        (transaction['state_dir'] / 'deployments' / 'formal-cache-smokes').iterdir()
    )
    assert len(receipt_dirs) == 1
    assert (receipt_dirs[0] / 'started').is_file()
    assert not (receipt_dirs[0] / 'completed').exists()


def test_formal_smoke_rejects_unexpected_endpoint_budget_delta(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    (transaction['state_dir'] / 'formal-smoke-budget-mode').write_text(
        'unexpected\n',
        encoding='utf-8',
    )

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '必须由 now、7d 与 weatheralert 三项各增加 1 次' in result.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'
    receipt_dirs = list(
        (transaction['state_dir'] / 'deployments' / 'formal-cache-smokes').iterdir()
    )
    assert len(receipt_dirs) == 1
    assert (receipt_dirs[0] / 'started').is_file()
    assert not (receipt_dirs[0] / 'completed').exists()
    assert list((transaction['state_dir'] / 'run').glob('formal-weather-smoke-*.ticket')) == []


def test_formal_smoke_requires_weather_sync_lock_quiescence(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    sync_lock = transaction['state_dir'] / 'run' / 'case-weather-sync.lock'
    sync_lock.parent.mkdir(parents=True, exist_ok=True)
    sync_lock.write_text('', encoding='ascii')
    fake_flock = transaction['state_dir'] / 'fake-weather-flock'
    _write_executable(
        fake_flock,
        """#!/bin/sh
if [ "$1" = "-n" ] && [ "$2" = "8" ]; then
    exit 1
fi
exit 0
""",
    )
    transaction['env']['FLOCK_BIN'] = str(fake_flock)

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '仍有同步周期持有同机锁' in result.stderr
    assert not counter_file.exists()
    assert not (
        transaction['state_dir'] / 'deployments' / 'formal-cache-smokes'
    ).exists()


def test_formal_smoke_busy_global_lease_fails_before_started_receipt(tmp_path):
    transaction = _prepare_transaction(tmp_path)
    _staged_text, counter_file = _configure_formal_smoke(transaction)
    (transaction['state_dir'] / 'formal-smoke-lease-mode').write_text(
        'busy\n',
        encoding='utf-8',
    )

    result = _run_activation(transaction)

    assert result.returncode != 0
    assert '无法在 started receipt 前取得全局租约' in result.stderr
    assert not counter_file.exists()
    receipt_root = (
        transaction['state_dir'] / 'deployments' / 'formal-cache-smokes'
    )
    assert receipt_root.is_dir()
    assert list(receipt_root.iterdir()) == []
    assert list(
        (transaction['state_dir'] / 'run').glob('formal-weather-smoke-*.ticket')
    ) == []
    assert transaction['current_link'].resolve() == transaction['old_release'].resolve()
    assert _database_value(transaction['database_file']) == 'old'


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
    attention = list(
        (transaction['state_dir'] / 'backups').rglob('POST_COMMIT_ATTENTION.txt')
    )
    assert len(attention) == 1

    # 即使下一事务的桩能生成 QWeather 数据，同一绑定也必须在请求前关闭。
    _configure_formal_smoke(transaction, provider='QWeather')
    reprovisioned_identity = (
        transaction['qweather_pending_key'].stat().st_dev,
        transaction['qweather_pending_key'].stat().st_ino,
    )
    committed_final_identity = (
        transaction['qweather_final_key'].stat().st_dev,
        transaction['qweather_final_key'].stat().st_ino,
    )
    assert reprovisioned_identity != committed_final_identity
    assert transaction['qweather_pending_key'].stat().st_nlink == 1
    assert transaction['qweather_pending_key'].stat().st_mode & 0o777 == 0o600
    (transaction['new_release'] / 'staged.env').write_text(staged_text, encoding='utf-8')
    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(
        attention[0].parent
    )
    second = _run_activation(transaction)

    assert second.returncode != 0
    assert '禁止自动重试' in second.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'
    assert (attention[0].parent / 'RECOVERY_CONFIRMED').is_file()


@pytest.mark.parametrize('pending_tamper', ('hardlink-final', 'wrong-digest', 'wrong-mode'))
def test_started_receipt_ack_rejects_untrusted_reprovisioned_pending(
    tmp_path,
    pending_tamper,
):
    transaction = _prepare_transaction(tmp_path)
    staged_text, counter_file = _configure_formal_smoke(
        transaction,
        provider='Open-Meteo',
    )
    first = _run_activation(transaction)
    assert first.returncode != 0
    attention = list(
        (transaction['state_dir'] / 'backups').rglob('POST_COMMIT_ATTENTION.txt')
    )
    assert len(attention) == 1

    _configure_formal_smoke(transaction, provider='QWeather')
    pending = transaction['qweather_pending_key']
    if pending_tamper == 'hardlink-final':
        pending.unlink()
        os.link(transaction['qweather_final_key'], pending)
    elif pending_tamper == 'wrong-digest':
        _write_test_ed25519_private_key(pending, mode=0o600)
    else:
        pending.chmod(0o640)
    (transaction['new_release'] / 'staged.env').write_text(staged_text, encoding='utf-8')
    transaction['env']['RECOVERY_ACKNOWLEDGED_TRANSACTION'] = str(
        attention[0].parent
    )

    second = _run_activation(transaction)

    assert second.returncode != 0
    assert 'QWeather 私钥转换计划的 committed 状态校验或回收失败' in second.stderr
    assert counter_file.read_text(encoding='utf-8') == '1'
    assert not (attention[0].parent / 'RECOVERY_CONFIRMED').exists()


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
