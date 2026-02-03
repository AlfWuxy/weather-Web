#!/bin/bash
# 下载服务器最新备份到本地
# 用法: ./scripts/download_backup.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"

load_deploy_env() {
    [ -f "$ENV_FILE" ] || return 0
    while IFS='=' read -r key value; do
        case "$key" in
            ''|\#*) continue ;;
            DEPLOY_SERVER|DEPLOY_USER|DEPLOY_PASSWORD|DEPLOY_BACKUP_DIR|DEPLOY_LOCAL_BACKUP_DIR|SSHPASS)
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

SERVER_HOST="${DEPLOY_SERVER:-172.245.126.42}"
SERVER_USER="${DEPLOY_USER:-root}"
SERVER="$SERVER_USER@$SERVER_HOST"
REMOTE_BACKUP_DIR="${DEPLOY_BACKUP_DIR:-/opt/case-weather/backups}"
LOCAL_BACKUP_DIR="${DEPLOY_LOCAL_BACKUP_DIR:-$ROOT_DIR/backups}"
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

# 创建本地备份目录
mkdir -p $LOCAL_BACKUP_DIR

echo "正在获取服务器最新备份..."

# 下载最新的备份文件
if use_sshpass && [ -n "$SSHPASS" ]; then
    SSHPASS="${SSHPASS:-$PASSWORD}" sshpass -e scp "$SERVER:$REMOTE_BACKUP_DIR/*.gz" "$LOCAL_BACKUP_DIR/"
elif use_expect && [ -n "$SSHPASS" ]; then
    expect -c "
        set timeout 120
        set password \$env(SSHPASS)
        spawn scp $SERVER:$REMOTE_BACKUP_DIR/*.gz $LOCAL_BACKUP_DIR/
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
    scp "$SERVER:$REMOTE_BACKUP_DIR/*.gz" "$LOCAL_BACKUP_DIR/"
fi

echo "备份已下载到: $LOCAL_BACKUP_DIR"
ls -lh $LOCAL_BACKUP_DIR/*.gz 2>/dev/null
