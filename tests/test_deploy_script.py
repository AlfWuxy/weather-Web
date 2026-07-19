# -*- coding: utf-8 -*-
"""部署脚本回归测试。"""

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_deploy_script():
    script_path = ROOT / "scripts" / "deploy.sh"
    return script_path.read_text(encoding="utf-8")


def _load_precompute_script():
    script_path = ROOT / "scripts" / "community_risk_precompute.sh"
    return script_path.read_text(encoding="utf-8")


def _load_activate_script():
    script_path = ROOT / "scripts" / "activate_release.sh"
    return script_path.read_text(encoding="utf-8")


def _write_executable(path, content):
    path.write_text(content, encoding='utf-8')
    path.chmod(0o755)


def test_deploy_script_checks_units_with_is_active():
    content = _load_deploy_script()
    activate = _load_activate_script()

    assert 'case-weather.service' in activate
    assert 'case-weather-backup.timer' in activate
    assert 'case-weather-cache-bootstrap.timer' in activate
    assert 'case-weather-cache.timer' in activate
    assert "bootstrap timer 状态应为 enabled" in activate
    assert "bootstrap timer 未保留完整的首轮 30 分钟等待窗口" in activate
    assert 'check_remote_unit_active "case-weather-dispatch.timer"' not in content
    assert 'case-weather-risk-precompute.timer' in activate
    assert "旧 systemd 单元仍存在" in activate
    assert '"$SYSTEMCTL_BIN" is-active --quiet "$unit"' in activate


def test_deploy_script_no_longer_swallows_systemctl_failures():
    content = _load_deploy_script()

    assert 'case-weather && systemctl restart case-weather && systemctl status --no-pager case-weather || true' not in content
    assert 'case-weather-dispatch.timer && systemctl status --no-pager case-weather-dispatch.timer || true' not in content
    assert 'case-weather-risk-precompute.timer && systemctl status --no-pager case-weather-risk-precompute.timer || true' not in content
    assert 'systemctl stop case-weather || true' not in content


def test_deploy_script_pins_duchang_cache_to_free_tier_budget():
    content = _load_deploy_script()

    assert 'WEATHER_SYNC_LOCATIONS=都昌县' in content
    assert 'QWEATHER_CANONICAL_LOCATION=116.20,29.27' in content
    assert 'QWEATHER_MONTHLY_REQUEST_LIMIT=40000' in content
    assert 'QWEATHER_REQUIRE_PERSISTENT_BUDGET=1' in content
    assert (
        'remote_env_update "QWEATHER_REQUIRE_PERSISTENT_BUDGET" "1" "always"'
        in content
    )
    for key, value in (
        ('QWEATHER_CANONICAL_LOCATION', '116.20,29.27'),
        ('QWEATHER_MONTHLY_REQUEST_LIMIT', '40000'),
        ('QWEATHER_BUDGET_FAIL_CLOSED', '1'),
        ('WEATHER_CACHE_TTL_MINUTES', '30'),
        ('FORECAST_CACHE_TTL_MINUTES', '30'),
        ('QWEATHER_WARNING_CACHE_TTL_MINUTES', '30'),
        ('WEATHER_SYNC_LOCATIONS', '都昌县'),
    ):
        assert f'remote_env_update "{key}" "{value}" "always"' in content
    assert '--probe-persistent-budget' in content
    assert '--seed-persistent-budget' in content
    assert 'QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED' in content
    assert 'QWEATHER_CONSOLE_USAGE_MONTH' in content
    assert 'QWEATHER_CONSOLE_USAGE_BASELINE' in content
    assert 'QWEATHER_EXPECTED_PROJECT_ID' in content
    assert 'QWEATHER_EXPECTED_KID' in content
    assert '微信正式发布必须固定使用 QWEATHER_AUTH_MODE=jwt' in content
    assert '私密发布表记录的 QWeather Project ID/KID 与实际部署配置不一致' in content
    assert content.index('--probe-persistent-budget') < content.index(
        '$RELEASE_VENV/bin/python -m pytest -q'
    )
    assert content.index('--seed-persistent-budget') < content.index(
        'bash $RELEASE_APP/scripts/activate_release.sh'
    )
    assert 'OnUnitActiveSec=30min' in content
    assert 'OnActiveSec=30min' in content
    assert 'OnSuccess=case-weather-dispatch.service case-weather-cache.timer' in content
    assert 'OnFailure=case-weather-cache.timer' in content
    assert "cat > $NEW_RELEASE/systemd/case-weather-dispatch.timer" not in content
    assert 'ExecStart=/bin/bash $CURRENT_LINK/app/scripts/weather_cache_sync.sh' in content


