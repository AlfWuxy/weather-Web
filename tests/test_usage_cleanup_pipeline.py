# -*- coding: utf-8 -*-
"""UsageEvent 独立清理 pipeline 与部署定时器测试。"""

from datetime import timedelta
import json
from pathlib import Path

from sqlalchemy import select

from core.time_utils import utcnow


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_cleanup_pipeline_enters_app_context(app, monkeypatch):
    from flask import current_app, has_app_context
    from services.pipelines import cleanup_usage_events as pipeline

    observed = {}

    def fake_delete_expired_usage_events(**options):
        observed['has_app_context'] = has_app_context()
        observed['app_name'] = current_app.name
        observed['options'] = options
        return {
            'deleted': 7,
            'cutoff': utcnow() - timedelta(days=30),
            'complete': False,
        }

    monkeypatch.setattr(
        pipeline,
        'delete_expired_usage_events',
        fake_delete_expired_usage_events,
    )

    result = pipeline.cleanup_usage_events(
        batch_size=50,
        max_batches=3,
        app_instance=app,
    )

    assert observed == {
        'has_app_context': True,
        'app_name': app.name,
        'options': {'batch_size': 50, 'max_batches': 3},
    }
    assert result['status'] == 'partial'
    assert result['retention_days'] == 30
    assert result['deleted'] == 7
    assert result['complete'] is False
    assert result['cutoff'].endswith('+00:00')


def test_cleanup_pipeline_deletes_expired_rows(app, db_session):
    from core.db_models import UsageEvent
    from services.pipelines import cleanup_usage_events as pipeline

    old_event = UsageEvent(
        event_type='template_view',
        source='web',
        created_at=utcnow() - timedelta(days=31),
    )
    fresh_event = UsageEvent(
        event_type='template_view',
        source='web',
        created_at=utcnow() - timedelta(days=29),
    )
    db_session.add_all((old_event, fresh_event))
    db_session.commit()
    old_event_id = old_event.id
    fresh_event_id = fresh_event.id

    result = pipeline.cleanup_usage_events(app_instance=app)

    assert result['status'] == 'success'
    assert result['deleted'] == 1
    assert result['complete'] is True
    remaining_ids = set(db_session.execute(select(UsageEvent.id)).scalars())
    assert remaining_ids == {fresh_event_id}
    assert old_event_id not in remaining_ids


def test_cleanup_cli_prints_json_audit_result(monkeypatch, capsys):
    from services.pipelines import cleanup_usage_events as pipeline

    observed = {}

    def fake_cleanup_usage_events(**options):
        observed.update(options)
        return {
            'status': 'success',
            'retention_days': 30,
            'cutoff': '2026-06-18T03:15:00+00:00',
            'deleted': 4,
            'complete': True,
        }

    monkeypatch.setattr(pipeline, 'cleanup_usage_events', fake_cleanup_usage_events)

    exit_code = pipeline.main(['--batch-size', '25', '--max-batches', '2'])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert observed == {'batch_size': 25, 'max_batches': 2}
    assert captured.err == ''
    assert json.loads(captured.out) == {
        'status': 'success',
        'retention_days': 30,
        'cutoff': '2026-06-18T03:15:00+00:00',
        'deleted': 4,
        'complete': True,
    }


def test_cleanup_cli_returns_nonzero_and_json_on_failure(monkeypatch, capsys):
    from services.pipelines import cleanup_usage_events as pipeline

    def fail_cleanup(**_options):
        raise RuntimeError('database unavailable')

    monkeypatch.setattr(pipeline, 'cleanup_usage_events', fail_cleanup)

    exit_code = pipeline.main([])
    captured = capsys.readouterr()
    error_result = json.loads(captured.err.splitlines()[-1])

    assert exit_code == 1
    assert captured.out == ''
    assert error_result == {
        'status': 'error',
        'error_type': 'RuntimeError',
        'message': 'database unavailable',
    }


def test_cleanup_cli_returns_nonzero_and_partial_json_for_backlog(
    monkeypatch,
    capsys,
):
    from services.pipelines import cleanup_usage_events as pipeline

    monkeypatch.setattr(
        pipeline,
        'cleanup_usage_events',
        lambda **_options: {
            'status': 'partial',
            'retention_days': 30,
            'cutoff': '2026-06-18T03:15:00+00:00',
            'deleted': 10_000,
            'complete': False,
        },
    )

    exit_code = pipeline.main([])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err == ''
    assert json.loads(captured.out) == {
        'status': 'partial',
        'retention_days': 30,
        'cutoff': '2026-06-18T03:15:00+00:00',
        'deleted': 10_000,
        'complete': False,
    }


def test_deploy_installs_independent_daily_cleanup_timer_idempotently():
    deploy_content = (ROOT_DIR / 'scripts' / 'deploy.sh').read_text(encoding='utf-8')
    activate_content = (ROOT_DIR / 'scripts' / 'activate_release.sh').read_text(
        encoding='utf-8'
    )
    timer_marker = (
        "cat > $NEW_RELEASE/systemd/case-weather-usage-cleanup.timer << 'EOF'"
    )
    timer_block = deploy_content.split(timer_marker, 1)[1].split('EOF"', 1)[0]

    assert 'OnCalendar=*-*-* 03:15:00' in timer_block
    assert 'OnUnitActiveSec=' not in timer_block
    assert 'Unit=case-weather-usage-cleanup.service' in timer_block
    assert 'Persistent=true' in timer_block
    assert (
        'ExecStart=/bin/bash $CURRENT_LINK/app/scripts/cleanup_usage_events.sh'
        in deploy_content
    )
    assert 'Restart=on-failure' in deploy_content
    assert 'RestartSec=1min' in deploy_content
    assert 'StartLimitBurst=20' in deploy_content
    assert 'Environment=DEPLOY_STATE_DIR=$PROJECT_DIR' in deploy_content
    assert 'case-weather-usage-cleanup.timer' in activate_content
    assert 'for unit in "${START_TIMER_UNITS[@]}"' in activate_content
    assert '"$SYSTEMCTL_BIN" enable "$unit"' in activate_content
    assert '"$SYSTEMCTL_BIN" restart "$unit"' in activate_content
    assert '"$SYSTEMCTL_BIN" is-active --quiet "$unit"' in activate_content
    assert 'case-weather-usage-cleanup.timer' in activate_content
    assert 'verify_release_state() {' in activate_content

    activation_tail = activate_content.split('switch_current_link "$NEW_RELEASE"', 1)[1]
    assert activation_tail.index('install_new_units') < activation_tail.index('start_new_release')
    start_block = activate_content.split('start_new_release() {', 1)[1].split('\n}', 1)[0]
    prepare_block = activate_content.split(
        'prepare_release_timer_states() {', 1
    )[1].split('\n}', 1)[0]
    timer_block = activate_content.split(
        'start_release_timers() {', 1
    )[1].split('\n}', 1)[0]
    assert 'wait_for_health' in start_block
    assert 'for unit in "${START_TIMER_UNITS[@]}"' in prepare_block
    assert '"$SYSTEMCTL_BIN" enable "$unit"' in prepare_block
    assert 'for unit in "${START_TIMER_UNITS[@]}"' in timer_block
    assert '"$SYSTEMCTL_BIN" enable "$unit"' not in timer_block
    assert '"$SYSTEMCTL_BIN" restart "$unit"' in timer_block


def test_deploy_rejects_password_fallback_without_sshpass():
    content = (ROOT_DIR / 'scripts' / 'deploy.sh').read_text(encoding='utf-8')

    assert 'expect -c' not in content
    assert '密码部署需要 sshpass' in content
