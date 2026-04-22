#!/bin/bash
# 部署脚本 - 将项目部署到远程服务器

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"

# SSH 默认选项：
# - 禁用 ssh-agent（部分环境下会导致 banner exchange 卡住）
# - 启用连接复用，减少短时间内频繁建连触发服务器 sshd 惩罚/限流
# - 关闭 known_hosts 写入，避免非交互部署失败
DEFAULT_SSH_OPTS="${DEFAULT_SSH_OPTS:--o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o IdentityAgent=none -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ControlMaster=auto -o ControlPersist=300 -o ControlPath=/tmp/cw-ssh-%r@%h-%p}"
SSH_OPTS="${SSH_OPTS:-$DEFAULT_SSH_OPTS}"

LOCAL_QWEATHER_KEY=""
LOCAL_QWEATHER_API_BASE=""
LOCAL_AMAP_KEY=""
LOCAL_WXPUSHER_APP_TOKEN=""

load_deploy_env() {
    [ -f "$ENV_FILE" ] || return 0
    while IFS='=' read -r key value; do
        case "$key" in
            ''|\#*) continue ;;
            DEPLOY_SERVER|DEPLOY_USER|DEPLOY_PASSWORD|DEPLOY_PROJECT_DIR|DEPLOY_LOCAL_DIR|SSHPASS)
                value="${value%%#*}"
                value="${value%"${value##*[![:space:]]}"}"
                value="${value#"${value%%[![:space:]]*}"}"
                if [[ "$value" == \"*\" && "$value" == *\" ]]; then
                    value="${value:1:${#value}-2}"
                fi
                export "$key"="$value"
                ;;
        esac
    done < "$ENV_FILE"
}

load_deploy_env

load_local_api_keys() {
    [ -f "$ENV_FILE" ] || return 0
    while IFS='=' read -r key value; do
        case "$key" in
            ''|\#*) continue ;;
            QWEATHER_KEY|QWEATHER_API_BASE|AMAP_KEY|WXPUSHER_APP_TOKEN)
                value="${value%%#*}"
                value="${value%"${value##*[![:space:]]}"}"
                value="${value#"${value%%[![:space:]]*}"}"
                if [[ "$value" == \"*\" && "$value" == *\" ]]; then
                    value="${value:1:${#value}-2}"
                fi
                case "$key" in
                    QWEATHER_KEY) LOCAL_QWEATHER_KEY="$value" ;;
                    QWEATHER_API_BASE) LOCAL_QWEATHER_API_BASE="$value" ;;
                    AMAP_KEY) LOCAL_AMAP_KEY="$value" ;;
                    WXPUSHER_APP_TOKEN) LOCAL_WXPUSHER_APP_TOKEN="$value" ;;
                esac
                ;;
        esac
    done < "$ENV_FILE"
}

load_local_api_keys

SERVER="${DEPLOY_SERVER:-}"
USER="${DEPLOY_USER:-}"
PROJECT_DIR="${DEPLOY_PROJECT_DIR:-/opt/your-app}"
LOCAL_DIR="${DEPLOY_LOCAL_DIR:-$ROOT_DIR}"
VENV_DIR="${DEPLOY_VENV_DIR:-$PROJECT_DIR/.venv2}"
PASSWORD="${DEPLOY_PASSWORD:-${SSHPASS:-}}"

require_env_value() {
    local name="$1"
    local value="$2"
    if [ -z "$value" ]; then
        echo "缺少必填环境变量: $name" >&2
        exit 1
    fi
}

require_env_value "DEPLOY_SERVER" "$SERVER"
require_env_value "DEPLOY_USER" "$USER"

if [ -z "$SSHPASS" ] && [ -n "$PASSWORD" ]; then
    export SSHPASS="$PASSWORD"
fi

use_sshpass() {
    command -v sshpass >/dev/null 2>&1
}

use_expect() {
    command -v expect >/dev/null 2>&1
}

echo "=== 开始部署 case-weather 项目 ==="

