#!/bin/bash
# 部署脚本 - 将项目部署到远程服务器
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/dotenv.sh
source "$SCRIPT_DIR/dotenv.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
WECHAT_RELEASE_FORM_FILE="${WECHAT_RELEASE_FORM_FILE:-$ROOT_DIR/.env.wechat-release}"

# SSH 默认选项：
# - 只连接已经人工核对并登记到 known_hosts 的服务器
# - 禁用 ssh-agent（部分环境下会导致 banner exchange 卡住）
# - 启用连接复用，减少短时间内频繁建连触发服务器 sshd 惩罚/限流
DEFAULT_SSH_OPTS="${DEFAULT_SSH_OPTS:--o StrictHostKeyChecking=yes -o IdentityAgent=none -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ControlMaster=auto -o ControlPersist=300 -o ControlPath=/tmp/cw-ssh-%C}"
SSH_OPTS="${SSH_OPTS:-$DEFAULT_SSH_OPTS}"

LOCAL_QWEATHER_KEY=""
LOCAL_QWEATHER_API_BASE=""
LOCAL_QWEATHER_AUTH_MODE=""
LOCAL_QWEATHER_JWT_KID=""
LOCAL_QWEATHER_JWT_PROJECT_ID=""
LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH=""
LOCAL_ALLOW_WEATHER_UNAVAILABLE="${ALLOW_WEATHER_UNAVAILABLE:-}"
LOCAL_AMAP_KEY=""
LOCAL_WXPUSHER_APP_TOKEN=""
LOCAL_PUBLIC_BASE_URL=""
LOCAL_ALLOW_INSECURE_PUBLIC_BASE_URL="${ALLOW_INSECURE_PUBLIC_BASE_URL:-}"
LOCAL_WX_MINIPROGRAM_APPID=""
LOCAL_WX_MINIPROGRAM_SECRET=""
LOCAL_WX_MINIPROGRAM_OPENID_PEPPER=""
LOCAL_WX_MINIPROGRAM_SESSION_SECRET=""
LOCAL_WX_MINIPROGRAM_PRIVACY_VERSION=""
LOCAL_WECHAT_FORM_READY="0"

load_deploy_env() {
    [ -f "$ENV_FILE" ] || return 0
    while IFS='=' read -r key value; do
        case "$key" in
            ''|\#*) continue ;;
            DEPLOY_SERVER|DEPLOY_USER|DEPLOY_PASSWORD|DEPLOY_PROJECT_DIR|DEPLOY_LOCAL_DIR|DEPLOY_RELEASE_ROOT|DEPLOY_RELEASE_ID|DEPLOY_REQUIRE_WECHAT_READY|DEPLOY_RECOVERY_ACKNOWLEDGED_TRANSACTION|WECHAT_RELEASE_FORM_FILE|SSHPASS)
                normalize_env_value "$value"
                value="$NORMALIZED_ENV_VALUE"
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
            QWEATHER_KEY|QWEATHER_API_BASE|QWEATHER_AUTH_MODE|QWEATHER_JWT_KID|QWEATHER_JWT_PROJECT_ID|QWEATHER_JWT_PRIVATE_KEY_PATH|ALLOW_WEATHER_UNAVAILABLE|AMAP_KEY|WXPUSHER_APP_TOKEN|PUBLIC_BASE_URL|ALLOW_INSECURE_PUBLIC_BASE_URL|WX_MINIPROGRAM_APPID|WX_MINIPROGRAM_SECRET|WX_MINIPROGRAM_OPENID_PEPPER|WX_MINIPROGRAM_SESSION_SECRET|WX_MINIPROGRAM_PRIVACY_VERSION)
                normalize_env_value "$value"
                value="$NORMALIZED_ENV_VALUE"
                case "$key" in
                    QWEATHER_KEY) LOCAL_QWEATHER_KEY="$value" ;;
                    QWEATHER_API_BASE) LOCAL_QWEATHER_API_BASE="$value" ;;
                    QWEATHER_AUTH_MODE) LOCAL_QWEATHER_AUTH_MODE="$value" ;;
                    QWEATHER_JWT_KID) LOCAL_QWEATHER_JWT_KID="$value" ;;
                    QWEATHER_JWT_PROJECT_ID) LOCAL_QWEATHER_JWT_PROJECT_ID="$value" ;;
                    QWEATHER_JWT_PRIVATE_KEY_PATH) LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH="$value" ;;
                    ALLOW_WEATHER_UNAVAILABLE) LOCAL_ALLOW_WEATHER_UNAVAILABLE="$value" ;;
                    AMAP_KEY) LOCAL_AMAP_KEY="$value" ;;
                    WXPUSHER_APP_TOKEN) LOCAL_WXPUSHER_APP_TOKEN="$value" ;;
                    PUBLIC_BASE_URL) LOCAL_PUBLIC_BASE_URL="$value" ;;
                    ALLOW_INSECURE_PUBLIC_BASE_URL) LOCAL_ALLOW_INSECURE_PUBLIC_BASE_URL="$value" ;;
                    WX_MINIPROGRAM_APPID) LOCAL_WX_MINIPROGRAM_APPID="$value" ;;
                    WX_MINIPROGRAM_SECRET) LOCAL_WX_MINIPROGRAM_SECRET="$value" ;;
                    WX_MINIPROGRAM_OPENID_PEPPER) LOCAL_WX_MINIPROGRAM_OPENID_PEPPER="$value" ;;
                    WX_MINIPROGRAM_SESSION_SECRET) LOCAL_WX_MINIPROGRAM_SESSION_SECRET="$value" ;;
                    WX_MINIPROGRAM_PRIVACY_VERSION) LOCAL_WX_MINIPROGRAM_PRIVACY_VERSION="$value" ;;
                esac
                ;;
        esac
    done < "$ENV_FILE"
}

