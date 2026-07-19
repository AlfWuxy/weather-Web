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
LOCAL_FEATURE_AUDIT_LOGS=""
LOCAL_FEATURE_WXPUSHER=""
LOCAL_WXPUSHER_APP_TOKEN=""
LOCAL_FEATURE_HEAT_EXPOSURE_GIS=""
LOCAL_PUBLIC_BASE_URL=""
LOCAL_ALLOW_INSECURE_PUBLIC_BASE_URL="${ALLOW_INSECURE_PUBLIC_BASE_URL:-}"
LOCAL_WX_MINIPROGRAM_APPID=""
LOCAL_WX_MINIPROGRAM_SECRET=""
LOCAL_WX_MINIPROGRAM_PRIVACY_VERSION=""
LOCAL_WECHAT_FORMAL_RUNTIME=""
LOCAL_QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED=""
LOCAL_QWEATHER_CONSOLE_USAGE_MONTH=""
LOCAL_QWEATHER_CONSOLE_USAGE_BASELINE=""
LOCAL_QWEATHER_EXPECTED_PROJECT_ID=""
LOCAL_QWEATHER_EXPECTED_KID=""
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
            QWEATHER_KEY|QWEATHER_API_BASE|QWEATHER_AUTH_MODE|QWEATHER_JWT_KID|QWEATHER_JWT_PROJECT_ID|QWEATHER_JWT_PRIVATE_KEY_PATH|ALLOW_WEATHER_UNAVAILABLE|AMAP_KEY|FEATURE_AUDIT_LOGS|FEATURE_WXPUSHER|WXPUSHER_APP_TOKEN|FEATURE_HEAT_EXPOSURE_GIS|PUBLIC_BASE_URL|ALLOW_INSECURE_PUBLIC_BASE_URL)
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
                    FEATURE_AUDIT_LOGS) LOCAL_FEATURE_AUDIT_LOGS="$value" ;;
                    FEATURE_WXPUSHER) LOCAL_FEATURE_WXPUSHER="$value" ;;
                    WXPUSHER_APP_TOKEN) LOCAL_WXPUSHER_APP_TOKEN="$value" ;;
                    FEATURE_HEAT_EXPOSURE_GIS) LOCAL_FEATURE_HEAT_EXPOSURE_GIS="$value" ;;
                    PUBLIC_BASE_URL) LOCAL_PUBLIC_BASE_URL="$value" ;;
                    ALLOW_INSECURE_PUBLIC_BASE_URL) LOCAL_ALLOW_INSECURE_PUBLIC_BASE_URL="$value" ;;
                esac
                ;;
        esac
    done < "$ENV_FILE"
}

load_local_api_keys

load_wechat_release_form() {
    local form_file="$1"
    [ -f "$form_file" ] || return 0
    while IFS='=' read -r key value; do
        case "$key" in
            ''|\#*) continue ;;
            WECHAT_FORM_READY|WECHAT_FORMAL_RUNTIME|WX_MINIPROGRAM_APPID|WX_MINIPROGRAM_SECRET|WX_MINIPROGRAM_PRIVACY_VERSION|FEATURE_AUDIT_LOGS|FEATURE_WXPUSHER|WXPUSHER_APP_TOKEN|FEATURE_HEAT_EXPOSURE_GIS|QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED|QWEATHER_CONSOLE_USAGE_MONTH|QWEATHER_CONSOLE_USAGE_BASELINE|QWEATHER_EXPECTED_PROJECT_ID|QWEATHER_EXPECTED_KID)
                normalize_env_value "$value"
                value="$NORMALIZED_ENV_VALUE"
                case "$key" in
                    WECHAT_FORM_READY) LOCAL_WECHAT_FORM_READY="$value" ;;
                    WECHAT_FORMAL_RUNTIME) LOCAL_WECHAT_FORMAL_RUNTIME="$value" ;;
                    WX_MINIPROGRAM_APPID) LOCAL_WX_MINIPROGRAM_APPID="$value" ;;
                    WX_MINIPROGRAM_SECRET) LOCAL_WX_MINIPROGRAM_SECRET="$value" ;;
                    WX_MINIPROGRAM_PRIVACY_VERSION) LOCAL_WX_MINIPROGRAM_PRIVACY_VERSION="$value" ;;
                    FEATURE_AUDIT_LOGS) LOCAL_FEATURE_AUDIT_LOGS="$value" ;;
                    FEATURE_WXPUSHER) LOCAL_FEATURE_WXPUSHER="$value" ;;
                    WXPUSHER_APP_TOKEN) LOCAL_WXPUSHER_APP_TOKEN="$value" ;;
                    FEATURE_HEAT_EXPOSURE_GIS) LOCAL_FEATURE_HEAT_EXPOSURE_GIS="$value" ;;
                    QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED) LOCAL_QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED="$value" ;;
                    QWEATHER_CONSOLE_USAGE_MONTH) LOCAL_QWEATHER_CONSOLE_USAGE_MONTH="$value" ;;
                    QWEATHER_CONSOLE_USAGE_BASELINE) LOCAL_QWEATHER_CONSOLE_USAGE_BASELINE="$value" ;;
                    QWEATHER_EXPECTED_PROJECT_ID) LOCAL_QWEATHER_EXPECTED_PROJECT_ID="$value" ;;
                    QWEATHER_EXPECTED_KID) LOCAL_QWEATHER_EXPECTED_KID="$value" ;;
                esac
                ;;
        esac
    done < "$form_file"
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
LOCAL_DEPLOY_TEMP_DIR=""
VERIFIED_COMMIT_FILE=""
VERIFIED_WECHAT_FORM_FILE=""
VERIFIED_COMMIT=""
FORMAL_WECHAT_CONFIG_ALLOWED="0"
RUNTIME_USER="case-weather"
RUNTIME_GROUP="case-weather"

