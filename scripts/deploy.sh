#!/bin/bash
# 部署脚本 - 将项目部署到远程服务器
set -e

# 一旦远程命令或测试失败，立即中止部署，避免把“半成功”误判为完成。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"

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

SERVER="${DEPLOY_SERVER:-172.245.126.42}"
USER="${DEPLOY_USER:-root}"
PROJECT_DIR="${DEPLOY_PROJECT_DIR:-/opt/case-weather}"
LOCAL_DIR="${DEPLOY_LOCAL_DIR:-$ROOT_DIR}"
VENV_DIR="${DEPLOY_VENV_DIR:-$PROJECT_DIR/.venv2}"
PASSWORD="${DEPLOY_PASSWORD:-${SSHPASS:-}}"

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

SSH_OPTS="${SSH_OPTS:--o BatchMode=yes}"
LOCAL_QWEATHER_KEY=""
LOCAL_QWEATHER_API_BASE=""
LOCAL_AMAP_KEY=""
LOCAL_AMAP_JS_API_KEY=""
LOCAL_AMAP_WEB_SERVICE_KEY=""
LOCAL_AMAP_SECURITY_JS_CODE=""
LOCAL_WXPUSHER_APP_TOKEN=""

load_local_api_keys() {
    [ -f "$ENV_FILE" ] || return 0
    while IFS='=' read -r key value; do
        case "$key" in
            ''|\#*) continue ;;
            QWEATHER_KEY|QWEATHER_API_BASE|AMAP_KEY|AMAP_JS_API_KEY|AMAP_WEB_SERVICE_KEY|AMAP_SECURITY_JS_CODE|WXPUSHER_APP_TOKEN)
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
                    AMAP_JS_API_KEY) LOCAL_AMAP_JS_API_KEY="$value" ;;
                    AMAP_WEB_SERVICE_KEY) LOCAL_AMAP_WEB_SERVICE_KEY="$value" ;;
                    AMAP_SECURITY_JS_CODE) LOCAL_AMAP_SECURITY_JS_CODE="$value" ;;
                    WXPUSHER_APP_TOKEN) LOCAL_WXPUSHER_APP_TOKEN="$value" ;;
                esac
                ;;
        esac
    done < "$ENV_FILE"
}

load_local_api_keys

# 使用 expect 执行远程命令的函数
remote_exec() {
    if use_sshpass && [ -n "$SSHPASS" ]; then
        SSHPASS="${SSHPASS:-$PASSWORD}" sshpass -e ssh -o StrictHostKeyChecking=no "$USER@$SERVER" "$1"
        return
    fi

    if use_expect && [ -n "$SSHPASS" ]; then
        expect -c "
            set timeout 300
            set password \$env(SSHPASS)
            spawn ssh -o StrictHostKeyChecking=no $USER@$SERVER \"$1\"
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
            -e ssh "$LOCAL_DIR/" "$USER@$SERVER:$PROJECT_DIR/"
        return
    fi

    if use_expect && [ -n "$SSHPASS" ]; then
        expect -c "
            set timeout 600
            set password \$env(SSHPASS)
        spawn rsync -avz --exclude '__pycache__' --exclude '*.pyc' --exclude 'instance' --exclude 'storage' --exclude 'health_weather.db' --exclude 'data/research/*.xlsx' --exclude 'data/research/*.xls' --exclude '.git' --exclude 'venv' --exclude '.venv' --exclude '.venv2' --exclude '.env' --exclude '.env.local' -e ssh $LOCAL_DIR/ $USER@$SERVER:$PROJECT_DIR/
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

    rsync -avz --exclude '__pycache__' --exclude '*.pyc' --exclude 'instance' --exclude 'storage' --exclude 'health_weather.db' --exclude 'data/research/*.xlsx' --exclude 'data/research/*.xls' --exclude '.git' --exclude 'venv' --exclude '.venv' --exclude '.venv2' --exclude '.env' --exclude '.env.local' -e ssh "$LOCAL_DIR/" "$USER@$SERVER:$PROJECT_DIR/"
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
REDIS_URL=redis://127.0.0.1:6379/0
RATE_LIMIT_STORAGE_URI=redis://127.0.0.1:6379/0
QWEATHER_KEY=
AMAP_KEY=
AMAP_JS_API_KEY=
AMAP_WEB_SERVICE_KEY=
AMAP_SECURITY_JS_CODE=
WXPUSHER_APP_TOKEN=
WXPUSHER_API_BASE=https://wxpusher.zjiecode.com/api
PUBLIC_BASE_URL=
EOF
echo '已创建新的 .env 文件'; else echo '.env 文件已存在，跳过创建'; fi"

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
if [ -n "$LOCAL_AMAP_JS_API_KEY" ]; then
    remote_exec "grep -q '^AMAP_JS_API_KEY=' $PROJECT_DIR/.env || echo 'AMAP_JS_API_KEY=' >> $PROJECT_DIR/.env"
    remote_exec "if grep -q '^AMAP_JS_API_KEY=$' $PROJECT_DIR/.env; then sed -i 's|^AMAP_JS_API_KEY=$|AMAP_JS_API_KEY=$LOCAL_AMAP_JS_API_KEY|' $PROJECT_DIR/.env; fi"
fi
if [ -n "$LOCAL_AMAP_WEB_SERVICE_KEY" ]; then
    remote_exec "grep -q '^AMAP_WEB_SERVICE_KEY=' $PROJECT_DIR/.env || echo 'AMAP_WEB_SERVICE_KEY=' >> $PROJECT_DIR/.env"
    remote_exec "if grep -q '^AMAP_WEB_SERVICE_KEY=$' $PROJECT_DIR/.env; then sed -i 's|^AMAP_WEB_SERVICE_KEY=$|AMAP_WEB_SERVICE_KEY=$LOCAL_AMAP_WEB_SERVICE_KEY|' $PROJECT_DIR/.env; fi"
fi
if [ -n "$LOCAL_AMAP_SECURITY_JS_CODE" ]; then
    remote_exec "grep -q '^AMAP_SECURITY_JS_CODE=' $PROJECT_DIR/.env || echo 'AMAP_SECURITY_JS_CODE=' >> $PROJECT_DIR/.env"
    remote_exec "if grep -q '^AMAP_SECURITY_JS_CODE=$' $PROJECT_DIR/.env; then sed -i 's|^AMAP_SECURITY_JS_CODE=$|AMAP_SECURITY_JS_CODE=$LOCAL_AMAP_SECURITY_JS_CODE|' $PROJECT_DIR/.env; fi"
fi
if [ -n "$LOCAL_WXPUSHER_APP_TOKEN" ]; then
    remote_exec "grep -q '^WXPUSHER_APP_TOKEN=' $PROJECT_DIR/.env || echo 'WXPUSHER_APP_TOKEN=' >> $PROJECT_DIR/.env"
    remote_exec "if grep -q '^WXPUSHER_APP_TOKEN=$' $PROJECT_DIR/.env; then sed -i 's|^WXPUSHER_APP_TOKEN=$|WXPUSHER_APP_TOKEN=$LOCAL_WXPUSHER_APP_TOKEN|' $PROJECT_DIR/.env; fi"
fi

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
ExecStart=$VENV_DIR/bin/gunicorn --workers 3 --bind 127.0.0.1:5000 --timeout 120 app:app
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF"

echo ""
echo "步骤8: 启动服务..."
remote_exec "systemctl daemon-reload && systemctl enable case-weather && systemctl start case-weather && systemctl status case-weather"

echo ""
echo "=== 部署完成 ==="
echo "访问地址: http://$SERVER:5000"
