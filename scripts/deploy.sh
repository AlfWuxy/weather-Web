#!/bin/bash
# 部署脚本 - 将项目部署到远程服务器
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/dotenv.sh
source "$SCRIPT_DIR/dotenv.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
WECHAT_RELEASE_FORM_FILE="${WECHAT_RELEASE_FORM_FILE:-$ROOT_DIR/.env.wechat-release}"
CALLER_DEPLOY_MODE_SET="${DEPLOY_MODE+x}"
CALLER_DEPLOY_MODE="${DEPLOY_MODE:-}"
CALLER_REQUIRE_WECHAT_READY_SET="${DEPLOY_REQUIRE_WECHAT_READY+x}"
CALLER_REQUIRE_WECHAT_READY="${DEPLOY_REQUIRE_WECHAT_READY:-}"

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
LOCAL_QWEATHER_JWT_PRIVATE_KEY_SOURCE=""
LOCAL_QWEATHER_JWT_PRIVATE_KEY_SNAPSHOT=""
LOCAL_QWEATHER_JWT_PRIVATE_KEY_SHA256=""
LOCAL_QWEATHER_JWT_PRIVATE_KEY_SIZE=""
LOCAL_ML_MODEL_ARTIFACT_DIR=""
LOCAL_ML_MODEL_ARTIFACT_SNAPSHOT_DIR=""
LOCAL_ML_MODEL_ARTIFACT_RECEIPT=""
LOCAL_ALLOW_WEATHER_UNAVAILABLE="${ALLOW_WEATHER_UNAVAILABLE:-}"
LOCAL_AMAP_JS_API_KEY=""
LOCAL_AMAP_WEB_SERVICE_KEY=""
LOCAL_AMAP_SECURITY_JS_CODE=""
LOCAL_COOLING_COORDINATE_VERIFICATION_TTL_DAYS=""
LOCAL_FEATURE_AUDIT_LOGS=""
LOCAL_FEATURE_STRUCTURED_LOGS=""
LOCAL_FEATURE_WXPUSHER=""
LOCAL_WXPUSHER_APP_TOKEN=""
LOCAL_FEATURE_HEAT_EXPOSURE_GIS=""
LOCAL_PUBLIC_BASE_URL=""
LOCAL_ALLOW_INSECURE_PUBLIC_BASE_URL="${ALLOW_INSECURE_PUBLIC_BASE_URL:-}"
LOCAL_WX_MINIPROGRAM_APPID=""
LOCAL_WX_MINIPROGRAM_SECRET=""
LOCAL_WX_MINIPROGRAM_PRIVACY_VERSION=""
LOCAL_WECHAT_FORMAL_RUNTIME=""
LOCAL_WEB_PRIVATE_FEATURES_ENABLED=""
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
            DEPLOY_SERVER|DEPLOY_USER|DEPLOY_PASSWORD|DEPLOY_PROJECT_DIR|DEPLOY_LOCAL_DIR|DEPLOY_RELEASE_ROOT|DEPLOY_RELEASE_ID|DEPLOY_MODE|DEPLOY_REQUIRE_WECHAT_READY|DEPLOY_RECOVERY_ACKNOWLEDGED_TRANSACTION|WECHAT_RELEASE_FORM_FILE|ML_MODEL_ARTIFACT_DIR|SSHPASS)
                normalize_env_value "$value"
                value="$NORMALIZED_ENV_VALUE"
                if [ "$key" = "DEPLOY_MODE" ] \
                    && [ "$CALLER_DEPLOY_MODE_SET" = "x" ] \
                    && [ "$value" != "$CALLER_DEPLOY_MODE" ]; then
                    echo "命令行 DEPLOY_MODE 与 ENV_FILE 冲突，已在远端操作前停止。" >&2
                    exit 64
                fi
                if [ "$key" = "DEPLOY_REQUIRE_WECHAT_READY" ] \
                    && [ "$CALLER_REQUIRE_WECHAT_READY_SET" = "x" ] \
                    && [ "$value" != "$CALLER_REQUIRE_WECHAT_READY" ]; then
                    echo "命令行 DEPLOY_REQUIRE_WECHAT_READY 与 ENV_FILE 冲突，已在远端操作前停止。" >&2
                    exit 64
                fi
                export "$key"="$value"
                ;;
        esac
    done < "$ENV_FILE"
}

load_deploy_env
LOCAL_ML_MODEL_ARTIFACT_DIR="${ML_MODEL_ARTIFACT_DIR:-}"

load_local_api_keys() {
    [ -f "$ENV_FILE" ] || return 0
    while IFS='=' read -r key value; do
        case "$key" in
            ''|\#*) continue ;;
            QWEATHER_KEY|QWEATHER_API_BASE|QWEATHER_AUTH_MODE|QWEATHER_JWT_KID|QWEATHER_JWT_PROJECT_ID|QWEATHER_JWT_PRIVATE_KEY_PATH|QWEATHER_JWT_PRIVATE_KEY_SOURCE|ALLOW_WEATHER_UNAVAILABLE|AMAP_JS_API_KEY|AMAP_WEB_SERVICE_KEY|AMAP_SECURITY_JS_CODE|COOLING_COORDINATE_VERIFICATION_TTL_DAYS|FEATURE_AUDIT_LOGS|FEATURE_STRUCTURED_LOGS|FEATURE_WXPUSHER|WXPUSHER_APP_TOKEN|FEATURE_HEAT_EXPOSURE_GIS|WEB_PRIVATE_FEATURES_ENABLED|PUBLIC_BASE_URL|ALLOW_INSECURE_PUBLIC_BASE_URL)
                normalize_env_value "$value"
                value="$NORMALIZED_ENV_VALUE"
                case "$key" in
                    QWEATHER_KEY) LOCAL_QWEATHER_KEY="$value" ;;
                    QWEATHER_API_BASE) LOCAL_QWEATHER_API_BASE="$value" ;;
                    QWEATHER_AUTH_MODE) LOCAL_QWEATHER_AUTH_MODE="$value" ;;
                    QWEATHER_JWT_KID) LOCAL_QWEATHER_JWT_KID="$value" ;;
                    QWEATHER_JWT_PROJECT_ID) LOCAL_QWEATHER_JWT_PROJECT_ID="$value" ;;
                    QWEATHER_JWT_PRIVATE_KEY_PATH) LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH="$value" ;;
                    QWEATHER_JWT_PRIVATE_KEY_SOURCE) LOCAL_QWEATHER_JWT_PRIVATE_KEY_SOURCE="$value" ;;
                    ALLOW_WEATHER_UNAVAILABLE) LOCAL_ALLOW_WEATHER_UNAVAILABLE="$value" ;;
                    AMAP_JS_API_KEY) LOCAL_AMAP_JS_API_KEY="$value" ;;
                    AMAP_WEB_SERVICE_KEY) LOCAL_AMAP_WEB_SERVICE_KEY="$value" ;;
                    AMAP_SECURITY_JS_CODE) LOCAL_AMAP_SECURITY_JS_CODE="$value" ;;
                    COOLING_COORDINATE_VERIFICATION_TTL_DAYS) LOCAL_COOLING_COORDINATE_VERIFICATION_TTL_DAYS="$value" ;;
                    FEATURE_AUDIT_LOGS) LOCAL_FEATURE_AUDIT_LOGS="$value" ;;
                    FEATURE_STRUCTURED_LOGS) LOCAL_FEATURE_STRUCTURED_LOGS="$value" ;;
                    FEATURE_WXPUSHER) LOCAL_FEATURE_WXPUSHER="$value" ;;
                    WXPUSHER_APP_TOKEN) LOCAL_WXPUSHER_APP_TOKEN="$value" ;;
                    FEATURE_HEAT_EXPOSURE_GIS) LOCAL_FEATURE_HEAT_EXPOSURE_GIS="$value" ;;
                    WEB_PRIVATE_FEATURES_ENABLED) LOCAL_WEB_PRIVATE_FEATURES_ENABLED="$value" ;;
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
            WECHAT_FORM_READY|WECHAT_FORMAL_RUNTIME|WEB_PRIVATE_FEATURES_ENABLED|WX_MINIPROGRAM_APPID|WX_MINIPROGRAM_SECRET|WX_MINIPROGRAM_PRIVACY_VERSION|FEATURE_AUDIT_LOGS|FEATURE_STRUCTURED_LOGS|FEATURE_WXPUSHER|WXPUSHER_APP_TOKEN|FEATURE_HEAT_EXPOSURE_GIS|QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED|QWEATHER_CONSOLE_USAGE_MONTH|QWEATHER_CONSOLE_USAGE_BASELINE|QWEATHER_EXPECTED_PROJECT_ID|QWEATHER_EXPECTED_KID)
                normalize_env_value "$value"
                value="$NORMALIZED_ENV_VALUE"
                case "$key" in
                    WECHAT_FORM_READY) LOCAL_WECHAT_FORM_READY="$value" ;;
                    WECHAT_FORMAL_RUNTIME) LOCAL_WECHAT_FORMAL_RUNTIME="$value" ;;
                    WEB_PRIVATE_FEATURES_ENABLED) LOCAL_WEB_PRIVATE_FEATURES_ENABLED="$value" ;;
                    WX_MINIPROGRAM_APPID) LOCAL_WX_MINIPROGRAM_APPID="$value" ;;
                    WX_MINIPROGRAM_SECRET) LOCAL_WX_MINIPROGRAM_SECRET="$value" ;;
                    WX_MINIPROGRAM_PRIVACY_VERSION) LOCAL_WX_MINIPROGRAM_PRIVACY_VERSION="$value" ;;
                    FEATURE_AUDIT_LOGS) LOCAL_FEATURE_AUDIT_LOGS="$value" ;;
                    FEATURE_STRUCTURED_LOGS) LOCAL_FEATURE_STRUCTURED_LOGS="$value" ;;
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
DEPLOY_MODE="${DEPLOY_MODE:-wechat_formal}"
REQUIRE_WECHAT_READY="${DEPLOY_REQUIRE_WECHAT_READY:-0}"
RECOVERY_ACKNOWLEDGED_TRANSACTION="${DEPLOY_RECOVERY_ACKNOWLEDGED_TRANSACTION:-}"
LOCAL_DEPLOY_TEMP_DIR=""
VERIFIED_COMMIT_FILE=""
VERIFIED_WECHAT_FORM_FILE=""
VERIFIED_COMMIT=""
VERIFIED_RELEASE_BRANCH=""
LOCAL_CI_PROOF_FILE=""
LOCAL_MINIPROGRAM_CI_PROOF_FILE=""
FORMAL_WECHAT_CONFIG_ALLOWED="0"
EFFECTIVE_REQUIRE_WECHAT_READY=""
EXPECTED_WECHAT_FORMAL_RUNTIME=""
EXPECTED_WEB_PRIVATE_FEATURES_ENABLED=""
RUNTIME_USER="case-weather"
RUNTIME_GROUP="case-weather"
REMOTE_QWEATHER_PRIVATE_DIR="$PROJECT_DIR/private"
REMOTE_QWEATHER_PENDING_KEY_PATH=""
ACTIVATION_QWEATHER_PENDING_KEY_PATH=""
REMOTE_QWEATHER_VALIDATION_PENDING_ARG=""
REMOTE_QWEATHER_PREACTIVATION_ROOT="$PROJECT_DIR/backups/qweather-preactivation"
REMOTE_QWEATHER_PREACTIVATION_ACTIVE="0"

# 部署目标必须显式区分网页/后端发布与微信正式发布。
# web_backend_only 不代表微信包已经就绪，也不会读取或写入微信正式发布表单。
case "$DEPLOY_MODE" in
    wechat_formal)
        if [ "$REQUIRE_WECHAT_READY" != "1" ]; then
            echo "DEPLOY_MODE=wechat_formal 必须同时设置 DEPLOY_REQUIRE_WECHAT_READY=1。" >&2
            exit 64
        fi
        ;;
    web_backend_only)
        if [ "$REQUIRE_WECHAT_READY" != "0" ]; then
            echo "DEPLOY_MODE=web_backend_only 必须保持 DEPLOY_REQUIRE_WECHAT_READY=0。" >&2
            exit 64
        fi
        ;;
    *)
        echo "DEPLOY_MODE 只能是 web_backend_only 或 wechat_formal。" >&2
        exit 64
        ;;
esac
case "$REQUIRE_WECHAT_READY" in
    0|1) ;;
    *)
        echo "DEPLOY_REQUIRE_WECHAT_READY 只能是 0 或 1。" >&2
        exit 64
        ;;
esac

# 网页/后端发布只能继承服务器现有天气运行态。本机 .env 中的所有
# QWeather 字段在该模式下均不参与候选配置、校验身份或私钥轮换。
if [ "$DEPLOY_MODE" = "web_backend_only" ]; then
    LOCAL_QWEATHER_KEY=""
    LOCAL_QWEATHER_API_BASE=""
    LOCAL_QWEATHER_AUTH_MODE=""
    LOCAL_QWEATHER_JWT_KID=""
    LOCAL_QWEATHER_JWT_PROJECT_ID=""
    LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH=""
    LOCAL_QWEATHER_JWT_PRIVATE_KEY_SOURCE=""
    LOCAL_QWEATHER_JWT_PRIVATE_KEY_SNAPSHOT=""
    LOCAL_QWEATHER_JWT_PRIVATE_KEY_SHA256=""
    LOCAL_QWEATHER_JWT_PRIVATE_KEY_SIZE=""
    LOCAL_ALLOW_WEATHER_UNAVAILABLE=""
    # 网页/后端发布只能继承服务器当前双端路由策略，本机值不得改写候选。
    LOCAL_WEB_PRIVATE_FEATURES_ENABLED=""
fi

# 退出时清理本地临时快照；若私钥仍处于预激活阶段，同时触发服务端身份绑定归档。
cleanup_local_deploy_temp() {
    local original_status=$?
    local remote_cleanup_status=0
    trap - EXIT
    set +e
    if [ "$REMOTE_QWEATHER_PREACTIVATION_ACTIVE" = "1" ] \
        && declare -F archive_qweather_preactivation_key >/dev/null 2>&1; then
        archive_qweather_preactivation_key || remote_cleanup_status=$?
        if [ "$remote_cleanup_status" -ne 0 ]; then
            echo "QWeather 预激活私钥未能自动归档；服务端耐久事务已保留，重试会先重新核对。" >&2
        fi
    fi
    if [ -n "$LOCAL_DEPLOY_TEMP_DIR" ] && [ -d "$LOCAL_DEPLOY_TEMP_DIR" ]; then
        rm -rf -- "$LOCAL_DEPLOY_TEMP_DIR"
    fi
    exit "$original_status"
}
trap cleanup_local_deploy_temp EXIT

LOCAL_DEPLOY_TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/case-weather-deploy.XXXXXX")"
chmod 0700 "$LOCAL_DEPLOY_TEMP_DIR"
if stat -f '%Lp' "$LOCAL_DEPLOY_TEMP_DIR" >/dev/null 2>&1; then
    local_temp_mode="$(stat -f '%Lp' "$LOCAL_DEPLOY_TEMP_DIR")"
elif local_temp_mode="$(stat -c '%a' "$LOCAL_DEPLOY_TEMP_DIR" 2>/dev/null)"; then
    :
else
    echo "无法读取本轮部署临时目录权限。" >&2
    exit 64
fi
if [ "$local_temp_mode" != "700" ]; then
    echo "本轮部署临时目录权限必须精确为 0700。" >&2
    exit 64