# 远端部署脚本只允许正式发布。未认证阶段继续使用本地微信 DevTools 预览，
# 防止“预览”误复用服务器正式凭据、数据库和 systemd 单元。
case "$REQUIRE_WECHAT_READY" in
    1) ;;
    0)
        echo "远端部署仅允许 DEPLOY_REQUIRE_WECHAT_READY=1；请继续使用本地微信 DevTools 预览。" >&2
        exit 64
        ;;
    *)
        echo "DEPLOY_REQUIRE_WECHAT_READY 只能是 0 或 1。" >&2
        exit 64
        ;;
esac

# 临时目录只保存本轮校验票据和正式提交快照，退出时统一清理。
cleanup_local_deploy_temp() {
    if [ -n "$LOCAL_DEPLOY_TEMP_DIR" ] && [ -d "$LOCAL_DEPLOY_TEMP_DIR" ]; then
        rm -rf -- "$LOCAL_DEPLOY_TEMP_DIR"
    fi
}
trap cleanup_local_deploy_temp EXIT

if [ "$REQUIRE_WECHAT_READY" = "1" ]; then
    LOCAL_DEPLOY_TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/case-weather-deploy.XXXXXX")"
    VERIFIED_COMMIT_FILE="$LOCAL_DEPLOY_TEMP_DIR/verified-commit"
    VERIFIED_WECHAT_FORM_FILE="$LOCAL_DEPLOY_TEMP_DIR/wechat-release.snapshot"
    python3 "$SCRIPT_DIR/validate_release_env.py" \
        --wechat-form "$WECHAT_RELEASE_FORM_FILE" \
        --snapshot-output "$VERIFIED_WECHAT_FORM_FILE" \
        --form-only \
        --require-wechat "$REQUIRE_WECHAT_READY" \
        --repo-root "$LOCAL_DIR" \
        --verified-commit-output "$VERIFIED_COMMIT_FILE"
    load_wechat_release_form "$VERIFIED_WECHAT_FORM_FILE"
    if [ "$LOCAL_WECHAT_FORM_READY" != "1" ]; then
        echo "微信正式发布私密表单尚未标记完成。" >&2
        exit 64
    fi
    if [ "$LOCAL_WECHAT_FORMAL_RUNTIME" != "1" ]; then
        echo "微信正式发布必须固定 WECHAT_FORMAL_RUNTIME=1。" >&2
        exit 64
    fi
    FORMAL_WECHAT_CONFIG_ALLOWED="1"
fi
if { [ -n "$LOCAL_WX_MINIPROGRAM_APPID" ] && [ -z "$LOCAL_WX_MINIPROGRAM_SECRET" ]; } \
    || { [ -z "$LOCAL_WX_MINIPROGRAM_APPID" ] && [ -n "$LOCAL_WX_MINIPROGRAM_SECRET" ]; }; then
    echo "WX_MINIPROGRAM_APPID 与 WX_MINIPROGRAM_SECRET 必须由同一次发布同时提供。" >&2
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

case "${LOCAL_ALLOW_WEATHER_UNAVAILABLE:-}" in
    ''|0|1) ;;
    *) echo "ALLOW_WEATHER_UNAVAILABLE 只能是 0 或 1。" >&2; exit 64 ;;
esac

case "${LOCAL_FEATURE_HEAT_EXPOSURE_GIS:-}" in
    ''|0|1) ;;
    *) echo "FEATURE_HEAT_EXPOSURE_GIS 只能是 0 或 1。" >&2; exit 64 ;;
esac
if [ "$REQUIRE_WECHAT_READY" = "1" ] && [ "$LOCAL_FEATURE_HEAT_EXPOSURE_GIS" != "1" ]; then
    echo "微信全功能正式发布必须启用 FEATURE_HEAT_EXPOSURE_GIS=1。" >&2
    exit 64
fi

case "${LOCAL_FEATURE_WXPUSHER:-}" in
    ''|0|1) ;;
    *) echo "FEATURE_WXPUSHER 只能是 0 或 1。" >&2; exit 64 ;;
esac
if [ "$REQUIRE_WECHAT_READY" = "1" ]; then
    if [ "$LOCAL_FEATURE_WXPUSHER" != "0" ]; then
        echo "1.0.0 微信正式发布必须固定 FEATURE_WXPUSHER=0。" >&2
        exit 64
    fi
    if [ -n "$LOCAL_WXPUSHER_APP_TOKEN" ]; then
        echo "FEATURE_WXPUSHER=0 时必须清空 WXPUSHER_APP_TOKEN。" >&2
        exit 64
    fi