load_local_api_keys

load_wechat_release_form() {
    [ -f "$WECHAT_RELEASE_FORM_FILE" ] || return 0
    while IFS='=' read -r key value; do
        case "$key" in
            ''|\#*) continue ;;
            WECHAT_FORM_READY|WX_MINIPROGRAM_APPID|WX_MINIPROGRAM_SECRET|WX_MINIPROGRAM_PRIVACY_VERSION)
                normalize_env_value "$value"
                value="$NORMALIZED_ENV_VALUE"
                case "$key" in
                    WECHAT_FORM_READY) LOCAL_WECHAT_FORM_READY="$value" ;;
                    WX_MINIPROGRAM_APPID) LOCAL_WX_MINIPROGRAM_APPID="$value" ;;
                    WX_MINIPROGRAM_SECRET) LOCAL_WX_MINIPROGRAM_SECRET="$value" ;;
                    WX_MINIPROGRAM_PRIVACY_VERSION) LOCAL_WX_MINIPROGRAM_PRIVACY_VERSION="$value" ;;
                esac
                ;;
        esac
    done < "$WECHAT_RELEASE_FORM_FILE"
}

SERVER="${DEPLOY_SERVER:-}"
USER="${DEPLOY_USER:-}"
PROJECT_DIR="${DEPLOY_PROJECT_DIR:-/opt/your-app}"
LOCAL_DIR="${DEPLOY_LOCAL_DIR:-$ROOT_DIR}"
PASSWORD="${DEPLOY_PASSWORD:-${SSHPASS:-}}"
RELEASE_ROOT="${DEPLOY_RELEASE_ROOT:-${PROJECT_DIR}-deploy}"
RELEASE_ID="${DEPLOY_RELEASE_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
CURRENT_LINK="$RELEASE_ROOT/current"
NEW_RELEASE="$RELEASE_ROOT/releases/$RELEASE_ID"
RELEASE_APP="$NEW_RELEASE/app"
RELEASE_VENV="$NEW_RELEASE/venv"
STAGED_ENV_FILE="$NEW_RELEASE/staged.env"
REQUIRE_WECHAT_READY="${DEPLOY_REQUIRE_WECHAT_READY:-0}"
RECOVERY_ACKNOWLEDGED_TRANSACTION="${DEPLOY_RECOVERY_ACKNOWLEDGED_TRANSACTION:-}"

if [ "$REQUIRE_WECHAT_READY" = "1" ] || [ -f "$WECHAT_RELEASE_FORM_FILE" ]; then
    python3 "$SCRIPT_DIR/validate_release_env.py" \
        --wechat-form "$WECHAT_RELEASE_FORM_FILE" \
        --form-only \
        --require-wechat "$REQUIRE_WECHAT_READY"
fi
load_wechat_release_form
if [ "$REQUIRE_WECHAT_READY" = "1" ] && [ "$LOCAL_WECHAT_FORM_READY" != "1" ]; then
    echo "微信正式发布私密表单尚未标记完成。" >&2
    exit 64
fi

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

case "$REQUIRE_WECHAT_READY" in
    0|1) ;;
    *) echo "DEPLOY_REQUIRE_WECHAT_READY 只能是 0 或 1。" >&2; exit 64 ;;
esac

case "${LOCAL_ALLOW_WEATHER_UNAVAILABLE:-}" in
    ''|0|1) ;;
    *) echo "ALLOW_WEATHER_UNAVAILABLE 只能是 0 或 1。" >&2; exit 64 ;;
esac

