#!/bin/bash
# 适用于经审核的新部署；已有不可变 release 应使用对应发布事务。
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
source "$SCRIPT_DIR/deployment_security.sh"
initialize_deploy_security
VENV_DIR="$PROJECT_DIR/.venv2"

check_remote_unit_active() {
    local unit="$1"
    remote_exec "systemctl is-active --quiet $unit"
}

# 配置完整性、预置证书和专用账号先检查，不能以公开明文充当部署兜底。
remote_exec "set -eu; test \"\$(id -u)\" = 0; test -s '$DEPLOY_TLS_CERT_FILE'; test -s '$DEPLOY_TLS_KEY_FILE'; openssl x509 -in '$DEPLOY_TLS_CERT_FILE' -checkend 86400 -noout"
remote_exec "set -eu; if ! id '$APP_USER' >/dev/null 2>&1; then useradd --system --user-group --home-dir '$PROJECT_DIR' --shell /usr/sbin/nologin '$APP_USER'; fi; test \"\$(id -u '$APP_USER')\" != 0"
remote_exec "apt-get update && apt-get install -y python3 python3-pip python3-venv rsync redis-server sqlite3 nginx curl"
remote_exec "systemctl enable --now redis-server"
remote_exec "set -eu; umask 077; test ! -L '$PROJECT_DIR'; install -d -o root -g '$APP_USER' -m 0750 '$PROJECT_DIR'"
upload_files
remote_exec "cd '$PROJECT_DIR' && python3 -m venv '$VENV_DIR' && '$VENV_DIR/bin/pip' install -r requirements.txt && '$VENV_DIR/bin/pip' install gunicorn"

# 新文件在写入任何秘密前即为0600，已有宽权限也在读取前纠正。
remote_exec "set -eu; umask 077; python3 '$PROJECT_DIR/scripts/secure_environment.py' protect '$PROJECT_DIR/.env'; if [ ! -s '$PROJECT_DIR/.env' ]; then cat > '$PROJECT_DIR/.env' <<'ENV_DEFAULTS'
FLASK_ENV=production
DEBUG=false
DATABASE_URI=sqlite:///health_weather.db
REDIS_URL=redis://127.0.0.1:6379/0
RATE_LIMIT_STORAGE_URI=redis://127.0.0.1:6379/0
QWEATHER_CANONICAL_LOCATION=116.20,29.27
QWEATHER_MONTHLY_REQUEST_LIMIT=40000
QWEATHER_BUDGET_FAIL_CLOSED=1
WEATHER_CACHE_TTL_MINUTES=30
FORECAST_CACHE_TTL_MINUTES=30
QWEATHER_WARNING_CACHE_TTL_MINUTES=30
WEATHER_SYNC_LOCATIONS=都昌县
ENV_DEFAULTS
fi; python3 '$PROJECT_DIR/scripts/secure_environment.py' defaults '$PROJECT_DIR/.env'"
# payload仅走SSH标准输入，不进入远端命令行、日志或临时环境快照。
python3 "$SCRIPT_DIR/secure_environment.py" payload "$ENV_FILE" | \
    remote_exec "python3 '$PROJECT_DIR/scripts/secure_environment.py' merge '$PROJECT_DIR/.env'"
remote_exec "set -eu; for name in instance storage logs; do test ! -L '$PROJECT_DIR'/\$name; install -d -o '$APP_USER' -g '$APP_USER' -m 0700 '$PROJECT_DIR'/\$name; done"
remote_exec "cd '$PROJECT_DIR' && PROJECT_DIR='$PROJECT_DIR' ENV_FILE='$PROJECT_DIR/.env' bash scripts/backup.sh --if-present"
remote_exec "systemctl stop case-weather.service case-weather-cache.timer case-weather-dispatch.timer case-weather-risk-precompute.timer 2>/dev/null || test \"\$(systemctl is-active case-weather.service)\" = inactive"
remote_exec "cd '$PROJECT_DIR' && VENV_PY='$VENV_DIR/bin/python' bash scripts/server_migrate.sh"
remote_exec "cd '$PROJECT_DIR' && '$VENV_DIR/bin/python' -m pytest -q"

# 代码由root持有；服务仅可写数据库、存储和日志，不能修改源码、环境或备份。
remote_exec "python3 '$PROJECT_DIR/scripts/deployment_security.py' secure-tree '$PROJECT_DIR' '$APP_USER'"
remote_exec "umask 077; python3 '$PROJECT_DIR/scripts/deployment_security.py' nginx-config '$PUBLIC_BASE_URL' '$DEPLOY_TLS_CERT_FILE' '$DEPLOY_TLS_KEY_FILE' > /etc/nginx/conf.d/case-weather.conf; nginx -t"

for unit in case-weather case-weather-cache case-weather-dispatch case-weather-risk-precompute; do
    case "$unit" in
        case-weather) TYPE=simple; START="$VENV_DIR/bin/gunicorn --workers 3 --bind 127.0.0.1:5000 --timeout 120 app:app" ;;
        case-weather-cache) TYPE=oneshot; START="/bin/bash $PROJECT_DIR/scripts/weather_cache_sync.sh" ;;
        case-weather-dispatch) TYPE=oneshot; START="/bin/bash $PROJECT_DIR/scripts/dispatch_alerts.sh --dedupe-hours 6" ;;
        case-weather-risk-precompute) TYPE=oneshot; START="/bin/bash $PROJECT_DIR/scripts/community_risk_precompute.sh" ;;
    esac
    remote_exec "umask 077; cat > /etc/systemd/system/$unit.service <<'UNIT'
[Unit]
Description=Case Weather service
After=network.target
[Service]
Type=$TYPE
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=VENV_PY=$VENV_DIR/bin/python
ExecStart=$START
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/logs
[Install]
WantedBy=multi-user.target
UNIT"
done
for spec in 'cache 30min 2min' 'dispatch 30min 2min' 'risk-precompute 60min 5min'; do
    read -r unit interval boot <<< "$spec"
    remote_exec "umask 077; cat > /etc/systemd/system/case-weather-$unit.timer <<'TIMER'
[Unit]
Description=Case Weather scheduled task
[Timer]
OnBootSec=$boot
OnUnitActiveSec=$interval
Persistent=true
Unit=case-weather-$unit.service
[Install]
WantedBy=timers.target
TIMER"
done
remote_exec "systemctl daemon-reload && systemctl enable --now nginx && systemctl reload nginx && systemctl enable --now case-weather && systemctl restart case-weather"
check_remote_unit_active "case-weather"
for unit in cache dispatch risk-precompute; do
    remote_exec "systemctl enable --now case-weather-$unit.timer"
done
check_remote_unit_active "case-weather-cache.timer"
check_remote_unit_active "case-weather-dispatch.timer"
check_remote_unit_active "case-weather-risk-precompute.timer"
verify_deployment_boundary
printf '部署完成，HTTPS与端口隔离检查通过：%s\n' "$PUBLIC_BASE_URL"