fi

case "${LOCAL_FEATURE_AUDIT_LOGS:-}" in
    ''|0|1) ;;
    *) echo "FEATURE_AUDIT_LOGS 只能是 0 或 1。" >&2; exit 64 ;;
esac
if [ "$REQUIRE_WECHAT_READY" = "1" ] && [ "$LOCAL_FEATURE_AUDIT_LOGS" != "0" ]; then
    echo "微信正式发布必须固定 FEATURE_AUDIT_LOGS=0。" >&2
    exit 64
fi

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
if [ "$REQUIRE_WECHAT_READY" = "1" ]; then
    if [ "$LOCAL_QWEATHER_AUTH_MODE" != "jwt" ]; then
        echo "微信正式发布必须固定使用 QWEATHER_AUTH_MODE=jwt。" >&2
        exit 64
    fi
    if [ -n "$LOCAL_QWEATHER_KEY" ]; then
        echo "微信正式发布使用 JWT 时必须清空旧 QWEATHER_KEY。" >&2
        exit 64
    fi
    if [ "$LOCAL_QWEATHER_EXPECTED_PROJECT_ID" != "$LOCAL_QWEATHER_JWT_PROJECT_ID" ] \
        || [ "$LOCAL_QWEATHER_EXPECTED_KID" != "$LOCAL_QWEATHER_JWT_KID" ]; then
        echo "私密发布表记录的 QWeather Project ID/KID 与实际部署配置不一致。" >&2
        exit 64
    fi
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

RELEASE_SOURCE_DIR="$LOCAL_DIR"
LOCAL_RELEASE_EXPORT_DIR=""