def test_formal_deploy_propagates_full_feature_release_flags():
    content = _load_deploy_script()

    assert 'FEATURE_WXPUSHER|WXPUSHER_APP_TOKEN|FEATURE_HEAT_EXPOSURE_GIS' in content
    assert 'LOCAL_FEATURE_WXPUSHER' in content
    assert 'LOCAL_FEATURE_HEAT_EXPOSURE_GIS' in content
    assert 'FEATURE_WXPUSHER=0' in content
    assert 'FEATURE_HEAT_EXPOSURE_GIS=0' in content
    assert 'remote_env_update "FEATURE_WXPUSHER" "0" "if-empty"' in content
    assert 'remote_env_update "FEATURE_WXPUSHER" "$LOCAL_FEATURE_WXPUSHER" "always"' in content
    assert 'remote_env_update "FEATURE_HEAT_EXPOSURE_GIS" "0" "if-empty"' in content
    assert 'remote_env_update "FEATURE_HEAT_EXPOSURE_GIS" "$LOCAL_FEATURE_HEAT_EXPOSURE_GIS" "always"' in content
    assert '微信全功能正式发布必须启用 FEATURE_HEAT_EXPOSURE_GIS=1' in content
    assert '1.0.0 微信正式发布必须固定 FEATURE_WXPUSHER=0' in content
    assert 'FEATURE_WXPUSHER=0 时必须清空 WXPUSHER_APP_TOKEN' in content


def test_deploy_script_uses_two_stage_failure_safe_cache_timers():
    content = _load_deploy_script()
    service_start = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-cache.service << 'EOF'"
    )
    service_end = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-cache.timer << 'EOF'",
        service_start,
    )
    service_block = content[service_start:service_end]
    recurring_start = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-cache.timer << 'EOF'"
    )
    recurring_end = content.index('EOF"', recurring_start)
    recurring_block = content[recurring_start:recurring_end]
    bootstrap_start = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-cache-bootstrap.timer << 'EOF'"
    )
    bootstrap_end = content.index('EOF"', bootstrap_start)
    bootstrap_block = content[bootstrap_start:bootstrap_end]

    assert 'OnSuccess=case-weather-dispatch.service case-weather-cache.timer' in service_block
    assert 'OnFailure=case-weather-cache.timer' in service_block
    assert 'OnActiveSec=30min' in recurring_block
    assert 'OnUnitActiveSec=30min' in recurring_block
    assert '[Install]' in recurring_block
    assert 'WantedBy=timers.target' in recurring_block
    assert "cat > $NEW_RELEASE/systemd/case-weather-cache-bootstrap.service" not in content
    assert 'OnActiveSec=30min' in bootstrap_block
    assert 'RemainAfterElapse=no' in bootstrap_block
    assert 'Unit=case-weather-cache.service' in bootstrap_block
    assert 'OnUnitActiveSec=' not in bootstrap_block
    assert '[Install]' in bootstrap_block
    activate = _load_activate_script()
    assert 'cache-bootstrap-' not in content
    assert 'cache-bootstrap.success' not in activate
    assert 'NextElapseUSecMonotonic' in activate
    assert 'remaining_us' in activate
    assert 'bootstrap timer 未保留完整的首轮 30 分钟等待窗口' in activate
    assert 'case_2dweather_2dcache_2dbootstrap_2etimer' in activate
    assert 'case-weather-cache-bootstrap.service --property=OnSuccess --value' not in activate
    assert 'case-weather-cache.service --property=OnFailure --value' in activate