# 使用 expect 执行远程命令的函数
remote_exec() {
    if use_sshpass && [ -n "$SSHPASS" ]; then
        SSHPASS="${SSHPASS:-$PASSWORD}" sshpass -e ssh $SSH_OPTS "$USER@$SERVER" "$1"
        return
    fi

    if use_expect && [ -n "$SSHPASS" ]; then
        expect -c "
            set timeout 300
            set password \$env(SSHPASS)
            spawn ssh $SSH_OPTS $USER@$SERVER \"$1\"
            expect {
                \"*password:\" {
                    send \"\$password\r\"
                    exp_continue
                }
                \"*yes/no*\" {
                    send \"yes\r\"
                    exp_continue
                }
                eof
            }
        "
        return
    fi

    ssh $SSH_OPTS "$USER@$SERVER" "$1"
}

check_remote_unit_active() {
    local unit="$1"
    remote_exec "systemctl is-active --quiet $unit"
    echo "已确认 systemd 单元运行中: $unit"
}

# 使用 rsync/scp 上传文件的函数
upload_files() {
    if use_sshpass && [ -n "$SSHPASS" ]; then
        SSHPASS="${SSHPASS:-$PASSWORD}" sshpass -e rsync -avz \
            --exclude '__pycache__' \
            --exclude '*.pyc' \
            --exclude 'instance' \
            --exclude 'storage' \
            --exclude 'health_weather.db' \
            --exclude 'data/research/*.xlsx' \
            --exclude 'data/research/*.xls' \
            --exclude '.git' \
            --exclude 'venv' \
            --exclude '.venv' \
            --exclude '.venv2' \
            --exclude '.env' \
            --exclude '.env.local' \
            -e "ssh $SSH_OPTS" "$LOCAL_DIR/" "$USER@$SERVER:$PROJECT_DIR/"
        return
    fi

    if use_expect && [ -n "$SSHPASS" ]; then
        expect -c "
            set timeout 600
            set password \$env(SSHPASS)
        spawn rsync -avz --exclude '__pycache__' --exclude '*.pyc' --exclude 'instance' --exclude 'storage' --exclude 'health_weather.db' --exclude 'data/research/*.xlsx' --exclude 'data/research/*.xls' --exclude '.git' --exclude 'venv' --exclude '.venv' --exclude '.venv2' --exclude '.env' --exclude '.env.local' -e \"ssh $SSH_OPTS\" $LOCAL_DIR/ $USER@$SERVER:$PROJECT_DIR/
        expect {
                \"*password:\" {
                    send \"\$password\r\"
                    exp_continue
                }
                \"*yes/no*\" {
                    send \"yes\r\"
                    exp_continue
                }
                eof
            }
        "
        return
    fi

    rsync -avz --exclude '__pycache__' --exclude '*.pyc' --exclude 'instance' --exclude 'storage' --exclude 'health_weather.db' --exclude 'data/research/*.xlsx' --exclude 'data/research/*.xls' --exclude '.git' --exclude 'venv' --exclude '.venv' --exclude '.venv2' --exclude '.env' --exclude '.env.local' -e "ssh $SSH_OPTS" "$LOCAL_DIR/" "$USER@$SERVER:$PROJECT_DIR/"
}

echo "步骤1: 测试服务器连接..."
remote_exec "echo '连接成功'"

echo ""
echo "步骤2: 安装系统依赖..."
remote_exec "apt-get update && apt-get install -y python3 python3-pip python3-venv rsync redis-server"

echo ""
echo "步骤2.1: 启动 Redis（用于生产环境限流存储）..."
remote_exec "systemctl enable --now redis-server || true"

echo ""
echo "步骤3: 创建项目目录..."
remote_exec "mkdir -p $PROJECT_DIR"

echo ""
echo "步骤4: 上传项目文件..."
upload_files

echo ""
echo "步骤5: 创建虚拟环境并安装依赖..."
remote_exec "cd $PROJECT_DIR && python3 -m venv $VENV_DIR && $VENV_DIR/bin/pip install --upgrade pip && $VENV_DIR/bin/pip install -r requirements.txt && $VENV_DIR/bin/pip install gunicorn"

