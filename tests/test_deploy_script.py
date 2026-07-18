# -*- coding: utf-8 -*-
"""部署脚本回归测试。"""

from pathlib import Path


def _load_deploy_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"
    return script_path.read_text(encoding="utf-8")


def _load_precompute_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "community_risk_precompute.sh"
    return script_path.read_text(encoding="utf-8")


def _load_activate_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "activate_release.sh"
    return script_path.read_text(encoding="utf-8")


def test_deploy_script_checks_units_with_is_active():
    content = _load_deploy_script()
    activate = _load_activate_script()

    assert 'case-weather.service' in activate
    assert 'case-weather-cache-bootstrap.timer' in activate
    assert "常规天气缓存 timer 在首轮等待期间不应提前运行" in activate
    assert "常规天气缓存 timer 状态应为 disabled" in activate
    assert "bootstrap timer 状态应为 enabled" in activate
    assert 'check_remote_unit_active "case-weather-dispatch.timer"' not in content
    assert 'case-weather-risk-precompute.timer' in activate
    assert "旧 dispatch.timer 仍存在" in activate
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
    assert 'OnUnitActiveSec=30min' in content
    assert 'OnActiveSec=30min' in content
    assert 'OnSuccess=case-weather-dispatch.service' in content
    assert "cat > $NEW_RELEASE/systemd/case-weather-dispatch.timer" not in content
    assert 'ExecStart=/bin/bash $CURRENT_LINK/app/scripts/weather_cache_sync.sh' in content


def test_deploy_script_delays_first_cache_refresh_then_starts_recurring_timer():
    content = _load_deploy_script()
    recurring_start = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-cache.timer << 'EOF'"
    )
    recurring_end = content.index('EOF"', recurring_start)
    recurring_block = content[recurring_start:recurring_end]
    bootstrap_start = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-cache-bootstrap.service << 'EOF'"
    )
    bootstrap_end = content.index('EOF"', bootstrap_start)
    bootstrap_block = content[bootstrap_start:bootstrap_end]

    assert 'OnActiveSec=30min' in recurring_block
    assert 'OnUnitActiveSec=30min' in recurring_block
    assert '[Install]' in recurring_block
    assert 'WantedBy=timers.target' in recurring_block
    assert 'Wants=case-weather-cache.service' in bootstrap_block
    assert 'After=network.target case-weather.service case-weather-cache.service' in bootstrap_block
    assert 'OnSuccess=case-weather-cache.timer' in bootstrap_block
    assert 'ExecStart=/usr/bin/true' in bootstrap_block
    assert 'OnActiveSec=30min' in content[bootstrap_start:]
    assert 'RemainAfterElapse=no' in content[bootstrap_start:]
    activate = _load_activate_script()
    assert 'NextElapseUSecMonotonic' in activate
    assert 'remaining_us' in activate
    assert 'bootstrap timer 未保留完整的首轮 30 分钟等待窗口' in activate
    assert 'case-weather-cache-bootstrap.service --property=OnSuccess --value' in activate


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
    assert 'systemctl busctl' in content
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


def test_activate_transaction_stops_every_writer_and_commits_last():
    content = _load_activate_script()

    for unit in (
        'case-weather.service',
        'case-weather-cache.service',
        'case-weather-cache-bootstrap.service',
        'case-weather-dispatch.service',
        'case-weather-risk-precompute.service',
        'case-weather-usage-cleanup.service',
        'case-weather-cache.timer',
        'case-weather-cache-bootstrap.timer',
        'case-weather-dispatch.timer',
        'case-weather-risk-precompute.timer',
        'case-weather-usage-cleanup.timer',
    ):
        assert unit in content
    assert content.index('start_candidate_release\n') < content.index('LINK_MUTATED=1')
    assert content.index('install_new_units\n') < content.index(
        'prepare_release_timer_states\n'
    )
    assert content.index('prepare_release_timer_states\n') < content.index(
        'arm_qweather_network_gate\n'
    )
    assert content.index('wait_for_health "$HEALTH_URL"') < content.index('COMMITTED=1')
    assert content.index('start_new_release\n') < content.index('COMMITTED=1')
    assert content.index('FORWARD_ONLY=1') < content.index('COMMITTED=1')
    assert content.index('COMMITTED=1') < content.index('start_release_timers\n')
    assert content.index('start_release_timers\n') < content.index(
        'verify_release_state\n'
    )
    assert content.index('verify_release_state\n') < content.index(
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
    assert content.count("--exclude 'project.private.config.json'") == 2
    assert "--exclude '.env'" not in content
    assert "--exclude '.env.local'" not in content


def test_deploy_script_requires_https_public_base_url():
    content = _load_deploy_script()

    assert 'ALLOW_INSECURE_PUBLIC_BASE_URL' in content
    assert 'PUBLIC_BASE_URL 必须优先使用 HTTPS' in content
    assert 'ALLOW_INSECURE_PUBLIC_BASE_URL' in content
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
    assert content.index('$RELEASE_VENV/bin/python -m pytest -q') < content.index(
        'ln -s $PROJECT_DIR/.env $RELEASE_APP/.env'
    )


def test_deploy_only_supports_key_or_sshpass_and_locks_private_files():
    content = _load_deploy_script()

    assert 'expect -c' not in content
    assert '密码部署需要 sshpass' in content
    assert content.count('UMask=0077') == 6
    assert 'chmod 0700 $PROJECT_DIR/instance' in content


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


def test_explicit_credentials_rotate_and_auth_modes_clear_stale_values():
    content = _load_deploy_script()

    assert 'remote_env_update "QWEATHER_KEY" "$LOCAL_QWEATHER_KEY" "always"' in content
    assert 'remote_env_update "QWEATHER_API_BASE" "$LOCAL_QWEATHER_API_BASE" "always"' in content
    assert 'remote_env_update "QWEATHER_JWT_KID" "$LOCAL_QWEATHER_JWT_KID" "always"' in content
    assert 'remote_env_update "QWEATHER_JWT_PROJECT_ID" "$LOCAL_QWEATHER_JWT_PROJECT_ID" "always"' in content
    assert 'remote_env_update "QWEATHER_JWT_PRIVATE_KEY_PATH" "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH" "always"' in content
    assert 'remote_env_update "WXPUSHER_APP_TOKEN" "$LOCAL_WXPUSHER_APP_TOKEN" "always"' in content
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