def test_deploy_script_sets_precompute_python_path():
    content = _load_deploy_script()

    assert 'Environment=VENV_PY=$CURRENT_LINK/venv/bin/python' in content


def test_deploy_script_uses_isolated_release_and_server_transaction():
    content = _load_deploy_script()
    activate = _load_activate_script()

    assert 'upload_files "$RELEASE_APP"' in content
    assert 'python3 -m venv $RELEASE_VENV' in content
    assert 'bash $RELEASE_APP/scripts/activate_release.sh' in content
    assert 'upload_files "$PROJECT_DIR"' not in content
    assert 'apt-get' not in content
    assert 'enable --now redis-server' not in content
    assert 'systemctl is-active --quiet redis-server' in content
    assert 'systemctl systemd-run systemd-analyze busctl' in content
    assert 'DB_BACKUP="$TRANSACTION_DIR/database-before.db"' in activate
    assert 'ENV_BACKUP="$TRANSACTION_DIR/environment-before.env"' in activate
    assert 'STAGED_ENV_FILE="$NEW_RELEASE/staged.env"' in content
    assert 'trap on_exit EXIT' in activate
    assert 'flock' in activate.lower()


def test_preflight_finishes_before_server_transaction_can_stop_units():
    content = _load_deploy_script()

    preflight = content.index('$RELEASE_VENV/bin/python -m pytest -q')
    activation = content.index('bash $RELEASE_APP/scripts/activate_release.sh')
    assert preflight < activation


def test_formal_freeze_preflight_runs_before_remote_change_or_rsync():
    content = _load_deploy_script()

    preflight = content.index('python3 "$SCRIPT_DIR/validate_release_env.py"')
    first_remote_call = content.index('remote_exec "echo \'连接成功\'"')
    first_rsync = content.index("rsync -avz")
    upload = content.index('upload_files "$RELEASE_APP"')

    assert '--repo-root "$LOCAL_DIR"' in content
    assert '--snapshot-output "$VERIFIED_WECHAT_FORM_FILE"' in content
    assert '--verified-commit-output "$VERIFIED_COMMIT_FILE"' in content
    assert preflight < first_remote_call
    assert preflight < first_rsync
    assert preflight < upload
    assert '冻结 GIS gzip 体积必须小于 300 KiB' in (
        ROOT / 'scripts' / 'validate_release_env.py'
    ).read_text(encoding='utf-8')


def test_deploy_keeps_control_directories_root_private_and_asserts_state():
    content = _load_deploy_script()

    assert 'chown root:root $PROJECT_DIR/backups $PROJECT_DIR/backups/daily $PROJECT_DIR/backups/validation $PROJECT_DIR/deployments' in content
    assert '$PROJECT_DIR/backups/daily' in content
    assert '$PROJECT_DIR/backups/validation' in content
    assert 'chmod 0700 $PROJECT_DIR/backups $PROJECT_DIR/backups/daily $PROJECT_DIR/backups/validation $PROJECT_DIR/deployments' in content
    assert "stat -c '%u:%g:%a' $PROJECT_DIR/backups" in content
    assert "stat -c '%u:%g:%a' $PROJECT_DIR/backups/daily" in content
    assert "stat -c '%u:%g:%a' $PROJECT_DIR/backups/validation" in content
    assert "stat -c '%u:%g:%a' $PROJECT_DIR/deployments" in content
    assert "'0:0:700'" in content


def test_formal_deploy_uploads_verified_commit_snapshot_instead_of_live_tree():
    content = _load_deploy_script()

    assert 'RELEASE_SOURCE_DIR="$LOCAL_DIR"' in content
    assert 'if [ "$FORMAL_WECHAT_CONFIG_ALLOWED" != "1" ]; then' in content
    assert 'IFS= read -r VERIFIED_COMMIT < "$VERIFIED_COMMIT_FILE"' in content
    assert 'git -C "$LOCAL_DIR" archive --format=tar "$VERIFIED_COMMIT"' in content
    assert 'RELEASE_SOURCE_DIR="$LOCAL_RELEASE_EXPORT_DIR"' in content
    assert '$NEW_RELEASE/private-metadata/source-commit.txt' in content
    assert 'EXPECTED_RELEASE_COMMIT=$VERIFIED_COMMIT' in content
    assert content.count('"$RELEASE_SOURCE_DIR/" "$USER@$SERVER:$remote_target/"') == 2
    assert '"$LOCAL_DIR/" "$USER@$SERVER:$remote_target/"' not in content