if [ -n "$LOCAL_QWEATHER_AUTH_MODE" ]; then
    case "$LOCAL_QWEATHER_AUTH_MODE" in
        disabled) ;;
        api_key)
            [ -n "$LOCAL_QWEATHER_KEY" ] && [ -n "$LOCAL_QWEATHER_API_BASE" ] || {
                echo "QWEATHER_AUTH_MODE=api_key 时必须同时提供 Key 与 API Base。" >&2
                exit 64
            }
            ;;
        jwt)
            [ -n "$LOCAL_QWEATHER_API_BASE" ] \
                && [ -n "$LOCAL_QWEATHER_JWT_KID" ] \
                && [ -n "$LOCAL_QWEATHER_JWT_PROJECT_ID" ] \
                && [ -n "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH" ] || {
                    echo "QWEATHER_AUTH_MODE=jwt 时必须完整提供 API Base 与三项 JWT 参数。" >&2
                    exit 64
                }
            ;;
        *) echo "QWEATHER_AUTH_MODE 只能是 disabled、api_key 或 jwt。" >&2; exit 64 ;;
    esac
elif [ -n "$LOCAL_QWEATHER_KEY" ] || [ -n "$LOCAL_QWEATHER_API_BASE" ] || [ -n "$LOCAL_QWEATHER_JWT_KID" ] || [ -n "$LOCAL_QWEATHER_JWT_PROJECT_ID" ] || [ -n "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH" ]; then
    echo "检测到和风天气配置，请同时显式设置 QWEATHER_AUTH_MODE，避免静默启用或停用天气同步。" >&2
    exit 64
fi