echo ""
echo "步骤6: 创建环境配置文件(如果不存在)..."
remote_exec "if [ ! -f $PROJECT_DIR/.env ]; then SECRET_KEY_GEN=\$(python3 -c 'import secrets; print(secrets.token_hex(32))'); PAIR_TOKEN_PEPPER_GEN=\$(python3 -c 'import secrets; print(secrets.token_hex(32))'); cat > $PROJECT_DIR/.env << EOF
FLASK_ENV=production
DEBUG=false
SECRET_KEY=\$SECRET_KEY_GEN
PAIR_TOKEN_PEPPER=\$PAIR_TOKEN_PEPPER_GEN
DATABASE_URI=sqlite:///health_weather.db
REDIS_URL=redis://127.0.0.1:6379/0
RATE_LIMIT_STORAGE_URI=redis://127.0.0.1:6379/0
QWEATHER_KEY=
AMAP_KEY=
WXPUSHER_APP_TOKEN=
WXPUSHER_API_BASE=https://wxpusher.zjiecode.com/api
PUBLIC_BASE_URL=
EOF
echo '已创建新的 .env 文件'; else echo '.env 文件已存在，跳过创建'; fi"

echo ""
echo "步骤6.1: 确保数据库目录与关键配置存在..."
remote_exec "mkdir -p $PROJECT_DIR/instance && (grep -q '^DATABASE_URI=' $PROJECT_DIR/.env || echo 'DATABASE_URI=sqlite:///health_weather.db' >> $PROJECT_DIR/.env)"

echo ""
echo "步骤6.1.1: 写入必要的 API Key（仅在服务器端为空/缺失时写入）..."
# PUBLIC_BASE_URL: by default point to the server IP:5000 for click tracking.
DEFAULT_PUBLIC_BASE_URL="http://$SERVER:5000"
remote_exec "grep -q '^PUBLIC_BASE_URL=' $PROJECT_DIR/.env || echo 'PUBLIC_BASE_URL=' >> $PROJECT_DIR/.env"
remote_exec "if grep -q '^PUBLIC_BASE_URL=$' $PROJECT_DIR/.env; then sed -i 's|^PUBLIC_BASE_URL=$|PUBLIC_BASE_URL=$DEFAULT_PUBLIC_BASE_URL|' $PROJECT_DIR/.env; fi"

if [ -n "$LOCAL_QWEATHER_KEY" ]; then
    remote_exec "grep -q '^QWEATHER_KEY=' $PROJECT_DIR/.env || echo 'QWEATHER_KEY=' >> $PROJECT_DIR/.env"
    remote_exec "if grep -q '^QWEATHER_KEY=$' $PROJECT_DIR/.env; then sed -i 's|^QWEATHER_KEY=$|QWEATHER_KEY=$LOCAL_QWEATHER_KEY|' $PROJECT_DIR/.env; fi"
fi
if [ -n "$LOCAL_QWEATHER_API_BASE" ]; then
    remote_exec "grep -q '^QWEATHER_API_BASE=' $PROJECT_DIR/.env || echo 'QWEATHER_API_BASE=' >> $PROJECT_DIR/.env"
    remote_exec "if grep -q '^QWEATHER_API_BASE=$' $PROJECT_DIR/.env; then sed -i 's|^QWEATHER_API_BASE=$|QWEATHER_API_BASE=$LOCAL_QWEATHER_API_BASE|' $PROJECT_DIR/.env; fi"
fi
if [ -n "$LOCAL_AMAP_KEY" ]; then
    remote_exec "grep -q '^AMAP_KEY=' $PROJECT_DIR/.env || echo 'AMAP_KEY=' >> $PROJECT_DIR/.env"
    remote_exec "if grep -q '^AMAP_KEY=$' $PROJECT_DIR/.env; then sed -i 's|^AMAP_KEY=$|AMAP_KEY=$LOCAL_AMAP_KEY|' $PROJECT_DIR/.env; fi"
