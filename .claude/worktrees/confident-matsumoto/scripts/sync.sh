#!/bin/bash
# 快速同步脚本 - 仅上传代码并重启服务（不修改配置）

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

echo "=== 快速同步 case-weather 项目 ==="

# 上传文件
echo "步骤1: 上传项目文件..."
if use_sshpass && [ -n "$SSHPASS" ]; then
    SSHPASS="${SSHPASS:-$PASSWORD}" sshpass -e rsync -avz \
        --exclude __pycache__ \
        --exclude *.pyc \
        --exclude instance \
        --exclude storage \
        --exclude health_weather.db \
        --exclude .git \
        --exclude venv \
        --exclude .venv \
        --exclude .venv2 \
        --exclude .env \
        --exclude .env.local \
        -e ssh "$LOCAL_DIR/" "$USER@$SERVER:$PROJECT_DIR/"
elif use_expect && [ -n "$SSHPASS" ]; then
    expect -c "
        set timeout 600
        set password \$env(SSHPASS)
        spawn rsync -avz --exclude __pycache__ --exclude *.pyc --exclude instance --exclude storage --exclude health_weather.db --exclude .git --exclude venv --exclude .venv --exclude .venv2 --exclude .env --exclude .env.local -e ssh $LOCAL_DIR/ $USER@$SERVER:$PROJECT_DIR/
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
else
    rsync -avz --exclude __pycache__ --exclude *.pyc --exclude instance --exclude storage --exclude health_weather.db --exclude .git --exclude venv --exclude .venv --exclude .venv2 --exclude .env --exclude .env.local -e ssh "$LOCAL_DIR/" "$USER@$SERVER:$PROJECT_DIR/"
fi

# 重启服务
echo ""
echo "步骤2: 重启服务..."
if use_sshpass && [ -n "$SSHPASS" ]; then
    SSHPASS="${SSHPASS:-$PASSWORD}" sshpass -e ssh -o StrictHostKeyChecking=no "$USER@$SERVER" "systemctl restart case-weather && systemctl status case-weather --no-pager"
elif use_expect && [ -n "$SSHPASS" ]; then
    expect -c "
        set timeout 30
        set password \$env(SSHPASS)
        spawn ssh -o StrictHostKeyChecking=no $USER@$SERVER \"systemctl restart case-weather && systemctl status case-weather --no-pager\"
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
else
    ssh -o StrictHostKeyChecking=no "$USER@$SERVER" "systemctl restart case-weather && systemctl status case-weather --no-pager"
fi

echo ""
echo "=== 同步完成 ==="
echo "访问地址: http://$SERVER:5000"