def test_deploy_archive_target_comes_from_same_validation_ticket():
    content = _load_deploy_script()
    form_loader_start = content.index("load_wechat_release_form() {")
    form_loader_end = content.index("\n}\n", form_loader_start)
    form_loader = content[form_loader_start:form_loader_end]

    assert "WECHAT_TARGET_COMMIT_SHA" not in form_loader
    assert 'local form_file="$1"' in form_loader
    assert 'done < "$form_file"' in form_loader
    assert "WECHAT_RELEASE_FORM_FILE" not in form_loader
    assert 'VERIFIED_COMMIT_FILE="$LOCAL_DEPLOY_TEMP_DIR/verified-commit"' in content
    assert content.index('--verified-commit-output "$VERIFIED_COMMIT_FILE"') < content.index(
        'IFS= read -r VERIFIED_COMMIT < "$VERIFIED_COMMIT_FILE"'
    )


def test_deploy_loader_uses_the_same_validated_form_snapshot():
    content = _load_deploy_script()

    snapshot_assignment = (
        'VERIFIED_WECHAT_FORM_FILE='
        '"$LOCAL_DEPLOY_TEMP_DIR/wechat-release.snapshot"'
    )
    snapshot_validation = '--snapshot-output "$VERIFIED_WECHAT_FORM_FILE"'
    snapshot_load = 'load_wechat_release_form "$VERIFIED_WECHAT_FORM_FILE"'

    assert snapshot_assignment in content
    assert content.index(snapshot_assignment) < content.index(snapshot_validation)
    assert content.index(snapshot_validation) < content.index(snapshot_load)
    assert "load_wechat_release_form\n" not in content


def test_activate_transaction_stops_every_writer_and_commits_last():
    content = _load_activate_script()

    for unit in (
        'case-weather.service',
        'case-weather-backup.service',
        'case-weather-cache.service',
        'case-weather-cache-bootstrap.service',
        'case-weather-dispatch.service',
        'case-weather-risk-precompute.service',
        'case-weather-usage-cleanup.service',
        'case-weather-cache.timer',
        'case-weather-backup.timer',
        'case-weather-cache-bootstrap.timer',
        'case-weather-dispatch.timer',
        'case-weather-sync.timer',
        'case-weather-sync.service',
        'case-weather-risk-precompute.timer',
        'case-weather-usage-cleanup.timer',
    ):
        assert unit in content
    assert content.index('start_candidate_release\n') < content.index('LINK_MUTATED=1')
    assert content.index('install_new_units\n') < content.index(
        'prepare_release_timer_states\n'
    )
    assert content.index('start_candidate_release\n') < content.index(
        'LINK_MUTATED=1'
    )
    assert content.index('LINK_MUTATED=1') < content.index(
        'prepare_release_timer_states\n'
    )
    assert content.index('prepare_release_timer_states\n') < content.index(
        'validate_managed_backup_service\n'
    )
    assert content.index('validate_managed_backup_service\n') < content.index(
        'validate_installed_backup_service\n'
    )
    assert content.index('validate_installed_backup_service\n') < content.index(
        'verify_pre_request_quiescence\n'
    )
    assert content.index('verify_pre_request_quiescence\n') < content.index(
        'run_formal_cache_smoke\n'
    )
    assert content.index('run_formal_cache_smoke\n') < content.index(
        'arm_qweather_network_gate\n'
    )
    assert content.index('arm_qweather_network_gate\n') < content.index(
        'start_new_release\n'
    )
    assert content.index('validate_managed_backup_service\n') < content.index(
        'start_new_release\n'
    )
    assert content.index('wait_for_health "$HEALTH_URL"') < content.index('COMMITTED=1')
    assert content.index('start_new_release\n') < content.index('COMMITTED=1')
    assert content.index('FORWARD_ONLY=1') < content.index('COMMITTED=1')
    assert content.index('COMMITTED=1') < content.index('start_release_timers\n')
    commit_flow = content.split('\nCOMMITTED=1\n', 1)[1]
    assert commit_flow.index('start_release_timers\n') < commit_flow.index(
        'verify_release_state\n'
    )
    assert commit_flow.index('verify_release_state\n') < commit_flow.index(
        'observe_post_commit_stability\n'
    )
    assert commit_flow.index('observe_post_commit_stability\n') < commit_flow.index(
        '"$TRANSACTION_DIR/COMMITTED"'
    )