fi
VERIFIED_COMMIT_FILE="$LOCAL_DEPLOY_TEMP_DIR/verified-commit"
LOCAL_CI_PROOF_FILE="$LOCAL_DEPLOY_TEMP_DIR/ci-proof.json"
LOCAL_MINIPROGRAM_CI_PROOF_FILE="$LOCAL_DEPLOY_TEMP_DIR/miniprogram-ci-proof.json"
LOCAL_ML_MODEL_ARTIFACT_RECEIPT="$LOCAL_DEPLOY_TEMP_DIR/model-artifacts.json"

# 网页/后端独立发布同样只接受干净 Git HEAD，并生成本轮私有 commit 票据。
freeze_web_backend_commit() {
    local expected_root=""
    local discovered_root=""
    local discovered_real_root=""
    local status_output=""
    local commit=""

    if ! expected_root="$(cd "$LOCAL_DIR" 2>/dev/null && pwd -P)"; then
        echo "网页/后端发布的 Git 工作树无法验证。" >&2
        exit 64
    fi
    if ! discovered_root="$(git -C "$LOCAL_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
        echo "网页/后端发布的 Git 工作树无法验证。" >&2
        exit 64
    fi
    if ! discovered_real_root="$(cd "$discovered_root" 2>/dev/null && pwd -P)"; then
        echo "网页/后端发布的 Git 工作树无法验证。" >&2
        exit 64
    fi
    if [ "$expected_root" != "$discovered_real_root" ]; then
        echo "网页/后端发布必须指向 Git 工作树根目录。" >&2
        exit 64
    fi
    if ! status_output="$(git -C "$LOCAL_DIR" status --porcelain=v1 --untracked-files=all --ignore-submodules=none 2>/dev/null)"; then
        echo "网页/后端发布的 Git 工作树状态无法验证。" >&2
        exit 64
    fi
    if [ -n "$status_output" ]; then
        echo "网页/后端发布要求 Git 工作树保持干净，检测到待提交内容。" >&2
        exit 64
    fi
    if ! commit="$(git -C "$LOCAL_DIR" rev-parse --verify 'HEAD^{commit}' 2>/dev/null)" \
        || [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
        echo "网页/后端发布的 Git HEAD 无法验证。" >&2
        exit 64
    fi
    (umask 077; printf '%s\n' "$commit" > "$VERIFIED_COMMIT_FILE")
    chmod 0600 "$VERIFIED_COMMIT_FILE"
}

if [ "$DEPLOY_MODE" = "wechat_formal" ]; then
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
    if [ "$LOCAL_WEB_PRIVATE_FEATURES_ENABLED" != "1" ]; then
        echo "双端正式发布必须固定 WEB_PRIVATE_FEATURES_ENABLED=1。" >&2
        exit 64
    fi
    if [ "$LOCAL_FEATURE_STRUCTURED_LOGS" != "1" ]; then
        echo "微信正式发布必须固定 FEATURE_STRUCTURED_LOGS=1。" >&2
        exit 64
    fi
    FORMAL_WECHAT_CONFIG_ALLOWED="1"
else
    freeze_web_backend_commit
fi
if [ -n "$LOCAL_WEB_PRIVATE_FEATURES_ENABLED" ] \
    && [ "$LOCAL_WEB_PRIVATE_FEATURES_ENABLED" != "0" ] \
    && [ "$LOCAL_WEB_PRIVATE_FEATURES_ENABLED" != "1" ]; then
    echo "WEB_PRIVATE_FEATURES_ENABLED 只能是 0 或 1。" >&2
    exit 64
fi
if { [ -n "$LOCAL_WX_MINIPROGRAM_APPID" ] && [ -z "$LOCAL_WX_MINIPROGRAM_SECRET" ]; } \
    || { [ -z "$LOCAL_WX_MINIPROGRAM_APPID" ] && [ -n "$LOCAL_WX_MINIPROGRAM_SECRET" ]; }; then
    echo "WX_MINIPROGRAM_APPID 与 WX_MINIPROGRAM_SECRET 必须由同一次发布同时提供。" >&2
    exit 64
fi

validate_local_amap_migration_intent() {
    local key value lowered
    local configured=0

    for value in \
        "$LOCAL_AMAP_JS_API_KEY" \
        "$LOCAL_AMAP_WEB_SERVICE_KEY" \
        "$LOCAL_AMAP_SECURITY_JS_CODE"; do
        [ -n "$value" ] && configured=$((configured + 1))
    done
    if [ "$configured" -eq 0 ]; then
        return 0
    fi
    if [ "$configured" -ne 3 ]; then
        echo "高德凭据迁移必须一次同时提供 JS Key、Web 服务 Key 与安全密钥。" >&2
        exit 64
    fi
    for key in \
        AMAP_JS_API_KEY \
        AMAP_WEB_SERVICE_KEY \
        AMAP_SECURITY_JS_CODE; do
        case "$key" in
            AMAP_JS_API_KEY) value="$LOCAL_AMAP_JS_API_KEY" ;;
            AMAP_WEB_SERVICE_KEY) value="$LOCAL_AMAP_WEB_SERVICE_KEY" ;;
            AMAP_SECURITY_JS_CODE) value="$LOCAL_AMAP_SECURITY_JS_CODE" ;;
        esac
        lowered="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
        case "$lowered" in
            your-*|change-me*)
                echo "$key 仍是示例占位值，拒绝进入发布候选。" >&2
                exit 64
                ;;
        esac
        if [[ ! "$value" =~ ^[A-Za-z0-9_-]{20,100}$ ]]; then
            echo "$key 格式或长度异常。" >&2
            exit 64
        fi
    done
    if [ "$LOCAL_AMAP_JS_API_KEY" = "$LOCAL_AMAP_WEB_SERVICE_KEY" ]; then
        echo "AMAP_JS_API_KEY 与 AMAP_WEB_SERVICE_KEY 必须使用不同用途的 Key。" >&2
        exit 64
    fi
}

validate_local_amap_migration_intent

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
require_env_value "ML_MODEL_ARTIFACT_DIR" "$LOCAL_ML_MODEL_ARTIFACT_DIR"

# 私钥算法校验只读取已固定到本轮私有临时目录的快照。
validate_qweather_jwt_private_key_snapshot() {
    local snapshot="$1"
    local public_text=""

    if ! command -v openssl >/dev/null 2>&1; then
        echo "本机缺少 openssl，无法离线校验 QWeather JWT 私钥。" >&2
        return 64
    fi
    if ! public_text="$(openssl pkey -in "$snapshot" -text_pub -noout 2>/dev/null)" \
        || [[ "$public_text" != "ED25519 Public-Key:"* ]] \
        || ! openssl pkey -in "$snapshot" -check -noout >/dev/null 2>&1; then
        echo "QWeather JWT 私钥快照必须是有效的 Ed25519 私钥。" >&2
        return 64
    fi
}

# 源文件只打开一次；类型、权限、大小与复制都绑定同一个文件描述符。
# 后续算法校验和 SSH 传输只读取本轮私有临时目录中的同一份快照。
snapshot_qweather_jwt_private_key_source() {
    local source="$1"
    local snapshot="$LOCAL_DEPLOY_TEMP_DIR/qweather-jwt-private"

    if [ -z "$LOCAL_DEPLOY_TEMP_DIR" ] || [ ! -d "$LOCAL_DEPLOY_TEMP_DIR" ]; then
        echo "QWeather JWT 私钥快照目录尚未创建。" >&2
        return 64
    fi
    if [ -e "$snapshot" ] || [ -L "$snapshot" ]; then
        echo "QWeather JWT 私钥快照路径已被占用。" >&2
        return 64
    fi
    if QWEATHER_PRIVATE_KEY_SOURCE="$source" \
        QWEATHER_PRIVATE_KEY_SNAPSHOT="$snapshot" \
        python3 - <<'PY'
import errno
import os
import stat
import sys


MAX_PRIVATE_KEY_BYTES = 16 * 1024


class SnapshotError(Exception):
    pass


def fail(message):
    raise SnapshotError(message)


def fingerprint(file_stat):
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


source = os.environ.get('QWEATHER_PRIVATE_KEY_SOURCE', '')
snapshot = os.environ.get('QWEATHER_PRIVATE_KEY_SNAPSHOT', '')
source_descriptor = None
snapshot_descriptor = None
snapshot_created = False

try:
    if not os.path.isabs(source) or not os.path.isabs(snapshot):
        fail('QWeather JWT 私钥源与快照必须使用绝对路径。')
    if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_CLOEXEC'):
        fail('本机缺少安全打开私钥所需的系统能力。')
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EISDIR}:
            fail('QWeather JWT 私钥源必须是普通非符号链接文件。')
        fail('QWeather JWT 私钥源当前无法安全读取。')

    before = os.fstat(source_descriptor)
    if not stat.S_ISREG(before.st_mode):
        fail('QWeather JWT 私钥源必须是普通非符号链接文件。')
    if stat.S_IMODE(before.st_mode) != 0o600:
        fail('QWeather JWT 私钥源权限必须精确为 0600。')
    if before.st_size <= 0 or before.st_size > MAX_PRIVATE_KEY_BYTES:
        fail('QWeather JWT 私钥源大小异常。')

    old_umask = os.umask(0o077)
    try:
        snapshot_descriptor = os.open(
            snapshot,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        snapshot_created = True
    except FileExistsError:
        fail('QWeather JWT 私钥快照路径已被占用。')
    except OSError:
        fail('QWeather JWT 私钥无法创建本轮安全快照。')
    finally:
        os.umask(old_umask)

    total = 0
    while True:
        chunk = os.read(
            source_descriptor,
            min(8192, MAX_PRIVATE_KEY_BYTES + 1 - total),
        )
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_PRIVATE_KEY_BYTES:
            fail('QWeather JWT 私钥源大小异常。')
        view = memoryview(chunk)
        while view:
            written = os.write(snapshot_descriptor, view)
            if written <= 0:
                fail('QWeather JWT 私钥快照写入失败。')
            view = view[written:]

    after = os.fstat(source_descriptor)
    snapshot_stat = os.fstat(snapshot_descriptor)
    if total != before.st_size or fingerprint(before) != fingerprint(after):
        fail('QWeather JWT 私钥源读取期间发生变化。')
    if (
        not stat.S_ISREG(snapshot_stat.st_mode)
        or stat.S_IMODE(snapshot_stat.st_mode) != 0o600
        or snapshot_stat.st_size != total
    ):
        fail('QWeather JWT 私钥快照状态异常。')
    os.fsync(snapshot_descriptor)
except SnapshotError as error:
    print(str(error), file=sys.stderr)
    if snapshot_created:
        try:
            os.unlink(snapshot)
        except OSError:
            pass
    raise SystemExit(64) from None
except OSError:
    if snapshot_created:
        try:
            os.unlink(snapshot)
        except OSError:
            pass
    print('QWeather JWT 私钥快照复制失败。', file=sys.stderr)
    raise SystemExit(64) from None
finally:
    if snapshot_descriptor is not None:
        os.close(snapshot_descriptor)
    if source_descriptor is not None:
        os.close(source_descriptor)
PY
    then
        :
    else
        local snapshot_status=$?
        return "$snapshot_status"
    fi
    if validate_qweather_jwt_private_key_snapshot "$snapshot"; then
        :
    else
        local validation_status=$?
        rm -f -- "$snapshot"
        return "$validation_status"
    fi
    LOCAL_QWEATHER_JWT_PRIVATE_KEY_SNAPSHOT="$snapshot"
    IFS=' ' read -r \
        LOCAL_QWEATHER_JWT_PRIVATE_KEY_SHA256 \
        LOCAL_QWEATHER_JWT_PRIVATE_KEY_SIZE < <(
        python3 - "$snapshot" <<'PY'
import hashlib
from pathlib import Path
import sys

payload = Path(sys.argv[1]).read_bytes()
print(hashlib.sha256(payload).hexdigest(), len(payload))
PY
    )
    if [[ ! "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_SHA256" =~ ^[0-9a-f]{64}$ ]] \
        || [[ ! "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_SIZE" =~ ^[1-9][0-9]*$ ]]; then
        echo "无法固定 QWeather JWT 私钥快照摘要。" >&2
        rm -f -- "$snapshot"
        LOCAL_QWEATHER_JWT_PRIVATE_KEY_SNAPSHOT=""
        return 64
    fi
}

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
        echo "1.1.1 微信正式发布必须固定 FEATURE_WXPUSHER=0。" >&2
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
    if [ -z "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_SOURCE" ]; then
        echo "微信正式 JWT 发布必须提供本机 QWEATHER_JWT_PRIVATE_KEY_SOURCE。" >&2
        exit 64
    fi
    snapshot_qweather_jwt_private_key_source "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_SOURCE" || exit $?
elif [ -n "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_SOURCE" ]; then
    if [ "$DEPLOY_MODE" = "web_backend_only" ]; then
        echo "web_backend_only 不轮换 QWeather 私钥；请清空 QWEATHER_JWT_PRIVATE_KEY_SOURCE 并复用服务器现有运行态私钥。" >&2
        exit 64
    fi
    if [ "$LOCAL_QWEATHER_AUTH_MODE" != "jwt" ]; then
        echo "QWEATHER_JWT_PRIVATE_KEY_SOURCE 只能与 QWEATHER_AUTH_MODE=jwt 同时使用。" >&2
        exit 64
    fi
    snapshot_qweather_jwt_private_key_source "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_SOURCE" || exit $?
fi

validate_remote_path() {
    local name="$1"
    local value="$2"
    if [[ "$value" != /* || "$value" = "/" || ! "$value" =~ ^[A-Za-z0-9._/-]+$ ]]; then
        echo "$name 必须是安全的规范绝对路径。" >&2
        exit 1
    fi
    case "$value" in
        *//*|*/./*|*/../*|*/.|*/..|*/)
            echo "$name 不得包含重复斜杠、点路径段或尾斜杠。" >&2
            exit 1
            ;;
    esac
}

validate_remote_path "DEPLOY_PROJECT_DIR" "$PROJECT_DIR"
validate_remote_path "DEPLOY_RELEASE_ROOT" "$RELEASE_ROOT"
if [ "$LOCAL_QWEATHER_AUTH_MODE" = "jwt" ]; then
    validate_remote_path "QWEATHER_JWT_PRIVATE_KEY_PATH" "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH"
    case "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH" in
        "$REMOTE_QWEATHER_PRIVATE_DIR"/*)
            qweather_key_name="${LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH#"$REMOTE_QWEATHER_PRIVATE_DIR"/}"
            case "$qweather_key_name" in
                ''|.|..|*/*)
                    echo "QWEATHER_JWT_PRIVATE_KEY_PATH 必须是 DEPLOY_PROJECT_DIR/private/ 下的直接文件。" >&2
                    exit 64
                    ;;
            esac
            ;;
        *)
            echo "QWEATHER_JWT_PRIVATE_KEY_PATH 必须位于 DEPLOY_PROJECT_DIR/private/。" >&2
            exit 64
            ;;
    esac
fi
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
if [ "${#RELEASE_ID}" -gt 64 ]; then
    echo "DEPLOY_RELEASE_ID 最长为 64 个字符。" >&2
    exit 1
fi
REMOTE_QWEATHER_PENDING_KEY_PATH="$REMOTE_QWEATHER_PRIVATE_DIR/.qweather-jwt.pending-$RELEASE_ID"
validate_remote_path "QWEATHER 私钥待激活路径" "$REMOTE_QWEATHER_PENDING_KEY_PATH"
if [ "$LOCAL_QWEATHER_AUTH_MODE" = "jwt" ] \
    && [ "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH" = "$REMOTE_QWEATHER_PENDING_KEY_PATH" ]; then
    echo "QWEATHER_JWT_PRIVATE_KEY_PATH 不得占用本轮待激活私钥路径。" >&2
    exit 64
