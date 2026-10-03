#!/bin/bash
# 部署与同步共用传输边界；不执行配置文件中的 shell 代码。
set -euo pipefail
umask 077

load_deploy_settings() {
    [ ! -e "$ENV_FILE" ] || python3 "$SCRIPT_DIR/secure_environment.py" protect "$ENV_FILE"
    [ -f "$ENV_FILE" ] || return 0
    local line key value
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|\#*) continue ;; esac
        [[ "$line" == *=* ]] || continue
        key="${line%%=*}"; value="${line#*=}"
        case "$key" in
            DEPLOY_SERVER|DEPLOY_USER|DEPLOY_PROJECT_DIR|DEPLOY_LOCAL_DIR|DEPLOY_APP_USER|DEPLOY_TLS_CERT_FILE|DEPLOY_TLS_KEY_FILE|DEPLOY_ORIGIN_ADDRESS|CW_SSH_KNOWN_HOSTS|PUBLIC_BASE_URL)
                # 部署定位不允许空白或 shell 元字符；密钥由独立解析器处理。
                value="${value%% #*}"
                if [[ "$value" == \"*\" ]] || [[ "$value" == \'*\' ]]; then value="${value:1:${#value}-2}"; fi
                if [ -z "${!key:-}" ]; then export "$key=$value"; fi
                ;;
        esac
    done < "$ENV_FILE"
}

initialize_deploy_security() {
    load_deploy_settings
    SERVER="${DEPLOY_SERVER:?必须设置 DEPLOY_SERVER}"
    DEPLOY_LOGIN="${DEPLOY_USER:?必须设置 DEPLOY_USER}"
    PROJECT_DIR="${DEPLOY_PROJECT_DIR:?必须设置 DEPLOY_PROJECT_DIR}"
    LOCAL_DIR="${DEPLOY_LOCAL_DIR:-$ROOT_DIR}"
    APP_USER="${DEPLOY_APP_USER:-case-weather}"
    PUBLIC_BASE_URL="${PUBLIC_BASE_URL:?必须配置已准备证书的 HTTPS PUBLIC_BASE_URL}"
    DEPLOY_ORIGIN_ADDRESS="${DEPLOY_ORIGIN_ADDRESS:?必须显式设置可从部署机检查的公网源站地址}"
    DEPLOY_TLS_CERT_FILE="${DEPLOY_TLS_CERT_FILE:?必须预置 TLS 证书文件}"
    DEPLOY_TLS_KEY_FILE="${DEPLOY_TLS_KEY_FILE:?必须预置 TLS 私钥文件}"
    [[ "$SERVER" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:-]*$ ]] || { echo 'SSH 目标无效' >&2; return 1; }
    [[ "$DEPLOY_LOGIN" =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo '部署账号无效' >&2; return 1; }
    [[ "$APP_USER" =~ ^[a-z_][a-z0-9_-]*$ && "$APP_USER" != root ]] || { echo '应用必须使用专用非 root 账号' >&2; return 1; }
    local path
    for path in "$PROJECT_DIR" "$DEPLOY_TLS_CERT_FILE" "$DEPLOY_TLS_KEY_FILE"; do
        [[ "$path" =~ ^/[a-zA-Z0-9_./-]+$ && "$path" != *'..'* && "$path" != *'//'* ]] || { echo '远端路径必须为无空白的绝对路径' >&2; return 1; }
    done
    [[ "$PROJECT_DIR" == /*/* && "$PROJECT_DIR" != /etc/* && "$PROJECT_DIR" != /usr/* && "$PROJECT_DIR" != /root/* ]] || { echo '项目目录不允许覆盖系统目录' >&2; return 1; }
    python3 "$SCRIPT_DIR/deployment_security.py" validate-url "$PUBLIC_BASE_URL"
    # 禁止旧的任意 SSH 参数、密码回退与自动接受新主机密钥路径。
    if [ -n "${SSH_OPTS:-}${DEFAULT_SSH_OPTS:-}${DEPLOY_PASSWORD:-}${SSHPASS:-}" ]; then
        echo '部署只支持预置主机密钥的 SSH Key；不接受 SSH_OPTS 或密码覆盖' >&2
        return 1
    fi
    KNOWN_HOSTS="${CW_SSH_KNOWN_HOSTS:-$HOME/.ssh/known_hosts}"
    python3 "$SCRIPT_DIR/deployment_security.py" known-hosts "$KNOWN_HOSTS"
    SSH_ARGS=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KNOWN_HOSTS"
        -o GlobalKnownHostsFile=/dev/null -o UpdateHostKeys=no -o VerifyHostKeyDNS=no
        -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no
        -o ForwardAgent=no -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3)
    printf -v SSH_RSYNC '%q ' ssh "${SSH_ARGS[@]}"
    export PUBLIC_BASE_URL
}

remote_exec() { ssh "${SSH_ARGS[@]}" "$DEPLOY_LOGIN@$SERVER" "$1"; }

upload_files() {
    rsync -avz --safe-links --exclude '__pycache__' --exclude '*.pyc' \
        --exclude 'instance' --exclude 'storage' --exclude 'logs' --exclude 'health_weather.db' \
        --exclude 'data/research/*.xlsx' --exclude 'data/research/*.xls' \
        --exclude '.git' --exclude '.claude' --exclude '.superpowers' --exclude '.pytest_cache' \
        --exclude '.playwright-cli' --exclude '.vscode' --exclude '.DS_Store' \
        --exclude 'venv' --exclude '.venv*' --exclude '.env*' --exclude 'backups' \
        --exclude 'tmp' --exclude 'output' --exclude 'blueprints/tools 2.py' --exclude=/analysis/ \
        -e "$SSH_RSYNC" "$LOCAL_DIR/" "$DEPLOY_LOGIN@$SERVER:$PROJECT_DIR/"
}

verify_deployment_boundary() {
    # systemd进入active不代表冷启动已完成；在明确上限内等待本机健康端点。
    remote_exec "for attempt in \$(seq 1 20); do if curl --fail --silent --max-time 2 http://127.0.0.1:5000/healthz >/dev/null; then exit 0; fi; sleep 1; done; exit 1"
    remote_exec "python3 '$PROJECT_DIR/scripts/deployment_security.py' verify-origin '$PROJECT_DIR' '$APP_USER' '$PUBLIC_BASE_URL' '$DEPLOY_TLS_CERT_FILE' '$DEPLOY_TLS_KEY_FILE'"
    python3 "$SCRIPT_DIR/deployment_security.py" verify-public "$PUBLIC_BASE_URL" "$DEPLOY_ORIGIN_ADDRESS"
}