def test_deploy_has_no_untracked_hard_checks_after_activation_returns():
    content = _load_deploy_script()
    activation = 'bash $RELEASE_APP/scripts/activate_release.sh"'
    tail = content.split(activation, 1)[1]

    assert 'check_remote_unit_active' not in tail
    assert 'remote_exec "' not in tail
    assert '已在原子激活事务内通过' in tail


def test_deploy_script_excludes_local_design_drafts():
    content = _load_deploy_script()

    assert "--exclude '.claude'" in content
    assert "--exclude '.superpowers'" in content
    assert "--exclude '.pytest_cache'" in content
    assert "--exclude 'backups'" in content
    assert "--exclude 'output'" in content
    assert "--exclude 'tmp'" in content
    assert "--exclude 'blueprints/tools 2.py'" in content
    assert content.count("--exclude '.env*'") == 2
    assert content.count("--exclude '.secrets/'") == 2
    assert content.count("--exclude '*.pem'") == 2
    assert content.count("--exclude '*.key'") == 2
    assert content.count("--exclude 'project.private.config.json'") == 2
    assert "--exclude '.env'" not in content
    assert "--exclude '.env.local'" not in content


def test_deploy_script_requires_https_public_base_url():
    content = _load_deploy_script()

    assert 'ALLOW_INSECURE_PUBLIC_BASE_URL' in content
    assert 'PUBLIC_BASE_URL=https://yilaoweather.org' in content
    assert 'remote_env_update "PUBLIC_BASE_URL" "https://yilaoweather.org" "always"' in content
    assert 'remote_env_update "ALLOW_INSECURE_PUBLIC_BASE_URL" "" "always"' in content
    assert 'DEFAULT_PUBLIC_BASE_URL="http://$SERVER:5000"' not in content
    assert 'scripts/validate_release_env.py --file $STAGED_ENV_FILE' in content


def test_deploy_secrets_use_stdin_and_staged_environment():
    content = _load_deploy_script()

    assert 'remote_exec_with_stdin' in content
    assert 'scripts/update_env_value.py --file $STAGED_ENV_FILE' in content
    assert 'sed -i' not in content
    assert 'QWEATHER_KEY=$LOCAL_QWEATHER_KEY' not in content
    assert 'AMAP_KEY=$LOCAL_AMAP_KEY' not in content
    assert 'WXPUSHER_APP_TOKEN=$LOCAL_WXPUSHER_APP_TOKEN' not in content
    assert content.index('upload_files "$RELEASE_APP"') < content.index(
        'remote_env_update "DATABASE_URI"'
    )
    assert 'ln -s $PROJECT_DIR/.env $RELEASE_APP/.env' not in content


def test_deploy_only_supports_key_or_sshpass_and_locks_private_files():
    content = _load_deploy_script()

    assert 'expect -c' not in content
    assert '密码部署需要 sshpass' in content
    assert content.count('UMask=0077') == 6
    assert 'chmod 0700 $PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run' in content