validate_remote_path() {
    local name="$1"
    local value="$2"
    if [[ "$value" != /* || "$value" = "/" || ! "$value" =~ ^[A-Za-z0-9._/-]+$ ]]; then
        echo "$name 必须是安全的绝对路径: $value" >&2
        exit 1
    fi
}

validate_remote_path "DEPLOY_PROJECT_DIR" "$PROJECT_DIR"
validate_remote_path "DEPLOY_RELEASE_ROOT" "$RELEASE_ROOT"
if [ -n "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ]; then
    validate_remote_path "DEPLOY_RECOVERY_ACKNOWLEDGED_TRANSACTION" "$RECOVERY_ACKNOWLEDGED_TRANSACTION"
    case "$RECOVERY_ACKNOWLEDGED_TRANSACTION" in
        "$PROJECT_DIR"/backups/deploy-transactions/*) ;;
        *)
            echo "DEPLOY_RECOVERY_ACKNOWLEDGED_TRANSACTION 必须指向本项目的部署事务目录。" >&2
            exit 64
            ;;
    esac
fi
if [[ ! "$RELEASE_ID" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "DEPLOY_RELEASE_ID 只能包含字母、数字、点、下划线和短横线。" >&2
    exit 1
fi

if [ -z "${SSHPASS:-}" ] && [ -n "$PASSWORD" ]; then
    export SSHPASS="$PASSWORD"
fi

use_sshpass() {
    command -v sshpass >/dev/null 2>&1
}

echo "=== 开始部署 case-weather 项目 ==="

remote_exec() {
    if use_sshpass && [ -n "${SSHPASS:-}" ]; then
        SSHPASS="${SSHPASS:-$PASSWORD}" sshpass -e ssh $SSH_OPTS "$USER@$SERVER" "$1"
        return
    fi

    if [ -n "${SSHPASS:-}" ]; then
        echo "密码部署需要 sshpass；也可以清空 DEPLOY_PASSWORD 后使用 SSH Key。" >&2
        return 64
    fi

    ssh $SSH_OPTS "$USER@$SERVER" "$1"
}

# 通过标准输入传递敏感值，避免密钥出现在 ssh 命令参数和进程列表中。
remote_exec_with_stdin() {
    local payload="$1"
    local remote_command="$2"

    if use_sshpass && [ -n "${SSHPASS:-}" ]; then
        printf '%s' "$payload" | SSHPASS="${SSHPASS:-$PASSWORD}" sshpass -e ssh $SSH_OPTS "$USER@$SERVER" "$remote_command"
        return
    fi

    if [ -n "${SSHPASS:-}" ]; then
        echo "安全传输密钥需要 sshpass；也可以清空 DEPLOY_PASSWORD 后使用 SSH Key。" >&2
        return 64
    fi

    printf '%s' "$payload" | ssh $SSH_OPTS "$USER@$SERVER" "$remote_command"
}

remote_env_update() {
    local key="$1"
    local value="$2"
    local mode="$3"
    case "$key" in
        [A-Z]*) ;;
        *) echo "环境变量名不合法: $key" >&2; return 64 ;;
    esac
    case "$key" in
        *[!A-Z0-9_]*) echo "环境变量名不合法: $key" >&2; return 64 ;;
    esac
    case "$mode" in
        always|if-empty) ;;
        *) echo "环境变量更新模式不合法: $mode" >&2; return 64 ;;
    esac
    remote_exec_with_stdin "$value" "flock $RELEASE_ROOT/deploy-env.lock python3 $RELEASE_APP/scripts/update_env_value.py --file $STAGED_ENV_FILE --key $key --mode $mode"
}

# 在服务器内生成随机值，密钥从不经过本机日志、SSH 参数或远程进程参数。
remote_env_generate_secret() {
    local key="$1"
    case "$key" in
        WX_MINIPROGRAM_OPENID_PEPPER|WX_MINIPROGRAM_SESSION_SECRET) ;;
        *) echo "不允许自动生成该环境变量: $key" >&2; return 64 ;;
    esac
    remote_exec "umask 077; python3 -c 'import secrets; print(secrets.token_hex(32), end=\"\")' | flock $RELEASE_ROOT/deploy-env.lock python3 $RELEASE_APP/scripts/update_env_value.py --file $STAGED_ENV_FILE --key $key --mode if-empty"
}

check_remote_unit_active() {
    local unit="$1"
    remote_exec "systemctl is-active --quiet $unit"
    echo "已确认 systemd 单元运行中: $unit"
}

# 使用 rsync/scp 上传文件的函数
upload_files() {
    local remote_target="$1"
    if use_sshpass && [ -n "${SSHPASS:-}" ]; then
        SSHPASS="${SSHPASS:-$PASSWORD}" sshpass -e rsync -avz \
            --exclude '__pycache__' \
            --exclude '*.pyc' \
            --exclude 'instance' \
            --exclude 'storage' \
            --exclude 'health_weather.db' \
            --exclude 'data/research/*.xlsx' \
            --exclude 'data/research/*.xls' \
            --exclude '.git' \
            --exclude '.claude' \
            --exclude 'venv' \
            --exclude '.venv' \
            --exclude '.venv2' \
            --exclude '.env*' \
            --exclude 'project.private.config.json' \
            --exclude '.superpowers' \
            --exclude '.pytest_cache' \
            --exclude '.playwright-cli' \
            --exclude '.vscode' \
            --exclude '.DS_Store' \
            --exclude 'backups' \
            --exclude 'tmp' \
            --exclude 'output' \
            --exclude 'blueprints/tools 2.py' \
            -e "ssh $SSH_OPTS" "$LOCAL_DIR/" "$USER@$SERVER:$remote_target/"
        return
    fi

    if [ -n "${SSHPASS:-}" ]; then
        echo "密码上传需要 sshpass；也可以清空 DEPLOY_PASSWORD 后使用 SSH Key。" >&2
        return 64
    fi

    rsync -avz --exclude '__pycache__' --exclude '*.pyc' --exclude 'instance' --exclude 'storage' --exclude 'health_weather.db' --exclude 'data/research/*.xlsx' --exclude 'data/research/*.xls' --exclude '.git' --exclude '.claude' --exclude 'venv' --exclude '.venv' --exclude '.venv2' --exclude '.env*' --exclude 'project.private.config.json' --exclude '.superpowers' --exclude '.pytest_cache' --exclude '.playwright-cli' --exclude '.vscode' --exclude '.DS_Store' --exclude 'backups' --exclude 'tmp' --exclude 'output' --exclude 'blueprints/tools 2.py' -e "ssh $SSH_OPTS" "$LOCAL_DIR/" "$USER@$SERVER:$remote_target/"
}

echo "步骤1: 测试服务器连接..."
remote_exec "echo '连接成功'"

echo ""
echo "步骤2: 检查服务器依赖（常规发布不修改全局软件）..."
remote_exec "for REQUIRED_COMMAND in python3 rsync sqlite3 curl flock systemctl busctl; do command -v \"\$REQUIRED_COMMAND\" >/dev/null || { echo \"缺少服务器依赖: \$REQUIRED_COMMAND，请先执行一次性服务器初始化。\" >&2; exit 1; }; done"

echo ""
echo "步骤2.1: 检查 Redis（用于生产环境限流存储）..."
remote_exec "systemctl is-active --quiet redis-server || { echo 'redis-server 未运行，请先完成一次性服务器初始化。' >&2; exit 1; }"

echo ""
echo "步骤2.2: 检查 systemd 的成功链路能力..."
remote_exec "SYSTEMD_VERSION=\$(systemd --version | awk 'NR == 1 {print \$2}'); if [ \"\$SYSTEMD_VERSION\" -lt 249 ]; then echo 'systemd 版本过低，无法安全使用 OnSuccess 推送链路。' >&2; exit 1; fi"

echo ""
echo "步骤3: 创建不可变发布目录并上传代码..."
remote_exec "mkdir -p $PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/backups $RELEASE_ROOT/releases && chmod 0700 $PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/backups"
remote_exec "if [ -e $NEW_RELEASE ]; then echo '发布 ID 已存在，拒绝覆盖不可变版本: $NEW_RELEASE' >&2; exit 1; fi; mkdir -p $RELEASE_APP $NEW_RELEASE/systemd"
upload_files "$RELEASE_APP"
remote_exec "ln -s $PROJECT_DIR/instance $RELEASE_APP/instance && ln -s $PROJECT_DIR/storage $RELEASE_APP/storage && ln -s $PROJECT_DIR/backups $RELEASE_APP/backups"

echo ""
echo "步骤4: 准备隔离的候选环境配置..."
# 首次部署尚无生产进程，可以安全创建初始配置。已有配置只复制到候选文件。
remote_exec "umask 077; if [ ! -f $PROJECT_DIR/.env ]; then SECRET_KEY_GEN=\$(python3 -c 'import secrets; print(secrets.token_hex(32))'); PAIR_TOKEN_PEPPER_GEN=\$(python3 -c 'import secrets; print(secrets.token_hex(32))'); WX_OPENID_PEPPER_GEN=\$(python3 -c 'import secrets; print(secrets.token_hex(32))'); WX_SESSION_SECRET_GEN=\$(python3 -c 'import secrets; print(secrets.token_hex(32))'); cat > $PROJECT_DIR/.env << EOF
FLASK_ENV=production
DEBUG=false
SECRET_KEY=\$SECRET_KEY_GEN
PAIR_TOKEN_PEPPER=\$PAIR_TOKEN_PEPPER_GEN
DATABASE_URI=sqlite:///health_weather.db
REDIS_URL=redis://127.0.0.1:6379/0
RATE_LIMIT_STORAGE_URI=redis://127.0.0.1:6379/0
QWEATHER_AUTH_MODE=disabled
QWEATHER_KEY=
QWEATHER_API_BASE=
QWEATHER_JWT_KID=
QWEATHER_JWT_PROJECT_ID=
QWEATHER_JWT_PRIVATE_KEY_PATH=
QWEATHER_CANONICAL_LOCATION=116.20,29.27
QWEATHER_MONTHLY_REQUEST_LIMIT=40000
QWEATHER_BUDGET_FAIL_CLOSED=1
ALLOW_WEATHER_UNAVAILABLE=
WEATHER_CACHE_TTL_MINUTES=30
FORECAST_CACHE_TTL_MINUTES=30
QWEATHER_WARNING_CACHE_TTL_MINUTES=30
WEATHER_SYNC_LOCATIONS=都昌县
AMAP_KEY=
WXPUSHER_APP_TOKEN=
WXPUSHER_API_BASE=https://wxpusher.zjiecode.com/api
WX_MINIPROGRAM_APPID=
WX_MINIPROGRAM_SECRET=
WX_MINIPROGRAM_OPENID_PEPPER=\$WX_OPENID_PEPPER_GEN
WX_MINIPROGRAM_SESSION_SECRET=\$WX_SESSION_SECRET_GEN
WX_MINIPROGRAM_PRIVACY_VERSION=2026-07-18
PUBLIC_BASE_URL=
ALLOW_INSECURE_PUBLIC_BASE_URL=
EOF
echo '已创建首次部署配置'; fi; cp -a $PROJECT_DIR/.env $STAGED_ENV_FILE; chmod 0600 $STAGED_ENV_FILE"

echo ""
echo "步骤4.1: 原子补齐候选配置..."
# 所有候选值均通过 stdin 写入；旧服务在激活事务前继续读取原配置。
remote_env_update "DATABASE_URI" "sqlite:///health_weather.db" "if-empty"
remote_env_update "QWEATHER_AUTH_MODE" "disabled" "if-empty"
remote_env_update "QWEATHER_CANONICAL_LOCATION" "116.20,29.27" "if-empty"
remote_env_update "QWEATHER_MONTHLY_REQUEST_LIMIT" "40000" "if-empty"
remote_env_update "QWEATHER_BUDGET_FAIL_CLOSED" "1" "if-empty"
remote_env_update "WEATHER_CACHE_TTL_MINUTES" "30" "if-empty"
remote_env_update "FORECAST_CACHE_TTL_MINUTES" "30" "if-empty"
remote_env_update "QWEATHER_WARNING_CACHE_TTL_MINUTES" "30" "if-empty"
remote_env_update "WEATHER_SYNC_LOCATIONS" "都昌县" "if-empty"
remote_env_update "WXPUSHER_API_BASE" "https://wxpusher.zjiecode.com/api" "if-empty"
remote_env_generate_secret "WX_MINIPROGRAM_OPENID_PEPPER"
remote_env_generate_secret "WX_MINIPROGRAM_SESSION_SECRET"
remote_env_update "WX_MINIPROGRAM_PRIVACY_VERSION" "2026-07-18" "if-empty"

echo ""
echo "步骤4.2: 安全写入显式提供的发布配置..."
# PUBLIC_BASE_URL 必须优先使用 HTTPS。HTTP/IP 只允许显式临时豁免。
if [ -n "$LOCAL_PUBLIC_BASE_URL" ]; then
    remote_env_update "PUBLIC_BASE_URL" "$LOCAL_PUBLIC_BASE_URL" "always"
else
    remote_env_update "PUBLIC_BASE_URL" "" "if-empty"
fi
if [ -n "${LOCAL_ALLOW_INSECURE_PUBLIC_BASE_URL:-}" ]; then
    remote_env_update "ALLOW_INSECURE_PUBLIC_BASE_URL" "$LOCAL_ALLOW_INSECURE_PUBLIC_BASE_URL" "always"
else
    remote_env_update "ALLOW_INSECURE_PUBLIC_BASE_URL" "" "if-empty"
fi

if [ -n "$LOCAL_QWEATHER_AUTH_MODE" ]; then
    remote_env_update "QWEATHER_AUTH_MODE" "$LOCAL_QWEATHER_AUTH_MODE" "always"
    case "$LOCAL_QWEATHER_AUTH_MODE" in
        disabled)
            remote_env_update "QWEATHER_KEY" "" "always"
            remote_env_update "QWEATHER_API_BASE" "" "always"
            remote_env_update "QWEATHER_JWT_KID" "" "always"
            remote_env_update "QWEATHER_JWT_PROJECT_ID" "" "always"
            remote_env_update "QWEATHER_JWT_PRIVATE_KEY_PATH" "" "always"
            ;;
        api_key)
            remote_env_update "QWEATHER_KEY" "$LOCAL_QWEATHER_KEY" "always"
            remote_env_update "QWEATHER_API_BASE" "$LOCAL_QWEATHER_API_BASE" "always"
            remote_env_update "QWEATHER_JWT_KID" "" "always"
            remote_env_update "QWEATHER_JWT_PROJECT_ID" "" "always"
            remote_env_update "QWEATHER_JWT_PRIVATE_KEY_PATH" "" "always"
            ;;
        jwt)
            remote_env_update "QWEATHER_KEY" "" "always"
            remote_env_update "QWEATHER_API_BASE" "$LOCAL_QWEATHER_API_BASE" "always"
            remote_env_update "QWEATHER_JWT_KID" "$LOCAL_QWEATHER_JWT_KID" "always"
            remote_env_update "QWEATHER_JWT_PROJECT_ID" "$LOCAL_QWEATHER_JWT_PROJECT_ID" "always"
            remote_env_update "QWEATHER_JWT_PRIVATE_KEY_PATH" "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH" "always"
            ;;
    esac
fi
if [ -n "${LOCAL_ALLOW_WEATHER_UNAVAILABLE:-}" ]; then
    remote_env_update "ALLOW_WEATHER_UNAVAILABLE" "$LOCAL_ALLOW_WEATHER_UNAVAILABLE" "always"
else
    remote_env_update "ALLOW_WEATHER_UNAVAILABLE" "" "if-empty"
fi
if [ -n "$LOCAL_AMAP_KEY" ]; then
    remote_env_update "AMAP_KEY" "$LOCAL_AMAP_KEY" "always"
fi
if [ -n "$LOCAL_WXPUSHER_APP_TOKEN" ]; then
    remote_env_update "WXPUSHER_APP_TOKEN" "$LOCAL_WXPUSHER_APP_TOKEN" "always"
fi
if [ -n "$LOCAL_WX_MINIPROGRAM_APPID" ]; then
    remote_env_update "WX_MINIPROGRAM_APPID" "$LOCAL_WX_MINIPROGRAM_APPID" "always"
fi
if [ -n "$LOCAL_WX_MINIPROGRAM_SECRET" ]; then
    remote_env_update "WX_MINIPROGRAM_SECRET" "$LOCAL_WX_MINIPROGRAM_SECRET" "always"
fi
if [ -n "$LOCAL_WX_MINIPROGRAM_OPENID_PEPPER" ]; then
    remote_env_update "WX_MINIPROGRAM_OPENID_PEPPER" "$LOCAL_WX_MINIPROGRAM_OPENID_PEPPER" "if-empty"
fi
if [ -n "$LOCAL_WX_MINIPROGRAM_SESSION_SECRET" ]; then
    remote_env_update "WX_MINIPROGRAM_SESSION_SECRET" "$LOCAL_WX_MINIPROGRAM_SESSION_SECRET" "if-empty"
fi
if [ -n "$LOCAL_WX_MINIPROGRAM_PRIVACY_VERSION" ]; then
    remote_env_update "WX_MINIPROGRAM_PRIVACY_VERSION" "$LOCAL_WX_MINIPROGRAM_PRIVACY_VERSION" "always"
fi
remote_exec "python3 $RELEASE_APP/scripts/validate_release_env.py --file $STAGED_ENV_FILE --require-wechat $REQUIRE_WECHAT_READY"

echo ""
echo "步骤6: 为新版本创建独立虚拟环境..."
remote_exec "python3 -m venv $RELEASE_VENV && $RELEASE_VENV/bin/pip install --upgrade pip && $RELEASE_VENV/bin/pip install -r $RELEASE_APP/requirements.txt && $RELEASE_VENV/bin/pip install gunicorn"

echo ""
echo "步骤6.1: 在停止生产服务前完成隔离测试..."
remote_exec "cd $RELEASE_APP && DATABASE_URI=sqlite:///:memory: DEBUG=true SECRET_KEY=release-preflight-secret-key-123456789 PAIR_TOKEN_PEPPER=release-preflight-pair-pepper-123456789 RATE_LIMIT_STORAGE_URI=memory:// REDIS_URL= QWEATHER_AUTH_MODE=disabled QWEATHER_KEY= QWEATHER_API_BASE= AMAP_KEY= AMAP_WEB_SERVICE_KEY= AMAP_SECURITY_JS_CODE= SILICONFLOW_API_KEY= WXPUSHER_APP_TOKEN= WX_MINIPROGRAM_APPID= WX_MINIPROGRAM_SECRET= WX_MINIPROGRAM_OPENID_PEPPER= WX_MINIPROGRAM_SESSION_SECRET= DEMO_MODE=1 $RELEASE_VENV/bin/python -m pytest -q"
remote_exec "ln -s $PROJECT_DIR/.env $RELEASE_APP/.env"

echo ""
echo "步骤6.2: 为新版本生成 systemd 单元模板..."
remote_exec "cat > $NEW_RELEASE/systemd/case-weather.service << 'EOF'
[Unit]
Description=Case Weather Flask Application
After=network.target

[Service]
User=root
UMask=0077
WorkingDirectory=$CURRENT_LINK/app
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$CURRENT_LINK/venv/bin/gunicorn --workers 3 --bind 127.0.0.1:5000 --timeout 120 app:app
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF"

remote_exec "cat > $NEW_RELEASE/systemd/case-weather-cache.service << 'EOF'
[Unit]
Description=Case Weather - refresh Duchang weather cache
After=network.target case-weather.service
OnSuccess=case-weather-dispatch.service

[Service]
Type=oneshot
User=root
UMask=0077
WorkingDirectory=$CURRENT_LINK/app
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
Environment=VENV_PY=$CURRENT_LINK/venv/bin/python
ExecStart=/bin/bash $CURRENT_LINK/app/scripts/weather_cache_sync.sh
TimeoutStartSec=15min
EOF

cat > $NEW_RELEASE/systemd/case-weather-cache.timer << 'EOF'
[Unit]
Description=Case Weather - refresh Duchang weather cache every 30 minutes

[Timer]
OnActiveSec=30min
OnUnitActiveSec=30min
AccuracySec=1s
Unit=case-weather-cache.service

[Install]
WantedBy=timers.target
EOF"

remote_exec "cat > $NEW_RELEASE/systemd/case-weather-cache-bootstrap.service << 'EOF'
[Unit]
Description=Case Weather - start the first cache refresh after a full 30-minute delay
Wants=case-weather-cache.service
After=network.target case-weather.service case-weather-cache.service
OnSuccess=case-weather-cache.timer

[Service]
Type=oneshot
User=root
UMask=0077
# 真实同步由 Wants 拉起；本单元在同步结束后成功退出，再启动常规定时器。
ExecStart=/usr/bin/true
TimeoutStartSec=16min
EOF

cat > $NEW_RELEASE/systemd/case-weather-cache-bootstrap.timer << 'EOF'
[Unit]
Description=Case Weather - delay the first cache refresh for 30 minutes

[Timer]
OnActiveSec=30min
AccuracySec=1s
RemainAfterElapse=no
Unit=case-weather-cache-bootstrap.service

[Install]
WantedBy=timers.target
EOF"

remote_exec "cat > $NEW_RELEASE/systemd/case-weather-dispatch.service << 'EOF'
[Unit]
Description=Case Weather - dispatch alerts (WxPusher)
After=network.target case-weather.service case-weather-cache.service

[Service]
Type=oneshot
User=root
UMask=0077
WorkingDirectory=$CURRENT_LINK/app
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
Environment=DEPLOY_STATE_DIR=$PROJECT_DIR
ExecStart=/bin/bash $CURRENT_LINK/app/scripts/dispatch_alerts.sh --dedupe-hours 6
TimeoutStartSec=15min
EOF"

remote_exec "cat > $NEW_RELEASE/systemd/case-weather-risk-precompute.service << 'EOF'
[Unit]
Description=Case Weather - precompute community risk cache
After=network.target case-weather.service

[Service]
Type=oneshot
User=root
UMask=0077
WorkingDirectory=$CURRENT_LINK/app
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
Environment=VENV_PY=$CURRENT_LINK/venv/bin/python
ExecStart=/bin/bash $CURRENT_LINK/app/scripts/community_risk_precompute.sh
EOF

cat > $NEW_RELEASE/systemd/case-weather-risk-precompute.timer << 'EOF'
[Unit]
Description=Case Weather - precompute community risk cache hourly

[Timer]
OnActiveSec=5min
OnUnitActiveSec=60min
Persistent=true
Unit=case-weather-risk-precompute.service

[Install]
WantedBy=timers.target
EOF"

remote_exec "cat > $NEW_RELEASE/systemd/case-weather-usage-cleanup.service << 'EOF'
[Unit]
Description=Case Weather - delete expired UsageEvent rows
StartLimitIntervalSec=1h
StartLimitBurst=20

[Service]
Type=oneshot
User=root
UMask=0077
WorkingDirectory=$CURRENT_LINK/app
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
Environment=VENV_PY=$CURRENT_LINK/venv/bin/python
Environment=DEPLOY_STATE_DIR=$PROJECT_DIR
ExecStart=/bin/bash $CURRENT_LINK/app/scripts/cleanup_usage_events.sh
Restart=on-failure
RestartSec=1min
EOF

cat > $NEW_RELEASE/systemd/case-weather-usage-cleanup.timer << 'EOF'
[Unit]
Description=Case Weather - delete expired UsageEvent rows daily

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true
Unit=case-weather-usage-cleanup.service

[Install]
WantedBy=timers.target
EOF"

echo ""
echo "步骤7: 在单个服务器事务中备份、迁移、切换并验活..."
remote_exec "STATE_DIR=$PROJECT_DIR RELEASE_ROOT=$RELEASE_ROOT NEW_RELEASE=$NEW_RELEASE CURRENT_LINK=$CURRENT_LINK ENV_FILE=$PROJECT_DIR/.env STAGED_ENV_FILE=$STAGED_ENV_FILE HEALTH_URL=http://127.0.0.1:5000/healthz RECOVERY_ACKNOWLEDGED_TRANSACTION=$RECOVERY_ACKNOWLEDGED_TRANSACTION bash $RELEASE_APP/scripts/activate_release.sh"

echo ""
echo "步骤8: 复核服务、定时器与当前版本..."
check_remote_unit_active "case-weather.service"
check_remote_unit_active "case-weather-cache-bootstrap.timer"
remote_exec "BOOTSTRAP_STATE=\$(systemctl is-enabled case-weather-cache-bootstrap.timer 2>/dev/null || true); test \"\$BOOTSTRAP_STATE\" = enabled || { echo \"bootstrap timer 状态应为 enabled，实际为 \${BOOTSTRAP_STATE:-unknown}。\" >&2; exit 1; }"
remote_exec "NEXT_US=\$(busctl get-property org.freedesktop.systemd1 /org/freedesktop/systemd1/unit/case_2dweather_2dcache_2dbootstrap_2etimer org.freedesktop.systemd1.Timer NextElapseUSecMonotonic | awk '{print \$2}'); UPTIME_US=\$(awk '{printf \"%.0f\", \$1 * 1000000}' /proc/uptime); REMAINING_US=\$((NEXT_US - UPTIME_US)); if [ \"\$REMAINING_US\" -lt 1700000000 ] || [ \"\$REMAINING_US\" -gt 1810000000 ]; then echo 'bootstrap timer 未保留完整的首轮 30 分钟等待窗口。' >&2; exit 1; fi"
remote_exec "systemctl cat case-weather-cache.timer >/dev/null; test \"\$(systemctl show case-weather-cache.timer --property=LoadState --value)\" = loaded"
remote_exec "RECURRING_STATE=\$(systemctl is-enabled case-weather-cache.timer 2>/dev/null || true); test \"\$RECURRING_STATE\" = disabled || { echo \"常规天气缓存 timer 状态应为 disabled，实际为 \${RECURRING_STATE:-unknown}。\" >&2; exit 1; }"
remote_exec "if systemctl is-active --quiet case-weather-cache.timer; then echo '常规天气缓存 timer 在首轮等待期间不应提前运行。' >&2; exit 1; fi"
check_remote_unit_active "case-weather-risk-precompute.timer"
check_remote_unit_active "case-weather-usage-cleanup.timer"
remote_exec "systemctl cat case-weather-dispatch.service >/dev/null"
remote_exec "systemctl show case-weather-cache.service --property=OnSuccess --value | grep -qw 'case-weather-dispatch.service'"
remote_exec "systemctl show case-weather-cache-bootstrap.service --property=OnSuccess --value | grep -qw 'case-weather-cache.timer'"
remote_exec "if systemctl is-active --quiet case-weather-dispatch.timer || systemctl cat case-weather-dispatch.timer >/dev/null 2>&1; then echo '旧 dispatch.timer 仍存在，拒绝完成发布。' >&2; exit 1; fi"
remote_exec "test \"\$(readlink $CURRENT_LINK)\" = '$NEW_RELEASE'"
remote_exec "test ! -e $STAGED_ENV_FILE"
remote_exec "curl --fail --silent --show-error --max-time 3 http://127.0.0.1:5000/healthz"

echo ""
echo "=== 部署完成 ==="
echo "发布版本: $RELEASE_ID"
echo "持久化目录: $PROJECT_DIR"
echo "当前版本入口: $CURRENT_LINK"