# 正式发布只上传冻结提交的快照，避免 rsync 在校验后继续读取可变化的工作目录。
prepare_release_source() {
    if [ "$FORMAL_WECHAT_CONFIG_ALLOWED" != "1" ]; then
        return
    fi
    if [ -z "$VERIFIED_COMMIT_FILE" ] || [ ! -f "$VERIFIED_COMMIT_FILE" ]; then
        echo "正式发布缺少同一次校验生成的目标提交票据。" >&2
        exit 64
    fi
    IFS= read -r VERIFIED_COMMIT < "$VERIFIED_COMMIT_FILE"
    if [[ ! "$VERIFIED_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
        echo "正式发布的目标提交票据格式异常。" >&2
        exit 64
    fi
    LOCAL_RELEASE_EXPORT_DIR="$LOCAL_DEPLOY_TEMP_DIR/release-source"
    mkdir -m 0700 "$LOCAL_RELEASE_EXPORT_DIR"
    git -C "$LOCAL_DIR" archive --format=tar "$VERIFIED_COMMIT" \
        | tar -xf - -C "$LOCAL_RELEASE_EXPORT_DIR"
    RELEASE_SOURCE_DIR="$LOCAL_RELEASE_EXPORT_DIR"
}

prepare_release_source

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

# 使用 rsync 上传已准备好的发布源；正式发布源是冻结提交的本机快照。
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
            --exclude '.secrets/' \
            --exclude '*.pem' \
            --exclude '*.key' \
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
            -e "ssh $SSH_OPTS" "$RELEASE_SOURCE_DIR/" "$USER@$SERVER:$remote_target/"
        return
    fi

    if [ -n "${SSHPASS:-}" ]; then
        echo "密码上传需要 sshpass；也可以清空 DEPLOY_PASSWORD 后使用 SSH Key。" >&2
        return 64
    fi

    rsync -avz --exclude '__pycache__' --exclude '*.pyc' --exclude 'instance' --exclude 'storage' --exclude 'health_weather.db' --exclude 'data/research/*.xlsx' --exclude 'data/research/*.xls' --exclude '.git' --exclude '.claude' --exclude 'venv' --exclude '.venv' --exclude '.venv2' --exclude '.env*' --exclude '.secrets/' --exclude '*.pem' --exclude '*.key' --exclude 'project.private.config.json' --exclude '.superpowers' --exclude '.pytest_cache' --exclude '.playwright-cli' --exclude '.vscode' --exclude '.DS_Store' --exclude 'backups' --exclude 'tmp' --exclude 'output' --exclude 'blueprints/tools 2.py' -e "ssh $SSH_OPTS" "$RELEASE_SOURCE_DIR/" "$USER@$SERVER:$remote_target/"
}

echo "步骤1: 测试服务器连接..."
remote_exec "echo '连接成功'"

echo ""
echo "步骤2: 检查服务器依赖（常规发布不修改全局软件）..."
remote_exec "for REQUIRED_COMMAND in python3 rsync sqlite3 curl flock systemctl systemd-run systemd-analyze busctl crontab pgrep runuser mktemp install findmnt sync getent groupadd useradd; do command -v \"\$REQUIRED_COMMAND\" >/dev/null || { echo \"缺少服务器依赖: \$REQUIRED_COMMAND，请先执行一次性服务器初始化。\" >&2; exit 1; }; done"

echo ""
echo "步骤2.1: 检查 Redis（用于生产环境限流存储）..."
remote_exec "systemctl is-active --quiet redis-server || { echo 'redis-server 未运行，请先完成一次性服务器初始化。' >&2; exit 1; }"

echo ""
echo "步骤2.2: 检查 systemd 的成功链路能力..."
remote_exec "SYSTEMD_VERSION=\$(systemd --version | awk 'NR == 1 {print \$2}'); if [ \"\$SYSTEMD_VERSION\" -lt 249 ]; then echo 'systemd 版本过低，无法安全使用 OnSuccess 推送链路。' >&2; exit 1; fi"

echo ""
echo "步骤2.3: 准备无登录权限的运行账户..."
remote_exec "getent group $RUNTIME_GROUP >/dev/null || groupadd --system $RUNTIME_GROUP; id -u $RUNTIME_USER >/dev/null 2>&1 || useradd --system --gid $RUNTIME_GROUP --home-dir /nonexistent --shell /usr/sbin/nologin $RUNTIME_USER; [ \"\$(id -gn $RUNTIME_USER)\" = \"$RUNTIME_GROUP\" ] || { echo 'case-weather 运行账户主组异常。' >&2; exit 1; }"

echo ""
echo "步骤3: 创建不可变发布目录并上传代码..."
remote_exec "mkdir -p $PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run $PROJECT_DIR/backups/daily $PROJECT_DIR/backups/validation $PROJECT_DIR/deployments $RELEASE_ROOT/releases; chown $RUNTIME_USER:$RUNTIME_GROUP $PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run; chmod 0700 $PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run; chown root:root $PROJECT_DIR/backups $PROJECT_DIR/backups/daily $PROJECT_DIR/backups/validation $PROJECT_DIR/deployments; chmod 0700 $PROJECT_DIR/backups $PROJECT_DIR/backups/daily $PROJECT_DIR/backups/validation $PROJECT_DIR/deployments; [ \"\$(stat -c '%u:%g:%a' $PROJECT_DIR/backups)\" = '0:0:700' ] && [ \"\$(stat -c '%u:%g:%a' $PROJECT_DIR/backups/daily)\" = '0:0:700' ] && [ \"\$(stat -c '%u:%g:%a' $PROJECT_DIR/backups/validation)\" = '0:0:700' ] && [ \"\$(stat -c '%u:%g:%a' $PROJECT_DIR/deployments)\" = '0:0:700' ] || { echo 'backups/daily/validation/deployments 权限或所有者异常。' >&2; exit 1; }; chown root:$RUNTIME_GROUP $PROJECT_DIR $RELEASE_ROOT $RELEASE_ROOT/releases; chmod 0750 $PROJECT_DIR $RELEASE_ROOT $RELEASE_ROOT/releases"
remote_exec "if [ -e $NEW_RELEASE ]; then echo '发布 ID 已存在，拒绝覆盖不可变版本: $NEW_RELEASE' >&2; exit 1; fi; mkdir -p $RELEASE_APP $NEW_RELEASE/systemd"
upload_files "$RELEASE_APP"
remote_exec "ln -s $PROJECT_DIR/instance $RELEASE_APP/instance && ln -s $PROJECT_DIR/storage $RELEASE_APP/storage && ln -s $PROJECT_DIR/backups $RELEASE_APP/backups"

echo ""
echo "步骤4: 准备隔离的候选环境配置..."
# 首次部署尚无生产进程，可以安全创建初始配置。已有配置只复制到候选文件。
remote_exec "umask 077; if [ ! -f $PROJECT_DIR/.env ]; then SECRET_KEY_GEN=\$(python3 -c 'import secrets; print(secrets.token_hex(32))'); PAIR_TOKEN_PEPPER_GEN=\$(python3 -c 'import secrets; print(secrets.token_hex(32))'); cat > $PROJECT_DIR/.env << EOF
FLASK_ENV=production
DEBUG=false
WECHAT_FORMAL_RUNTIME=0
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
QWEATHER_REQUIRE_PERSISTENT_BUDGET=1
QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED=
QWEATHER_CONSOLE_USAGE_MONTH=
QWEATHER_CONSOLE_USAGE_BASELINE=
QWEATHER_EXPECTED_PROJECT_ID=
QWEATHER_EXPECTED_KID=
ALLOW_WEATHER_UNAVAILABLE=
WEATHER_CACHE_TTL_MINUTES=30
FORECAST_CACHE_TTL_MINUTES=30
QWEATHER_WARNING_CACHE_TTL_MINUTES=30
WEATHER_SYNC_LOCATIONS=都昌县
AMAP_KEY=
FEATURE_WEB_AI=0
FEATURE_AUDIT_LOGS=0
SILICONFLOW_API_KEY=
SILICONFLOW_API_BASE=https://api.siliconflow.cn/v1
FEATURE_WXPUSHER=0
WXPUSHER_APP_TOKEN=
WXPUSHER_API_BASE=https://wxpusher.zjiecode.com/api
DISPATCH_LOCK_PATH=$PROJECT_DIR/run/case-weather-dispatch.lock
FEATURE_HEAT_EXPOSURE_GIS=0
WX_MINIPROGRAM_APPID=
WX_MINIPROGRAM_SECRET=
WX_MINIPROGRAM_OPENID_PEPPER=
WX_MINIPROGRAM_SESSION_SECRET=
WX_MINIPROGRAM_PRIVACY_VERSION=2026-07-18
PUBLIC_BASE_URL=https://yilaoweather.org
ALLOW_INSECURE_PUBLIC_BASE_URL=
EOF
echo '已创建首次部署配置'; fi; cp -a $PROJECT_DIR/.env $STAGED_ENV_FILE; chmod 0600 $STAGED_ENV_FILE"

echo ""
echo "步骤4.1: 原子补齐候选配置..."
# 所有候选值均通过 stdin 写入；旧服务在激活事务前继续读取原配置。
remote_env_update "DATABASE_URI" "sqlite:///health_weather.db" "if-empty"
remote_env_update "QWEATHER_AUTH_MODE" "disabled" "if-empty"
remote_env_update "QWEATHER_CANONICAL_LOCATION" "116.20,29.27" "always"
remote_env_update "QWEATHER_MONTHLY_REQUEST_LIMIT" "40000" "always"
remote_env_update "QWEATHER_BUDGET_FAIL_CLOSED" "1" "always"
remote_env_update "QWEATHER_REQUIRE_PERSISTENT_BUDGET" "1" "always"
remote_env_update "WEATHER_CACHE_TTL_MINUTES" "30" "always"
remote_env_update "FORECAST_CACHE_TTL_MINUTES" "30" "always"
remote_env_update "QWEATHER_WARNING_CACHE_TTL_MINUTES" "30" "always"
remote_env_update "WEATHER_SYNC_LOCATIONS" "都昌县" "always"
remote_env_update "WXPUSHER_API_BASE" "https://wxpusher.zjiecode.com/api" "always"
remote_env_update "FEATURE_WXPUSHER" "0" "if-empty"
remote_env_update "FEATURE_WEB_AI" "0" "always"
remote_env_update "SILICONFLOW_API_KEY" "" "always"
remote_env_update "SILICONFLOW_API_BASE" "https://api.siliconflow.cn/v1" "always"
remote_env_update "DISPATCH_LOCK_PATH" "$PROJECT_DIR/run/case-weather-dispatch.lock" "always"
remote_env_update "FEATURE_HEAT_EXPOSURE_GIS" "0" "if-empty"
remote_env_update "WECHAT_FORMAL_RUNTIME" "0" "if-empty"
remote_env_update "WX_MINIPROGRAM_PRIVACY_VERSION" "2026-07-18" "if-empty"

echo ""
echo "步骤4.2: 安全写入显式提供的发布配置..."
# 正式入口与第三方凭证接收端每次部署都收敛到固定 origin。
remote_env_update "PUBLIC_BASE_URL" "https://yilaoweather.org" "always"
remote_env_update "ALLOW_INSECURE_PUBLIC_BASE_URL" "" "always"

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
if [ -n "$LOCAL_FEATURE_HEAT_EXPOSURE_GIS" ]; then
    remote_env_update "FEATURE_HEAT_EXPOSURE_GIS" "$LOCAL_FEATURE_HEAT_EXPOSURE_GIS" "always"
fi
# 只有同一次验证快照同时满足 require=1 与 ready=1，才允许写入正式凭据。
if [ "$FORMAL_WECHAT_CONFIG_ALLOWED" = "1" ]; then
    # 个人主体 1.0.0 不持久化审计日志，旧远端即使为 1 也强制收敛为 0。
    remote_env_update "WECHAT_FORMAL_RUNTIME" "$LOCAL_WECHAT_FORMAL_RUNTIME" "always"
    remote_env_update "FEATURE_AUDIT_LOGS" "$LOCAL_FEATURE_AUDIT_LOGS" "always"
    remote_env_update "FEATURE_WXPUSHER" "$LOCAL_FEATURE_WXPUSHER" "always"
    remote_env_update "WXPUSHER_APP_TOKEN" "$LOCAL_WXPUSHER_APP_TOKEN" "always"
    remote_env_update "WX_MINIPROGRAM_APPID" "$LOCAL_WX_MINIPROGRAM_APPID" "always"
    remote_env_update "WX_MINIPROGRAM_SECRET" "$LOCAL_WX_MINIPROGRAM_SECRET" "always"
    remote_env_generate_secret "WX_MINIPROGRAM_OPENID_PEPPER"
    remote_env_generate_secret "WX_MINIPROGRAM_SESSION_SECRET"
    remote_env_update "WX_MINIPROGRAM_PRIVACY_VERSION" "$LOCAL_WX_MINIPROGRAM_PRIVACY_VERSION" "always"
    remote_env_update "QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED" "$LOCAL_QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED" "always"
    remote_env_update "QWEATHER_CONSOLE_USAGE_MONTH" "$LOCAL_QWEATHER_CONSOLE_USAGE_MONTH" "always"
    remote_env_update "QWEATHER_CONSOLE_USAGE_BASELINE" "$LOCAL_QWEATHER_CONSOLE_USAGE_BASELINE" "always"
    remote_env_update "QWEATHER_EXPECTED_PROJECT_ID" "$LOCAL_QWEATHER_EXPECTED_PROJECT_ID" "always"
    remote_env_update "QWEATHER_EXPECTED_KID" "$LOCAL_QWEATHER_EXPECTED_KID" "always"
fi
remote_exec "python3 $RELEASE_APP/scripts/validate_release_env.py --file $STAGED_ENV_FILE --require-wechat $REQUIRE_WECHAT_READY"

echo ""
echo "步骤6: 为新版本创建独立虚拟环境..."
remote_exec "set -e; EXPECTED_LOCK_SHA=c7e450c30d7d3c56bdf210f69a58620cba9d99e462e0e2c254ab45456271f853; ACTUAL_LOCK_SHA=\$(python3 -c 'import hashlib; print(hashlib.sha256(open(\"$RELEASE_APP/requirements.lock\", \"rb\").read()).hexdigest())'); [ \"\$ACTUAL_LOCK_SHA\" = \"\$EXPECTED_LOCK_SHA\" ] || { echo 'requirements.lock 摘要不匹配。' >&2; exit 1; }; python3 -m venv $RELEASE_VENV; $RELEASE_VENV/bin/python -m pip install --index-url https://pypi.org/simple --require-hashes --only-binary=:all: -r $RELEASE_APP/requirements.lock; [ -x $RELEASE_VENV/bin/gunicorn ] || { echo '锁定依赖安装后缺少 gunicorn。' >&2; exit 1; }; umask 077; mkdir -p $NEW_RELEASE/private-metadata; $RELEASE_VENV/bin/python --version > $NEW_RELEASE/private-metadata/python-version.txt 2>&1; printf '%s\n' \"\$ACTUAL_LOCK_SHA\" > $NEW_RELEASE/private-metadata/requirements-lock.sha256; $RELEASE_VENV/bin/python -m pip inspect --local > $NEW_RELEASE/private-metadata/pip-inspect.json; chmod 0700 $NEW_RELEASE/private-metadata; chmod 0600 $NEW_RELEASE/private-metadata/python-version.txt $NEW_RELEASE/private-metadata/requirements-lock.sha256 $NEW_RELEASE/private-metadata/pip-inspect.json"
remote_exec "$RELEASE_VENV/bin/python $RELEASE_APP/scripts/validate_release_env.py --file $STAGED_ENV_FILE --require-wechat $REQUIRE_WECHAT_READY --probe-persistent-budget"
if [ "$FORMAL_WECHAT_CONFIG_ALLOWED" = "1" ]; then
    # commit 只含十六进制字符，写入 release 私有 metadata 后由激活脚本再次核对。
    remote_exec "umask 077; printf '%s\n' '$VERIFIED_COMMIT' > $NEW_RELEASE/private-metadata/source-commit.txt; chmod 0600 $NEW_RELEASE/private-metadata/source-commit.txt"
fi

echo ""
echo "步骤6.1: 在停止生产服务前完成隔离测试..."
remote_exec "cd $RELEASE_APP && DATABASE_URI=sqlite:///:memory: DEBUG=true WECHAT_FORMAL_RUNTIME=0 SECRET_KEY=release-preflight-secret-key-123456789 PAIR_TOKEN_PEPPER=release-preflight-pair-pepper-123456789 RATE_LIMIT_STORAGE_URI=memory:// REDIS_URL= QWEATHER_AUTH_MODE=disabled QWEATHER_KEY= QWEATHER_API_BASE= AMAP_KEY= AMAP_WEB_SERVICE_KEY= AMAP_SECURITY_JS_CODE= SILICONFLOW_API_KEY= WXPUSHER_APP_TOKEN= WX_MINIPROGRAM_APPID= WX_MINIPROGRAM_SECRET= WX_MINIPROGRAM_OPENID_PEPPER= WX_MINIPROGRAM_SESSION_SECRET= DEMO_MODE=1 $RELEASE_VENV/bin/python -m pytest -q"

echo ""
echo "步骤6.2: 为新版本生成 systemd 单元模板..."
remote_exec "cat > $NEW_RELEASE/systemd/case-weather.service << 'EOF'
[Unit]
Description=Case Weather Flask Application
ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress
ConditionPathExists=|/run/case-weather/activation-permit
After=network.target

[Service]
User=case-weather
Group=case-weather
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible
ProcSubset=pid
RestrictSUIDSGID=true
RestrictNamespaces=true
RestrictRealtime=true
LockPersonality=true
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
ReadOnlyPaths=$CURRENT_LINK $PROJECT_DIR/.env
ReadWritePaths=$PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run
WorkingDirectory=$CURRENT_LINK/app
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$CURRENT_LINK/venv/bin/gunicorn --workers 3 --bind 127.0.0.1:5000 --timeout 120 app:app
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF"

remote_exec "cat > $NEW_RELEASE/systemd/case-weather-backup.service << 'EOF'
[Unit]
Description=Case Weather - root-only SQLite backup
ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress
ConditionPathExists=|/run/case-weather/activation-permit
After=local-fs.target
RequiresMountsFor=$PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/backups/daily

[Service]
Type=oneshot
User=root
Group=root
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
PrivateNetwork=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible
ProcSubset=pid
RestrictSUIDSGID=true
RestrictNamespaces=true
RestrictRealtime=true
LockPersonality=true
MemoryDenyWriteExecute=true
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX
CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_SETUID CAP_SETGID
ReadOnlyPaths=$CURRENT_LINK $PROJECT_DIR/.env
ReadWritePaths=$PROJECT_DIR/backups/daily $PROJECT_DIR/instance $PROJECT_DIR/storage
InaccessiblePaths=$PROJECT_DIR/backups/deploy-transactions $PROJECT_DIR/deployments $PROJECT_DIR/run
WorkingDirectory=$CURRENT_LINK/app
Environment=PROJECT_DIR=$PROJECT_DIR
Environment=ENV_FILE=$PROJECT_DIR/.env
Environment=BACKUP_DIR=$PROJECT_DIR/backups/daily
Environment=DEFAULT_DB_FILE=$PROJECT_DIR/instance/health_weather.db
Environment=BACKUP_RUNTIME_USER=$RUNTIME_USER
Environment=RUNUSER_BIN=runuser
Environment=SQLITE3_BIN=sqlite3
Environment=MKTEMP_BIN=mktemp
Environment=INSTALL_BIN=install
EnvironmentFile=$PROJECT_DIR/backups/backup-runtime.env
ExecStart=/bin/bash $CURRENT_LINK/app/scripts/backup.sh
TimeoutStartSec=15min
EOF

cat > $NEW_RELEASE/systemd/case-weather-backup.timer << 'EOF'
[Unit]
Description=Case Weather - daily SQLite backup in Asia/Shanghai
ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress
ConditionPathExists=|/run/case-weather/activation-permit

[Timer]
OnCalendar=*-*-* 03:00:00 Asia/Shanghai
Persistent=true
AccuracySec=1min
Unit=case-weather-backup.service

[Install]
WantedBy=timers.target
EOF"

remote_exec "cat > $NEW_RELEASE/systemd/case-weather-cache.service << 'EOF'
[Unit]
Description=Case Weather - refresh Duchang weather cache
ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress
ConditionPathExists=|/run/case-weather/activation-permit
After=network.target case-weather.service
OnSuccess=case-weather-dispatch.service case-weather-cache.timer
OnFailure=case-weather-cache.timer

[Service]
Type=oneshot
User=case-weather
Group=case-weather
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible
ProcSubset=pid
RestrictSUIDSGID=true
RestrictNamespaces=true
RestrictRealtime=true
LockPersonality=true
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
ReadOnlyPaths=$CURRENT_LINK $PROJECT_DIR/.env
ReadWritePaths=$PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run
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
ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress
ConditionPathExists=|/run/case-weather/activation-permit

[Timer]
OnActiveSec=30min
OnUnitActiveSec=30min
AccuracySec=1s
Unit=case-weather-cache.service

[Install]
WantedBy=timers.target
EOF"

remote_exec "cat > $NEW_RELEASE/systemd/case-weather-cache-bootstrap.timer << 'EOF'
[Unit]
Description=Case Weather - delay the first cache refresh for 30 minutes
ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress
ConditionPathExists=|/run/case-weather/activation-permit

[Timer]
OnActiveSec=30min
AccuracySec=1s
RemainAfterElapse=no
Unit=case-weather-cache.service

[Install]
WantedBy=timers.target
EOF"

remote_exec "cat > $NEW_RELEASE/systemd/case-weather-dispatch.service << 'EOF'
[Unit]
Description=Case Weather - dispatch alerts (WxPusher)
ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress
ConditionPathExists=|/run/case-weather/activation-permit
After=network.target case-weather.service case-weather-cache.service

[Service]
Type=oneshot
User=case-weather
Group=case-weather
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible
ProcSubset=pid
RestrictSUIDSGID=true
RestrictNamespaces=true
RestrictRealtime=true
LockPersonality=true
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
ReadOnlyPaths=$CURRENT_LINK $PROJECT_DIR/.env
ReadWritePaths=$PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run
WorkingDirectory=$CURRENT_LINK/app
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/bin/bash $CURRENT_LINK/app/scripts/dispatch_alerts.sh --dedupe-hours 6
TimeoutStartSec=15min
EOF"

remote_exec "cat > $NEW_RELEASE/systemd/case-weather-risk-precompute.service << 'EOF'
[Unit]
Description=Case Weather - precompute community risk cache
ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress
ConditionPathExists=|/run/case-weather/activation-permit
After=network.target case-weather.service

[Service]
Type=oneshot
User=case-weather
Group=case-weather
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible
ProcSubset=pid
RestrictSUIDSGID=true
RestrictNamespaces=true
RestrictRealtime=true
LockPersonality=true
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
ReadOnlyPaths=$CURRENT_LINK $PROJECT_DIR/.env
ReadWritePaths=$PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run
WorkingDirectory=$CURRENT_LINK/app
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
Environment=VENV_PY=$CURRENT_LINK/venv/bin/python
ExecStart=/bin/bash $CURRENT_LINK/app/scripts/community_risk_precompute.sh
EOF

cat > $NEW_RELEASE/systemd/case-weather-risk-precompute.timer << 'EOF'
[Unit]
Description=Case Weather - precompute community risk cache hourly
ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress
ConditionPathExists=|/run/case-weather/activation-permit

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
ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress
ConditionPathExists=|/run/case-weather/activation-permit
StartLimitIntervalSec=1h
StartLimitBurst=20

[Service]
Type=oneshot
User=case-weather
Group=case-weather
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible
ProcSubset=pid
RestrictSUIDSGID=true
RestrictNamespaces=true
RestrictRealtime=true
LockPersonality=true
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
ReadOnlyPaths=$CURRENT_LINK $PROJECT_DIR/.env
ReadWritePaths=$PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run
WorkingDirectory=$CURRENT_LINK/app
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
Environment=VENV_PY=$CURRENT_LINK/venv/bin/python
ExecStart=/bin/bash $CURRENT_LINK/app/scripts/cleanup_usage_events.sh
Restart=on-failure
RestartSec=1min
EOF

cat > $NEW_RELEASE/systemd/case-weather-usage-cleanup.timer << 'EOF'
[Unit]
Description=Case Weather - delete expired UsageEvent rows daily
ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress
ConditionPathExists=|/run/case-weather/activation-permit

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true
Unit=case-weather-usage-cleanup.service

[Install]
WantedBy=timers.target
EOF"

remote_exec "systemd-analyze verify $NEW_RELEASE/systemd/*.service $NEW_RELEASE/systemd/*.timer"

echo ""
echo "步骤6.2.1: 给现有与新调度安装共享断电保护门..."
remote_exec "set -e; for UNIT in case-weather.service case-weather-backup.service case-weather-backup.timer case-weather-cache.service case-weather-cache.timer case-weather-cache-bootstrap.service case-weather-cache-bootstrap.timer case-weather-dispatch.service case-weather-dispatch.timer case-weather-risk-precompute.service case-weather-risk-precompute.timer case-weather-usage-cleanup.service case-weather-usage-cleanup.timer case-weather-sync.service case-weather-sync.timer; do DROPIN=/etc/systemd/system/\$UNIT.d; mkdir -p \$DROPIN; printf '%s\n' '[Unit]' 'ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress' 'ConditionPathExists=|/run/case-weather/activation-permit' > \$DROPIN/10-case-weather-activation-guard.conf; chown root:root \$DROPIN/10-case-weather-activation-guard.conf; chmod 0644 \$DROPIN/10-case-weather-activation-guard.conf; done; systemctl daemon-reload"

echo ""
echo "步骤6.3: 收敛发布文件与运行数据权限..."
remote_exec "chown -R root:$RUNTIME_GROUP $NEW_RELEASE; chmod -R g+rX,o-rwx $NEW_RELEASE; chown root:$RUNTIME_GROUP $PROJECT_DIR/.env $STAGED_ENV_FILE; chmod 0640 $PROJECT_DIR/.env $STAGED_ENV_FILE; chown $RUNTIME_USER:$RUNTIME_GROUP $PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run; chmod 0700 $PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run"
if [ "$FORMAL_WECHAT_CONFIG_ALLOWED" = "1" ]; then
    # 控制台当月已用量只做原子 max 合并，绝不降低 Redis 中已有计数。
    remote_exec "$RELEASE_VENV/bin/python $RELEASE_APP/scripts/validate_release_env.py --file $STAGED_ENV_FILE --require-wechat 1 --probe-persistent-budget --seed-persistent-budget"
fi

echo ""
echo "步骤7: 在单个服务器事务中备份、迁移、切换并验活..."
remote_exec "STATE_DIR=$PROJECT_DIR RELEASE_ROOT=$RELEASE_ROOT NEW_RELEASE=$NEW_RELEASE CURRENT_LINK=$CURRENT_LINK ENV_FILE=$PROJECT_DIR/.env STAGED_ENV_FILE=$STAGED_ENV_FILE HEALTH_URL=http://127.0.0.1:5000/healthz REQUIRE_WECHAT_READY=$REQUIRE_WECHAT_READY EXPECTED_RELEASE_COMMIT=$VERIFIED_COMMIT RECOVERY_ACKNOWLEDGED_TRANSACTION=$RECOVERY_ACKNOWLEDGED_TRANSACTION RUNTIME_USER=$RUNTIME_USER RUNTIME_GROUP=$RUNTIME_GROUP bash $RELEASE_APP/scripts/activate_release.sh"

echo ""
echo "步骤8: 服务、timer、OnSuccess、current 链接与健康检查已在原子激活事务内通过。"

echo ""
echo "=== 部署完成 ==="
echo "发布版本: $RELEASE_ID"
echo "持久化目录: $PROJECT_DIR"
echo "当前版本入口: $CURRENT_LINK"