def test_deploy_runs_all_runtime_units_as_hardened_service_user():
    content = _load_deploy_script()

    assert content.count('User=root') == 1
    assert content.count('Group=root') == 1
    assert content.count('User=case-weather') == 5
    assert content.count('Group=case-weather') == 5
    for directive in (
        'NoNewPrivileges=true',
        'PrivateTmp=true',
        'PrivateDevices=true',
        'ProtectSystem=strict',
        'ProtectHome=true',
        'RestrictSUIDSGID=true',
        'RestrictNamespaces=true',
        'CapabilityBoundingSet=',
        'ReadWritePaths=$PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run',
    ):
        expected_count = 5 if directive.startswith('ReadWritePaths=') else 6
        assert content.count(directive) == expected_count
    assert 'DISPATCH_LOCK_PATH=$PROJECT_DIR/run/case-weather-dispatch.lock' in content
    assert 'useradd --system' in content
    assert 'RUNTIME_USER=$RUNTIME_USER RUNTIME_GROUP=$RUNTIME_GROUP' in content
    activate = _load_activate_script()
    assert '--preserve-environment' not in activate
    assert 'runtime_exec "$VENV_DIR/bin/gunicorn"' in activate
    assert 'runtime_exec /bin/bash scripts/weather_cache_sync.sh --skip-nowcast' in activate
    assert "local runtime_env=(\n        -i" in activate
    runtime_block = activate.split('runtime_exec() {', 1)[1].split('\n}', 1)[0]
    for inherited_secret in (
        'SSHPASS',
        'WX_MINIPROGRAM_SECRET',
        'QWEATHER_KEY',
        'SILICONFLOW_API_KEY',
        'WXPUSHER_APP_TOKEN',
    ):
        assert inherited_secret not in runtime_block


def test_deploy_generates_root_only_sandboxed_daily_backup_units():
    content = _load_deploy_script()
    service_start = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-backup.service << 'EOF'"
    )
    timer_start = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-backup.timer << 'EOF'",
        service_start,
    )
    service_block = content[service_start:timer_start]
    timer_end = content.index('EOF"', timer_start)
    timer_block = content[timer_start:timer_end]

    for directive in (
        'Type=oneshot',
        'User=root',
        'Group=root',
        'PrivateNetwork=true',
        'NoNewPrivileges=true',
        'ProtectSystem=strict',
        'CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_SETUID CAP_SETGID',
        'RestrictAddressFamilies=AF_UNIX',
        'ReadOnlyPaths=$CURRENT_LINK $PROJECT_DIR/.env',
        'RequiresMountsFor=$PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/backups/daily',
        'ReadWritePaths=$PROJECT_DIR/backups/daily $PROJECT_DIR/instance $PROJECT_DIR/storage',
        'InaccessiblePaths=$PROJECT_DIR/backups/deploy-transactions',
        'Environment=BACKUP_RUNTIME_USER=$RUNTIME_USER',
        'Environment=MKTEMP_BIN=mktemp',
        'Environment=INSTALL_BIN=install',
        'EnvironmentFile=$PROJECT_DIR/backups/backup-runtime.env',
        'ExecStart=/bin/bash $CURRENT_LINK/app/scripts/backup.sh',
        'TimeoutStartSec=15min',
        'ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress',
        'ConditionPathExists=|/run/case-weather/activation-permit',
    ):
        assert directive in service_block
    assert 'OnCalendar=*-*-* 03:00:00 Asia/Shanghai' in timer_block
    assert 'Persistent=true' in timer_block
    assert 'Unit=case-weather-backup.service' in timer_block
    assert 'ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress' in timer_block
    assert 'ConditionPathExists=|/run/case-weather/activation-permit' in timer_block
    assert 'systemctl systemd-run systemd-analyze busctl crontab pgrep runuser mktemp install findmnt sync' in content
    assert 'systemd-analyze verify $NEW_RELEASE/systemd/*.service $NEW_RELEASE/systemd/*.timer' in content
    assert 'InaccessiblePaths=$PROJECT_DIR/storage' not in service_block
    assert 'backup-validation.env' not in service_block
    assert '$CURRENT_LINK/app/scripts/backup.sh --if-present' not in service_block
    for unit in (
        'case-weather.service',
        'case-weather-backup.service',
        'case-weather-backup.timer',
        'case-weather-cache.service',
        'case-weather-cache.timer',
        'case-weather-cache-bootstrap.service',
        'case-weather-cache-bootstrap.timer',
        'case-weather-dispatch.service',
        'case-weather-dispatch.timer',
        'case-weather-risk-precompute.service',
        'case-weather-risk-precompute.timer',
        'case-weather-usage-cleanup.service',
        'case-weather-usage-cleanup.timer',
        'case-weather-sync.service',
        'case-weather-sync.timer',
    ):
        assert unit in content.split('步骤6.2.1: 给现有与新调度安装共享断电保护门', 1)[1]


