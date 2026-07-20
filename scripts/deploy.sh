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
LOCAL_QWEATHER_JWT_PRIVATE_KEY_SOURCE=""
LOCAL_QWEATHER_JWT_PRIVATE_KEY_SNAPSHOT=""
LOCAL_QWEATHER_JWT_PRIVATE_KEY_SHA256=""
LOCAL_QWEATHER_JWT_PRIVATE_KEY_SIZE=""
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
            QWEATHER_KEY|QWEATHER_API_BASE|QWEATHER_AUTH_MODE|QWEATHER_JWT_KID|QWEATHER_JWT_PROJECT_ID|QWEATHER_JWT_PRIVATE_KEY_PATH|QWEATHER_JWT_PRIVATE_KEY_SOURCE|ALLOW_WEATHER_UNAVAILABLE|AMAP_KEY|FEATURE_AUDIT_LOGS|FEATURE_WXPUSHER|WXPUSHER_APP_TOKEN|FEATURE_HEAT_EXPOSURE_GIS|PUBLIC_BASE_URL|ALLOW_INSECURE_PUBLIC_BASE_URL)
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
REMOTE_QWEATHER_PRIVATE_DIR="$PROJECT_DIR/private"
REMOTE_QWEATHER_PENDING_KEY_PATH=""
REMOTE_QWEATHER_PREACTIVATION_ROOT="$PROJECT_DIR/backups/qweather-preactivation"
REMOTE_QWEATHER_PREACTIVATION_ACTIVE="0"

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

if [ "$REQUIRE_WECHAT_READY" = "1" ]; then
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
    if [ -z "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_SOURCE" ]; then
        echo "微信正式 JWT 发布必须提供本机 QWEATHER_JWT_PRIVATE_KEY_SOURCE。" >&2
        exit 64
    fi
    snapshot_qweather_jwt_private_key_source "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_SOURCE" || exit $?
elif [ -n "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_SOURCE" ]; then
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
REMOTE_QWEATHER_PENDING_KEY_PATH="$REMOTE_QWEATHER_PRIVATE_DIR/.qweather-jwt.pending-$RELEASE_ID"
validate_remote_path "QWEATHER 私钥待激活路径" "$REMOTE_QWEATHER_PENDING_KEY_PATH"
if [ "$LOCAL_QWEATHER_AUTH_MODE" = "jwt" ] \
    && [ "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH" = "$REMOTE_QWEATHER_PENDING_KEY_PATH" ]; then
    echo "QWEATHER_JWT_PRIVATE_KEY_PATH 不得占用本轮待激活私钥路径。" >&2
    exit 64
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
provision_qweather_jwt_private_key
remote_exec "python3 $RELEASE_APP/scripts/validate_release_env.py --file $STAGED_ENV_FILE --require-wechat $REQUIRE_WECHAT_READY --qweather-private-key-pending-path $REMOTE_QWEATHER_PENDING_KEY_PATH"

echo ""
echo "步骤6: 为新版本创建独立虚拟环境..."
remote_exec "set -e; EXPECTED_LOCK_SHA=c7e450c30d7d3c56bdf210f69a58620cba9d99e462e0e2c254ab45456271f853; ACTUAL_LOCK_SHA=\$(python3 -c 'import hashlib; print(hashlib.sha256(open(\"$RELEASE_APP/requirements.lock\", \"rb\").read()).hexdigest())'); [ \"\$ACTUAL_LOCK_SHA\" = \"\$EXPECTED_LOCK_SHA\" ] || { echo 'requirements.lock 摘要不匹配。' >&2; exit 1; }; python3 -m venv $RELEASE_VENV; $RELEASE_VENV/bin/python -m pip install --index-url https://pypi.org/simple --require-hashes --only-binary=:all: -r $RELEASE_APP/requirements.lock; [ -x $RELEASE_VENV/bin/gunicorn ] || { echo '锁定依赖安装后缺少 gunicorn。' >&2; exit 1; }; umask 077; mkdir -p $NEW_RELEASE/private-metadata; $RELEASE_VENV/bin/python --version > $NEW_RELEASE/private-metadata/python-version.txt 2>&1; printf '%s\n' \"\$ACTUAL_LOCK_SHA\" > $NEW_RELEASE/private-metadata/requirements-lock.sha256; $RELEASE_VENV/bin/python -m pip inspect --local > $NEW_RELEASE/private-metadata/pip-inspect.json; chmod 0700 $NEW_RELEASE/private-metadata; chmod 0600 $NEW_RELEASE/private-metadata/python-version.txt $NEW_RELEASE/private-metadata/requirements-lock.sha256 $NEW_RELEASE/private-metadata/pip-inspect.json"
remote_exec "$RELEASE_VENV/bin/python $RELEASE_APP/scripts/validate_release_env.py --file $STAGED_ENV_FILE --require-wechat $REQUIRE_WECHAT_READY --qweather-private-key-pending-path $REMOTE_QWEATHER_PENDING_KEY_PATH --probe-persistent-budget"
if [ "$FORMAL_WECHAT_CONFIG_ALLOWED" = "1" ]; then
    # commit 只含十六进制字符，写入 release 私有 metadata 后由激活脚本再次核对。
    remote_exec "umask 077; printf '%s\n' '$VERIFIED_COMMIT' > $NEW_RELEASE/private-metadata/source-commit.txt; chmod 0600 $NEW_RELEASE/private-metadata/source-commit.txt"
