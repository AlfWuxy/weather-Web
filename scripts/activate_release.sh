#!/bin/bash
# 在服务器端原子激活一个已完成预检的不可变发布版本。
set -Eeuo pipefail
umask 077

STATE_DIR="${STATE_DIR:-}"
RELEASE_ROOT="${RELEASE_ROOT:-}"
NEW_RELEASE="${NEW_RELEASE:-}"
CURRENT_LINK="${CURRENT_LINK:-$RELEASE_ROOT/current}"
ENV_FILE="${ENV_FILE:-$STATE_DIR/.env}"
STAGED_ENV_FILE="${STAGED_ENV_FILE:-$NEW_RELEASE/staged.env}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:5000/healthz}"
CANDIDATE_BIND="${CANDIDATE_BIND:-127.0.0.1:5001}"
CANDIDATE_HEALTH_URL="${CANDIDATE_HEALTH_URL:-http://127.0.0.1:5001/healthz}"
HEALTH_ATTEMPTS="${HEALTH_ATTEMPTS:-20}"
HEALTH_SLEEP_SECONDS="${HEALTH_SLEEP_SECONDS:-1}"
# 成功切换后保留一个短观察窗，捕获紧随部署发生的误清理。
POST_COMMIT_STABILITY_SECONDS="${POST_COMMIT_STABILITY_SECONDS:-45}"
POST_COMMIT_STABILITY_INTERVAL_SECONDS="${POST_COMMIT_STABILITY_INTERVAL_SECONDS:-5}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SQLITE3_BIN="${SQLITE3_BIN:-sqlite3}"
CURL_BIN="${CURL_BIN:-curl}"
FLOCK_BIN="${FLOCK_BIN:-flock}"
BUSCTL_BIN="${BUSCTL_BIN:-busctl}"
RUNUSER_BIN="${RUNUSER_BIN:-runuser}"
CHOWN_BIN="${CHOWN_BIN:-chown}"
ENV_BIN="${ENV_BIN:-/usr/bin/env}"
UPTIME_FILE="${UPTIME_FILE:-/proc/uptime}"
DATABASE_FILE="${DATABASE_FILE:-}"
RECOVERY_ACKNOWLEDGED_TRANSACTION="${RECOVERY_ACKNOWLEDGED_TRANSACTION:-}"
REQUIRE_WECHAT_READY="${REQUIRE_WECHAT_READY:-0}"
EXPECTED_RELEASE_COMMIT="${EXPECTED_RELEASE_COMMIT:-}"
RUNTIME_USER="${RUNTIME_USER:-case-weather}"
RUNTIME_GROUP="${RUNTIME_GROUP:-case-weather}"
CONTROL_OWNER_UID="${CONTROL_OWNER_UID:-0}"
CONTROL_OWNER_GID="${CONTROL_OWNER_GID:-0}"
EXPECTED_REQUIREMENTS_LOCK_SHA256="c7e450c30d7d3c56bdf210f69a58620cba9d99e462e0e2c254ab45456271f853"

APP_DIR="$NEW_RELEASE/app"
VENV_DIR="$NEW_RELEASE/venv"
RELEASE_ID="${NEW_RELEASE##*/}"
TRANSACTION_ROOT="$STATE_DIR/backups/deploy-transactions"
FORMAL_SMOKE_RECEIPT_ROOT="$STATE_DIR/deployments/formal-cache-smokes"
TRANSACTION_DIR="$TRANSACTION_ROOT/${RELEASE_ID}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
STATE_FILE="$TRANSACTION_DIR/unit-state.tsv"
OLD_LINK_FILE="$TRANSACTION_DIR/old-current-link"
DB_BACKUP="$TRANSACTION_DIR/database-before.db"
ENV_BACKUP="$TRANSACTION_DIR/environment-before.env"
FAILURE_MARKER="$TRANSACTION_DIR/ROLLBACK_REQUIRED.txt"
POST_COMMIT_MARKER="$TRANSACTION_DIR/POST_COMMIT_ATTENTION.txt"
STARTED_MARKER="$TRANSACTION_DIR/ACTIVATION_STARTED"
ROLLED_BACK_MARKER="$TRANSACTION_DIR/ROLLED_BACK"
RECOVERY_CONFIRMED_MARKER_NAME="RECOVERY_CONFIRMED"

START_TIMER_UNITS=(
    case-weather-cache-bootstrap.timer
    case-weather-risk-precompute.timer
    case-weather-usage-cleanup.timer
)
DEFERRED_TIMER_UNITS=(
    case-weather-cache.timer
)
MANAGED_TIMER_UNITS=("${START_TIMER_UNITS[@]}" "${DEFERRED_TIMER_UNITS[@]}")
LEGACY_UNITS=(
    case-weather-dispatch.timer
)
SERVICE_UNITS=(
    case-weather-cache-bootstrap.service
    case-weather-cache.service
    case-weather-dispatch.service
    case-weather-risk-precompute.service
    case-weather-usage-cleanup.service
    case-weather.service
)
INSTALL_UNITS=("${MANAGED_TIMER_UNITS[@]}" "${SERVICE_UNITS[@]}")
ALL_UNITS=("${INSTALL_UNITS[@]}" "${LEGACY_UNITS[@]}")

COMMITTED=0
FORWARD_ONLY=0
MUTATION_STARTED=0
DB_MUTATION_STARTED=0
DB_EXISTED=0
DB_BACKUP_READY=0
ENV_MUTATION_STARTED=0
ENV_EXISTED=0
ENV_BACKUP_READY=0
LINK_MUTATED=0
UNITS_MUTATED=0
CANDIDATE_PID=""
FORMAL_RELEASE_COMMIT=""
FORMAL_RELEASE_CONFIG_FINGERPRINT=""
FORMAL_SMOKE_RECEIPT_DIR=""
FORMAL_SMOKE_REUSED=0

log() {
    printf '[activate_release] %s\n' "$*"
}

fail() {
    log "失败: $*" >&2
    return 1
}