def test_deploy_verifies_ssh_host_and_keeps_gunicorn_private():
    content = _load_deploy_script()

    assert 'StrictHostKeyChecking=yes' in content
    assert 'StrictHostKeyChecking=no' not in content
    assert 'UserKnownHostsFile=/dev/null' not in content
    assert '--bind 127.0.0.1:5000' in content
    assert '--bind 0.0.0.0:5000' not in content


def test_deploy_can_stage_formal_wechat_and_weather_readiness():
    content = _load_deploy_script()

    assert 'DEPLOY_REQUIRE_WECHAT_READY' in content
    assert 'WECHAT_RELEASE_FORM_FILE' in content
    assert '--form-only' in content
    assert 'LOCAL_WECHAT_FORM_READY' in content
    assert 'remote_env_update "WX_MINIPROGRAM_APPID"' in content
    assert 'remote_env_update "WX_MINIPROGRAM_SECRET"' in content
    assert 'remote_env_generate_secret "WX_MINIPROGRAM_OPENID_PEPPER"' in content
    assert 'remote_env_generate_secret "WX_MINIPROGRAM_SESSION_SECRET"' in content
    assert 'ALLOW_WEATHER_UNAVAILABLE' in content


def test_preview_does_not_generate_partial_wechat_authentication():
    content = _load_deploy_script()

    assert 'WX_MINIPROGRAM_OPENID_PEPPER=\n' in content
    assert 'WX_MINIPROGRAM_SESSION_SECRET=\n' in content
    assert 'WX_OPENID_PEPPER_GEN=' not in content
    assert 'WX_SESSION_SECRET_GEN=' not in content
    guard = 'if [ "$FORMAL_WECHAT_CONFIG_ALLOWED" = "1" ]; then'
    assert guard in content
    assert content.index(guard) < content.index(
        'remote_env_update "WX_MINIPROGRAM_SECRET"'
    )
    assert content.count('remote_env_generate_secret "WX_MINIPROGRAM_OPENID_PEPPER"') == 1
    assert content.count('remote_env_generate_secret "WX_MINIPROGRAM_SESSION_SECRET"') == 1
    assert 'WX_MINIPROGRAM_APPID 与 WX_MINIPROGRAM_SECRET 必须由同一次发布同时提供。' in content


def test_explicit_credentials_rotate_and_auth_modes_clear_stale_values():
    content = _load_deploy_script()

    assert 'remote_env_update "QWEATHER_KEY" "$LOCAL_QWEATHER_KEY" "always"' in content
    assert 'remote_env_update "QWEATHER_API_BASE" "$LOCAL_QWEATHER_API_BASE" "always"' in content
    assert 'remote_env_update "QWEATHER_JWT_KID" "$LOCAL_QWEATHER_JWT_KID" "always"' in content
    assert 'remote_env_update "QWEATHER_JWT_PROJECT_ID" "$LOCAL_QWEATHER_JWT_PROJECT_ID" "always"' in content
    assert 'remote_env_update "QWEATHER_JWT_PRIVATE_KEY_PATH" "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH" "always"' in content
    assert 'remote_env_update "WXPUSHER_APP_TOKEN" "$LOCAL_WXPUSHER_APP_TOKEN" "always"' in content
    assert 'remote_env_update "FEATURE_WXPUSHER" "$LOCAL_FEATURE_WXPUSHER" "always"' in content
    assert 'remote_env_update "QWEATHER_KEY" "" "always"' in content
    assert 'remote_env_update "QWEATHER_JWT_KID" "" "always"' in content