fi
if [ "$DEPLOY_MODE" = "wechat_formal" ]; then
    ACTIVATION_QWEATHER_PENDING_KEY_PATH="$REMOTE_QWEATHER_PENDING_KEY_PATH"
    REMOTE_QWEATHER_VALIDATION_PENDING_ARG="--qweather-private-key-pending-path $REMOTE_QWEATHER_PENDING_KEY_PATH"
fi

RELEASE_SOURCE_DIR="$LOCAL_DIR"
LOCAL_RELEASE_EXPORT_DIR=""

# 两种远端模式都只上传冻结提交快照，避免 rsync 在校验后继续读取可变化的工作目录。
prepare_release_source() {
    if [ -z "$VERIFIED_COMMIT_FILE" ] || [ ! -f "$VERIFIED_COMMIT_FILE" ]; then
        echo "远端发布缺少同一次校验生成的目标提交票据。" >&2
        exit 64
    fi
    IFS= read -r VERIFIED_COMMIT < "$VERIFIED_COMMIT_FILE"
    if [[ ! "$VERIFIED_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
        echo "远端发布的目标提交票据格式异常。" >&2
        exit 64
    fi
    if ! VERIFIED_RELEASE_BRANCH="$(
        git -C "$LOCAL_DIR" symbolic-ref --quiet --short HEAD 2>/dev/null
    )"; then
        echo "远端发布要求使用 main 或 codex/* 命名分支，不能从 detached HEAD 发布。" >&2
        exit 64
    fi
    case "$VERIFIED_RELEASE_BRANCH" in
        main|codex/*) ;;
        *)
            echo "远端发布分支只能是 main 或 codex/*。" >&2
            exit 64
            ;;
    esac
    LOCAL_RELEASE_EXPORT_DIR="$LOCAL_DEPLOY_TEMP_DIR/release-source"
    mkdir -m 0700 "$LOCAL_RELEASE_EXPORT_DIR"
    git -C "$LOCAL_DIR" archive --format=tar "$VERIFIED_COMMIT" \
        | tar -xf - -C "$LOCAL_RELEASE_EXPORT_DIR"
    RELEASE_SOURCE_DIR="$LOCAL_RELEASE_EXPORT_DIR"
}

prepare_model_artifacts() {
    local helper="$RELEASE_SOURCE_DIR/scripts/model_artifact.py"
    local manifest="$RELEASE_SOURCE_DIR/models/feature_config.json"
    LOCAL_ML_MODEL_ARTIFACT_SNAPSHOT_DIR="$LOCAL_DEPLOY_TEMP_DIR/model-artifacts"

    if [ ! -f "$helper" ] || [ ! -f "$manifest" ]; then
        echo "冻结发布快照缺少模型制品校验器或清单。" >&2
        exit 64
    fi
    python3 "$helper" snapshot \
        --source-dir "$LOCAL_ML_MODEL_ARTIFACT_DIR" \
        --manifest "$manifest" \
        --output-dir "$LOCAL_ML_MODEL_ARTIFACT_SNAPSHOT_DIR" \
        --receipt "$LOCAL_ML_MODEL_ARTIFACT_RECEIPT" \
        --commit "$VERIFIED_COMMIT"
}

verify_miniprogram_release_proof() {
    local verifier="$RELEASE_SOURCE_DIR/scripts/verify_github_ci.py"
    if [ ! -f "$verifier" ]; then
        echo "冻结发布快照缺少 GitHub CI 校验器。" >&2
        exit 64
    fi
    if [ -s "$LOCAL_MINIPROGRAM_CI_PROOF_FILE" ]; then
        return 0
    fi
    python3 "$verifier" verify-online \
        --repo AlfWuxy/weather-Web \
        --workflow .github/workflows/miniprogram.yml \
        --commit "$VERIFIED_COMMIT" \
        --branch "$VERIFIED_RELEASE_BRANCH" \
        --proof-job "小程序可发布提交证明" \
        --output "$LOCAL_MINIPROGRAM_CI_PROOF_FILE"
}

verify_github_release_proofs() {
    local verifier="$RELEASE_SOURCE_DIR/scripts/verify_github_ci.py"
    if [ ! -f "$verifier" ]; then
        echo "冻结发布快照缺少 GitHub CI 校验器。" >&2
        exit 64
    fi
    python3 "$verifier" verify-online \
        --repo AlfWuxy/weather-Web \
        --workflow .github/workflows/ci.yml \
        --commit "$VERIFIED_COMMIT" \
        --branch "$VERIFIED_RELEASE_BRANCH" \
        --proof-job "可发布提交证明" \
        --output "$LOCAL_CI_PROOF_FILE"
    # 两种发布模式都在首次 SSH 前冻结同一提交的小程序证明；
    # 候选有效正式态再决定是否上传并强制激活复核。
    verify_miniprogram_release_proof
}

prepare_release_source
prepare_model_artifacts
verify_github_release_proofs

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

# 文件内容直接作为 SSH stdin，避免私钥进入 shell 变量、命令参数或日志。
remote_exec_with_file_stdin() {
    local local_file="$1"
    local remote_command="$2"

    if use_sshpass && [ -n "${SSHPASS:-}" ]; then
        SSHPASS="${SSHPASS:-$PASSWORD}" sshpass -e ssh $SSH_OPTS "$USER@$SERVER" "$remote_command" < "$local_file"
        return
    fi

    if [ -n "${SSHPASS:-}" ]; then
        echo "安全传输私钥需要 sshpass；也可以清空 DEPLOY_PASSWORD 后使用 SSH Key。" >&2
        return 64
    fi

    ssh $SSH_OPTS "$USER@$SERVER" "$remote_command" < "$local_file"
}

# 发布收据只通过 stdin 进入新 release 的 root 私有 metadata。
upload_private_metadata_receipt() {
    local local_file="$1"
    local remote_name="$2"
    case "$remote_name" in
        ci-proof.json|miniprogram-ci-proof.json|model-artifacts.json) ;;
        *)
            echo "私有发布收据文件名不在允许清单中。" >&2
            exit 64
            ;;
    esac
    if [ ! -f "$local_file" ]; then
        echo "本机缺少待上传的发布收据: $remote_name" >&2
        exit 64
    fi
    remote_exec_with_file_stdin "$local_file" "set -eu
umask 077
TARGET=$NEW_RELEASE/private-metadata/$remote_name
[ ! -e \"\$TARGET\" ] && [ ! -L \"\$TARGET\" ] || {
    echo '候选发布收据目标已存在，拒绝覆盖。' >&2
    exit 1
}
cat > \"\$TARGET\"
chown root:root \"\$TARGET\"
chmod 0600 \"\$TARGET\""
}

# 预激活私钥管理器只在服务端 root 私有目录中工作。它先落盘清单，再暴露 pending 名称；
# 任一失败只做身份绑定的原子归档，激活事务已经写入 plan 后不会触碰私钥对象。
qweather_preactivation_manager_source() {
    command cat <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


MAX_KEY_BYTES = 16 * 1024
MAX_CONTROL_BYTES = 32 * 1024
MANIFEST_KEYS = {
    'version', 'release_id', 'pending_path', 'final_path', 'sha256',
    'pending_device', 'pending_inode', 'pending_nlink', 'pending_size',
}


class StateError(Exception):
    pass


def fail(message):
    raise StateError(message)


def plain_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def exists(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        fail('服务端预激活路径无法安全读取。')
    return True


def fingerprint(file_stat):
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_uid,
        file_stat.st_gid,
        file_stat.st_nlink,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_directory(path, *, uid, gid=None, modes=None):
    try:
        file_stat = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        fail('服务端预激活目录无法安全验证。')
    if (
        not stat.S_ISDIR(file_stat.st_mode)
        or stat.S_ISLNK(file_stat.st_mode)
        or resolved != path
        or file_stat.st_uid != uid
        or (gid is not None and file_stat.st_gid != gid)
        or (modes is not None and stat.S_IMODE(file_stat.st_mode) not in modes)
    ):
        fail('服务端预激活目录身份或权限异常。')
    return file_stat


def create_private_directory(path, *, uid, gid):
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    current = path.lstat()
    if current.st_uid != uid or current.st_gid != gid:
        os.chown(path, uid, gid)
    require_directory(path, uid=uid, gid=gid, modes={0o700})
    fsync_directory(path.parent)


def stable_read(
    path,
    *,
    uid,
    gid,
    mode,
    nlink=1,
    max_bytes=MAX_CONTROL_BYTES,
    allow_empty=False,
):
    no_follow = getattr(os, 'O_NOFOLLOW', None)
    if no_follow is None:
        fail('服务端缺少安全文件打开能力。')
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | no_follow | getattr(os, 'O_CLOEXEC', 0),
        )
    except OSError:
        fail('服务端预激活文件无法安全打开。')
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_uid != uid
            or before.st_gid != gid
            or before.st_nlink != nlink
            or before.st_size < 0
            or (before.st_size == 0 and not allow_empty)
            or before.st_size > max_bytes
        ):
            fail('服务端预激活文件身份、权限、链接数或大小异常。')
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(8192, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                fail('服务端预激活文件大小异常。')
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if total != before.st_size or fingerprint(before) != fingerprint(after):
            fail('服务端预激活文件读取期间发生变化。')
        return b''.join(chunks), before
    finally:
        os.close(descriptor)


def write_all(descriptor, payload):
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            fail('服务端预激活文件写入失败。')
        view = view[written:]


def write_file_exclusive_atomic(path, payload, *, uid, gid):
    if exists(path):
        fail('服务端预激活原子发布目标已存在。')
    temporary = path.parent / (
        f'.atomic-{path.name}-{os.urandom(16).hex()}'
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        current = os.fstat(descriptor)
        if current.st_uid != uid or current.st_gid != gid:
            os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, 0o600)
        write_all(descriptor, payload)
        os.fsync(descriptor)
        completed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(completed.st_mode)
            or stat.S_IMODE(completed.st_mode) != 0o600
            or completed.st_uid != uid
            or completed.st_gid != gid
            or completed.st_nlink != 1
            or completed.st_size != len(payload)
        ):
            fail('服务端预激活临时文件状态异常。')
    finally:
        os.close(descriptor)
    if exists(path):
        fail('服务端预激活原子发布目标发生并发变化。')
    os.replace(temporary, path)
    fsync_directory(path.parent)
    published, published_stat = stable_read(
        path,
        uid=uid,
        gid=gid,
        mode=0o600,
        nlink=1,
        max_bytes=max(MAX_CONTROL_BYTES, MAX_KEY_BYTES),
        allow_empty=True,
    )
    if published != payload or (
        published_stat.st_dev,
        published_stat.st_ino,
    ) != (completed.st_dev, completed.st_ino):
        fail('服务端预激活原子发布身份异常。')
    return published_stat


def write_json_exclusive(path, payload, *, uid, gid):
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n'
    ).encode('utf-8')
    return write_file_exclusive_atomic(path, encoded, uid=uid, gid=gid)


def append_event(transaction, event, *, uid, gid):
    path = transaction / 'events.jsonl'
    payload = (
        json.dumps(event, sort_keys=True, separators=(',', ':')) + '\n'
    ).encode('utf-8')
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_uid != uid
            or current.st_gid != gid
            or current.st_nlink != 1
        ):
            fail('服务端预激活事件日志身份或权限异常。')
        write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(transaction)


def validate_key_path(path, private_dir, release_id, *, pending):
    if not path.is_absolute() or Path(os.path.normpath(path)) != path:
        fail('服务端 QWeather 私钥路径不规范。')
    if path.parent != private_dir:
        fail('服务端 QWeather 私钥不在固定私有目录。')
    if pending:
        if path.name != f'.qweather-jwt.pending-{release_id}':
            fail('服务端 QWeather pending 命名与 release 不一致。')
    elif not path.name or path.name.startswith('.qweather-jwt.pending-'):
        fail('服务端 QWeather final 命名异常。')


def ensure_context(*, create_private=False, create_preactivation=False):
    project_stat = require_directory(project_root, uid=owner_uid)
    if stat.S_IMODE(project_stat.st_mode) & 0o022:
        fail('服务端项目目录允许非 root 写入。')
    release_stat = require_directory(release_root, uid=owner_uid)
    if stat.S_IMODE(release_stat.st_mode) & 0o022:
        fail('服务端发布目录允许非 root 写入。')
    backups = project_root / 'backups'
    require_directory(backups, uid=owner_uid, gid=owner_gid, modes={0o700})
    if preactivation_root != backups / 'qweather-preactivation':
        fail('服务端 QWeather 预激活事务根路径异常。')
    if not exists(preactivation_root) and not create_preactivation:
        return False
    if private_dir != project_root / 'private':
        fail('服务端 QWeather 私钥目录不在固定位置。')
    if not exists(private_dir):
        if not create_private:
            fail('服务端 QWeather 私钥目录缺失。')
        create_private_directory(private_dir, uid=owner_uid, gid=owner_gid)
    private_stat = require_directory(
        private_dir,
        uid=owner_uid,
        modes={0o700, 0o750},
    )
    private_state = (private_stat.st_gid, stat.S_IMODE(private_stat.st_mode))
    if private_state not in {(owner_gid, 0o700), (runtime_gid, 0o750)}:
        fail('服务端 QWeather 私钥目录属组或权限异常。')
    if not exists(preactivation_root):
        create_private_directory(
            preactivation_root,
            uid=owner_uid,
            gid=owner_gid,
        )
    require_directory(
        preactivation_root,
        uid=owner_uid,
        gid=owner_gid,
        modes={0o700},
    )
    return True


def ensure_transaction(release_id_value, *, create=False):
    transaction = preactivation_root / release_id_value
    if not exists(transaction):
        if not create:
            return None
        create_private_directory(transaction, uid=owner_uid, gid=owner_gid)
    require_directory(
        transaction,
        uid=owner_uid,
        gid=owner_gid,
        modes={0o700},
    )
    if transaction.parent.resolve(strict=True) != preactivation_root:
        fail('服务端 QWeather 预激活事务路径越界。')
    return transaction


def validate_transaction_children(transaction, *, allow_unproven=False):
    allowed = {
        'manifest.json',
        'events.jsonl',
        'source.pem',
        'qweather-key-recovery',
    }
    if allow_unproven:
        allowed.add('UNPROVEN_ARCHIVED.json')
    for child in transaction.iterdir():
        if child.name not in allowed:
            fail('服务端 QWeather 预激活事务含未知对象。')


def load_json_file(path):
    content, _file_stat = stable_read(
        path,
        uid=owner_uid,
        gid=owner_gid,
        mode=0o600,
        nlink=1,
        max_bytes=MAX_CONTROL_BYTES,
    )
    try:
        return json.loads(content)
    except (UnicodeDecodeError, ValueError):
        fail('服务端 QWeather 预激活清单无法解析。')


def load_manifest(transaction, *, expected_release=None):
    manifest_path = transaction / 'manifest.json'
    if not exists(manifest_path):
        return None
    content, _manifest_stat = stable_read(
        manifest_path,
        uid=owner_uid,
        gid=owner_gid,
        mode=0o600,
        nlink=1,
        max_bytes=MAX_CONTROL_BYTES,
        allow_empty=True,
    )
    try:
        manifest = json.loads(content)
    except (UnicodeDecodeError, ValueError):
        if recover_incomplete_manifest(transaction, content):
            return None
        fail('服务端 QWeather 预激活清单无法解析。')
    if set(manifest) != MANIFEST_KEYS or manifest.get('version') != 1:
        fail('服务端 QWeather 预激活清单结构异常。')
    release_value = manifest.get('release_id')
    if (
        not isinstance(release_value, str)
        or not re.fullmatch(r'[A-Za-z0-9._-]+', release_value)
        or transaction.name != release_value
        or (expected_release is not None and release_value != expected_release)
    ):
        fail('服务端 QWeather 预激活 release 绑定异常。')
    manifest_pending = Path(manifest.get('pending_path', ''))
    manifest_final = Path(manifest.get('final_path', ''))
    validate_key_path(manifest_pending, private_dir, release_value, pending=True)
    validate_key_path(manifest_final, private_dir, release_value, pending=False)
    digest = manifest.get('sha256')
    if (
        manifest_pending == manifest_final
        or not isinstance(digest, str)
        or not re.fullmatch(r'[0-9a-f]{64}', digest)
        or not plain_int(manifest.get('pending_device'))
        or not plain_int(manifest.get('pending_inode'))
        or manifest.get('pending_nlink') != 1
        or not plain_int(manifest.get('pending_size'))
        or manifest.get('pending_size') <= 0
        or manifest.get('pending_size') > MAX_KEY_BYTES
    ):
        fail('服务端 QWeather 预激活清单字段异常。')
    validate_transaction_children(transaction, allow_unproven=True)
    return manifest


def verify_manifest_key(path, manifest):
    payload, file_stat = stable_read(
        path,
        uid=owner_uid,
        gid=owner_gid,
        mode=0o600,
        nlink=1,
        max_bytes=MAX_KEY_BYTES,
    )
    if (
        hashlib.sha256(payload).hexdigest() != manifest['sha256']
        or file_stat.st_size != manifest['pending_size']
        or (file_stat.st_dev, file_stat.st_ino)
        != (manifest['pending_device'], manifest['pending_inode'])
    ):
        fail('服务端 QWeather 预激活私钥与耐久清单不一致。')
    return payload, file_stat


def iter_activation_plans():
    if not exists(activation_root):
        return
    require_directory(
        activation_root,
        uid=owner_uid,
        gid=owner_gid,
        modes={0o700},
    )
    for transaction in sorted(activation_root.iterdir()):
        require_directory(
            transaction,
            uid=owner_uid,
            gid=owner_gid,
            modes={0o700},
        )
        plan_path = transaction / 'qweather-key-transition.json'
        if not exists(plan_path):
            continue
        plan = load_json_file(plan_path)
        if not isinstance(plan, dict):
            fail('激活私钥计划结构异常，无法安全回收预激活私钥。')
        yield plan


def activation_adopted(manifest):
    matches = []
    for plan in iter_activation_plans() or ():
        if plan.get('pending_path') != manifest['pending_path']:
            continue
        if (
            plan.get('version') != 2
            or plan.get('release_id') != manifest['release_id']
            or plan.get('final_path') != manifest['final_path']
            or plan.get('sha256') != manifest['sha256']
            or plan.get('pending_device') != manifest['pending_device']
            or plan.get('pending_inode') != manifest['pending_inode']
            or plan.get('pending_nlink') != 1
            or plan.get('pending_size') != manifest['pending_size']
        ):
            fail('激活私钥计划与预激活清单错配，拒绝触碰私钥。')
        matches.append(plan)
    if len(matches) > 1:
        fail('多个激活私钥计划声明同一 pending，拒绝触碰私钥。')
    return bool(matches)


def ensure_recovery_directory(transaction):
    recovery = transaction / 'qweather-key-recovery'
    if not exists(recovery):
        create_private_directory(recovery, uid=owner_uid, gid=owner_gid)
    require_directory(
        recovery,
        uid=owner_uid,
        gid=owner_gid,
        modes={0o700},
    )
    validate_recovery_evidence(recovery)
    return recovery


def validate_recovery_evidence(recovery):
    evidence_pattern = re.compile(
        r'evidence-(?:temp-source|temp-manifest|temp-marker|'
        r'partial-source|partial-manifest|partial-record)-[0-9]+-[0-9]+\.bin'
    )
    for child in recovery.iterdir():
        if child.name in {'pending.pem', 'unproven.pem'}:
            continue
        if not evidence_pattern.fullmatch(child.name):
            fail('服务端 QWeather 私钥恢复目录含未知对象。')
        stable_read(
            child,
            uid=owner_uid,
            gid=owner_gid,
            mode=0o600,
            nlink=1,
            max_bytes=MAX_CONTROL_BYTES,
            allow_empty=True,
        )


def activation_claims_context(transaction):
    expected_pending = str(
        private_dir / f'.qweather-jwt.pending-{transaction.name}'
    )
    for plan in iter_activation_plans() or ():
        if (
            plan.get('release_id') == transaction.name
            or plan.get('pending_path') == expected_pending
        ):
            return True
    return False


def quarantine_incomplete(transaction, path, kind):
    if activation_claims_context(transaction):
        fail('激活计划已接管本轮私钥，拒绝隔离预激活文件。')
    allowed_parents = {transaction}
    recovery_path = transaction / 'qweather-key-recovery'
    if exists(recovery_path):
        require_directory(
            recovery_path,
            uid=owner_uid,
            gid=owner_gid,
            modes={0o700},
        )
        allowed_parents.add(recovery_path)
    if path.parent not in allowed_parents:
        fail('预激活半文件不在固定事务路径。')
    payload, file_stat = stable_read(
        path,
        uid=owner_uid,
        gid=owner_gid,
        mode=0o600,
        nlink=1,
        max_bytes=MAX_CONTROL_BYTES,
        allow_empty=True,
    )
    recovery = ensure_recovery_directory(transaction)
    destination = recovery / (
        f'evidence-{kind}-{file_stat.st_dev}-{file_stat.st_ino}.bin'
    )
    if exists(destination):
        fail('预激活半文件证据目标已存在。')
    os.replace(path, destination)
    fsync_directory(path.parent)
    if path.parent != recovery:
        fsync_directory(recovery)
    preserved, preserved_stat = stable_read(
        destination,
        uid=owner_uid,
        gid=owner_gid,
        mode=0o600,
        nlink=1,
        max_bytes=MAX_CONTROL_BYTES,
        allow_empty=True,
    )
    if preserved != payload or (
        preserved_stat.st_dev,
        preserved_stat.st_ino,
    ) != (file_stat.st_dev, file_stat.st_ino):
        fail('预激活半文件证据身份异常。')
    return payload, file_stat


def reconcile_atomic_temps(transaction):
    pattern = re.compile(
        r'\.atomic-(source\.pem|manifest\.json|'
        r'UNPROVEN_ARCHIVED\.json)-[0-9a-f]{32}'
    )
    kind_by_name = {
        'source.pem': 'temp-source',
        'manifest.json': 'temp-manifest',
        'UNPROVEN_ARCHIVED.json': 'temp-marker',
    }
    for child in sorted(transaction.iterdir()):
        match = pattern.fullmatch(child.name)
        if match is None:
            continue
        canonical = transaction / match.group(1)
        if exists(canonical):
            fail('预激活临时文件与 canonical 文件同时存在。')
        quarantine_incomplete(
            transaction,
            child,
            kind_by_name[match.group(1)],
        )
    validate_transaction_children(transaction, allow_unproven=True)
    recovery = transaction / 'qweather-key-recovery'
    if exists(recovery):
        require_directory(
            recovery,
            uid=owner_uid,
            gid=owner_gid,
            modes={0o700},
        )
        validate_recovery_evidence(recovery)


def recover_incomplete_manifest(transaction, content):
    source = transaction / 'source.pem'
    expected_pending = private_dir / f'.qweather-jwt.pending-{transaction.name}'
    recovery_pending = transaction / 'qweather-key-recovery' / 'pending.pem'
    recovery_unproven = transaction / 'qweather-key-recovery' / 'unproven.pem'
    if (
        not exists(source)
        or exists(expected_pending)
        or exists(recovery_pending)
        or exists(recovery_unproven)
    ):
        return False
    source_payload, source_stat = stable_read(
        source,
        uid=owner_uid,
        gid=owner_gid,
        mode=0o600,
        nlink=1,
        max_bytes=MAX_KEY_BYTES,
    )
    expected = (
        json.dumps(
            {
                'version': 1,
                'release_id': transaction.name,
                'pending_path': str(expected_pending),
                'final_path': str(final_path),
                'sha256': hashlib.sha256(source_payload).hexdigest(),
                'pending_device': source_stat.st_dev,
                'pending_inode': source_stat.st_ino,
                'pending_nlink': 1,
                'pending_size': source_stat.st_size,
            },
            sort_keys=True,
            separators=(',', ':'),
        ) + '\n'
    ).encode('utf-8')
    if len(content) >= len(expected) or not expected.startswith(content):
        return False
    quarantine_incomplete(
        transaction,
        transaction / 'manifest.json',
        'partial-manifest',
    )
    return True


def manifest_location(transaction, manifest):
    source = transaction / 'source.pem'
    pending = Path(manifest['pending_path'])
    recovery_directory = transaction / 'qweather-key-recovery'
    recovery = recovery_directory / 'pending.pem'
    if exists(recovery_directory):
        require_directory(
            recovery_directory,
            uid=owner_uid,
            gid=owner_gid,
            modes={0o700},
        )
        validate_recovery_evidence(recovery_directory)
    present = [path for path in (source, pending, recovery) if exists(path)]
    if len(present) != 1:
        fail('服务端 QWeather 预激活私钥位置不唯一。')
    verify_manifest_key(present[0], manifest)
    if present[0] == source:
        return 'source', source
    if present[0] == pending:
        return 'pending', pending
    return 'recovery', recovery


def load_unproven(transaction):
    marker = transaction / 'UNPROVEN_ARCHIVED.json'
    source = transaction / 'source.pem'
    recovery_directory = transaction / 'qweather-key-recovery'
    recovery = recovery_directory / 'unproven.pem'
    if not exists(marker) and not exists(recovery):
        return None
    if exists(recovery_directory):
        require_directory(
            recovery_directory,
            uid=owner_uid,
            gid=owner_gid,
            modes={0o700},
        )
        validate_recovery_evidence(recovery_directory)
    present = [path for path in (source, recovery) if exists(path)]
    if len(present) != 1:
        fail('未完成清单的预激活私钥归档状态不完整。')
    current = present[0]
    payload, file_stat = stable_read(
        current,
        uid=owner_uid,
        gid=owner_gid,
        mode=0o600,
        nlink=1,
        max_bytes=MAX_KEY_BYTES,
        allow_empty=True,
    )
    if not exists(marker):
        if current != recovery:
            return None
        write_json_exclusive(
            marker,
            {
                'version': 1,
                'release_id': transaction.name,
                'sha256': hashlib.sha256(payload).hexdigest(),
                'device': file_stat.st_dev,
                'inode': file_stat.st_ino,
                'size': file_stat.st_size,
            },
            uid=owner_uid,
            gid=owner_gid,
        )
    record = load_json_file(marker)
    required = {'version', 'release_id', 'sha256', 'device', 'inode', 'size'}
    if set(record) != required or record.get('version') != 1:
        fail('未完成清单的预激活私钥归档记录异常。')
    if (
        record.get('release_id') != transaction.name
        or record.get('sha256') != hashlib.sha256(payload).hexdigest()
        or record.get('device') != file_stat.st_dev
        or record.get('inode') != file_stat.st_ino
        or record.get('size') != file_stat.st_size
    ):
        fail('未完成清单的预激活私钥归档身份异常。')
    return payload, file_stat, current


def archive_unproven(transaction):
    validate_transaction_children(transaction, allow_unproven=True)
    pending_candidate = private_dir / f'.qweather-jwt.pending-{transaction.name}'
    if exists(pending_candidate):
        fail('发现没有耐久清单的 pending，拒绝自动触碰。')
    existing = load_unproven(transaction)
    if existing is not None:
        return 'unproven-archived'
    source = transaction / 'source.pem'
    if not exists(source):
        return 'empty'
    payload, file_stat = stable_read(
        source,
        uid=owner_uid,
        gid=owner_gid,
        mode=0o600,
        nlink=1,
        max_bytes=MAX_KEY_BYTES,
        allow_empty=True,
    )
    recovery = ensure_recovery_directory(transaction)
    destination = recovery / 'unproven.pem'
    if exists(destination):
        fail('未完成清单的归档位置已被占用。')
    os.replace(source, destination)
    fsync_directory(transaction)
    fsync_directory(recovery)
    write_json_exclusive(
        transaction / 'UNPROVEN_ARCHIVED.json',
        {
            'version': 1,
            'release_id': transaction.name,
            'sha256': hashlib.sha256(payload).hexdigest(),
            'device': file_stat.st_dev,
            'inode': file_stat.st_ino,
            'size': file_stat.st_size,
        },
        uid=owner_uid,
        gid=owner_gid,
    )
    return 'unproven-archived'


def archive_transaction(transaction):
    reconcile_atomic_temps(transaction)
    manifest = load_manifest(transaction)
    if manifest is None:
        return archive_unproven(transaction)
    if activation_adopted(manifest):
        return 'activation-adopted'
    location, source = manifest_location(transaction, manifest)
    if location == 'recovery':
        return 'archived'
    recovery = ensure_recovery_directory(transaction)
    destination = recovery / 'pending.pem'
    if exists(destination):
        fail('服务端 QWeather 预激活归档位置已被占用。')
    os.replace(source, destination)
    fsync_directory(source.parent)
    fsync_directory(recovery)
    append_event(
        transaction,
        {'event': 'archived', 'from': location},
        uid=owner_uid,
        gid=owner_gid,
    )
    verify_manifest_key(destination, manifest)
    return 'archived'


def read_secret_input():
    chunks = []
    total = 0
    while True:
        chunk = os.read(3, min(8192, MAX_KEY_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_KEY_BYTES:
            fail('QWeather JWT 私钥传输大小异常。')
        chunks.append(chunk)
    payload = b''.join(chunks)
    if not payload:
        fail('QWeather JWT 私钥传输为空。')
    if (
        len(payload) != expected_input_size
        or hashlib.sha256(payload).hexdigest() != expected_input_digest
    ):
        fail('QWeather JWT 私钥传输不完整。')
    return payload


def validate_existing_final(payload):
    if not exists(final_path):
        return
    final_payload, _file_stat = stable_read(
        final_path,
        uid=owner_uid,
        gid=runtime_gid,
        mode=0o640,
        nlink=1,
        max_bytes=MAX_KEY_BYTES,
    )
    if final_payload != payload:
        fail('QWeather 私钥目标内容不同，停止发布且不覆盖。')


def write_source(transaction, payload):
    source = transaction / 'source.pem'
    write_file_exclusive_atomic(
        source,
        payload,
        uid=owner_uid,
        gid=owner_gid,
    )
    return source


def provision():
    payload = read_secret_input()
    digest = hashlib.sha256(payload).hexdigest()
    ensure_context(create_private=True, create_preactivation=True)
    validate_key_path(pending_path, private_dir, release_id, pending=True)
    validate_key_path(final_path, private_dir, release_id, pending=False)
    if pending_path == final_path:
        fail('QWeather pending 与 final 路径相同。')
    validate_existing_final(payload)
    transaction = ensure_transaction(release_id, create=True)
    reconcile_atomic_temps(transaction)
    manifest = load_manifest(transaction, expected_release=release_id)
    if manifest is not None:
        if (
            manifest['pending_path'] != str(pending_path)
            or manifest['final_path'] != str(final_path)
            or manifest['sha256'] != digest
            or manifest['pending_size'] != len(payload)
        ):
            fail('同 release 的 QWeather 预激活清单与本次输入不一致。')
        if activation_adopted(manifest):
            fail('QWeather 私钥已经由激活事务接管。')
    else:
        # 清单前 SIGKILL 只可能留下事务内 source 或已归档的 unproven；两者都先与本次输入绑定。
        unproven = load_unproven(transaction)
        source = transaction / 'source.pem'
        if unproven is not None:
            unproven_payload, _unproven_stat, unproven_path = unproven
            if unproven_payload != payload:
                if (
                    len(unproven_payload) < len(payload)
                    and payload.startswith(unproven_payload)
                ):
                    quarantine_incomplete(
                        transaction,
                        unproven_path,
                        'partial-source',
                    )
                    marker = transaction / 'UNPROVEN_ARCHIVED.json'
                    if exists(marker):
                        quarantine_incomplete(
                            transaction,
                            marker,
                            'partial-record',
                        )
                    source = write_source(transaction, payload)
                else:
                    fail('同 release 的未完成私钥与本次输入不一致。')
            elif unproven_path != source:
                if exists(source):
                    fail('同 release 的未完成私钥位置不唯一。')
                os.replace(unproven_path, source)
                fsync_directory(unproven_path.parent)
                fsync_directory(transaction)
        elif exists(source):
            source_payload, _source_stat = stable_read(
                source,
                uid=owner_uid,
                gid=owner_gid,
                mode=0o600,
                nlink=1,
                max_bytes=MAX_KEY_BYTES,
                allow_empty=True,
            )
            if source_payload != payload:
                if (
                    len(source_payload) < len(payload)
                    and payload.startswith(source_payload)
                ):
                    quarantine_incomplete(
                        transaction,
                        source,
                        'partial-source',
                    )
                    source = write_source(transaction, payload)
                else:
                    fail('同 release 的未完成私钥与本次输入不一致。')
        else:
            if exists(pending_path):
                fail('发现没有耐久清单的 pending，拒绝覆盖。')
            source = write_source(transaction, payload)
        source_payload, source_stat = stable_read(
            source,
            uid=owner_uid,
            gid=owner_gid,
            mode=0o600,
            nlink=1,
            max_bytes=MAX_KEY_BYTES,
        )
        if source_payload != payload:
            fail('服务端 QWeather 私钥固定快照不一致。')
        manifest = {
            'version': 1,
            'release_id': release_id,
            'pending_path': str(pending_path),
            'final_path': str(final_path),
            'sha256': digest,
            'pending_device': source_stat.st_dev,
            'pending_inode': source_stat.st_ino,
            'pending_nlink': 1,
            'pending_size': source_stat.st_size,
        }
        write_json_exclusive(
            transaction / 'manifest.json',
            manifest,
            uid=owner_uid,
            gid=owner_gid,
        )
        append_event(
            transaction,
            {'event': 'manifest-durable'},
            uid=owner_uid,
            gid=owner_gid,
        )
    location, current = manifest_location(transaction, manifest)
    if location == 'pending':
        append_event(
            transaction,
            {'event': 'staged-reused'},
            uid=owner_uid,
            gid=owner_gid,
        )
        return 'staged'
    if location == 'recovery':
        source = transaction / 'source.pem'
        os.replace(current, source)
        fsync_directory(current.parent)
        fsync_directory(transaction)
        current = source
    if exists(pending_path):
        fail('QWeather pending 路径发生并发变化。')
    os.replace(current, pending_path)
    fsync_directory(current.parent)
    fsync_directory(private_dir)
    verify_manifest_key(pending_path, manifest)
    append_event(
        transaction,
        {'event': 'staged'},
        uid=owner_uid,
        gid=owner_gid,
    )
    return 'staged'


def archive_current():
    if not ensure_context(create_private=False, create_preactivation=False):
        return 'clean'
    transaction = ensure_transaction(release_id, create=False)
    if transaction is None:
        return 'clean'
    return archive_transaction(transaction)


def reconcile_all():
    if not ensure_context(create_private=False, create_preactivation=False):
        return 'clean'
    states = []
    for transaction in sorted(preactivation_root.iterdir()):
        require_directory(
            transaction,
            uid=owner_uid,
            gid=owner_gid,
            modes={0o700},
        )
        if not re.fullmatch(r'[A-Za-z0-9._-]+', transaction.name):
            fail('服务端 QWeather 预激活事务名称异常。')
        states.append(archive_transaction(transaction))
    return 'reconciled' if states else 'clean'


try:
    if len(sys.argv) != 15:
        fail('QWeather 预激活管理器参数数量异常。')
    (
        action,
        project_raw,
        release_root_raw,
        release_id,
        private_raw,
        pending_raw,
        final_raw,
        preactivation_raw,
        activation_raw,
        owner_uid_raw,
        owner_gid_raw,
        runtime_gid_raw,
        expected_input_digest,
        expected_input_size_raw,
    ) = sys.argv[1:]
    if not re.fullmatch(r'[A-Za-z0-9._-]+', release_id):
        fail('QWeather 预激活 release ID 异常。')
    if any(
        not raw
        or not os.path.isabs(raw)
        or raw != os.path.normpath(raw)
        or any(character in raw for character in '\r\n\t')
        for raw in (
            project_raw,
            release_root_raw,
            private_raw,
            pending_raw,
            final_raw,
            preactivation_raw,
            activation_raw,
        )
    ):
        fail('QWeather 预激活路径参数异常。')
    owner_uid = int(owner_uid_raw)
    owner_gid = int(owner_gid_raw)
    runtime_gid = int(runtime_gid_raw)
    expected_input_size = int(expected_input_size_raw)
    if any(value < 0 for value in (owner_uid, owner_gid, runtime_gid)):
        fail('QWeather 预激活身份参数异常。')
    if (
        not re.fullmatch(r'[0-9a-f]{64}', expected_input_digest)
        or expected_input_size <= 0
        or expected_input_size > MAX_KEY_BYTES
    ):
        fail('QWeather 私钥传输摘要参数异常。')
    project_root = Path(project_raw)
    release_root = Path(release_root_raw)
    private_dir = Path(private_raw)
    pending_path = Path(pending_raw)
    final_path = Path(final_raw)
    preactivation_root = Path(preactivation_raw)
    activation_root = Path(activation_raw)
    if activation_root != project_root / 'backups' / 'deploy-transactions':
        fail('激活事务根路径异常。')
    if action == 'provision':
        result = provision()
    elif action == 'archive':
        result = archive_current()
    elif action == 'reconcile-all':
        result = reconcile_all()
    else:
        fail('QWeather 预激活管理器动作异常。')
except (OSError, StateError, ValueError) as error:
    message = str(error) if isinstance(error, StateError) else '服务端 QWeather 预激活事务操作失败。'
    print(message, file=sys.stderr)
    raise SystemExit(64) from None

print(result)
PY
}

run_qweather_preactivation_manager() {
    local action="$1"
    local manager_source remote_command expected_digest expected_size
    manager_source="$(qweather_preactivation_manager_source)"
    expected_digest="${LOCAL_QWEATHER_JWT_PRIVATE_KEY_SHA256:-0000000000000000000000000000000000000000000000000000000000000000}"
    expected_size="${LOCAL_QWEATHER_JWT_PRIVATE_KEY_SIZE:-1}"
    remote_command="set -eu
exec 9>'$RELEASE_ROOT/deploy.lock'
if ! flock -n 9; then
    echo 'QWeather 预激活事务无法取得 deploy.lock。' >&2
    exit 75
fi
RUNTIME_GID=\$(id -g '$RUNTIME_USER')
python3 /dev/fd/4 '$action' '$PROJECT_DIR' '$RELEASE_ROOT' '$RELEASE_ID' '$REMOTE_QWEATHER_PRIVATE_DIR' '$REMOTE_QWEATHER_PENDING_KEY_PATH' '$LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH' '$REMOTE_QWEATHER_PREACTIVATION_ROOT' '$PROJECT_DIR/backups/deploy-transactions' 0 0 \"\$RUNTIME_GID\" '$expected_digest' '$expected_size' 3<&0 4<<'QWEATHER_MANAGER_PY'
$manager_source
QWEATHER_MANAGER_PY"
    if [ "$action" = provision ]; then
        remote_exec_with_file_stdin \
            "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_SNAPSHOT" \
            "$remote_command"
    else
        remote_exec "$remote_command" </dev/null
    fi
}

reconcile_qweather_preactivation_transactions() {
    [ "$LOCAL_QWEATHER_AUTH_MODE" = "jwt" ] || return 0
    run_qweather_preactivation_manager reconcile-all >/dev/null
}

archive_qweather_preactivation_key() {
    local archive_status=0
    [ "$LOCAL_QWEATHER_AUTH_MODE" = "jwt" ] || return 0
    run_qweather_preactivation_manager archive >/dev/null || archive_status=$?
    if [ "$archive_status" -eq 0 ]; then
        REMOTE_QWEATHER_PREACTIVATION_ACTIVE="0"
    fi
    return "$archive_status"
}

# 激活前只写入本轮 release 专属的 root 私有待激活文件。
# 已配置的正式私钥只允许校验同内容，真正发布与授权由激活事务在停服后完成。
provision_qweather_jwt_private_key() {
    [ "$LOCAL_QWEATHER_AUTH_MODE" = "jwt" ] || return 0
    [ -n "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_SNAPSHOT" ] || return 0
    REMOTE_QWEATHER_PREACTIVATION_ACTIVE="1"
    run_qweather_preactivation_manager provision >/dev/null
    echo 'QWeather JWT 私钥已写入 root 私有待激活事务。'
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
        WX_MINIPROGRAM_OPENID_PEPPER|WX_MINIPROGRAM_SESSION_SECRET|ACCOUNT_LINK_CODE_PEPPER) ;;
        *) echo "不允许自动生成该环境变量: $key" >&2; return 64 ;;
    esac
    remote_exec "umask 077; python3 -c 'import secrets; print(secrets.token_hex(32), end=\"\")' | flock $RELEASE_ROOT/deploy-env.lock python3 $RELEASE_APP/scripts/update_env_value.py --file $STAGED_ENV_FILE --key $key --mode if-empty"
}

# 候选配置在激活锁之外准备，因此先固定活动环境和 current 链接基线。
# 激活脚本会在取得 deploy.lock 后执行 CAS，拒绝覆盖并发部署产生的新状态。
candidate_base_state_capture_source() {
    command cat <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


MAX_ENV_BYTES = 1024 * 1024
QWEATHER_PROTECTED_KEYS = (
    'ALLOW_WEATHER_UNAVAILABLE',
    'FORECAST_CACHE_TTL_MINUTES',
    'QWEATHER_API_BASE',
    'QWEATHER_AUTH_MODE',
    'QWEATHER_BUDGET_FAIL_CLOSED',
    'QWEATHER_CANONICAL_LOCATION',
    'QWEATHER_CONSOLE_USAGE_BASELINE',
    'QWEATHER_CONSOLE_USAGE_MONTH',
    'QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED',
    'QWEATHER_EXPECTED_KID',
    'QWEATHER_EXPECTED_PROJECT_ID',
    'QWEATHER_JWT_KID',
    'QWEATHER_JWT_PRIVATE_KEY_PATH',
    'QWEATHER_JWT_PROJECT_ID',
    'QWEATHER_KEY',
    'QWEATHER_MONTHLY_REQUEST_LIMIT',
    'QWEATHER_NETWORK_NOT_BEFORE_EPOCH',
    'QWEATHER_REQUIRE_PERSISTENT_BUDGET',
    'QWEATHER_WARNING_CACHE_TTL_MINUTES',
    'REDIS_URL',
    'WEATHER_CACHE_REDIS_URL',
    'WEATHER_CACHE_TTL_MINUTES',
    'WEATHER_SYNC_LOCATIONS',
)
RUNTIME_GATE_KEYS = (
    'WECHAT_FORMAL_RUNTIME',
    'WEB_PRIVATE_FEATURES_ENABLED',
)


def fail():
    raise SystemExit(64)


def fingerprint(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def read_regular_stably(path):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    descriptor = None
    try:
        path_before = path.lstat()
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(path_before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or fingerprint(path_before) != fingerprint(before)
            or before.st_size > MAX_ENV_BYTES
        ):
            fail()
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_ENV_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_ENV_BYTES:
                fail()
        after = os.fstat(descriptor)
        path_after = path.lstat()
        if fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(path_after):
            fail()
        return b''.join(chunks)
    except OSError:
        fail()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def current_link_state(path):
    try:
        before = path.lstat()
    except FileNotFoundError:
        return hashlib.sha256(b'absent').hexdigest()
    except OSError:
        fail()
    if not stat.S_ISLNK(before.st_mode):
        fail()
    try:
        target = os.readlink(path)
        after = path.lstat()
    except OSError:
        fail()
    if fingerprint(before) != fingerprint(after):
        fail()
    encoded = os.fsencode(target)
    if not encoded or any(character in encoded for character in (0, 10, 13)):
        fail()
    return hashlib.sha256(b'link\0' + encoded).hexdigest()


def qweather_configuration_hash(payload):
    try:
        text = payload.decode('utf-8')
    except UnicodeDecodeError:
        fail()
    values = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in raw_line:
            continue
        key, value = raw_line.split('=', 1)
        key = key.strip()
        if key not in QWEATHER_PROTECTED_KEYS:
            continue
        if key in values:
            fail()
        values[key] = value
    canonical = b''.join(
        key.encode('ascii') + b'=' + values.get(key, '').encode('utf-8') + b'\0'
        for key in QWEATHER_PROTECTED_KEYS
    )
    return hashlib.sha256(canonical).hexdigest()


def runtime_gate_values(payload, *, allow_missing_web_private=False):
    try:
        text = payload.decode('utf-8')
    except UnicodeDecodeError:
        fail()
    matches = {key: [] for key in RUNTIME_GATE_KEYS}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in raw_line:
            continue
        key, value = raw_line.split('=', 1)
        key = key.strip()
        if key in matches:
            matches[key].append(value.strip().strip('"').strip("'"))
    values = {}
    for key in RUNTIME_GATE_KEYS:
        candidates = matches[key]
        if (
            key == 'WEB_PRIVATE_FEATURES_ENABLED'
            and not candidates
            and allow_missing_web_private
        ):
            # 旧活动环境缺少新开关时按关闭态冻结，首次升级不得扩大访问面。
            candidates = ['0']
        if len(candidates) != 1 or candidates[0] not in {'0', '1'}:
            fail()
        values[key] = candidates[0]
    return values


def create_private_file(path, payload):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_CLOEXEC', 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                fail()
            view = view[written:]
        os.fsync(descriptor)
    except OSError:
        fail()
    finally:
        if descriptor is not None:
            os.close(descriptor)


active_env, staged_env, current_link, metadata_file = map(Path, sys.argv[1:5])
deployment_intent = sys.argv[5]
if deployment_intent not in {'web_backend_only', 'wechat_formal'}:
    fail()
for value in (active_env, staged_env, current_link, metadata_file):
    if not value.is_absolute() or value == Path('/'):
        fail()
metadata_directory = metadata_file.parent
try:
    metadata_directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    metadata_stat = metadata_directory.lstat()
except OSError:
    fail()
if not stat.S_ISDIR(metadata_stat.st_mode) or stat.S_ISLNK(metadata_stat.st_mode):
    fail()

active_content = read_regular_stably(active_env)
active_hash = hashlib.sha256(active_content).hexdigest()
qweather_hash = qweather_configuration_hash(active_content)
runtime_flags = runtime_gate_values(
    active_content,
    allow_missing_web_private=True,
)
current_hash = current_link_state(current_link)
created = []
try:
    create_private_file(staged_env, active_content)
    created.append(staged_env)
    if (
        hashlib.sha256(read_regular_stably(active_env)).hexdigest() != active_hash
        or current_link_state(current_link) != current_hash
    ):
        fail()
    metadata = {
        'active_env_sha256': active_hash,
        'current_link_state_sha256': current_hash,
        'deployment_intent': deployment_intent,
        'qweather_config_sha256': qweather_hash,
        'wechat_formal_runtime': runtime_flags['WECHAT_FORMAL_RUNTIME'],
        'web_private_features_enabled': runtime_flags[
            'WEB_PRIVATE_FEATURES_ENABLED'
        ],
        'version': 3,
    }
    metadata_payload = (
        json.dumps(metadata, sort_keys=True, separators=(',', ':')) + '\n'
    ).encode('utf-8')
    create_private_file(metadata_file, metadata_payload)
    created.append(metadata_file)
    for directory in {staged_env.parent, metadata_directory}:
        directory_descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
except (OSError, SystemExit):
    for path in reversed(created):
        try:
            path.unlink()
        except OSError:
            pass
    fail()
PY
}

capture_remote_candidate_base_state() {
    local source=""
    source="$(candidate_base_state_capture_source)"
    remote_exec "set -eu
umask 077
python3 /dev/fd/4 '$PROJECT_DIR/.env' '$STAGED_ENV_FILE' '$CURRENT_LINK' '$NEW_RELEASE/private-metadata/candidate-base-state.json' '$DEPLOY_MODE' 4<<'CANDIDATE_BASE_STATE_PY'
$source
CANDIDATE_BASE_STATE_PY"
}

# 旧正式环境只有原四项微信配置。网页/后端升级不读取微信发布表，
# 仅在运行态已经是 formal=1 时补齐新增的一次性绑定码应用 pepper。
account_link_pepper_backfill_source() {
    command cat <<'PY'
import importlib.util
from pathlib import Path
import secrets
import stat
import sys


MAX_ENV_BYTES = 64 * 1024


def fail():
    raise SystemExit(64)


env_path = Path(sys.argv[1])
helper_path = Path(sys.argv[2])
try:
    before = env_path.lstat()
    helper_stat = helper_path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(helper_stat.st_mode)
        or stat.S_ISLNK(helper_stat.st_mode)
        or before.st_size > MAX_ENV_BYTES
    ):
        fail()
    content = env_path.read_bytes()
    after = env_path.lstat()
except OSError:
    fail()
if (
    len(content) > MAX_ENV_BYTES
    or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
):
    fail()
try:
    text = content.decode('utf-8')
except UnicodeDecodeError:
    fail()
values = {}
for raw_line in text.splitlines():
    line = raw_line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    key, value = line.split('=', 1)
    values[key.strip()] = value.strip().strip('"').strip("'")
if values.get('WECHAT_FORMAL_RUNTIME') != '1':
    raise SystemExit(0)
if values.get('ACCOUNT_LINK_CODE_PEPPER'):
    raise SystemExit(0)

spec = importlib.util.spec_from_file_location(
    'case_weather_update_env_value',
    helper_path,
)
if spec is None or spec.loader is None:
    fail()
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.update_env_value(
    env_path,
    'ACCOUNT_LINK_CODE_PEPPER',
    secrets.token_hex(32),
    'if-empty',
)
PY
}

backfill_account_link_pepper_for_existing_formal_runtime() {
    local source=""
    [ "$DEPLOY_MODE" = "web_backend_only" ] || return 0
    source="$(account_link_pepper_backfill_source)"
    remote_exec "set -eu
umask 077
exec 9>$RELEASE_ROOT/deploy-env.lock
flock -x 9
python3 /dev/fd/4 '$STAGED_ENV_FILE' '$RELEASE_APP/scripts/update_env_value.py' 4<<'ACCOUNT_LINK_PEPPER_BACKFILL_PY'
$source
ACCOUNT_LINK_PEPPER_BACKFILL_PY"
}

amap_atomic_migration_source() {
    command cat <<'PY'
import importlib.util
from pathlib import Path
import re
import sys


PATTERN = re.compile(r'^[A-Za-z0-9_-]{20,100}$')
MAX_PAYLOAD_BYTES = 1024


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(64)


payload = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
if len(payload) > MAX_PAYLOAD_BYTES:
    fail('高德凭据迁移输入过长。')
try:
    values = payload.decode('utf-8').splitlines()
except UnicodeDecodeError:
    fail('高德凭据迁移输入必须是 UTF-8。')
if len(values) != 3 or any(not value for value in values):
    fail('高德凭据迁移必须一次提供完整三项配置。')
js_key, web_service_key, security_code = values
for name, value in (
    ('AMAP_JS_API_KEY', js_key),
    ('AMAP_WEB_SERVICE_KEY', web_service_key),
    ('AMAP_SECURITY_JS_CODE', security_code),
):
    if value.lower().startswith(('your-', 'change-me')) or not PATTERN.fullmatch(value):
        fail(f'{name} 格式异常或仍是占位值。')
if js_key == web_service_key:
    fail('AMAP_JS_API_KEY 与 AMAP_WEB_SERVICE_KEY 必须使用不同用途的 Key。')

env_path = Path(sys.argv[1])
helper_path = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location(
    'case_weather_update_env_values',
    helper_path,
)
if spec is None or spec.loader is None:
    fail('无法加载候选环境原子更新器。')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.update_env_values(
    env_path,
    {
        'AMAP_KEY': '',
        'AMAP_JS_API_KEY': js_key,
        'AMAP_WEB_SERVICE_KEY': web_service_key,
        'AMAP_SECURITY_JS_CODE': security_code,
    },
    require_existing=False,
)
PY
}

# 仅在本机明确提供完整三项凭据时执行迁移。旧 Key 与新三项在同一次
# 原子替换中切换，任一输入缺失时保持服务器候选原样。
migrate_amap_credentials_atomically() {
    local source payload
    [ -n "$LOCAL_AMAP_JS_API_KEY" ] || return 0
    source="$(amap_atomic_migration_source)"
    payload="$LOCAL_AMAP_JS_API_KEY
$LOCAL_AMAP_WEB_SERVICE_KEY
$LOCAL_AMAP_SECURITY_JS_CODE"
    remote_exec_with_stdin "$payload" "set -eu
umask 077
exec 9>$RELEASE_ROOT/deploy-env.lock
flock -x 9
python3 /dev/fd/4 '$STAGED_ENV_FILE' '$RELEASE_APP/scripts/update_env_value.py' 4<<'AMAP_ATOMIC_MIGRATION_PY'
$source
AMAP_ATOMIC_MIGRATION_PY"
}

candidate_runtime_reader_source() {
    command cat <<'PY'
import os
from pathlib import Path
import stat
import sys


MAX_ENV_BYTES = 1024 * 1024
path = Path(sys.argv[1])
flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
descriptor = None
try:
    descriptor = os.open(path, flags)
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_ENV_BYTES:
        raise OSError
    chunks = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(65536, MAX_ENV_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_ENV_BYTES:
            raise OSError
    content = b''.join(chunks)
    after = os.fstat(descriptor)
    if (
        len(content) != before.st_size
        or before.st_size > MAX_ENV_BYTES
        or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
    ):
        raise OSError
finally:
    if descriptor is not None:
        os.close(descriptor)
try:
    text = content.decode('utf-8')
except UnicodeDecodeError:
    raise SystemExit(64) from None
matches = {
    'WECHAT_FORMAL_RUNTIME': [],
    'WEB_PRIVATE_FEATURES_ENABLED': [],
}
for raw_line in text.splitlines():
    line = raw_line.strip()
    if not line or line.startswith('#') or '=' not in raw_line:
        continue
    key, value = raw_line.split('=', 1)
    normalized_key = key.strip()
    if normalized_key in matches:
        matches[normalized_key].append(value.strip().strip('"').strip("'"))
values = []
for key in ('WECHAT_FORMAL_RUNTIME', 'WEB_PRIVATE_FEATURES_ENABLED'):
    candidates = matches[key]
    if len(candidates) != 1 or candidates[0] not in {'0', '1'}:
        raise SystemExit(64)
    values.append(candidates[0])
print(':'.join(values))
PY
}

resolve_effective_formal_runtime() {
    local source runtime_flags
    source="$(candidate_runtime_reader_source)"
    if ! runtime_flags="$(remote_exec "python3 /dev/fd/4 '$STAGED_ENV_FILE' 4<<'CANDIDATE_RUNTIME_READER_PY'
$source
CANDIDATE_RUNTIME_READER_PY")"; then
        echo "无法从候选环境确定唯一的正式态与双端网页开关。" >&2
        exit 64
    fi
    case "$runtime_flags" in
        0:0|0:1|1:0|1:1)
            EXPECTED_WECHAT_FORMAL_RUNTIME="${runtime_flags%%:*}"
            EXPECTED_WEB_PRIVATE_FEATURES_ENABLED="${runtime_flags#*:}"
            EFFECTIVE_REQUIRE_WECHAT_READY="$EXPECTED_WECHAT_FORMAL_RUNTIME"
            ;;
        *)
            echo "候选环境的正式态与双端网页开关输出异常。" >&2
            exit 64
            ;;
    esac
    if [ "$DEPLOY_MODE" = "wechat_formal" ] \
        && { [ "$EXPECTED_WECHAT_FORMAL_RUNTIME" != "1" ] \
            || [ "$EXPECTED_WEB_PRIVATE_FEATURES_ENABLED" != "1" ]; }; then
        echo "微信正式部署候选必须保持正式态与双端网页开关均为 1。" >&2
        exit 64
    fi
}

# 使用 rsync 上传已准备好的发布源；正式发布源是冻结提交的本机快照。
upload_files() {
    local remote_target="$1"
    local -a release_excludes=(--exclude=/analysis/)
    if [ "$DEPLOY_MODE" = "web_backend_only" ]; then
        release_excludes+=(--exclude=/miniprogram/)
    fi
    if use_sshpass && [ -n "${SSHPASS:-}" ]; then
        SSHPASS="${SSHPASS:-$PASSWORD}" sshpass -e rsync -avz \
            --exclude '__pycache__' \
            --exclude '*.pyc' \
            --exclude 'instance' \
            --exclude 'storage' \
            --exclude 'health_weather.db' \
            --exclude 'data/research/*.xlsx' \
            --exclude 'data/research/*.xls' \
            --exclude 'models/*.pkl' \
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
            "${release_excludes[@]}" \
            -e "ssh $SSH_OPTS" "$RELEASE_SOURCE_DIR/" "$USER@$SERVER:$remote_target/"
        return
    fi

    if [ -n "${SSHPASS:-}" ]; then
        echo "密码上传需要 sshpass；也可以清空 DEPLOY_PASSWORD 后使用 SSH Key。" >&2
        return 64
    fi

    rsync -avz --exclude '__pycache__' --exclude '*.pyc' --exclude 'instance' --exclude 'storage' --exclude 'health_weather.db' --exclude 'data/research/*.xlsx' --exclude 'data/research/*.xls' --exclude 'models/*.pkl' --exclude '.git' --exclude '.claude' --exclude 'venv' --exclude '.venv2' --exclude '.env*' --exclude '.secrets/' --exclude '*.pem' --exclude '*.key' --exclude 'project.private.config.json' --exclude '.superpowers' --exclude '.pytest_cache' --exclude '.playwright-cli' --exclude '.vscode' --exclude '.DS_Store' --exclude 'backups' --exclude 'tmp' --exclude 'output' --exclude 'blueprints/tools 2.py' "${release_excludes[@]}" -e "ssh $SSH_OPTS" "$RELEASE_SOURCE_DIR/" "$USER@$SERVER:$remote_target/"
}

upload_model_artifacts() {
    local model_name=""
    local model_file=""

    remote_exec "set -eu
[ -d $RELEASE_APP/models ] && [ ! -L $RELEASE_APP/models ] || {
    echo '候选 release 的 models 目录状态异常。' >&2
    exit 1
}
chown root:root $RELEASE_APP/models
chmod 0700 $RELEASE_APP/models"

    for model_name in disease_predictor.pkl scaler.pkl label_encoder.pkl; do
        model_file="$LOCAL_ML_MODEL_ARTIFACT_SNAPSHOT_DIR/$model_name"
        if [ ! -f "$model_file" ] || [ -L "$model_file" ]; then
            echo "本轮模型制品快照缺失或类型异常: $model_name" >&2
            exit 64
        fi
        remote_exec_with_file_stdin "$model_file" "set -eu
umask 077
TARGET=$RELEASE_APP/models/$model_name
[ ! -e \"\$TARGET\" ] && [ ! -L \"\$TARGET\" ] || {
    echo '候选模型制品目标已存在，拒绝覆盖。' >&2
    exit 1
}
cat > \"\$TARGET\"
chown root:root \"\$TARGET\"
chmod 0600 \"\$TARGET\""
    done

    remote_exec "python3 $RELEASE_APP/scripts/model_artifact.py verify \
        --artifact-dir $RELEASE_APP/models \
        --manifest $RELEASE_APP/models/feature_config.json"
}

echo "步骤1: 测试服务器连接..."
remote_exec "echo '连接成功'"

echo ""
echo "步骤2: 检查服务器依赖（常规发布不修改全局软件）..."
remote_exec "for REQUIRED_COMMAND in python3 rsync sqlite3 curl flock systemctl systemd-run systemd-analyze busctl crontab pgrep runuser mktemp install findmnt sync cmp ln stat chown chmod cat realpath getent groupadd useradd; do command -v \"\$REQUIRED_COMMAND\" >/dev/null || { echo \"缺少服务器依赖: \$REQUIRED_COMMAND，请先执行一次性服务器初始化。\" >&2; exit 1; }; done"

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
# 上一次本机 SIGKILL、SSH 断线或预检失败留下的服务端事务先幂等归档。
# 管理器与 activate_release 共用 deploy.lock；发现激活 plan 时只读取证据并保持私钥原位。
reconcile_qweather_preactivation_transactions
remote_exec "if [ -e $NEW_RELEASE ]; then echo '发布 ID 已存在，拒绝覆盖不可变版本: $NEW_RELEASE' >&2; exit 1; fi; mkdir -p $RELEASE_APP $NEW_RELEASE/systemd"
upload_files "$RELEASE_APP"
if [ "$DEPLOY_MODE" = "web_backend_only" ]; then
    remote_exec "if [ -e $RELEASE_APP/miniprogram ]; then echo '网页/后端发布不得包含微信小程序源码。' >&2; exit 1; fi"
fi
remote_exec "ln -s $PROJECT_DIR/instance $RELEASE_APP/instance && ln -s $PROJECT_DIR/storage $RELEASE_APP/storage && ln -s $PROJECT_DIR/backups $RELEASE_APP/backups"
upload_model_artifacts
if [ "$FORMAL_WECHAT_CONFIG_ALLOWED" = "1" ]; then
    # 在写入正式候选凭据前检查完整 Nginx 配置，失败时不产生敏感候选状态。
    remote_exec "python3 $RELEASE_APP/scripts/verify_runtime_log_boundary.py --active-nginx"
fi

echo ""
echo "步骤4: 准备隔离的候选环境配置..."
# 首次部署的初始配置属于活动状态准备，必须与激活共用 deploy.lock。
# 已有活动配置不会在候选准备阶段改权或改内容。
remote_exec "set -eu
umask 077
exec 9>$RELEASE_ROOT/deploy.lock
if ! flock -n 9; then
    echo '初始活动配置准备无法取得 deploy.lock。' >&2
    exit 73
fi
if [ ! -f $PROJECT_DIR/.env ]; then SECRET_KEY_GEN=\$(python3 -c 'import secrets; print(secrets.token_hex(32))'); PAIR_TOKEN_PEPPER_GEN=\$(python3 -c 'import secrets; print(secrets.token_hex(32))'); cat > $PROJECT_DIR/.env << EOF
FLASK_ENV=production
DEBUG=false
WECHAT_FORMAL_RUNTIME=0
WEB_PRIVATE_FEATURES_ENABLED=0
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
AMAP_JS_API_KEY=
AMAP_WEB_SERVICE_KEY=
AMAP_SECURITY_JS_CODE=
COOLING_COORDINATE_VERIFICATION_TTL_DAYS=365
FEATURE_WEB_AI=0
FEATURE_AUDIT_LOGS=0
FEATURE_STRUCTURED_LOGS=1
SENTRY_DSN=
SENTRY_TRACES_SAMPLE_RATE=0
SENTRY_SEND_PII=0
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
ACCOUNT_LINK_CODE_PEPPER=
WX_MINIPROGRAM_PRIVACY_VERSION=2026-07-21
PUBLIC_BASE_URL=https://yilaoweather.org
ALLOW_INSECURE_PUBLIC_BASE_URL=
EOF
chmod 0600 $PROJECT_DIR/.env
echo '已在 deploy.lock 内创建首次部署配置'; fi"
capture_remote_candidate_base_state

echo ""
echo "步骤4.1: 原子补齐候选配置..."
# 所有候选值均通过 stdin 写入；旧服务在激活事务前继续读取原配置。
remote_env_update "DATABASE_URI" "sqlite:///health_weather.db" "if-empty"
if [ "$DEPLOY_MODE" = "wechat_formal" ]; then
    remote_env_update "QWEATHER_AUTH_MODE" "disabled" "if-empty"
    remote_env_update "QWEATHER_CANONICAL_LOCATION" "116.20,29.27" "always"
    remote_env_update "QWEATHER_MONTHLY_REQUEST_LIMIT" "40000" "always"
    remote_env_update "QWEATHER_BUDGET_FAIL_CLOSED" "1" "always"
    remote_env_update "QWEATHER_REQUIRE_PERSISTENT_BUDGET" "1" "always"
    remote_env_update "WEATHER_CACHE_TTL_MINUTES" "30" "always"
    remote_env_update "FORECAST_CACHE_TTL_MINUTES" "30" "always"
    remote_env_update "QWEATHER_WARNING_CACHE_TTL_MINUTES" "30" "always"
    remote_env_update "WEATHER_SYNC_LOCATIONS" "都昌县" "always"
fi
remote_env_update "COOLING_COORDINATE_VERIFICATION_TTL_DAYS" "365" "if-empty"
remote_env_update "WXPUSHER_API_BASE" "https://wxpusher.zjiecode.com/api" "always"
remote_env_update "FEATURE_WXPUSHER" "0" "if-empty"
remote_env_update "FEATURE_WEB_AI" "0" "always"
remote_env_update "FEATURE_STRUCTURED_LOGS" "1" "always"
remote_env_update "SILICONFLOW_API_KEY" "" "always"
remote_env_update "SILICONFLOW_API_BASE" "https://api.siliconflow.cn/v1" "always"
remote_env_update "DISPATCH_LOCK_PATH" "$PROJECT_DIR/run/case-weather-dispatch.lock" "always"
remote_env_update "FEATURE_HEAT_EXPOSURE_GIS" "0" "if-empty"
remote_env_update "WECHAT_FORMAL_RUNTIME" "0" "if-empty"
remote_env_update "WEB_PRIVATE_FEATURES_ENABLED" "0" "if-empty"
remote_env_update "WX_MINIPROGRAM_PRIVACY_VERSION" "2026-07-21" "if-empty"

echo ""
echo "步骤4.2: 安全写入显式提供的发布配置..."
# 正式入口与第三方凭证接收端每次部署都收敛到固定 origin。
remote_env_update "PUBLIC_BASE_URL" "https://yilaoweather.org" "always"
remote_env_update "ALLOW_INSECURE_PUBLIC_BASE_URL" "" "always"

if [ "$DEPLOY_MODE" = "wechat_formal" ] \
    && [ -n "$LOCAL_QWEATHER_AUTH_MODE" ]; then
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
if [ "$DEPLOY_MODE" = "wechat_formal" ]; then
    if [ -n "${LOCAL_ALLOW_WEATHER_UNAVAILABLE:-}" ]; then
        remote_env_update "ALLOW_WEATHER_UNAVAILABLE" "$LOCAL_ALLOW_WEATHER_UNAVAILABLE" "always"
    else
        remote_env_update "ALLOW_WEATHER_UNAVAILABLE" "" "if-empty"
    fi
fi
# 旧 Key 只有在完整三项新凭据同一次原子写入时才会清空。
migrate_amap_credentials_atomically
if [ -n "$LOCAL_COOLING_COORDINATE_VERIFICATION_TTL_DAYS" ]; then
    remote_env_update "COOLING_COORDINATE_VERIFICATION_TTL_DAYS" "$LOCAL_COOLING_COORDINATE_VERIFICATION_TTL_DAYS" "always"
fi
if [ -n "$LOCAL_FEATURE_HEAT_EXPOSURE_GIS" ]; then
    remote_env_update "FEATURE_HEAT_EXPOSURE_GIS" "$LOCAL_FEATURE_HEAT_EXPOSURE_GIS" "always"
fi
if [ "$DEPLOY_MODE" = "wechat_formal" ] \
    && [ -n "$LOCAL_WEB_PRIVATE_FEATURES_ENABLED" ]; then
    remote_env_update "WEB_PRIVATE_FEATURES_ENABLED" "$LOCAL_WEB_PRIVATE_FEATURES_ENABLED" "always"
fi
backfill_account_link_pepper_for_existing_formal_runtime
# 只有同一次验证快照同时满足 require=1 与 ready=1，才允许写入正式凭据。
if [ "$FORMAL_WECHAT_CONFIG_ALLOWED" = "1" ]; then
    # 个人主体 1.1.1 不持久化审计日志，也禁止第三方异常平台接收请求上下文。
    remote_env_update "WECHAT_FORMAL_RUNTIME" "$LOCAL_WECHAT_FORMAL_RUNTIME" "always"
    remote_env_update "FEATURE_AUDIT_LOGS" "$LOCAL_FEATURE_AUDIT_LOGS" "always"
    remote_env_update "FEATURE_STRUCTURED_LOGS" "$LOCAL_FEATURE_STRUCTURED_LOGS" "always"
    remote_env_update "FEATURE_WXPUSHER" "$LOCAL_FEATURE_WXPUSHER" "always"
    remote_env_update "WXPUSHER_APP_TOKEN" "$LOCAL_WXPUSHER_APP_TOKEN" "always"
    remote_env_update "WX_MINIPROGRAM_APPID" "$LOCAL_WX_MINIPROGRAM_APPID" "always"
    remote_env_update "WX_MINIPROGRAM_SECRET" "$LOCAL_WX_MINIPROGRAM_SECRET" "always"
    remote_env_generate_secret "WX_MINIPROGRAM_OPENID_PEPPER"
    remote_env_generate_secret "WX_MINIPROGRAM_SESSION_SECRET"
    remote_env_generate_secret "ACCOUNT_LINK_CODE_PEPPER"
    remote_env_update "WX_MINIPROGRAM_PRIVACY_VERSION" "$LOCAL_WX_MINIPROGRAM_PRIVACY_VERSION" "always"
    remote_env_update "QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED" "$LOCAL_QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED" "always"
    remote_env_update "QWEATHER_CONSOLE_USAGE_MONTH" "$LOCAL_QWEATHER_CONSOLE_USAGE_MONTH" "always"
    remote_env_update "QWEATHER_CONSOLE_USAGE_BASELINE" "$LOCAL_QWEATHER_CONSOLE_USAGE_BASELINE" "always"
    remote_env_update "QWEATHER_EXPECTED_PROJECT_ID" "$LOCAL_QWEATHER_EXPECTED_PROJECT_ID" "always"
    remote_env_update "QWEATHER_EXPECTED_KID" "$LOCAL_QWEATHER_EXPECTED_KID" "always"
fi
resolve_effective_formal_runtime
# 网页独立发布会继承服务器正式运行态，因此同样收敛第三方异常平台隐私边界。
if [ "$EFFECTIVE_REQUIRE_WECHAT_READY" = "1" ]; then
    remote_env_update "SENTRY_DSN" "" "always"
    remote_env_update "SENTRY_TRACES_SAMPLE_RATE" "0" "always"
    remote_env_update "SENTRY_SEND_PII" "0" "always"
fi
provision_qweather_jwt_private_key
remote_exec "python3 $RELEASE_APP/scripts/validate_release_env.py --file $STAGED_ENV_FILE --require-wechat $EFFECTIVE_REQUIRE_WECHAT_READY --require-weather-ready $REMOTE_QWEATHER_VALIDATION_PENDING_ARG"

echo ""
echo "步骤6: 为新版本创建独立虚拟环境..."
remote_exec "set -eu
# 先尝试从已激活 release 严格核验并低内存克隆；证据不足才进入原网络安装门禁。
INSTALL_AVAILABLE_MIB=\$(df -Pm $RELEASE_ROOT | awk 'NR == 2 {print \$4}')
INSTALL_INODE_USE_PERCENT=\$(df -Pi $RELEASE_ROOT | awk 'NR == 2 {gsub(\"%\", \"\", \$5); print \$5}')
INSTALL_REUSE_MEM_AVAILABLE_KIB=\$(awk '/^MemAvailable:/ {print \$2}' /proc/meminfo)
[ \"\${INSTALL_REUSE_MEM_AVAILABLE_KIB:-0}\" -ge 262144 ] || {
    echo '发布依赖复用前可用内存不足 256 MiB，安全停止发布。' >&2
    exit 1
}
[ \"\${INSTALL_AVAILABLE_MIB:-0}\" -ge 2048 ] || {
    echo '发布磁盘可用空间不足 2048 MiB，停止依赖准备。' >&2
    exit 1
}
[ \"\${INSTALL_INODE_USE_PERCENT:-100}\" -le 90 ] || {
    echo '发布磁盘可用 inode 不足 10%，停止依赖准备。' >&2
    exit 1
}

umask 077
INSTALL_ROOT=$NEW_RELEASE/dependency-install
INSTALL_HOME=\$INSTALL_ROOT/home
INSTALL_TMP=\$INSTALL_ROOT/tmp
INSTALL_REUSE_UNIT=case-weather-reuse-$RELEASE_ID.service
INSTALL_NETWORK_UNIT=case-weather-install-$RELEASE_ID.service
cleanup_install() {
    INSTALL_CLEANUP_STATUS=\$?
    INSTALL_UNIT_STILL_ACTIVE=0
    for INSTALL_UNIT in \"\$INSTALL_REUSE_UNIT\" \"\$INSTALL_NETWORK_UNIT\"; do
        if systemctl is-active --quiet \"\$INSTALL_UNIT\"; then
            systemctl stop \"\$INSTALL_UNIT\" >/dev/null 2>&1 \
                || INSTALL_CLEANUP_STATUS=1
            for INSTALL_STOP_ATTEMPT in 1 2 3 4 5; do
                systemctl is-active --quiet \"\$INSTALL_UNIT\" || break
                sleep 1
            done
        fi
        if systemctl is-active --quiet \"\$INSTALL_UNIT\"; then
            echo \"依赖准备 unit 未停止，保留临时目录并停止发布: \$INSTALL_UNIT\" >&2
            INSTALL_UNIT_STILL_ACTIVE=1
            INSTALL_CLEANUP_STATUS=1
        fi
    done
    if [ \"\$INSTALL_UNIT_STILL_ACTIVE\" = 0 ]; then
        rm -rf -- \"\$INSTALL_ROOT\"
    fi
    exit \"\$INSTALL_CLEANUP_STATUS\"
}
trap cleanup_install EXIT
install -d -o root -g root -m 0700 \"\$INSTALL_ROOT\" \"\$INSTALL_HOME\" \"\$INSTALL_TMP\"

# 当前 release 的锁、私有收据、Python 与 live 包集合全相等时才允许无网络克隆。
# 17,845 条目实测父进程与 pip 子进程组合峰值约 146 MiB，保留 192 MiB 硬上限。
INSTALL_REUSE_STATUS=0
systemd-run --quiet --wait --pipe --collect --service-type=exec \
    --unit=\"\$INSTALL_REUSE_UNIT\" \
    --property=MemoryHigh=160M \
    --property=MemoryMax=192M \
    --property=MemorySwapMax=0 \
    --property=TasksMax=32 \
    --property=OOMPolicy=stop \
    --property=TimeoutStartSec=10min \
    --property=RuntimeMaxSec=10min \
    --property=PrivateTmp=yes \
    --property=PrivateDevices=yes \
    --property=PrivateNetwork=yes \
    --property=NoNewPrivileges=yes \
    --working-directory=$RELEASE_APP \
    /usr/bin/env -i \
    HOME=\"\$INSTALL_HOME\" \
    TMPDIR=\"\$INSTALL_TMP\" \
    PIP_NO_INDEX=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_CONFIG_FILE=/dev/null \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PATH=/usr/local/bin:/usr/bin:/bin \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    /bin/bash $RELEASE_APP/scripts/install_release_dependencies.sh \
        $RELEASE_APP \
        $RELEASE_VENV \
        $NEW_RELEASE/private-metadata \
        c7e450c30d7d3c56bdf210f69a58620cba9d99e462e0e2c254ab45456271f853 \
        reuse-current \
        $CURRENT_LINK \
        3.11 || INSTALL_REUSE_STATUS=\$?

if [ \"\$INSTALL_REUSE_STATUS\" = 0 ]; then
    echo '已从 current release 严格核验并低内存克隆依赖。'
elif [ \"\$INSTALL_REUSE_STATUS\" = 75 ]; then
    # 只有明确的“不可复用”状态才允许回退；320 MiB 门禁仍在任何 PyPI 请求之前。
    # 复用核对最长可运行 10 分钟，网络安装必须重新读取当下资源，禁止使用旧快照。
    INSTALL_MEM_TOTAL_KIB=\$(awk '/^MemTotal:/ {print \$2}' /proc/meminfo)
    INSTALL_MEM_AVAILABLE_KIB=\$(awk '/^MemAvailable:/ {print \$2}' /proc/meminfo)
    INSTALL_AVAILABLE_MIB=\$(df -Pm $RELEASE_ROOT | awk 'NR == 2 {print \$4}')
    INSTALL_INODE_USE_PERCENT=\$(df -Pi $RELEASE_ROOT | awk 'NR == 2 {gsub(\"%\", \"\", \$5); print \$5}')
    [ \"\${INSTALL_MEM_TOTAL_KIB:-0}\" -ge 460800 ] || {
        echo '当前 release 不可安全复用，且服务器总内存不足 450 MiB，停止依赖安装。' >&2
        exit 1
    }
    [ \"\${INSTALL_MEM_AVAILABLE_KIB:-0}\" -ge 327680 ] || {
        echo '当前 release 不可安全复用，且服务器可用内存不足 320 MiB，停止依赖安装。' >&2
        exit 1
    }
    [ \"\${INSTALL_AVAILABLE_MIB:-0}\" -ge 2048 ] || {
        echo '网络安装前发布磁盘可用空间不足 2048 MiB，停止依赖安装。' >&2
        exit 1
    }
    [ \"\${INSTALL_INODE_USE_PERCENT:-100}\" -le 90 ] || {
        echo '网络安装前发布磁盘可用 inode 不足 10%，停止依赖安装。' >&2
        exit 1
    }
    # PyPI 网络仅对本轮受限安装开放；超内存或超时会在激活前安全停止。
    systemd-run --quiet --wait --pipe --collect --service-type=exec \
        --unit=\"\$INSTALL_NETWORK_UNIT\" \
        --property=MemoryHigh=192M \
        --property=MemoryMax=256M \
        --property=MemorySwapMax=0 \
        --property=TasksMax=64 \
        --property=OOMPolicy=stop \
        --property=TimeoutStartSec=15min \
        --property=RuntimeMaxSec=15min \
        --property=PrivateTmp=yes \
        --property=PrivateDevices=yes \
        --property=NoNewPrivileges=yes \
        --working-directory=$RELEASE_APP \
        /usr/bin/env -i \
        HOME=\"\$INSTALL_HOME\" \
        TMPDIR=\"\$INSTALL_TMP\" \
        PIP_NO_CACHE_DIR=1 \
        PIP_DISABLE_PIP_VERSION_CHECK=1 \
        PIP_CONFIG_FILE=/dev/null \
        PYTHONDONTWRITEBYTECODE=1 \
        PYTHONNOUSERSITE=1 \
        PATH=/usr/local/bin:/usr/bin:/bin \
        LANG=C.UTF-8 LC_ALL=C.UTF-8 \
        /bin/bash $RELEASE_APP/scripts/install_release_dependencies.sh \
            $RELEASE_APP \
            $RELEASE_VENV \
            $NEW_RELEASE/private-metadata \
            c7e450c30d7d3c56bdf210f69a58620cba9d99e462e0e2c254ab45456271f853 \
            install \
            /dev/null \
            3.11
else
    echo \"current release 依赖复用发生非回退型失败（状态 \$INSTALL_REUSE_STATUS），停止发布。\" >&2
    exit 1
fi"
remote_exec "$RELEASE_VENV/bin/python $RELEASE_APP/scripts/validate_release_env.py --file $STAGED_ENV_FILE --require-wechat $EFFECTIVE_REQUIRE_WECHAT_READY --require-weather-ready $REMOTE_QWEATHER_VALIDATION_PENDING_ARG --probe-persistent-budget"
# commit 只含十六进制字符。有效正式运行态会由激活脚本再次核对两份 CI 收据。
remote_exec "umask 077; printf '%s\n' '$VERIFIED_COMMIT' > $NEW_RELEASE/private-metadata/source-commit.txt; chmod 0600 $NEW_RELEASE/private-metadata/source-commit.txt"
upload_private_metadata_receipt \
    "$LOCAL_ML_MODEL_ARTIFACT_RECEIPT" \
    "model-artifacts.json"
upload_private_metadata_receipt "$LOCAL_CI_PROOF_FILE" "ci-proof.json"
if [ "$EFFECTIVE_REQUIRE_WECHAT_READY" = "1" ]; then
    upload_private_metadata_receipt \
        "$LOCAL_MINIPROGRAM_CI_PROOF_FILE" \
        "miniprogram-ci-proof.json"
fi

echo ""
echo "步骤6.1: 在停止生产服务前完成低内存隔离预检..."
# 完整 Python、激活事务和小程序测试由精确提交的 GitHub CI 收据负责。
# 服务器只做语法、锁定依赖、Python 3.11 与 Alembic 单 head 运行态核对。
remote_exec "set -eu
umask 077
PREFLIGHT_ROOT=$NEW_RELEASE/preflight-runtime
PREFLIGHT_HOME=\$PREFLIGHT_ROOT/home
PREFLIGHT_TMP=\$PREFLIGHT_ROOT/tmp
PREFLIGHT_PYCACHE=\$PREFLIGHT_ROOT/pycache
PREFLIGHT_RECEIPT=\$PREFLIGHT_ROOT/runtime-smoke.json
PREFLIGHT_UNIT_PREFIX=case-weather-preflight-$RELEASE_ID
cleanup_preflight() {
    rm -rf -- \"\$PREFLIGHT_ROOT\"
}
trap cleanup_preflight EXIT
# 候选代码与虚拟环境只向运行组开放读取和执行，所有产生物限制在运行用户私有目录。
chown root:$RUNTIME_GROUP $NEW_RELEASE
chmod 0750 $NEW_RELEASE
chown -R root:$RUNTIME_GROUP $RELEASE_APP $RELEASE_VENV
chmod -R g+rX,o-rwx $RELEASE_APP $RELEASE_VENV
$RELEASE_VENV/bin/python $RELEASE_APP/scripts/model_artifact.py verify \
    --artifact-dir $RELEASE_APP/models \
    --manifest $RELEASE_APP/models/feature_config.json \
    --receipt $NEW_RELEASE/private-metadata/model-artifacts.json \
    --commit $VERIFIED_COMMIT \
    --expected-owner root \
    --expected-group $RUNTIME_GROUP \
    --expected-file-mode 0640 \
    --expected-dir-mode 0750
install -d -o $RUNTIME_USER -g $RUNTIME_GROUP -m 0700 \"\$PREFLIGHT_ROOT\" \"\$PREFLIGHT_HOME\" \"\$PREFLIGHT_TMP\" \"\$PREFLIGHT_PYCACHE\"

systemd-run --quiet --wait --collect --service-type=exec \
    --unit=\"\$PREFLIGHT_UNIT_PREFIX-compile.service\" \
    --property=User=$RUNTIME_USER \
    --property=Group=$RUNTIME_GROUP \
    --property=UMask=0077 \
    --property=MemoryMax=96M \
    --property=MemorySwapMax=0 \
    --property=TasksMax=64 \
    --property=OOMPolicy=stop \
    --property=PrivateNetwork=yes \
    --property=PrivateTmp=yes \
    --property=PrivateDevices=yes \
    --property=NoNewPrivileges=yes \
    --working-directory=$RELEASE_APP \
    /usr/bin/env -i \
    HOME=\"\$PREFLIGHT_HOME\" \
    TMPDIR=\"\$PREFLIGHT_TMP\" \
    PYTHONPYCACHEPREFIX=\"\$PREFLIGHT_PYCACHE\" \
    PYTHONNOUSERSITE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_NO_INDEX=1 \
    PIP_CONFIG_FILE=/dev/null \
    PATH=$RELEASE_VENV/bin:/usr/local/bin:/usr/bin:/bin \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    USER=$RUNTIME_USER LOGNAME=$RUNTIME_USER \
    $RELEASE_VENV/bin/python -m compileall -q -j 1 \
        app.py blueprints core services utils migrations

systemd-run --quiet --wait --collect --service-type=exec \
    --unit=\"\$PREFLIGHT_UNIT_PREFIX-shell.service\" \
    --property=User=$RUNTIME_USER \
    --property=Group=$RUNTIME_GROUP \
    --property=UMask=0077 \
    --property=MemoryMax=96M \
    --property=MemorySwapMax=0 \
    --property=TasksMax=64 \
    --property=OOMPolicy=stop \
    --property=PrivateNetwork=yes \
    --property=PrivateTmp=yes \
    --property=PrivateDevices=yes \
    --property=NoNewPrivileges=yes \
    --working-directory=$RELEASE_APP \
    /usr/bin/env -i \
    HOME=\"\$PREFLIGHT_HOME\" TMPDIR=\"\$PREFLIGHT_TMP\" \
    PATH=$RELEASE_VENV/bin:/usr/local/bin:/usr/bin:/bin \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    USER=$RUNTIME_USER LOGNAME=$RUNTIME_USER \
    /bin/bash -ceu '
/bin/bash -n scripts/deploy.sh
/bin/bash -n scripts/install_release_dependencies.sh
/bin/bash -n scripts/activate_release.sh
/bin/bash -n scripts/server_migrate.sh
/bin/bash -n scripts/weather_cache_sync.sh
/bin/bash -n scripts/backup.sh
'

systemd-run --quiet --wait --collect --service-type=exec \
    --unit=\"\$PREFLIGHT_UNIT_PREFIX-runtime.service\" \
    --property=User=$RUNTIME_USER \
    --property=Group=$RUNTIME_GROUP \
    --property=UMask=0077 \
    --property=MemoryMax=96M \
    --property=MemorySwapMax=0 \
    --property=TasksMax=64 \
    --property=OOMPolicy=stop \
    --property=PrivateNetwork=yes \
    --property=PrivateTmp=yes \
    --property=PrivateDevices=yes \
    --property=NoNewPrivileges=yes \
    --working-directory=$RELEASE_APP \
    /usr/bin/env -i \
    HOME=\"\$PREFLIGHT_HOME\" \
    TMPDIR=\"\$PREFLIGHT_TMP\" \
    PYTHONPYCACHEPREFIX=\"\$PREFLIGHT_PYCACHE\" \
    PYTHONNOUSERSITE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_NO_INDEX=1 \
    PIP_CONFIG_FILE=/dev/null \
    PATH=$RELEASE_VENV/bin:/usr/local/bin:/usr/bin:/bin \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    USER=$RUNTIME_USER LOGNAME=$RUNTIME_USER \
    $RELEASE_VENV/bin/python scripts/release_runtime_smoke.py run \
        --repo-root $RELEASE_APP \
        --expected-commit $VERIFIED_COMMIT \
        --expected-python $RELEASE_VENV/bin/python \
        --expected-python-minor 3.11 \
        --expected-lock-sha c7e450c30d7d3c56bdf210f69a58620cba9d99e462e0e2c254ab45456271f853 \
        --output \"\$PREFLIGHT_RECEIPT\"

install -o root -g root -m 0600 \
    \"\$PREFLIGHT_RECEIPT\" \
    $NEW_RELEASE/private-metadata/runtime-smoke.json
$RELEASE_VENV/bin/python $RELEASE_APP/scripts/release_runtime_smoke.py \
    verify-receipt \
    --receipt $NEW_RELEASE/private-metadata/runtime-smoke.json \
    --repo-root $RELEASE_APP \
    --expected-commit $VERIFIED_COMMIT \
    --expected-python $RELEASE_VENV/bin/python \
    --expected-python-minor 3.11 \
    --expected-lock-sha c7e450c30d7d3c56bdf210f69a58620cba9d99e462e0e2c254ab45456271f853"

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
ExecStart=$CURRENT_LINK/venv/bin/python -m gunicorn --workers 1 --bind 127.0.0.1:5000 --timeout 120 app:app
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
OnUnitInactiveSec=30min
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
Environment=VENV_PY=$CURRENT_LINK/venv/bin/python
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
echo "步骤6.3: 收敛发布文件与运行数据权限..."
remote_exec "set -eu
chown -R root:$RUNTIME_GROUP $NEW_RELEASE
chmod -R g+rX,o-rwx $NEW_RELEASE
# 发布证明和运行态清单只供 root 激活事务读取，递归开放运行组后立即重新收紧。
chown -R root:root $NEW_RELEASE/private-metadata
chmod -R u=rwX,go= $NEW_RELEASE/private-metadata
for PRIVATE_RECEIPT in \
    $NEW_RELEASE/private-metadata/dependency-receipt.json \
    $NEW_RELEASE/private-metadata/ci-proof.json \
    $NEW_RELEASE/private-metadata/model-artifacts.json \
    $NEW_RELEASE/private-metadata/runtime-smoke.json \
    $NEW_RELEASE/private-metadata/source-commit.txt; do
    [ \"\$(stat -c '%u:%g:%a' \"\$PRIVATE_RECEIPT\")\" = '0:0:600' ] || {
        echo '发布私有收据权限异常。' >&2
        exit 1
    }
done
if [ '$EFFECTIVE_REQUIRE_WECHAT_READY' = 1 ]; then
    [ \"\$(stat -c '%u:%g:%a' $NEW_RELEASE/private-metadata/miniprogram-ci-proof.json)\" = '0:0:600' ] || {
        echo '小程序发布私有收据权限异常。' >&2
        exit 1
    }
fi
chown root:$RUNTIME_GROUP $STAGED_ENV_FILE
chmod 0640 $STAGED_ENV_FILE
chown $RUNTIME_USER:$RUNTIME_GROUP $PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run
chmod 0700 $PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run"
if [ "$EFFECTIVE_REQUIRE_WECHAT_READY" = "1" ]; then
    # 控制台当月已用量只做原子 max 合并，绝不降低 Redis 中已有计数。
    remote_exec "$RELEASE_VENV/bin/python $RELEASE_APP/scripts/validate_release_env.py --file $STAGED_ENV_FILE --require-wechat 1 --require-weather-ready $REMOTE_QWEATHER_VALIDATION_PENDING_ARG --probe-persistent-budget --seed-persistent-budget"
fi

echo ""
echo "步骤7: 在单个服务器事务中备份、迁移、切换并验活..."
if [ "$EFFECTIVE_REQUIRE_WECHAT_READY" = "1" ]; then
    # 激活前复查活动配置，缩小上传预检与生产切换之间的竞态窗口。
    remote_exec "python3 $RELEASE_APP/scripts/verify_runtime_log_boundary.py --active-nginx"
fi
ACTIVATION_EXPECTED_RELEASE_COMMIT="$VERIFIED_COMMIT"
if remote_exec "STATE_DIR=$PROJECT_DIR RELEASE_ROOT=$RELEASE_ROOT NEW_RELEASE=$NEW_RELEASE CURRENT_LINK=$CURRENT_LINK ENV_FILE=$PROJECT_DIR/.env STAGED_ENV_FILE=$STAGED_ENV_FILE HEALTH_URL=http://127.0.0.1:5000/healthz DEPLOY_INTENT=$DEPLOY_MODE REQUIRE_WECHAT_READY=$EFFECTIVE_REQUIRE_WECHAT_READY EXPECTED_WECHAT_FORMAL_RUNTIME=$EXPECTED_WECHAT_FORMAL_RUNTIME EXPECTED_WEB_PRIVATE_FEATURES_ENABLED=$EXPECTED_WEB_PRIVATE_FEATURES_ENABLED EXPECTED_RELEASE_COMMIT=$ACTIVATION_EXPECTED_RELEASE_COMMIT EXPECTED_RELEASE_BRANCH=$VERIFIED_RELEASE_BRANCH RECOVERY_ACKNOWLEDGED_TRANSACTION=$RECOVERY_ACKNOWLEDGED_TRANSACTION RUNTIME_USER=$RUNTIME_USER RUNTIME_GROUP=$RUNTIME_GROUP QWEATHER_PENDING_KEY_PATH=$ACTIVATION_QWEATHER_PENDING_KEY_PATH bash $RELEASE_APP/scripts/activate_release.sh"; then
    # 激活事务已消费或精确复用 pending，并负责后续回滚/向前恢复；本地 EXIT 不再介入。
    REMOTE_QWEATHER_PREACTIVATION_ACTIVE="0"
else
    activation_status=$?
    exit "$activation_status"
fi

echo ""
echo "步骤8: 服务、timer、OnSuccess、current 链接与健康检查已在原子激活事务内通过。"

echo ""
echo "=== 部署完成 ==="
echo "发布版本: $RELEASE_ID"
echo "持久化目录: $PROJECT_DIR"
echo "当前版本入口: $CURRENT_LINK"