validate_absolute_path() {
    local name="$1"
    local value="$2"
    if [[ "$value" != /* || "$value" = "/" || "$value" == *"'"* || "$value" == *$'\n'* ]]; then
        echo "$name 必须是安全的绝对路径: $value" >&2
        exit 2
    fi
}

require_file() {
    [ -f "$1" ] || {
        echo "缺少文件: $1" >&2
        exit 2
    }
}

require_executable() {
    [ -x "$1" ] || {
        echo "缺少可执行文件: $1" >&2
        exit 2
    }
}

unit_exists() {
    "$SYSTEMCTL_BIN" cat "$1" >/dev/null 2>&1
}

capture_previous_state() {
    mkdir -p "$TRANSACTION_DIR/units"
    : > "$STATE_FILE"
    if [ -L "$CURRENT_LINK" ]; then
        readlink "$CURRENT_LINK" > "$OLD_LINK_FILE"
    else
        printf '%s\n' '__ABSENT__' > "$OLD_LINK_FILE"
    fi

    local unit exists enabled active
    for unit in "${ALL_UNITS[@]}"; do
        exists=0
        enabled=not-found
        active=inactive
        if unit_exists "$unit"; then
            exists=1
            enabled="$($SYSTEMCTL_BIN is-enabled "$unit" 2>/dev/null || true)"
            active="$($SYSTEMCTL_BIN is-active "$unit" 2>/dev/null || true)"
            if [ -f "$UNIT_DIR/$unit" ]; then
                cp -a "$UNIT_DIR/$unit" "$TRANSACTION_DIR/units/$unit"
            fi
        fi
        printf '%s\t%s\t%s\t%s\n' "$unit" "$exists" "$enabled" "$active" >> "$STATE_FILE"
    done
}

detect_unfinished_transactions() {
    local marker transaction
    while IFS= read -r marker; do
        transaction="$(dirname "$marker")"
        if [ -f "$transaction/ROLLBACK_REQUIRED.txt" ] \
            || [ -f "$transaction/POST_COMMIT_ATTENTION.txt" ]; then
            if [ -f "$transaction/$RECOVERY_CONFIRMED_MARKER_NAME" ]; then
                continue
            fi
            fail "发现尚未人工确认的部署恢复事务: $transaction"
            return 1
        fi
        if [ -f "$transaction/COMMITTED" ] || [ -f "$transaction/ROLLED_BACK" ]; then
            continue
        fi
        fail "发现上次进程中断留下的未完成事务: $transaction"
        return 1
    done < <(
        find "$TRANSACTION_ROOT" \
            -mindepth 2 \
            -maxdepth 2 \
            -type f \
            -name ACTIVATION_STARTED \
            -print
    )
}

acknowledge_recovery_transaction() {
    [ -n "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ] || return 0
    if [ ! -d "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ] \
        || [ -L "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ]; then
        fail "待确认的恢复事务目录不存在或不是普通目录"
        return 1
    fi
    if [ ! -f "$RECOVERY_ACKNOWLEDGED_TRANSACTION/ROLLBACK_REQUIRED.txt" ] \
        && [ ! -f "$RECOVERY_ACKNOWLEDGED_TRANSACTION/POST_COMMIT_ATTENTION.txt" ]; then
        fail "指定事务没有需要人工确认的故障标记"
        return 1
    fi
    if [ -L "$RECOVERY_ACKNOWLEDGED_TRANSACTION/ROLLBACK_REQUIRED.txt" ] \
        || [ -L "$RECOVERY_ACKNOWLEDGED_TRANSACTION/POST_COMMIT_ATTENTION.txt" ]; then
        fail "指定事务的故障标记不得为符号链接"
        return 1
    fi
    if ! "$VENV_DIR/bin/python" - \
        "$RECOVERY_ACKNOWLEDGED_TRANSACTION/$RECOVERY_CONFIRMED_MARKER_NAME" \
        "$NEW_RELEASE" <<'PY'
from datetime import datetime, timezone
import os
import sys

path, release = sys.argv[1:]
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_CLOEXEC', 0)
flags |= getattr(os, 'O_NOFOLLOW', 0)
payload = (
    f"confirmed_at={datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
    f"confirmed_before_release={release}\n"
).encode('utf-8')
try:
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
except OSError:
    raise SystemExit(1) from None
PY
    then
        fail "恢复确认标记无法安全创建"
        return 1
    fi
    log "已登记人工恢复确认: $RECOVERY_ACKNOWLEDGED_TRANSACTION"
}

prepare_control_directories() {
    local control_dir
    for control_dir in \
        "$STATE_DIR/backups" \
        "$STATE_DIR/deployments" \
        "$TRANSACTION_ROOT"; do
        if [ -L "$control_dir" ]; then
            fail "发布控制目录不得为符号链接"
            return 1
        fi
        mkdir -p "$control_dir"
        "$CHOWN_BIN" root:root "$control_dir"
        chmod 0700 "$control_dir"
    done
    if ! "$VENV_DIR/bin/python" - \
        "$CONTROL_OWNER_UID" \
        "$CONTROL_OWNER_GID" \
        "$STATE_DIR/backups" \
        "$STATE_DIR/deployments" \
        "$TRANSACTION_ROOT" <<'PY'
import os
import stat
import sys

expected_uid = int(sys.argv[1])
expected_gid = int(sys.argv[2])
for raw_path in sys.argv[3:]:
    try:
        file_stat = os.lstat(raw_path)
    except OSError:
        raise SystemExit(1)
    if (
        not stat.S_ISDIR(file_stat.st_mode)
        or stat.S_ISLNK(file_stat.st_mode)
        or file_stat.st_uid != expected_uid
        or file_stat.st_gid != expected_gid
        or stat.S_IMODE(file_stat.st_mode) != 0o700
    ):
        raise SystemExit(1)
PY
    then
        fail "backups/deployments 控制目录必须由 root:root 持有且权限为 0700"
        return 1
    fi
}

validate_recovery_transaction_realpath() {
    [ -n "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ] || return 0
    if ! "$VENV_DIR/bin/python" - \
        "$TRANSACTION_ROOT" \
        "$RECOVERY_ACKNOWLEDGED_TRANSACTION" <<'PY'
import os
import stat
import sys

root_raw, candidate_raw = sys.argv[1:]
try:
    root_stat = os.lstat(root_raw)
    candidate_stat = os.lstat(candidate_raw)
    root_real = os.path.realpath(root_raw, strict=True)
    candidate_real = os.path.realpath(candidate_raw, strict=True)
except (OSError, TypeError):
    raise SystemExit(1)
if (
    not stat.S_ISDIR(root_stat.st_mode)
    or stat.S_ISLNK(root_stat.st_mode)
    or not stat.S_ISDIR(candidate_stat.st_mode)
    or stat.S_ISLNK(candidate_stat.st_mode)
    or os.path.normpath(root_raw) != root_raw
    or os.path.normpath(candidate_raw) != candidate_raw
    or os.path.abspath(root_raw) != root_real
    or os.path.abspath(candidate_raw) != candidate_real
    or os.path.dirname(candidate_real) != root_real
):
    raise SystemExit(1)
relative = os.path.relpath(candidate_real, root_real)
if relative in {'.', '..'} or os.sep in relative or relative.startswith('..'):
    raise SystemExit(1)
PY
    then
        fail "恢复事务 realpath 必须是部署事务根目录下的真实直接子目录"
        return 1
    fi
}

stop_units_strictly() {
    local unit
    for unit in "${ALL_UNITS[@]}"; do
        if unit_exists "$unit"; then
            "$SYSTEMCTL_BIN" stop "$unit"
        fi
    done
    for unit in "${ALL_UNITS[@]}"; do
        if unit_exists "$unit" && "$SYSTEMCTL_BIN" is-active --quiet "$unit"; then
            fail "systemd 单元仍在运行: $unit"
        fi
    done
}

stop_units_best_effort() {
    local failed=0
    local unit
    for unit in "${ALL_UNITS[@]}"; do
        if unit_exists "$unit"; then
            "$SYSTEMCTL_BIN" stop "$unit" >/dev/null 2>&1 || failed=1
        fi
    done
    for unit in "${ALL_UNITS[@]}"; do
        if unit_exists "$unit" && "$SYSTEMCTL_BIN" is-active --quiet "$unit"; then
            failed=1
        fi
    done
    return "$failed"
}

resolve_database_file() {
    if [ -n "$DATABASE_FILE" ]; then
        printf '%s\n' "$DATABASE_FILE"
        return
    fi
    (
        cd "$APP_DIR"
        "$VENV_DIR/bin/python" - <<'PY'
from pathlib import Path

from core.app import create_app
from core.config import resolve_sqlite_db_path

app = create_app(register_blueprints=False)
path = resolve_sqlite_db_path(
    app.config['SQLALCHEMY_DATABASE_URI'],
    repo_root=Path.cwd(),
    instance_dir=Path(app.instance_path),
)
if path is None:
    raise SystemExit('正式发布事务当前只支持 SQLite 数据库')
print(path)
PY
    )
}

sqlite_quick_check() {
    local target="$1"
    local result
    result="$($SQLITE3_BIN "$target" 'PRAGMA quick_check;')"
    [ "$result" = "ok" ] || fail "SQLite quick_check 未通过: $target ($result)"
}

sqlite_foreign_key_check() {
    local target="$1"
    local result
    result="$($SQLITE3_BIN "$target" 'PRAGMA foreign_key_check;')"
    [ -z "$result" ] || fail "SQLite foreign_key_check 未通过: $target ($result)"
}

tighten_database_permissions() {
    local suffix database_dir
    database_dir="$(dirname "$DATABASE_FILE")"
    case "$database_dir" in
        "$STATE_DIR/instance"|"$STATE_DIR/instance/"*)
            mkdir -p "$database_dir"
            chmod 0700 "$database_dir"
            "$CHOWN_BIN" "$RUNTIME_USER:$RUNTIME_GROUP" "$database_dir"
            ;;
        *)
            [ -d "$database_dir" ] || fail "外置 SQLite 目录不存在: $database_dir"
            ;;
    esac
    for suffix in '' -wal -shm; do
        if [ -e "$DATABASE_FILE$suffix" ]; then
            chmod 0600 "$DATABASE_FILE$suffix"
            "$CHOWN_BIN" "$RUNTIME_USER:$RUNTIME_GROUP" "$DATABASE_FILE$suffix"
        fi
    done
}

tighten_environment_permissions() {
    require_file "$ENV_FILE"
    "$CHOWN_BIN" "root:$RUNTIME_GROUP" "$ENV_FILE"
    chmod 0640 "$ENV_FILE"
}

prepare_runtime_permissions() {
    local runtime_dir
    for runtime_dir in "$STATE_DIR/instance" "$STATE_DIR/storage" "$STATE_DIR/run"; do
        mkdir -p "$runtime_dir"
        "$CHOWN_BIN" "$RUNTIME_USER:$RUNTIME_GROUP" "$runtime_dir"
        chmod 0700 "$runtime_dir"
    done
    tighten_environment_permissions
}

runtime_exec() {
    # 仅向非特权进程传递运行所需的白名单环境。
    local runtime_path='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    local runtime_env=(
        -i
        "CASE_WEATHER_ENV_FILE=$ENV_FILE"
        "DATABASE_FILE=${DATABASE_FILE:-}"
        "HOME=$STATE_DIR/run"
        'LANG=C.UTF-8'
        'LC_ALL=C.UTF-8'
        "PATH=$runtime_path"
        'PYTHONUNBUFFERED=1'
        'TMPDIR=/tmp'
        'TZ=Asia/Shanghai'
        "VENV_PY=$VENV_DIR/bin/python"
    )
    if [ "$(id -u)" = "$(id -u "$RUNTIME_USER")" ]; then
        exec "$ENV_BIN" "${runtime_env[@]}" "$@"
    fi
    exec "$RUNUSER_BIN" -u "$RUNTIME_USER" -- \
        "$ENV_BIN" "${runtime_env[@]}" "$@"
}

backup_database() {
    if [ ! -f "$DATABASE_FILE" ]; then
        DB_EXISTED=0
        log "数据库尚不存在，记录为空库发布"
        return
    fi
    DB_EXISTED=1
    "$SQLITE3_BIN" "$DATABASE_FILE" 'PRAGMA wal_checkpoint(TRUNCATE);'
    sqlite_quick_check "$DATABASE_FILE"
    "$SQLITE3_BIN" "$DATABASE_FILE" ".backup '$DB_BACKUP'"
    chmod 0600 "$DB_BACKUP"
    sqlite_quick_check "$DB_BACKUP"
    DB_BACKUP_READY=1
}

atomic_replace() {
    local source="$1"
    local destination="$2"
    "$VENV_DIR/bin/python" - "$source" "$destination" <<'PY'
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
}

backup_environment() {
    if [ ! -f "$ENV_FILE" ]; then
        ENV_EXISTED=0
        return
    fi
    ENV_EXISTED=1
    cp -a "$ENV_FILE" "$ENV_BACKUP"
    chmod 0600 "$ENV_BACKUP"
    ENV_BACKUP_READY=1
}

apply_staged_environment() {
    require_file "$STAGED_ENV_FILE"
    "$CHOWN_BIN" "root:$RUNTIME_GROUP" "$STAGED_ENV_FILE"
    chmod 0640 "$STAGED_ENV_FILE"
    ENV_MUTATION_STARTED=1
    atomic_replace "$STAGED_ENV_FILE" "$ENV_FILE"
    tighten_environment_permissions
}

arm_qweather_network_gate() {
    local now_epoch not_before_epoch
    now_epoch="$(date +%s)"
    if [[ ! "$now_epoch" =~ ^[0-9]+$ ]]; then
        fail "服务器时间无法转换为 Unix 秒"
        return 1
    fi
    not_before_epoch=$((now_epoch + 1800))
    printf '%s' "$not_before_epoch" \
        | "$VENV_DIR/bin/python" "$APP_DIR/scripts/update_env_value.py" \
            --file "$ENV_FILE" \
            --key QWEATHER_NETWORK_NOT_BEFORE_EPOCH \
            --mode always
    tighten_environment_permissions
    log "已设置 QWeather 部署保护窗口，从当前切换点起 30 分钟内禁止出网"
}

switch_current_link() {
    local target="$1"
    local next_link="$CURRENT_LINK.next.$$"
    ln -s "$target" "$next_link"
    atomic_replace "$next_link" "$CURRENT_LINK"
}

install_new_units() {
    local unit source temporary
    UNITS_MUTATED=1
    for unit in "${INSTALL_UNITS[@]}"; do
        source="$NEW_RELEASE/systemd/$unit"
        require_file "$source"
        temporary="$UNIT_DIR/$unit.new.$$"
        install -m 0644 "$source" "$temporary"
        mv -f "$temporary" "$UNIT_DIR/$unit"
    done
    mkdir -p "$TRANSACTION_DIR/retired-legacy-units"
    for unit in "${LEGACY_UNITS[@]}"; do
        if unit_exists "$unit"; then
            "$SYSTEMCTL_BIN" disable "$unit" >/dev/null
        fi
        if [ -e "$UNIT_DIR/$unit" ] || [ -L "$UNIT_DIR/$unit" ]; then
            mv "$UNIT_DIR/$unit" "$TRANSACTION_DIR/retired-legacy-units/$unit"
        fi
    done
    "$SYSTEMCTL_BIN" daemon-reload
}

wait_for_health() {
    local url="$1"
    local watched_pid="${2:-}"
    local attempt body
    for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
        if [ -n "$watched_pid" ] && ! kill -0 "$watched_pid" >/dev/null 2>&1; then
            fail "候选应用进程提前退出，请检查 $TRANSACTION_DIR/candidate-gunicorn.log"
            return 1
        fi
        body="$($CURL_BIN --fail --silent --show-error --max-time 2 "$url" 2>/dev/null || true)"
        if printf '%s' "$body" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
            return 0
        fi
        sleep "$HEALTH_SLEEP_SECONDS"
    done
    fail "应用健康检查失败: $url"
}

stop_candidate_release() {
    if [ -n "$CANDIDATE_PID" ]; then
        kill "$CANDIDATE_PID" >/dev/null 2>&1 || true
        wait "$CANDIDATE_PID" >/dev/null 2>&1 || true
        CANDIDATE_PID=""
    fi
}

start_candidate_release() {
    log "在仅本机可访问的端口验证候选版本"
    (
        cd "$APP_DIR"
        runtime_exec "$VENV_DIR/bin/gunicorn" \
            --workers 1 \
            --bind "$CANDIDATE_BIND" \
            --timeout 60 \
            app:app
    ) > "$TRANSACTION_DIR/candidate-gunicorn.log" 2>&1 &
    CANDIDATE_PID=$!
    wait_for_health "$CANDIDATE_HEALTH_URL" "$CANDIDATE_PID"
    stop_candidate_release
}

validate_release_dependencies() {
    local actual_lock_sha recorded_lock_sha
    require_file "$APP_DIR/requirements.lock"
    require_executable "$VENV_DIR/bin/gunicorn"
    require_file "$NEW_RELEASE/private-metadata/requirements-lock.sha256"
    require_file "$NEW_RELEASE/private-metadata/python-version.txt"
    require_file "$NEW_RELEASE/private-metadata/pip-inspect.json"
    actual_lock_sha="$("$VENV_DIR/bin/python" - "$APP_DIR/requirements.lock" <<'PY'
import hashlib
import sys

with open(sys.argv[1], 'rb') as handle:
    print(hashlib.sha256(handle.read()).hexdigest())
PY
)"
    IFS= read -r recorded_lock_sha < "$NEW_RELEASE/private-metadata/requirements-lock.sha256"
    if [ "$actual_lock_sha" != "$EXPECTED_REQUIREMENTS_LOCK_SHA256" ] \
        || [ "$recorded_lock_sha" != "$EXPECTED_REQUIREMENTS_LOCK_SHA256" ]; then
        fail "部署依赖锁摘要与正式基线不一致"
        return 1
    fi
}

validate_formal_release_identity() {
    local metadata_file="$NEW_RELEASE/private-metadata/source-commit.txt"
    local metadata_commit=""
    if [ "$REQUIRE_WECHAT_READY" != 1 ]; then
        if [ -n "$EXPECTED_RELEASE_COMMIT" ]; then
            fail "游客部署不得携带正式发布 commit 票据"
            return 1
        fi
        return 0
    fi
    if [[ ! "$EXPECTED_RELEASE_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
        fail "正式发布缺少有效的冻结 commit 票据"
        return 1
    fi
    if [ -L "$metadata_file" ]; then
        fail "正式发布 commit metadata 不得为符号链接"
        return 1
    fi
    require_file "$metadata_file"
    IFS= read -r metadata_commit < "$metadata_file"
    if [[ ! "$metadata_commit" =~ ^[0-9a-f]{40}$ ]] \
        || [ "$metadata_commit" != "$EXPECTED_RELEASE_COMMIT" ]; then
        fail "上传 release 与冻结 commit 票据不一致"
        return 1
    fi
    FORMAL_RELEASE_COMMIT="$metadata_commit"
}

compute_formal_release_config_fingerprint() {
    "$VENV_DIR/bin/python" - "$ENV_FILE" <<'PY'
import hashlib
import json
import os
import stat
import sys

path = sys.argv[1]
# 指纹只绑定会改变 QWeather 请求、预算或正式快照判定的天气配置。
# 微信、推送、GIS 与公开域名轮换不能获得第二次自动烟测机会。
keys = (
    'QWEATHER_AUTH_MODE',
    'QWEATHER_KEY',
    'QWEATHER_API_BASE',
    'QWEATHER_JWT_KID',
    'QWEATHER_JWT_PROJECT_ID',
    'QWEATHER_JWT_PRIVATE_KEY_PATH',
    'QWEATHER_CANONICAL_LOCATION',
    'QWEATHER_MONTHLY_REQUEST_LIMIT',
    'QWEATHER_BUDGET_FAIL_CLOSED',
    'QWEATHER_REQUIRE_PERSISTENT_BUDGET',
    'ALLOW_WEATHER_UNAVAILABLE',
    'WEATHER_CACHE_TTL_MINUTES',
    'FORECAST_CACHE_TTL_MINUTES',
    'QWEATHER_WARNING_CACHE_TTL_MINUTES',
    'WEATHER_SYNC_LOCATIONS',
)
values = {}
with open(path, encoding='utf-8-sig') as handle:
    for raw_line in handle:
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key in keys:
            values[key] = value
payload = {key: values.get(key, '') for key in keys}


def file_fingerprint(file_stat):
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def private_key_digest(key_path):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    no_follow = getattr(os, 'O_NOFOLLOW', None)
    if no_follow is None:
        raise SystemExit('正式 JWT 私钥安全校验失败')
    try:
        descriptor = os.open(key_path, flags | no_follow)
    except OSError:
        raise SystemExit('正式 JWT 私钥安全校验失败') from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > 16 * 1024
        ):
            raise SystemExit('正式 JWT 私钥安全校验失败')
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(8192, (16 * 1024) + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 16 * 1024:
                raise SystemExit('正式 JWT 私钥安全校验失败')
        after = os.fstat(descriptor)
    except OSError:
        raise SystemExit('正式 JWT 私钥安全校验失败') from None
    finally:
        os.close(descriptor)
    if total != before.st_size or file_fingerprint(before) != file_fingerprint(after):
        raise SystemExit('正式 JWT 私钥安全校验失败')
    return hashlib.sha256(b''.join(chunks)).hexdigest()


payload['QWEATHER_JWT_PRIVATE_KEY_SHA256'] = ''
if values.get('QWEATHER_AUTH_MODE', '').lower() == 'jwt':
    key_path = values.get('QWEATHER_JWT_PRIVATE_KEY_PATH', '')
    if not key_path or not os.path.isabs(key_path):
        raise SystemExit('正式 JWT 私钥安全校验失败')
    payload['QWEATHER_JWT_PRIVATE_KEY_SHA256'] = private_key_digest(key_path)
encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
print(hashlib.sha256(encoded.encode('utf-8')).hexdigest())
PY
}

receipt_value() {
    local file="$1"
    local key="$2"
    awk -F '=' -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; found=1; exit} END {if (!found) exit 1}' "$file"
}

verify_receipt_binding() {
    local binding_file="$FORMAL_SMOKE_RECEIPT_DIR/binding"
    local stored_commit stored_fingerprint
    if [ -L "$FORMAL_SMOKE_RECEIPT_DIR" ] || [ ! -d "$FORMAL_SMOKE_RECEIPT_DIR" ]; then
        fail "正式天气烟测 receipt 路径异常"
        return 1
    fi
    if [ -L "$binding_file" ] || [ ! -f "$binding_file" ]; then
        fail "正式天气烟测 receipt 缺少可信绑定信息"
        return 1
    fi
    stored_commit="$(receipt_value "$binding_file" release_commit || true)"
    stored_fingerprint="$(receipt_value "$binding_file" config_fingerprint || true)"
    if [ "$stored_commit" != "$FORMAL_RELEASE_COMMIT" ] \
        || [ "$stored_fingerprint" != "$FORMAL_RELEASE_CONFIG_FINGERPRINT" ]; then
        fail "正式天气烟测 receipt 与本次冻结发布不匹配"
        return 1
    fi
}

latest_snapshot_id() {
    "$SQLITE3_BIN" "$DATABASE_FILE" \
        "SELECT COALESCE(snapshot_id, '') FROM miniprogram_snapshots ORDER BY fetched_at DESC, id DESC LIMIT 1;" \
        2>/dev/null || true
}

verify_fresh_qweather_snapshot() {
    local snapshot_id="$1"
    local state
    if [[ ! "$snapshot_id" =~ ^[A-Za-z0-9._-]{1,100}$ ]]; then
        fail "正式天气烟测快照标识格式异常"
        return 1
    fi
    if ! state="$("$VENV_DIR/bin/python" - "$DATABASE_FILE" "$snapshot_id" <<'PY'
import json
import sqlite3
import sys
from datetime import datetime, timezone


def load_json(raw, expected):
    try:
        value = json.loads(raw or '')
    except (TypeError, ValueError):
        raise SystemExit('快照 JSON 无法解析')
    if not isinstance(value, expected):
        raise SystemExit('快照 JSON 类型异常')
    return value


def parse_time(raw):
    text = str(raw or '').strip().replace('Z', '+00:00')
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise SystemExit('快照过期时间异常')
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


database, snapshot_id = sys.argv[1:]
connection = sqlite3.connect(database)
try:
    row = connection.execute(
        '''
        SELECT available, expires_at, current_json, forecast_json, source_status_json
        FROM miniprogram_snapshots
        WHERE snapshot_id = ?
        LIMIT 1
        ''',
        (snapshot_id,),
    ).fetchone()
finally:
    connection.close()
if row is None:
    raise SystemExit('receipt 指向的持久化快照不存在')
available, expires_at, current_raw, forecast_raw, source_raw = row
if int(available or 0) != 1 or parse_time(expires_at) <= datetime.now(timezone.utc):
    raise SystemExit('持久化快照不可用或已经过期')
current = load_json(current_raw, dict)
forecast = load_json(forecast_raw, list)
source_status = load_json(source_raw, dict)
provider = str(current.get('data_source') or current.get('source') or '').strip().casefold()
if provider != 'qweather' or current.get('is_mock') or current.get('is_demo'):
    raise SystemExit('实况来源不是 QWeather 官方数据')
weather_status = source_status.get('weather')
if not isinstance(weather_status, dict):
    raise SystemExit('实况来源状态缺失')
if (
    str(weather_status.get('provider') or '').strip().casefold() != 'qweather'
    or not weather_status.get('available')
    or weather_status.get('is_mock')
):
    raise SystemExit('实况来源状态不是 QWeather 官方数据')
if not forecast:
    raise SystemExit('QWeather 七日预报为空')
for item in forecast:
    if not isinstance(item, dict):
        raise SystemExit('七日预报结构异常')
    item_provider = str(item.get('data_source') or item.get('source') or '').strip().casefold()
    if item_provider != 'qweather' or item.get('is_mock') or item.get('is_demo'):
        raise SystemExit('七日预报包含 Open-Meteo、fallback 或模拟来源')
forecast_status = source_status.get('forecast')
if not isinstance(forecast_status, dict) or not forecast_status.get('available'):
    raise SystemExit('QWeather 七日预报来源状态不可用')
providers = forecast_status.get('providers')
if not isinstance(providers, list) or {
    str(value).strip().casefold() for value in providers
} != {'qweather'}:
    raise SystemExit('七日预报来源状态不是唯一 QWeather')
forecast_meta = forecast_status.get('meta')
if not isinstance(forecast_meta, dict) or str(
    forecast_meta.get('source') or ''
).strip().casefold() != 'qweather':
    raise SystemExit('七日预报元数据不是 QWeather 官方来源')
warning_status = source_status.get('warnings')
if (
    not isinstance(warning_status, dict)
    or not warning_status.get('available')
    or str(warning_status.get('status') or '').strip().casefold() not in {'ok', 'success'}
):
    raise SystemExit('QWeather 官方预警同步未成功完成')
print('ready')
PY
)"; then
        fail "正式天气烟测快照校验失败: $state"
        return 1
    fi
    [ "$state" = ready ] || fail "正式天气烟测快照校验没有返回 ready"
}

prepare_formal_smoke_receipt() {
    local binding_file started_file completed_file snapshot_id now
    FORMAL_RELEASE_CONFIG_FINGERPRINT="$(compute_formal_release_config_fingerprint)"
    if [[ ! "$FORMAL_RELEASE_CONFIG_FINGERPRINT" =~ ^[0-9a-f]{64}$ ]]; then
        fail "正式发布配置指纹生成失败"
        return 1
    fi
    if [ -L "$FORMAL_SMOKE_RECEIPT_ROOT" ]; then
        fail "正式天气烟测 receipt 根目录不得为符号链接"
        return 1
    fi
    mkdir -p "$FORMAL_SMOKE_RECEIPT_ROOT"
    chmod 0700 "$FORMAL_SMOKE_RECEIPT_ROOT"
    FORMAL_SMOKE_RECEIPT_DIR="$FORMAL_SMOKE_RECEIPT_ROOT/${FORMAL_RELEASE_COMMIT}-${FORMAL_RELEASE_CONFIG_FINGERPRINT}"
    binding_file="$FORMAL_SMOKE_RECEIPT_DIR/binding"
    started_file="$FORMAL_SMOKE_RECEIPT_DIR/started"
    completed_file="$FORMAL_SMOKE_RECEIPT_DIR/completed"
    if [ -e "$FORMAL_SMOKE_RECEIPT_DIR" ] || [ -L "$FORMAL_SMOKE_RECEIPT_DIR" ]; then
        verify_receipt_binding
        if [ -L "$started_file" ] || [ ! -f "$started_file" ]; then
            fail "正式天气烟测 receipt 状态不完整，必须人工核对"
            return 1
        fi
        if [ -e "$completed_file" ] || [ -L "$completed_file" ]; then
            if [ -L "$completed_file" ] || [ ! -f "$completed_file" ]; then
                fail "正式天气烟测 completed receipt 状态异常"
                return 1
            fi
            snapshot_id="$(receipt_value "$completed_file" snapshot_id || true)"
            verify_fresh_qweather_snapshot "$snapshot_id"
            FORMAL_SMOKE_REUSED=1
            printf 'snapshot_id=%s\nmode=reused_completed_receipt\n' "$snapshot_id" \
                > "$TRANSACTION_DIR/CACHE_SMOKE_VERIFIED"
            chmod 0600 "$TRANSACTION_DIR/CACHE_SMOKE_VERIFIED"
            log "已复用同一冻结发布的 completed 天气烟测 receipt，未再次请求上游"
            return 0
        fi
        fail "同一冻结 commit 与配置已有 started 天气烟测 receipt；禁止自动重试，请人工核对上游计数与数据库"
        return 1
    fi
    mkdir "$FORMAL_SMOKE_RECEIPT_DIR"
    chmod 0700 "$FORMAL_SMOKE_RECEIPT_DIR"
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    {
        printf 'release_commit=%s\n' "$FORMAL_RELEASE_COMMIT"
        printf 'config_fingerprint=%s\n' "$FORMAL_RELEASE_CONFIG_FINGERPRINT"
    } > "$binding_file.next.$$"
    chmod 0600 "$binding_file.next.$$"
    mv -f "$binding_file.next.$$" "$binding_file"
    printf 'started_at=%s\n' "$now" > "$started_file.next.$$"
    chmod 0600 "$started_file.next.$$"
    mv -f "$started_file.next.$$" "$started_file"
}

complete_formal_smoke_receipt() {
    local snapshot_id="$1"
    local completed_file="$FORMAL_SMOKE_RECEIPT_DIR/completed"
    {
        printf 'snapshot_id=%s\n' "$snapshot_id"
        printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$completed_file.next.$$"
    chmod 0600 "$completed_file.next.$$"
    mv -f "$completed_file.next.$$" "$completed_file"
}

run_formal_cache_smoke() {
    local previous_snapshot current_snapshot
    [ "$REQUIRE_WECHAT_READY" = 1 ] || return 0
    prepare_formal_smoke_receipt
    if [ "$FORMAL_SMOKE_REUSED" = 1 ]; then
        return 0
    fi
    previous_snapshot="$(latest_snapshot_id)"
    # 回滚保护已就绪后才临时开放本次烟测；成功后立即重新设置 30 分钟网络门。
    printf '0' \
        | "$VENV_DIR/bin/python" "$APP_DIR/scripts/update_env_value.py" \
            --file "$ENV_FILE" \
            --key QWEATHER_NETWORK_NOT_BEFORE_EPOCH \
            --mode always
    tighten_environment_permissions
    (
        cd "$APP_DIR"
        runtime_exec /bin/bash scripts/weather_cache_sync.sh --skip-nowcast
    )
    current_snapshot="$(latest_snapshot_id)"
    if [ -z "$current_snapshot" ] || [ "$current_snapshot" = "$previous_snapshot" ]; then
        fail "唯一一次天气同步烟测未生成新的持久化快照"
        return 1
    fi
    verify_fresh_qweather_snapshot "$current_snapshot"
    complete_formal_smoke_receipt "$current_snapshot"
    printf 'snapshot_id=%s\nmode=new_request\n' "$current_snapshot" > "$TRANSACTION_DIR/CACHE_SMOKE_VERIFIED"
    chmod 0600 "$TRANSACTION_DIR/CACHE_SMOKE_VERIFIED"
    log "唯一一次天气同步烟测与持久化快照校验通过"
}

start_new_release() {
    "$SYSTEMCTL_BIN" enable case-weather.service
    # 从公网服务启动这一刻起，可能已有用户写入，后续失败只允许向前修复。
    FORWARD_ONLY=1
    "$SYSTEMCTL_BIN" restart case-weather.service
    "$SYSTEMCTL_BIN" is-active --quiet case-weather.service
    wait_for_health "$HEALTH_URL"
}

prepare_release_timer_states() {
    local unit unit_file_state
    # 在公网切换前固定开机状态，失败时仍可由事务恢复旧配置。
    for unit in "${DEFERRED_TIMER_UNITS[@]}"; do
        "$SYSTEMCTL_BIN" disable "$unit" >/dev/null 2>&1 || true
        unit_exists "$unit" || {
            fail "延迟 timer 未正确安装: $unit"
            return 1
        }
        unit_file_state="$($SYSTEMCTL_BIN is-enabled "$unit" 2>/dev/null || true)"
        if [ "$unit_file_state" != disabled ]; then
            fail "延迟 timer 状态应为 disabled，实际为 ${unit_file_state:-unknown}: $unit"
            return 1
        fi
        if "$SYSTEMCTL_BIN" is-active --quiet "$unit"; then
            fail "延迟 timer 在首轮等待前已运行: $unit"
            return 1
        fi
    done
    for unit in "${START_TIMER_UNITS[@]}"; do
        "$SYSTEMCTL_BIN" enable "$unit"
        unit_file_state="$($SYSTEMCTL_BIN is-enabled "$unit" 2>/dev/null || true)"
        if [ "$unit_file_state" != enabled ]; then
            fail "开机 timer 状态应为 enabled，实际为 ${unit_file_state:-unknown}: $unit"
            return 1
        fi
        if "$SYSTEMCTL_BIN" is-active --quiet "$unit"; then
            fail "开机 timer 在正式提交前不应运行: $unit"
            return 1
        fi
    done
}

start_release_timers() {
    local unit
    for unit in "${START_TIMER_UNITS[@]}"; do
        "$SYSTEMCTL_BIN" restart "$unit"
        "$SYSTEMCTL_BIN" is-active --quiet "$unit"
    done
}

verify_release_state() {
    local unit unit_file_state on_success next_us uptime_us remaining_us link_target

    for unit in case-weather.service \
        case-weather-cache-bootstrap.timer \
        case-weather-risk-precompute.timer \
        case-weather-usage-cleanup.timer; do
        if ! "$SYSTEMCTL_BIN" is-active --quiet "$unit"; then
            fail "发布后单元未处于 active: $unit"
            return 1
        fi
    done

    unit_file_state="$($SYSTEMCTL_BIN is-enabled case-weather-cache-bootstrap.timer 2>/dev/null || true)"
    if [ "$unit_file_state" != enabled ]; then
        fail "bootstrap timer 状态应为 enabled，实际为 ${unit_file_state:-unknown}"
        return 1
    fi
    unit_exists case-weather-cache.timer || {
        fail "常规天气缓存 timer 未正确安装"
        return 1
    }
    unit_file_state="$($SYSTEMCTL_BIN is-enabled case-weather-cache.timer 2>/dev/null || true)"
    if [ "$unit_file_state" != disabled ]; then
        fail "常规天气缓存 timer 状态应为 disabled，实际为 ${unit_file_state:-unknown}"
        return 1
    fi
    if "$SYSTEMCTL_BIN" is-active --quiet case-weather-cache.timer; then
        fail "常规天气缓存 timer 在首轮等待期间不应提前运行"
        return 1
    fi

    on_success="$($SYSTEMCTL_BIN show case-weather-cache.service --property=OnSuccess --value)"
    case " $on_success " in
        *" case-weather-dispatch.service "*) ;;
        *) fail "天气缓存服务缺少 dispatch OnSuccess"; return 1 ;;
    esac
    on_success="$($SYSTEMCTL_BIN show case-weather-cache-bootstrap.service --property=OnSuccess --value)"
    case " $on_success " in
        *" case-weather-cache.timer "*) ;;
        *) fail "bootstrap 服务缺少 recurring timer OnSuccess"; return 1 ;;
    esac
    if "$SYSTEMCTL_BIN" is-active --quiet case-weather-dispatch.timer \
        || unit_exists case-weather-dispatch.timer; then
        fail "旧 dispatch.timer 仍存在"
        return 1
    fi

    next_us="$($BUSCTL_BIN get-property \
        org.freedesktop.systemd1 \
        /org/freedesktop/systemd1/unit/case_2dweather_2dcache_2dbootstrap_2etimer \
        org.freedesktop.systemd1.Timer \
        NextElapseUSecMonotonic \
        | awk '{print $2}')"
    uptime_us="$(awk '{printf "%.0f", $1 * 1000000}' "$UPTIME_FILE")"
    if [[ ! "$next_us" =~ ^[0-9]+$ || ! "$uptime_us" =~ ^[0-9]+$ ]]; then
        fail "bootstrap timer 单调时钟状态无效"
        return 1
    fi
    remaining_us=$((next_us - uptime_us))
    if [ "$remaining_us" -lt 1700000000 ] || [ "$remaining_us" -gt 1810000000 ]; then
        fail "bootstrap timer 未保留完整的首轮 30 分钟等待窗口"
        return 1
    fi

    link_target="$(readlink "$CURRENT_LINK")"
    if [ "$link_target" != "$NEW_RELEASE" ]; then
        fail "current 链接未指向本次发布"
        return 1
    fi
    if [ -e "$STAGED_ENV_FILE" ]; then
        fail "候选环境文件在提交前未清理"
        return 1
    fi
    wait_for_health "$HEALTH_URL"
    log "发布后服务、timer、OnSuccess、链接与健康检查全部通过"
}

observe_post_commit_stability() {
    local elapsed=0 wait_seconds remaining
    if [ "$POST_COMMIT_STABILITY_SECONDS" -eq 0 ]; then
        return 0
    fi

    log "进入 ${POST_COMMIT_STABILITY_SECONDS} 秒发布稳定观察窗"
    while [ "$elapsed" -lt "$POST_COMMIT_STABILITY_SECONDS" ]; do
        remaining=$((POST_COMMIT_STABILITY_SECONDS - elapsed))
        wait_seconds="$POST_COMMIT_STABILITY_INTERVAL_SECONDS"
        if [ "$wait_seconds" -gt "$remaining" ]; then
            wait_seconds="$remaining"
        fi
        sleep "$wait_seconds"
        elapsed=$((elapsed + wait_seconds))
        verify_release_state
    done
    log "发布稳定观察窗通过"
}

captured_unit_active() {
    local wanted="$1"
    awk -F '\t' -v wanted="$wanted" '
        $1 == wanted && $2 == "1" && $4 == "active" { found = 1 }
        END { exit(found ? 0 : 1) }
    ' "$STATE_FILE"
}

captured_unit_running() {
    local wanted="$1"
    awk -F '\t' -v wanted="$wanted" '
        $1 == wanted && $2 == "1" && ($4 == "active" || $4 == "activating" || $4 == "reloading") { found = 1 }
        END { exit(found ? 0 : 1) }
    ' "$STATE_FILE"
}

restore_start_unit() {
    local unit="$1"
    "$SYSTEMCTL_BIN" start "$unit" || return 1
    "$SYSTEMCTL_BIN" is-active --quiet "$unit" || return 1
}

restore_database() {
    local suffix moved_path restore_tmp
    if [ "$DB_MUTATION_STARTED" -ne 1 ]; then
        return 0
    fi
    mkdir -p "$TRANSACTION_DIR/database-sidecars"
    for suffix in -wal -shm; do
        if [ -e "$DATABASE_FILE$suffix" ]; then
            moved_path="$TRANSACTION_DIR/database-sidecars/$(basename "$DATABASE_FILE")$suffix"
            mv "$DATABASE_FILE$suffix" "$moved_path" || return 1
        fi
    done
    if [ "$DB_EXISTED" -eq 1 ]; then
        [ "$DB_BACKUP_READY" -eq 1 ] || return 1
        sqlite_quick_check "$DB_BACKUP" || return 1
        restore_tmp="$DATABASE_FILE.rollback.$$"
        cp -a "$DB_BACKUP" "$restore_tmp" || return 1
        atomic_replace "$restore_tmp" "$DATABASE_FILE" || return 1
        tighten_database_permissions || return 1
        sqlite_quick_check "$DATABASE_FILE" || return 1
    elif [ -e "$DATABASE_FILE" ]; then
        mv "$DATABASE_FILE" "$TRANSACTION_DIR/database-created-by-failed-release.db" || return 1
    fi
}

restore_environment() {
    local failed_env="$TRANSACTION_DIR/environment-from-failed-release.env"
    [ "$ENV_MUTATION_STARTED" -eq 1 ] || return 0
    if [ -e "$ENV_FILE" ]; then
        mv "$ENV_FILE" "$failed_env" || return 1
        chmod 0600 "$failed_env" || return 1
    fi
    if [ "$ENV_EXISTED" -eq 1 ]; then
        [ "$ENV_BACKUP_READY" -eq 1 ] || return 1
        cp -a "$ENV_BACKUP" "$ENV_FILE.restore.$$" || return 1
        chmod 0600 "$ENV_FILE.restore.$$" || return 1
        atomic_replace "$ENV_FILE.restore.$$" "$ENV_FILE" || return 1
        tighten_environment_permissions || return 1
    fi
}

restore_current_link() {
    local old_target
    [ "$LINK_MUTATED" -eq 1 ] || return 0
    old_target="$(cat "$OLD_LINK_FILE")"
    if [ "$old_target" = '__ABSENT__' ]; then
        if [ -L "$CURRENT_LINK" ]; then
            mv "$CURRENT_LINK" "$TRANSACTION_DIR/current-link-from-failed-release" || return 1
        fi
        return 0
    fi
    switch_current_link "$old_target" || return 1
}

restore_unit_files() {
    local unit exists _enabled _active
    local removed_dir="$TRANSACTION_DIR/units-from-failed-release"
    mkdir -p "$removed_dir"
    while IFS=$'\t' read -r unit exists _enabled _active; do
        if [ "$exists" = 1 ] && [ -f "$TRANSACTION_DIR/units/$unit" ]; then
            cp -a "$TRANSACTION_DIR/units/$unit" "$UNIT_DIR/$unit.restore.$$" || return 1
            mv -f "$UNIT_DIR/$unit.restore.$$" "$UNIT_DIR/$unit" || return 1
        elif [ -e "$UNIT_DIR/$unit" ] || [ -L "$UNIT_DIR/$unit" ]; then
            # 首次发布失败时先清掉新 timer 的 enable 链接，避免留下悬空开机入口。
            if [[ "$unit" == *.timer ]]; then
                "$SYSTEMCTL_BIN" disable "$unit" >/dev/null 2>&1 || return 1
            fi
            mv "$UNIT_DIR/$unit" "$removed_dir/$unit" || return 1
        fi
    done < "$STATE_FILE"
    "$SYSTEMCTL_BIN" daemon-reload || return 1
}

restore_unit_states() {
    local unit exists enabled active
    while IFS=$'\t' read -r unit exists enabled active; do
        [ "$exists" = 1 ] || continue
        case "$enabled" in
            enabled) "$SYSTEMCTL_BIN" enable "$unit" >/dev/null || return 1 ;;
            enabled-runtime) "$SYSTEMCTL_BIN" enable --runtime "$unit" >/dev/null || return 1 ;;
            disabled) "$SYSTEMCTL_BIN" disable "$unit" >/dev/null || return 1 ;;
        esac
    done < "$STATE_FILE"

    # 先恢复公网应用，再恢复 timer。被中断的 oneshot writer 不直接重跑，避免重复写入或额外天气调用。
    if captured_unit_running case-weather.service; then
        restore_start_unit case-weather.service || return 1
    fi

    for unit in case-weather-risk-precompute.timer \
        case-weather-usage-cleanup.timer \
        "${LEGACY_UNITS[@]}"; do
        if captured_unit_active "$unit"; then
            restore_start_unit "$unit" || return 1
        fi
    done

    # 天气调度只恢复一个阶段，防止 bootstrap 与 recurring 双重触发。
    if captured_unit_active case-weather-cache.timer; then
        restore_start_unit case-weather-cache.timer || return 1
    elif captured_unit_active case-weather-cache-bootstrap.timer; then
        restore_start_unit case-weather-cache-bootstrap.timer || return 1
    elif captured_unit_running case-weather-cache-bootstrap.service \
        || captured_unit_running case-weather-cache.service; then
        if unit_exists case-weather-cache-bootstrap.timer; then
            restore_start_unit case-weather-cache-bootstrap.timer || return 1
            log "检测到被中断的天气同步，已改为 30 分钟后安全重试"
        fi
    fi
}

rollback_release() {
    local failed=0
    log "激活失败，开始恢复部署前状态"
    set +e
    if ! stop_units_best_effort; then
        failed=1
    fi
    if [ "$failed" -eq 0 ]; then
        restore_database || failed=1
        restore_environment || failed=1
        restore_current_link || failed=1
        if [ "$UNITS_MUTATED" -eq 1 ]; then
            restore_unit_files || failed=1
        fi
        restore_unit_states || failed=1
    fi
    set -e

    if [ "$failed" -ne 0 ]; then
        stop_units_best_effort >/dev/null 2>&1 || true
        {
            echo '自动回滚未完整成功。全部业务单元已尽力停止。'
            echo "事务目录: $TRANSACTION_DIR"
            echo '请人工核对数据库、current 链接和 systemd unit 后再启动服务。'
        } > "$FAILURE_MARKER"
        log "回滚失败，已写入人工恢复标记: $FAILURE_MARKER" >&2
        return 1
    fi
    printf '%s\n' 'success' > "$ROLLED_BACK_MARKER"
    log "已恢复部署前配置、数据库、代码入口与 systemd 状态"
}

on_exit() {
    local rc=$?
    trap - EXIT INT TERM HUP
    stop_candidate_release
    if [ "$rc" -eq 0 ]; then
        exit 0
    fi
    if [ "$COMMITTED" -eq 1 ] || [ "$FORWARD_ONLY" -eq 1 ]; then
        {
            if [ "$COMMITTED" -eq 1 ]; then
                echo '新版本已通过首次公网健康检查并进入向前提交阶段；timer 启动或完整状态复核失败，为避免覆盖用户写入，本次不会回滚数据库。'
            else
                echo '公网服务已尝试启动，期间可能已有用户写入；本次保留向前迁移后的数据库、环境和代码入口。'
            fi
            echo "事务目录: $TRANSACTION_DIR"
            echo '请检查 systemctl status、应用日志与 timer 状态，并在当前版本上向前修复。'
        } > "$POST_COMMIT_MARKER"
        log "向前修复阶段失败，已保留新版本并写入标记: $POST_COMMIT_MARKER" >&2
        exit "$rc"
    fi
    if [ "$MUTATION_STARTED" -eq 0 ]; then
        exit "$rc"
    fi
    if rollback_release; then
        exit "$rc"
    fi
    exit 70
}

trap on_exit EXIT
trap 'exit 130' INT TERM HUP

validate_absolute_path STATE_DIR "$STATE_DIR"
validate_absolute_path RELEASE_ROOT "$RELEASE_ROOT"
validate_absolute_path NEW_RELEASE "$NEW_RELEASE"
validate_absolute_path CURRENT_LINK "$CURRENT_LINK"
validate_absolute_path ENV_FILE "$ENV_FILE"
validate_absolute_path STAGED_ENV_FILE "$STAGED_ENV_FILE"
validate_absolute_path UNIT_DIR "$UNIT_DIR"
if [ -n "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ]; then
    validate_absolute_path RECOVERY_ACKNOWLEDGED_TRANSACTION "$RECOVERY_ACKNOWLEDGED_TRANSACTION"
    case "$RECOVERY_ACKNOWLEDGED_TRANSACTION" in
        "$TRANSACTION_ROOT"/*) ;;
        *) echo 'RECOVERY_ACKNOWLEDGED_TRANSACTION 必须位于部署事务根目录下' >&2; exit 2 ;;
    esac
fi
case "$NEW_RELEASE" in
    "$RELEASE_ROOT"/releases/*) ;;
    *) echo 'NEW_RELEASE 必须位于 RELEASE_ROOT/releases 下' >&2; exit 2 ;;
esac
case "$STAGED_ENV_FILE" in
    "$NEW_RELEASE"/*) ;;
    *) echo 'STAGED_ENV_FILE 必须位于 NEW_RELEASE 下' >&2; exit 2 ;;
esac
if [[ ! "$CANDIDATE_BIND" =~ ^127\.0\.0\.1:[0-9]{2,5}$ ]]; then
    echo 'CANDIDATE_BIND 必须使用 127.0.0.1 的高位端口' >&2
    exit 2
fi
CANDIDATE_PORT="${CANDIDATE_BIND##*:}"
if [ "$CANDIDATE_PORT" -lt 1024 ] || [ "$CANDIDATE_PORT" -gt 65535 ]; then
    echo 'CANDIDATE_BIND 端口必须位于 1024 至 65535' >&2
    exit 2
fi
if [ "$CANDIDATE_HEALTH_URL" != "http://$CANDIDATE_BIND/healthz" ]; then
    echo 'CANDIDATE_HEALTH_URL 必须与本机候选端口一致' >&2
    exit 2
fi
case "$POST_COMMIT_STABILITY_SECONDS" in
    ''|*[!0-9]*)
        echo 'POST_COMMIT_STABILITY_SECONDS 必须是 0 至 90 的整数' >&2
        exit 2
        ;;
esac
if [ "$POST_COMMIT_STABILITY_SECONDS" -gt 90 ]; then
    echo 'POST_COMMIT_STABILITY_SECONDS 必须是 0 至 90 的整数' >&2
    exit 2
fi
case "$POST_COMMIT_STABILITY_INTERVAL_SECONDS" in
    ''|0|*[!0-9]*)
        echo 'POST_COMMIT_STABILITY_INTERVAL_SECONDS 必须是 1 至 30 的整数' >&2
        exit 2
        ;;
esac
if [ "$POST_COMMIT_STABILITY_INTERVAL_SECONDS" -gt 30 ]; then
    echo 'POST_COMMIT_STABILITY_INTERVAL_SECONDS 必须是 1 至 30 的整数' >&2
    exit 2
fi
case "$REQUIRE_WECHAT_READY" in
    0|1) ;;
    *) echo 'REQUIRE_WECHAT_READY 必须是 0 或 1' >&2; exit 2 ;;
esac
if [[ ! "$RUNTIME_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] \
    || [[ ! "$RUNTIME_GROUP" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
    echo '运行账户或组名格式异常' >&2
    exit 2
fi
if [[ ! "$CONTROL_OWNER_UID" =~ ^[0-9]+$ ]] \
    || [[ ! "$CONTROL_OWNER_GID" =~ ^[0-9]+$ ]]; then
    echo '控制目录所有者 UID/GID 格式异常' >&2
    exit 2
fi
id -u "$RUNTIME_USER" >/dev/null 2>&1 || {
    echo '缺少 case-weather 运行账户' >&2
    exit 2
}
if [ "$(id -gn "$RUNTIME_USER")" != "$RUNTIME_GROUP" ]; then
    echo 'case-weather 运行账户主组异常' >&2
    exit 2
fi
require_file "$ENV_FILE"
require_file "$STAGED_ENV_FILE"
require_file "$APP_DIR/scripts/server_migrate.sh"
require_file "$APP_DIR/scripts/update_env_value.py"
if [ "$REQUIRE_WECHAT_READY" = 1 ]; then
    require_file "$APP_DIR/scripts/weather_cache_sync.sh"
fi
require_executable "$VENV_DIR/bin/python"
require_executable "$VENV_DIR/bin/gunicorn"
command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1 || require_executable "$SYSTEMCTL_BIN"
command -v "$SQLITE3_BIN" >/dev/null 2>&1 || require_executable "$SQLITE3_BIN"
command -v "$CURL_BIN" >/dev/null 2>&1 || require_executable "$CURL_BIN"
command -v "$FLOCK_BIN" >/dev/null 2>&1 || require_executable "$FLOCK_BIN"
command -v "$BUSCTL_BIN" >/dev/null 2>&1 || require_executable "$BUSCTL_BIN"
if [ "$(id -u)" != "$(id -u "$RUNTIME_USER")" ]; then
    command -v "$RUNUSER_BIN" >/dev/null 2>&1 || require_executable "$RUNUSER_BIN"
fi
command -v "$CHOWN_BIN" >/dev/null 2>&1 || require_executable "$CHOWN_BIN"
command -v "$ENV_BIN" >/dev/null 2>&1 || require_executable "$ENV_BIN"
require_file "$UPTIME_FILE"
validate_release_dependencies
validate_formal_release_identity

mkdir -p "$RELEASE_ROOT"
prepare_control_directories
validate_recovery_transaction_realpath
exec 9> "$RELEASE_ROOT/deploy.lock"
if ! "$FLOCK_BIN" -n 9; then
    echo '已有另一个部署事务正在运行，本次发布未修改生产状态。' >&2
    exit 73
fi

acknowledge_recovery_transaction
detect_unfinished_transactions
mkdir -p "$TRANSACTION_DIR"
capture_previous_state
printf '%s\n' "$NEW_RELEASE" > "$STARTED_MARKER"

MUTATION_STARTED=1
stop_units_strictly
backup_environment
apply_staged_environment
prepare_runtime_permissions
# 候选进程、迁移和正式烟测必须读取刚刚应用的同一份外置配置。
export CASE_WEATHER_ENV_FILE="$ENV_FILE"

DATABASE_FILE="$(resolve_database_file)"
validate_absolute_path DATABASE_FILE "$DATABASE_FILE"
export DATABASE_FILE
tighten_database_permissions
backup_database

DB_MUTATION_STARTED=1
log "运行数据库迁移"
(
    cd "$APP_DIR"
    VENV_PY="$VENV_DIR/bin/python" bash scripts/server_migrate.sh
)
tighten_database_permissions
sqlite_quick_check "$DATABASE_FILE"
sqlite_foreign_key_check "$DATABASE_FILE"

start_candidate_release
run_formal_cache_smoke
arm_qweather_network_gate

LINK_MUTATED=1
switch_current_link "$NEW_RELEASE"
install_new_units
prepare_release_timer_states
start_new_release

mkdir -p "$STATE_DIR/deployments"
printf '%s\n' "$NEW_RELEASE" > "$STATE_DIR/deployments/current-release.next.$$"
mv -f "$STATE_DIR/deployments/current-release.next.$$" "$STATE_DIR/deployments/current-release"
COMMITTED=1
start_release_timers
verify_release_state
observe_post_commit_stability
printf '%s\n' 'success' > "$TRANSACTION_DIR/COMMITTED"
log "发布已提交: $NEW_RELEASE"
log "运维提示：后续只清理临时 preflight/activate 单元，禁止停止或禁用天气缓存 timer"