def test_deploy_requires_exact_recovery_transaction_acknowledgement():
    content = _load_deploy_script()
    activate = _load_activate_script()

    assert 'DEPLOY_RECOVERY_ACKNOWLEDGED_TRANSACTION' in content
    assert 'RECOVERY_ACKNOWLEDGED_TRANSACTION=$RECOVERY_ACKNOWLEDGED_TRANSACTION' in content
    assert 'ROLLBACK_REQUIRED.txt' in activate
    assert 'RECOVERY_CONFIRMED' in activate
    assert '尚未人工确认的部署恢复事务' in activate


def test_activation_arms_qweather_gate_immediately_before_public_restart():
    content = _load_activate_script()

    assert '--key QWEATHER_NETWORK_NOT_BEFORE_EPOCH' in content
    assert 'not_before_epoch=$((now_epoch + 1800))' in content
    assert content.index('arm_qweather_network_gate\n') < content.index('start_new_release\n')
    assert content.index('start_candidate_release\n') < content.index('arm_qweather_network_gate\n')


def test_precompute_script_respects_deploy_venv_dir():
    content = _load_precompute_script()

    assert '${DEPLOY_VENV_DIR:+$DEPLOY_VENV_DIR/bin/python}' in content
    assert 'VENV_PY="${VENV_PY:-python3}"' in content


def test_remote_preview_is_rejected_before_any_remote_command(tmp_path):
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    remote_log = tmp_path / 'remote.log'
    _write_executable(
        fake_bin / 'ssh',
        '''#!/bin/bash
set -euo pipefail
payload="$(/bin/cat)"
{
    printf 'COMMAND'
    printf ' <%s>' "$@"
    printf '\nPAYLOAD=%s\n' "$payload"
} >> "$FAKE_DEPLOY_LOG"
''',
    )
    _write_executable(
        fake_bin / 'rsync',
        '''#!/bin/bash
set -euo pipefail
printf 'RSYNC' >> "$FAKE_DEPLOY_LOG"
printf ' <%s>' "$@" >> "$FAKE_DEPLOY_LOG"
printf '\n' >> "$FAKE_DEPLOY_LOG"
''',
    )
    form_file = tmp_path / 'wechat-release.env'
    form_file.write_text(
        '''WECHAT_FORM_READY=0
WX_MINIPROGRAM_APPID=wx-preview-canary
WX_MINIPROGRAM_SECRET=preview-secret-canary
WXPUSHER_APP_TOKEN=preview-wxpusher-canary
WX_MINIPROGRAM_PRIVACY_VERSION=preview-privacy-canary
FEATURE_HEAT_EXPOSURE_GIS=1
''',
        encoding='utf-8',
    )
    deploy_env = tmp_path / 'deploy.env'
    deploy_env.write_text(
        f'''DEPLOY_SERVER=fake.example
DEPLOY_USER=deployer
DEPLOY_PROJECT_DIR=/srv/case-weather
DEPLOY_RELEASE_ROOT=/srv/case-weather-deploy
DEPLOY_RELEASE_ID=preview-test
DEPLOY_LOCAL_DIR={ROOT}
DEPLOY_REQUIRE_WECHAT_READY=0
WECHAT_RELEASE_FORM_FILE={form_file}
''',
        encoding='utf-8',
    )
    environment = os.environ.copy()
    environment.update(
        {
            'ENV_FILE': str(deploy_env),
            'WECHAT_RELEASE_FORM_FILE': str(form_file),
            'FAKE_DEPLOY_LOG': str(remote_log),
            'PATH': f"{fake_bin}:{environment['PATH']}",
        }
    )

    result = subprocess.run(
        ['bash', str(ROOT / 'scripts' / 'deploy.sh')],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 64
    assert '本地微信 DevTools 预览' in result.stderr
    assert not remote_log.exists()
    remote_text = ''
    for canary in (
        'wx-preview-canary',
        'preview-secret-canary',
        'preview-wxpusher-canary',
        'preview-privacy-canary',
    ):
        assert canary not in remote_text
    assert '--key WX_MINIPROGRAM_OPENID_PEPPER' not in remote_text
    assert '--key WX_MINIPROGRAM_SESSION_SECRET' not in remote_text
