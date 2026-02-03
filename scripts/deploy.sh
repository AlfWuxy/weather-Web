#!/bin/bash
# 部署脚本 - 将项目部署到远程服务器

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
        spawn rsync -avz --exclude '__pycache__' --exclude '*.pyc' --exclude 'instance' --exclude 'storage' --exclude 'health_weather.db' --exclude '.git' --exclude 'venv' --exclude '.venv' --exclude '.venv2' --exclude '.env' --exclude '.env.local' -e ssh $LOCAL_DIR/ $USER@$SERVER:$PROJECT_DIR/
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

    rsync -avz --exclude '__pycache__' --exclude '*.pyc' --exclude 'instance' --exclude 'storage' --exclude 'health_weather.db' --exclude '.git' --exclude 'venv' --exclude '.venv' --exclude '.venv2' --exclude '.env' --exclude '.env.local' -e ssh "$LOCAL_DIR/" "$USER@$SERVER:$PROJECT_DIR/"
}

echo "步骤1: 测试服务器连接..."
remote_exec "echo '连接成功'"

echo ""
echo "步骤2: 安装系统依赖..."
remote_exec "apt-get update && apt-get install -y python3 python3-pip python3-venv rsync"

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
remote_exec "if [ ! -f $PROJECT_DIR/.env ]; then SECRET_KEY_GEN=\$(python3 -c 'import secrets; print(secrets.token_hex(32))'); cat > $PROJECT_DIR/.env << EOF
FLASK_ENV=production
SECRET_KEY=\$SECRET_KEY_GEN
QWEATHER_KEY=
AMAP_KEY=
EOF
echo '已创建新的 .env 文件'; else echo '.env 文件已存在，跳过创建'; fi"

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