fi
if [ -n "$LOCAL_WXPUSHER_APP_TOKEN" ]; then
    remote_exec "grep -q '^WXPUSHER_APP_TOKEN=' $PROJECT_DIR/.env || echo 'WXPUSHER_APP_TOKEN=' >> $PROJECT_DIR/.env"
    remote_exec "if grep -q '^WXPUSHER_APP_TOKEN=$' $PROJECT_DIR/.env; then sed -i 's|^WXPUSHER_APP_TOKEN=$|WXPUSHER_APP_TOKEN=$LOCAL_WXPUSHER_APP_TOKEN|' $PROJECT_DIR/.env; fi"
fi

echo ""
echo "步骤6.2: 初始化/迁移数据库（安全 stamp + upgrade）..."
remote_exec "cd $PROJECT_DIR && mkdir -p backups && if [ -f instance/health_weather.db ]; then cp -a instance/health_weather.db backups/health_weather.db.$(date +%Y%m%d_%H%M%S); echo '已备份 instance/health_weather.db'; else echo '未发现 instance/health_weather.db，跳过备份'; fi"
remote_exec "systemctl stop case-weather || true; systemctl stop case-weather-dispatch.timer || true; systemctl stop case-weather-risk-precompute.timer || true"
remote_exec "cd $PROJECT_DIR && VENV_PY=$VENV_DIR/bin/python bash scripts/server_migrate.sh"

echo ""
echo "步骤6.3: 运行 pytest（不触碰生产库，用临时库）..."
remote_exec "cd $PROJECT_DIR && $VENV_DIR/bin/python -m pytest -q"

echo ""
echo "步骤7: 创建 systemd 服务..."
remote_exec "cat > /etc/systemd/system/case-weather.service << 'EOF'
[Unit]
Description=Case Weather Flask Application
After=network.target

[Service]
User=root
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/gunicorn --workers 3 --bind 0.0.0.0:5000 --timeout 120 app:app
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF"

echo ""
echo "步骤7.1: 创建预警推送定时任务（systemd timer）..."
remote_exec "cat > /etc/systemd/system/case-weather-dispatch.service << 'EOF'
[Unit]
Description=Case Weather - dispatch alerts (WxPusher)
After=network.target case-weather.service

[Service]
Type=oneshot
User=root
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$PROJECT_DIR/scripts/dispatch_alerts.sh --dedupe-hours 6
EOF

cat > /etc/systemd/system/case-weather-dispatch.timer << 'EOF'
[Unit]
Description=Case Weather - dispatch alerts every 30 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
EOF"

echo ""
echo "步骤7.2: 创建社区风险预计算定时任务（systemd timer）..."
remote_exec "cat > /etc/systemd/system/case-weather-risk-precompute.service << 'EOF'
[Unit]
Description=Case Weather - precompute community risk cache
After=network.target case-weather.service

[Service]
Type=oneshot
User=root
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/bin/bash $PROJECT_DIR/scripts/community_risk_precompute.sh
EOF

cat > /etc/systemd/system/case-weather-risk-precompute.timer << 'EOF'
[Unit]
Description=Case Weather - precompute community risk cache hourly

[Timer]
OnBootSec=5min
OnUnitActiveSec=60min
Persistent=true

[Install]
WantedBy=timers.target
EOF"

echo ""
echo "步骤8: 启动服务..."
remote_exec "systemctl daemon-reload"
remote_exec "systemctl enable --now case-weather"
remote_exec "systemctl restart case-weather"
remote_exec "systemctl status --no-pager case-weather"
check_remote_unit_active "case-weather"

echo ""
echo "步骤8.1: 启动定时器..."
remote_exec "systemctl enable --now case-weather-dispatch.timer"
remote_exec "systemctl status --no-pager case-weather-dispatch.timer"
check_remote_unit_active "case-weather-dispatch.timer"

echo ""
echo "步骤8.2: 启动社区风险预计算定时器..."
remote_exec "systemctl enable --now case-weather-risk-precompute.timer"
remote_exec "systemctl status --no-pager case-weather-risk-precompute.timer"
check_remote_unit_active "case-weather-risk-precompute.timer"

echo ""
echo "=== 部署完成 ==="
echo "访问地址: http://$SERVER:5000"