fi

echo ""
echo "步骤6.1: 在停止生产服务前完成隔离测试..."
# 完整非激活测试与四个激活分片由 GitHub CI 负责；服务器资源受限，只运行八个发布关键冒烟文件。
# 禁止恢复不带文件清单的裸全量 pytest，避免发布主机再次触发 OOM。
remote_exec "set -eu
umask 077
PREFLIGHT_ROOT=$NEW_RELEASE/preflight-runtime
PREFLIGHT_HOME=\$PREFLIGHT_ROOT/home
PREFLIGHT_TMP=\$PREFLIGHT_ROOT/tmp
cleanup_preflight() {
    rm -rf -- \"\$PREFLIGHT_ROOT\"
}
trap cleanup_preflight EXIT
# 候选代码与虚拟环境只向运行组开放读取和执行，测试产生物全部限制在运行用户私有目录。
chown root:$RUNTIME_GROUP $NEW_RELEASE
chmod 0750 $NEW_RELEASE
chown -R root:$RUNTIME_GROUP $RELEASE_APP $RELEASE_VENV
chmod -R g+rX,o-rwx $RELEASE_APP $RELEASE_VENV
install -d -o $RUNTIME_USER -g $RUNTIME_GROUP -m 0700 \"\$PREFLIGHT_ROOT\" \"\$PREFLIGHT_HOME\" \"\$PREFLIGHT_TMP\"
cd $RELEASE_APP
runuser --user $RUNTIME_USER -- /usr/bin/env -i HOME=\"\$PREFLIGHT_HOME\" TMPDIR=\"\$PREFLIGHT_TMP\" PATH=$RELEASE_VENV/bin:/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 USER=$RUNTIME_USER LOGNAME=$RUNTIME_USER PYTHONDONTWRITEBYTECODE=1 DATABASE_URI=sqlite:///:memory: DEBUG=true WECHAT_FORMAL_RUNTIME=0 SECRET_KEY=release-preflight-secret-key-123456789 PAIR_TOKEN_PEPPER=release-preflight-pair-pepper-123456789 RATE_LIMIT_STORAGE_URI=memory:// REDIS_URL= QWEATHER_AUTH_MODE=disabled QWEATHER_KEY= QWEATHER_API_BASE= AMAP_KEY= AMAP_WEB_SERVICE_KEY= AMAP_SECURITY_JS_CODE= SILICONFLOW_API_KEY= WXPUSHER_APP_TOKEN= WX_MINIPROGRAM_APPID= WX_MINIPROGRAM_SECRET= WX_MINIPROGRAM_OPENID_PEPPER= WX_MINIPROGRAM_SESSION_SECRET= DEMO_MODE=1 $RELEASE_VENV/bin/python -m pytest -q -p no:cacheprovider tests/test_smoke.py tests/test_database_bootstrap.py tests/test_server_migrate.py tests/test_miniprogram_runtime.py tests/test_formal_web_gate.py tests/test_web_weather_fail_closed.py tests/test_security_headers.py tests/test_mp_api_auth.py"

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
    remote_exec "$RELEASE_VENV/bin/python $RELEASE_APP/scripts/validate_release_env.py --file $STAGED_ENV_FILE --require-wechat 1 --qweather-private-key-pending-path $REMOTE_QWEATHER_PENDING_KEY_PATH --probe-persistent-budget --seed-persistent-budget"
fi

echo ""
echo "步骤7: 在单个服务器事务中备份、迁移、切换并验活..."
if remote_exec "STATE_DIR=$PROJECT_DIR RELEASE_ROOT=$RELEASE_ROOT NEW_RELEASE=$NEW_RELEASE CURRENT_LINK=$CURRENT_LINK ENV_FILE=$PROJECT_DIR/.env STAGED_ENV_FILE=$STAGED_ENV_FILE HEALTH_URL=http://127.0.0.1:5000/healthz REQUIRE_WECHAT_READY=$REQUIRE_WECHAT_READY EXPECTED_RELEASE_COMMIT=$VERIFIED_COMMIT RECOVERY_ACKNOWLEDGED_TRANSACTION=$RECOVERY_ACKNOWLEDGED_TRANSACTION RUNTIME_USER=$RUNTIME_USER RUNTIME_GROUP=$RUNTIME_GROUP QWEATHER_PENDING_KEY_PATH=$REMOTE_QWEATHER_PENDING_KEY_PATH bash $RELEASE_APP/scripts/activate_release.sh"; then
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
