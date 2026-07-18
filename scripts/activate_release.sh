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
BACKUP_WAIT_ATTEMPTS="${BACKUP_WAIT_ATTEMPTS:-180}"
BACKUP_WAIT_SLEEP_SECONDS="${BACKUP_WAIT_SLEEP_SECONDS:-5}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SYSTEMD_RUN_BIN="${SYSTEMD_RUN_BIN:-systemd-run}"
SQLITE3_BIN="${SQLITE3_BIN:-sqlite3}"
CURL_BIN="${CURL_BIN:-curl}"
FLOCK_BIN="${FLOCK_BIN:-flock}"
BUSCTL_BIN="${BUSCTL_BIN:-busctl}"
FINDMNT_BIN="${FINDMNT_BIN:-findmnt}"
SYNC_BIN="${SYNC_BIN:-/bin/sync}"
RUNUSER_BIN="${RUNUSER_BIN:-runuser}"
CHOWN_BIN="${CHOWN_BIN:-chown}"
ENV_BIN="${ENV_BIN:-/usr/bin/env}"
CRONTAB_BIN="${CRONTAB_BIN:-crontab}"
PGREP_BIN="${PGREP_BIN:-pgrep}"
UPTIME_FILE="${UPTIME_FILE:-/proc/uptime}"
INHERITED_DATABASE_FILE="${DATABASE_FILE:-}"
INHERITED_DATABASE_URI="${DATABASE_URI:-}"
DATABASE_FILE=""
unset DATABASE_URI
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
BACKUP_RUNTIME_ENV_FILE="$STATE_DIR/backups/backup-runtime.env"
BACKUP_RUNTIME_ENV_BACKUP="$TRANSACTION_DIR/backup-runtime-before.env"
FAILURE_MARKER="$TRANSACTION_DIR/ROLLBACK_REQUIRED.txt"
POST_COMMIT_MARKER="$TRANSACTION_DIR/POST_COMMIT_ATTENTION.txt"
STARTED_MARKER="$TRANSACTION_DIR/ACTIVATION_STARTED"
ROLLED_BACK_MARKER="$TRANSACTION_DIR/ROLLED_BACK"
CAPTURED_STATE_CHECKPOINT="$TRANSACTION_DIR/CAPTURED_STATE_DURABLE"
RECOVERY_MATERIALS_CHECKPOINT="$TRANSACTION_DIR/RECOVERY_MATERIALS_DURABLE"
RECOVERY_CONFIRMED_MARKER_NAME="RECOVERY_CONFIRMED"
ROOT_CRONTAB_SNAPSHOT="$TRANSACTION_DIR/root-crontab.before"
ROOT_CRONTAB_SNAPSHOT_STATUS="$TRANSACTION_DIR/root-crontab.before.status"
ROOT_CRONTAB_SNAPSHOT_SHA256="$TRANSACTION_DIR/root-crontab.before.sha256"
ROOT_CRONTAB_FILTERED="$TRANSACTION_DIR/root-crontab.after-removal"
ROOT_CRONTAB_PREFLIGHT_PLAN="$TRANSACTION_DIR/root-crontab.preflight.plan"
ROOT_CRONTAB_BEFORE_ACTIVATION="$TRANSACTION_DIR/root-crontab.before-activation.verified"
ROOT_CRONTAB_BEFORE_ACTIVATION_STATUS="$TRANSACTION_DIR/root-crontab.before-activation.verified.status"
BACKUP_VALIDATION_DIR="$STATE_DIR/backups/validation/${TRANSACTION_DIR##*/}"
BACKUP_VALIDATION_ARCHIVE_DIR="$TRANSACTION_DIR/managed-backup-validation"
RUNTIME_BOOT_GUARD_DIR="${RUNTIME_BOOT_GUARD_DIR:-/run/case-weather}"
ALLOW_NONROOT_TEST_RUNTIME_GUARD="${ALLOW_NONROOT_TEST_RUNTIME_GUARD:-0}"
RUNTIME_BOOT_GUARD_FILE="$RUNTIME_BOOT_GUARD_DIR/activation-permit"
ACTIVATION_BOOT_GUARD_FILE="$STATE_DIR/deployments/activation-in-progress"
ACTIVATION_GUARD_DROPIN_NAME="10-case-weather-activation-guard.conf"
LEGACY_BACKUP_CRON_LINE="0 3 * * * $STATE_DIR/backup.sh >> $STATE_DIR/backups/backup.log 2>&1"
LEGACY_BACKUP_RELEASE_CRON_LINE="0 3 * * * PROJECT_DIR=$STATE_DIR ENV_FILE=$STATE_DIR/.env BACKUP_DIR=$STATE_DIR/backups $CURRENT_LINK/app/scripts/backup.sh >> $STATE_DIR/backups/backup.log 2>&1"
LEGACY_SYNC_CRON_LINE="0 6 * * * TZ=Asia/Shanghai $STATE_DIR/venv/bin/python3 $STATE_DIR/services/pipelines/sync_weather_data.py --daily >> $STATE_DIR/logs/weather_sync.log 2>&1"

START_TIMER_UNITS=(
    case-weather-backup.timer
    case-weather-cache-bootstrap.timer
    case-weather-risk-precompute.timer
    case-weather-usage-cleanup.timer
)
DEFERRED_TIMER_UNITS=(
    case-weather-cache.timer
)
MANAGED_TIMER_UNITS=("${START_TIMER_UNITS[@]}" "${DEFERRED_TIMER_UNITS[@]}")
LEGACY_TIMER_UNITS=(
    case-weather-dispatch.timer
    case-weather-sync.timer
)
LEGACY_SERVICE_UNITS=(
    case-weather-sync.service
)
SERVICE_UNITS=(
    case-weather-backup.service
    case-weather-cache-bootstrap.service
    case-weather-cache.service
    case-weather-dispatch.service
    case-weather-risk-precompute.service
    case-weather-usage-cleanup.service
    case-weather.service
)
INSTALL_UNITS=("${MANAGED_TIMER_UNITS[@]}" "${SERVICE_UNITS[@]}")
LEGACY_UNITS=("${LEGACY_TIMER_UNITS[@]}" "${LEGACY_SERVICE_UNITS[@]}")
SCHEDULER_UNITS=("${MANAGED_TIMER_UNITS[@]}" "${LEGACY_TIMER_UNITS[@]}")
STOPPABLE_SERVICE_UNITS=(
    case-weather-cache-bootstrap.service
    case-weather-cache.service
    case-weather-dispatch.service
    case-weather-risk-precompute.service
    case-weather-usage-cleanup.service
    case-weather.service
    "${LEGACY_SERVICE_UNITS[@]}"
)
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
BACKUP_RUNTIME_ENV_MUTATION_STARTED=0
BACKUP_RUNTIME_ENV_EXISTED=0
BACKUP_RUNTIME_ENV_BACKUP_READY=0
LINK_MUTATED=0
UNITS_MUTATED=0
RUNTIME_QUIESCE_STARTED=0
CANDIDATE_PID=""
FORMAL_RELEASE_COMMIT=""
FORMAL_RELEASE_CONFIG_FINGERPRINT=""
FORMAL_SMOKE_RECEIPT_DIR=""
FORMAL_SMOKE_REUSED=0
FORMAL_SMOKE_IRREVERSIBLE=0
FORMAL_NETWORK_GATE_OPEN=0

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

validate_runtime_boot_guard_location() {
    local runtime_uid runtime_fstype
    runtime_uid="$(id -u)"
    case "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" in
        0|1) ;;
        *) echo 'ALLOW_NONROOT_TEST_RUNTIME_GUARD 必须是 0 或 1' >&2; exit 2 ;;
    esac
    if [ "$runtime_uid" -ne 0 ]; then
        if [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" != 1 ]; then
            fail "正式激活必须由 root 执行并使用易失运行目录"
            return 1
        fi
        return 0
    fi
    if [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" != 0 ] \
        || [ "$RUNTIME_BOOT_GUARD_DIR" != /run/case-weather ]; then
        fail "正式激活的运行期开机许可必须固定在 /run/case-weather"
        return 1
    fi
    command -v "$FINDMNT_BIN" >/dev/null 2>&1 || require_executable "$FINDMNT_BIN"
    if ! runtime_fstype="$($FINDMNT_BIN -n -o FSTYPE -T /run 2>/dev/null)" \
        || [ "$runtime_fstype" != tmpfs ]; then
        fail "/run 必须由 tmpfs 提供，防止 activation permit 跨重启残留"
        return 1
    fi
    if [ "$SYNC_BIN" != /bin/sync ] || [ ! -x "$SYNC_BIN" ]; then
        fail "正式激活的 durability barrier 必须固定为 /bin/sync"
        return 1
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

UNIT_LOAD_STATE=""
UNIT_ACTIVE_STATE=""

query_unit_load_state() {
    local unit="$1"
    local state
    UNIT_LOAD_STATE=""
    if ! state="$($SYSTEMCTL_BIN show \
        "$unit" \
        --property=LoadState \
        --value 2>/dev/null)"; then
        fail "无法可靠读取 systemd 单元 LoadState: $unit"
        return 1
    fi
    case "$state" in
        loaded|not-found) UNIT_LOAD_STATE="$state" ;;
        *)
            fail "systemd 单元 LoadState 不确定: $unit=${state:-unknown}"
            return 1
            ;;
    esac
}

query_unit_active_state() {
    local unit="$1"
    local state rc=0
    UNIT_ACTIVE_STATE=""
    state="$($SYSTEMCTL_BIN is-active "$unit" 2>/dev/null)" || rc=$?
    case "$rc" in
        0|3) ;;
        *)
            fail "无法可靠读取 systemd 单元 ActiveState: $unit"
            return 1
            ;;
    esac
    case "$state" in
        active|activating|reloading|deactivating|inactive|failed)
            UNIT_ACTIVE_STATE="$state"
            ;;
        *)
            fail "systemd 单元 ActiveState 不确定: $unit=${state:-unknown}"
            return 1
            ;;
    esac
}

capture_previous_state() {
    mkdir -p "$TRANSACTION_DIR/units"
    : > "$STATE_FILE"
    if [ -L "$CURRENT_LINK" ]; then
        readlink "$CURRENT_LINK" > "$OLD_LINK_FILE"
    else
        printf '%s\n' '__ABSENT__' > "$OLD_LINK_FILE"
    fi

    local unit source source_present exists enabled active enabled_rc
    for unit in "${ALL_UNITS[@]}"; do
        exists=0
        enabled=not-found
        active=inactive
        source_present=0
        source="$UNIT_DIR/$unit"
        if [ -e "$source" ] || [ -L "$source" ]; then
            if ! "$VENV_DIR/bin/python" - \
                "$source" \
                "$UNIT_DIR" \
                "$CONTROL_OWNER_UID" \
                "$CONTROL_OWNER_GID" <<'PY'
from pathlib import Path
import stat
import sys

source = Path(sys.argv[1])
unit_root = Path(sys.argv[2]).resolve(strict=True)
file_stat = source.lstat()
if (
    source.parent.resolve(strict=True) != unit_root
    or not stat.S_ISREG(file_stat.st_mode)
    or stat.S_ISLNK(file_stat.st_mode)
    or file_stat.st_uid != int(sys.argv[3])
    or file_stat.st_gid != int(sys.argv[4])
):
    raise SystemExit(1)
PY
            then
                fail "旧 systemd unit 文件类型、路径或所有权异常: $source"
                return 1
            fi
            cp -a "$source" "$TRANSACTION_DIR/units/$unit"
            source_present=1
        fi

        query_unit_load_state "$unit"
        if [ "$UNIT_LOAD_STATE" = loaded ]; then
            if [ "$source_present" -ne 1 ]; then
                fail "已加载的 systemd unit 不在受控路径: $unit"
                return 1
            fi
            exists=1
            enabled_rc=0
            enabled="$($SYSTEMCTL_BIN is-enabled "$unit" 2>/dev/null)" || enabled_rc=$?
            if [ "$enabled_rc" -gt 1 ] \
                || [[ ! "$enabled" =~ ^(enabled|enabled-runtime|disabled)$ ]]; then
                fail "无法可靠读取旧 systemd unit 的 enable 状态: $unit"
                return 1
            fi
            query_unit_active_state "$unit"
            active="$UNIT_ACTIVE_STATE"
        elif [ "$source_present" -ne 0 ]; then
            fail "受控路径中的 systemd unit 未被 systemd 正确加载: $unit"
            return 1
        fi
        printf '%s\t%s\t%s\t%s\n' "$unit" "$exists" "$enabled" "$active" >> "$STATE_FILE"
    done
}

capture_root_crontab_to() {
    local destination="$1"
    local status_file="$2"
    local error_file="$destination.stderr"
    local rc

    if LC_ALL=C "$CRONTAB_BIN" -u root -l > "$destination" 2> "$error_file"; then
        printf '%s\n' present > "$status_file"
    else
        rc=$?
        if [ "$rc" -eq 1 ] && grep -Fqi 'no crontab for' "$error_file"; then
            : > "$destination"
            printf '%s\n' absent > "$status_file"
        else
            fail "无法读取 root crontab，拒绝继续"
            return 1
        fi
    fi
    chmod 0600 "$destination" "$status_file" "$error_file"
}

hash_file_sha256() {
    local source="$1"
    local destination="$2"
    "$VENV_DIR/bin/python" - "$source" "$destination" <<'PY'
import hashlib
from pathlib import Path
import sys

source, destination = map(Path, sys.argv[1:])
destination.write_text(hashlib.sha256(source.read_bytes()).hexdigest() + '\n')
PY
    chmod 0600 "$destination"
}

build_root_crontab_removal_plan() {
    local source="$1"
    local filtered="$2"
    local plan_file="$3"
    "$VENV_DIR/bin/python" - \
        "$source" \
        "$filtered" \
        "$plan_file" \
        "$LEGACY_BACKUP_CRON_LINE" \
        "$LEGACY_BACKUP_RELEASE_CRON_LINE" \
        "$LEGACY_SYNC_CRON_LINE" \
        "$STATE_DIR" <<'PY'
import os
from pathlib import Path
import sys

source, filtered, plan_file = map(Path, sys.argv[1:4])
backup_lines = {
    os.fsencode(sys.argv[4]),
    os.fsencode(sys.argv[5]),
}
sync_line = os.fsencode(sys.argv[6])
state_dir = os.fsencode(sys.argv[7].rstrip('/'))
data = source.read_bytes()


def records(payload):
    """按 LF 拆分并保留每个字节，避免改写无关 cron。"""
    result = []
    start = 0
    while True:
        end = payload.find(b'\n', start)
        if end < 0:
            if start < len(payload):
                result.append(payload[start:])
            return result
        result.append(payload[start:end + 1])
        start = end + 1


def body(record):
    return record[:-1] if record.endswith(b'\n') else record


cron_records = records(data)
bodies = [body(record) for record in cron_records]
backup_count = sum(bodies.count(value) for value in backup_lines)
sync_count = bodies.count(sync_line)
recognized = backup_lines | {sync_line}
suspicious_tokens = (
    state_dir + b'/backup.sh',
    state_dir + b'/backups/backup.log',
    state_dir + b'/venv/bin/python3',
    state_dir + b'/services/pipelines/sync_weather_data.py',
    state_dir + b'/logs/weather_sync.log',
    b'case-weather-sync',
    b'case-weather-backup',
)
suspicious = [
    value for value in bodies
    if value not in recognized
    and any(token in value for token in suspicious_tokens)
]

if backup_count == 1 and sync_count == 1 and not suspicious:
    filtered.write_bytes(b''.join(
        record for record in cron_records
        if body(record) not in recognized
    ))
    plan_file.write_text('remove\n', encoding='ascii')
elif backup_count == 0 and sync_count == 0 and not suspicious:
    filtered.write_bytes(data)
    plan_file.write_text('noop\n', encoding='ascii')
else:
    print(
        'root crontab 中旧任务必须各出现一次或同时完全缺席；'
        f'backup={backup_count}, sync={sync_count}, suspicious={len(suspicious)}',
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
    chmod 0600 "$filtered" "$plan_file"
}

preflight_root_crontab() {
    capture_root_crontab_to \
        "$ROOT_CRONTAB_SNAPSHOT" \
        "$ROOT_CRONTAB_SNAPSHOT_STATUS"
    hash_file_sha256 "$ROOT_CRONTAB_SNAPSHOT" "$ROOT_CRONTAB_SNAPSHOT_SHA256"
    if ! build_root_crontab_removal_plan \
        "$ROOT_CRONTAB_SNAPSHOT" \
        "$ROOT_CRONTAB_FILTERED" \
        "$ROOT_CRONTAB_PREFLIGHT_PLAN"; then
        fail "root crontab 旧任务存在缺失、重复或漂移，尚未修改生产状态"
        return 1
    fi
    if [ "$(<"$ROOT_CRONTAB_PREFLIGHT_PLAN")" = remove ]; then
        fail "检测到旧 root cron；请先在受控维护窗口完成快照、精确迁移与复核，激活事务不会整表改写 crontab"
        return 1
    fi
}

verify_root_crontab_retired_before_activation() {
    local live_file="$TRANSACTION_DIR/root-crontab.before-activation"
    local live_status="$TRANSACTION_DIR/root-crontab.before-activation.status"
    local live_filtered="$TRANSACTION_DIR/root-crontab.before-activation.filtered"
    local live_plan="$TRANSACTION_DIR/root-crontab.before-activation.plan"

    # 激活事务只读校验 cron，避免 crontab 整表安装覆盖并发人工编辑。
    capture_root_crontab_to "$live_file" "$live_status"
    if ! build_root_crontab_removal_plan \
        "$live_file" \
        "$live_filtered" \
        "$live_plan"; then
        fail "root crontab 在预检后出现缺失、重复或漂移，尚未修改生产状态"
        return 1
    fi
    if [ "$(<"$live_plan")" != noop ]; then
        fail "root crontab 在激活前重新出现旧任务；请完成受控迁移后重试"
        return 1
    fi
    cp -a "$live_file" "$ROOT_CRONTAB_BEFORE_ACTIVATION"
    cp -a "$live_status" "$ROOT_CRONTAB_BEFORE_ACTIVATION_STATUS"
    log "root crontab 已由发布前受控迁移清理，本事务保持只读"
}

verify_root_crontab_retired() {
    local current="$TRANSACTION_DIR/root-crontab.verified"
    local status="$TRANSACTION_DIR/root-crontab.verified.status"
    local filtered="$TRANSACTION_DIR/root-crontab.verified.filtered"
    local plan="$TRANSACTION_DIR/root-crontab.verified.plan"
    capture_root_crontab_to "$current" "$status"
    build_root_crontab_removal_plan "$current" "$filtered" "$plan"
    if [ "$(<"$plan")" != noop ]; then
        fail "发布后 root crontab 仍含旧任务"
        return 1
    fi
}

fsync_directory() {
    "$VENV_DIR/bin/python" - "$1" <<'PY'
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

write_durable_marker() {
    local marker="$1"
    local payload="$2"
    "$VENV_DIR/bin/python" - "$marker" "$payload" <<'PY'
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
payload = (sys.argv[2] + '\n').encode('utf-8')
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_CLOEXEC', 0)
flags |= getattr(os, 'O_NOFOLLOW', 0)
descriptor = os.open(path, flags, 0o600)
try:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError('short write')
        view = view[written:]
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
}

durably_checkpoint_recovery_materials() {
    local phase="$1"
    local checkpoint_marker
    case "$phase" in
        captured-state) checkpoint_marker="$CAPTURED_STATE_CHECKPOINT" ;;
        recovery-backups) checkpoint_marker="$RECOVERY_MATERIALS_CHECKPOINT" ;;
        *) fail "恢复材料 durability checkpoint 阶段无效: $phase"; return 1 ;;
    esac

    "$VENV_DIR/bin/python" - \
        "$phase" \
        "$TRANSACTION_DIR" \
        "$STATE_FILE" \
        "$OLD_LINK_FILE" \
        "$UNIT_DIR" \
        "$CAPTURED_STATE_CHECKPOINT" \
        "$ENV_BACKUP" \
        "$ENV_EXISTED" \
        "$ENV_BACKUP_READY" \
        "$BACKUP_RUNTIME_ENV_BACKUP" \
        "$BACKUP_RUNTIME_ENV_EXISTED" \
        "$BACKUP_RUNTIME_ENV_BACKUP_READY" \
        "$DB_BACKUP" \
        "$DB_EXISTED" \
        "$DB_BACKUP_READY" \
        -- "${ALL_UNITS[@]}" <<'PY'
import os
from pathlib import Path
import stat
import sys

separator = sys.argv.index('--')
(
    phase,
    transaction_raw,
    state_raw,
    old_link_raw,
    unit_dir_raw,
    captured_checkpoint_raw,
    env_backup_raw,
    env_existed_raw,
    env_ready_raw,
    backup_env_raw,
    backup_env_existed_raw,
    backup_env_ready_raw,
    db_backup_raw,
    db_existed_raw,
    db_ready_raw,
) = sys.argv[1:separator]
expected_units = sys.argv[separator + 1:]

transaction = Path(transaction_raw).resolve(strict=True)
state_file = Path(state_raw)
old_link_file = Path(old_link_raw)
unit_dir = Path(unit_dir_raw).resolve(strict=True)
captured_checkpoint = Path(captured_checkpoint_raw)
units_backup_dir = transaction / 'units'


def ensure_transaction_path(path):
    try:
        parent = path.parent.resolve(strict=True)
    except OSError:
        raise SystemExit(1) from None
    if parent not in {transaction, units_backup_dir}:
        raise SystemExit(1)


def require_regular(path):
    ensure_transaction_path(path)
    try:
        file_stat = path.lstat()
    except OSError:
        raise SystemExit(1) from None
    if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
        raise SystemExit(1)
    return path


def path_exists(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise SystemExit(1) from None
    return True


def fsync_regular(path):
    require_regular(path)
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


transaction_stat = transaction.lstat()
units_stat = units_backup_dir.lstat()
if (
    not stat.S_ISDIR(transaction_stat.st_mode)
    or stat.S_ISLNK(transaction_stat.st_mode)
    or not stat.S_ISDIR(units_stat.st_mode)
    or stat.S_ISLNK(units_stat.st_mode)
):
    raise SystemExit(1)

state_lines = require_regular(state_file).read_text(encoding='utf-8').splitlines()
if len(state_lines) != len(expected_units):
    raise SystemExit(1)
for line, expected_unit in zip(state_lines, expected_units, strict=True):
    fields = line.split('\t')
    if (
        len(fields) != 4
        or fields[0] != expected_unit
        or fields[1] not in {'0', '1'}
        or not fields[2]
        or not fields[3]
    ):
        raise SystemExit(1)

old_link_lines = require_regular(old_link_file).read_text(encoding='utf-8').splitlines()
if len(old_link_lines) != 1 or not old_link_lines[0]:
    raise SystemExit(1)

backup_units = []
for child in units_backup_dir.iterdir():
    if child.name not in expected_units:
        raise SystemExit(1)
    backup_units.append(require_regular(child))
for unit in expected_units:
    source = unit_dir / unit
    try:
        source_stat = source.lstat()
    except FileNotFoundError:
        continue
    except OSError:
        raise SystemExit(1) from None
    if stat.S_ISLNK(source_stat.st_mode):
        raise SystemExit(1)
    if stat.S_ISREG(source_stat.st_mode):
        require_regular(units_backup_dir / unit)

files_to_sync = [state_file, old_link_file, *backup_units]
if phase == 'recovery-backups':
    require_regular(captured_checkpoint)
    files_to_sync.append(captured_checkpoint)
    backup_specs = (
        (Path(env_backup_raw), env_existed_raw, env_ready_raw),
        (Path(backup_env_raw), backup_env_existed_raw, backup_env_ready_raw),
        (Path(db_backup_raw), db_existed_raw, db_ready_raw),
    )
    for backup_path, existed_raw, ready_raw in backup_specs:
        if existed_raw not in {'0', '1'} or ready_raw not in {'0', '1'}:
            raise SystemExit(1)
        if existed_raw != ready_raw:
            raise SystemExit(1)
        if existed_raw == '1':
            files_to_sync.append(require_regular(backup_path))
        elif path_exists(backup_path):
            raise SystemExit(1)
elif phase != 'captured-state':
    raise SystemExit(1)

for file_path in files_to_sync:
    fsync_regular(file_path)
for directory_path in (units_backup_dir, transaction, transaction.parent):
    descriptor = os.open(
        directory_path,
        os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
    write_durable_marker "$checkpoint_marker" "$phase"
    "$SYNC_BIN"
}

read_activation_guard_transaction() {
    "$VENV_DIR/bin/python" - \
        "$ACTIVATION_BOOT_GUARD_FILE" \
        "$TRANSACTION_ROOT" \
        "$CONTROL_OWNER_UID" \
        "$CONTROL_OWNER_GID" <<'PY'
from pathlib import Path
import stat
import sys

marker = Path(sys.argv[1])
transaction_root = Path(sys.argv[2]).resolve(strict=True)
file_stat = marker.lstat()
if (
    not stat.S_ISREG(file_stat.st_mode)
    or stat.S_ISLNK(file_stat.st_mode)
    or file_stat.st_uid != int(sys.argv[3])
    or file_stat.st_gid != int(sys.argv[4])
    or stat.S_IMODE(file_stat.st_mode) != 0o600
):
    raise SystemExit(1)
values = {}
for line in marker.read_text(encoding='utf-8').splitlines():
    key, separator, value = line.partition('=')
    if not separator or not key or not value or key in values:
        raise SystemExit(1)
    values[key] = value
if set(values) != {'release_id', 'transaction', 'started_at'}:
    raise SystemExit(1)
transaction = Path(values['transaction']).resolve(strict=True)
if not transaction.is_dir() or transaction.parent != transaction_root:
    raise SystemExit(1)
print(transaction)
PY
}

validate_runtime_guard_permit() {
    local expected_transaction="$1"
    [ -e "$RUNTIME_BOOT_GUARD_FILE" ] || [ -L "$RUNTIME_BOOT_GUARD_FILE" ] || return 0
    "$VENV_DIR/bin/python" - \
        "$RUNTIME_BOOT_GUARD_FILE" \
        "$expected_transaction" \
        "$CONTROL_OWNER_UID" \
        "$CONTROL_OWNER_GID" <<'PY'
from pathlib import Path
import stat
import sys

permit = Path(sys.argv[1])
expected = str(Path(sys.argv[2]).resolve(strict=True))
file_stat = permit.lstat()
if (
    not stat.S_ISREG(file_stat.st_mode)
    or stat.S_ISLNK(file_stat.st_mode)
    or file_stat.st_uid != int(sys.argv[3])
    or file_stat.st_gid != int(sys.argv[4])
    or stat.S_IMODE(file_stat.st_mode) != 0o600
):
    raise SystemExit(1)
values = {}
for line in permit.read_text(encoding='utf-8').splitlines():
    key, separator, value = line.partition('=')
    if not separator or not key or not value or key in values:
        raise SystemExit(1)
    values[key] = value
if set(values) != {'release_id', 'transaction'}:
    raise SystemExit(1)
if str(Path(values['transaction']).resolve(strict=True)) != expected:
    raise SystemExit(1)
PY
}

detect_unfinished_transactions() {
    local transaction transaction_list
    if ! transaction_list="$($VENV_DIR/bin/python - \
        "$TRANSACTION_ROOT" \
        "$CONTROL_OWNER_UID" \
        "$CONTROL_OWNER_GID" <<'PY'
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1]).resolve(strict=True)
owner_uid = int(sys.argv[2])
owner_gid = int(sys.argv[3])
marker_names = (
    'ACTIVATION_STARTED',
    'RECOVERY_CONFIRMED',
    'ROLLBACK_REQUIRED.txt',
    'POST_COMMIT_ATTENTION.txt',
    'COMMITTED',
    'ROLLED_BACK',
)
for transaction in sorted(root.iterdir()):
    transaction_stat = transaction.lstat()
    if not stat.S_ISDIR(transaction_stat.st_mode) or stat.S_ISLNK(transaction_stat.st_mode):
        raise SystemExit(1)
    if '\n' in transaction.name or '\r' in transaction.name:
        raise SystemExit(1)
    started = transaction / 'ACTIVATION_STARTED'
    for name in marker_names:
        marker = transaction / name
        try:
            marker_stat = marker.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or stat.S_ISLNK(marker_stat.st_mode)
            or marker_stat.st_uid != owner_uid
            or marker_stat.st_gid != owner_gid
        ):
            raise SystemExit(1)
    if started.exists():
        print(transaction)
PY
    )"; then
        fail "无法完整枚举或验证历史部署事务"
        return 1
    fi
    while IFS= read -r transaction; do
        [ -n "$transaction" ] || continue
        if [ -f "$transaction/$RECOVERY_CONFIRMED_MARKER_NAME" ]; then
            continue
        fi
        if [ -f "$transaction/ROLLBACK_REQUIRED.txt" ] \
            || [ -f "$transaction/POST_COMMIT_ATTENTION.txt" ]; then
            fail "发现尚未人工确认的部署恢复事务: $transaction"
            return 1
        fi
        if [ -f "$transaction/COMMITTED" ] || [ -f "$transaction/ROLLED_BACK" ]; then
            continue
        fi
        fail "发现上次进程中断留下的未完成事务: $transaction"
        return 1
    done <<< "$transaction_list"
}

acknowledge_recovery_transaction() {
    local confirmation guard_transaction="" has_fault_marker=0
    [ -n "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ] || return 0
    if [ ! -d "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ] \
        || [ -L "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ]; then
        fail "待确认的恢复事务目录不存在或不是普通目录"
        return 1
    fi
    if [ -L "$RECOVERY_ACKNOWLEDGED_TRANSACTION/ROLLBACK_REQUIRED.txt" ] \
        || [ -L "$RECOVERY_ACKNOWLEDGED_TRANSACTION/POST_COMMIT_ATTENTION.txt" ]; then
        fail "指定事务的故障标记不得为符号链接"
        return 1
    fi
    if [ -f "$RECOVERY_ACKNOWLEDGED_TRANSACTION/ROLLBACK_REQUIRED.txt" ] \
        || [ -f "$RECOVERY_ACKNOWLEDGED_TRANSACTION/POST_COMMIT_ATTENTION.txt" ]; then
        has_fault_marker=1
    fi
    if [ "$has_fault_marker" -eq 0 ]; then
        if [ ! -f "$RECOVERY_ACKNOWLEDGED_TRANSACTION/ACTIVATION_STARTED" ] \
            || [ -L "$RECOVERY_ACKNOWLEDGED_TRANSACTION/ACTIVATION_STARTED" ] \
            || [ -e "$RECOVERY_ACKNOWLEDGED_TRANSACTION/COMMITTED" ] \
            || [ -e "$RECOVERY_ACKNOWLEDGED_TRANSACTION/ROLLED_BACK" ]; then
            fail "指定事务既无故障标记，也不是可确认的中断激活事务"
            return 1
        fi
        if [ -e "$ACTIVATION_BOOT_GUARD_FILE" ] \
            || [ -L "$ACTIVATION_BOOT_GUARD_FILE" ]; then
            if ! guard_transaction="$(read_activation_guard_transaction)" \
                || [ "$guard_transaction" != "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ]; then
                fail "中断激活事务与持久开机门不匹配"
                return 1
            fi
        fi
    fi
    confirmation="$RECOVERY_ACKNOWLEDGED_TRANSACTION/$RECOVERY_CONFIRMED_MARKER_NAME"
    if [ -e "$confirmation" ] || [ -L "$confirmation" ]; then
        if ! "$VENV_DIR/bin/python" - \
            "$confirmation" \
            "$CONTROL_OWNER_UID" \
            "$CONTROL_OWNER_GID" <<'PY'
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
file_stat = path.lstat()
if (
    not stat.S_ISREG(file_stat.st_mode)
    or stat.S_ISLNK(file_stat.st_mode)
    or file_stat.st_uid != int(sys.argv[2])
    or file_stat.st_gid != int(sys.argv[3])
    or stat.S_IMODE(file_stat.st_mode) != 0o600
):
    raise SystemExit(1)
values = {}
for line in path.read_text(encoding='utf-8').splitlines():
    key, separator, value = line.partition('=')
    if not separator or not key or not value or key in values:
        raise SystemExit(1)
    values[key] = value
if set(values) != {'confirmed_at', 'confirmed_before_release'}:
    raise SystemExit(1)
PY
        then
            fail "已有恢复确认标记的内容或权限无效"
            return 1
        fi
        log "复用已安全落盘的人工恢复确认: $RECOVERY_ACKNOWLEDGED_TRANSACTION"
        return 0
    fi
    if ! "$VENV_DIR/bin/python" - \
        "$confirmation" \
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
    fsync_directory "$RECOVERY_ACKNOWLEDGED_TRANSACTION"
    log "已登记人工恢复确认: $RECOVERY_ACKNOWLEDGED_TRANSACTION"
}

recover_activation_boot_guard_if_acknowledged() {
    local guard_transaction recovered_guard terminal_count=0
    [ -e "$ACTIVATION_BOOT_GUARD_FILE" ] \
        || [ -L "$ACTIVATION_BOOT_GUARD_FILE" ] \
        || return 0
    if ! guard_transaction="$(read_activation_guard_transaction)"; then
        fail "持久开机门内容、权限或事务路径无效"
        return 1
    fi
    [ -f "$guard_transaction/COMMITTED" ] && terminal_count=$((terminal_count + 1))
    [ -f "$guard_transaction/ROLLED_BACK" ] && terminal_count=$((terminal_count + 1))
    if [ "$terminal_count" -gt 1 ]; then
        fail "持久开机门对应事务同时存在两个终态标记"
        return 1
    fi
    if [ "$terminal_count" -eq 0 ]; then
        if [ -z "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ]; then
            fail "发现没有终态的持久开机门；必须显式确认其精确事务后才能继续"
            return 1
        fi
        if [ "$guard_transaction" != "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ] \
            || [ ! -f "$guard_transaction/$RECOVERY_CONFIRMED_MARKER_NAME" ] \
            || [ -L "$guard_transaction/$RECOVERY_CONFIRMED_MARKER_NAME" ]; then
            fail "持久开机门与已确认恢复事务不匹配"
            return 1
        fi
    fi
    if [ -L "$guard_transaction/COMMITTED" ] \
        || [ -L "$guard_transaction/ROLLED_BACK" ]; then
        fail "事务终态标记不得为符号链接"
        return 1
    fi
    if ! validate_runtime_guard_permit "$guard_transaction"; then
        fail "运行期开机许可与持久开机门不匹配"
        return 1
    fi
    recovered_guard="$guard_transaction/activation-in-progress.recovered"
    if [ -e "$recovered_guard" ] || [ -L "$recovered_guard" ]; then
        fail "恢复事务中已存在开机门归档，拒绝覆盖"
        return 1
    fi
    if [ -f "$RUNTIME_BOOT_GUARD_FILE" ]; then
        rm -f -- "$RUNTIME_BOOT_GUARD_FILE"
        fsync_directory "$RUNTIME_BOOT_GUARD_DIR"
    fi
    mv "$ACTIVATION_BOOT_GUARD_FILE" "$recovered_guard"
    chmod 0600 "$recovered_guard"
    fsync_directory "$STATE_DIR/deployments"
    fsync_directory "$guard_transaction"
    log "已归档匹配且具备终态或人工确认的断电保护门"
}

prepare_control_directories() {
    local control_dir
    for control_dir in \
        "$STATE_DIR/backups" \
        "$STATE_DIR/backups/daily" \
        "$STATE_DIR/backups/validation" \
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
        "$STATE_DIR/backups/daily" \
        "$STATE_DIR/backups/validation" \
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
    # 先关闭备份入口并等待已经开始的备份自然结束，期间保持公网服务与其他调度不变。
    query_unit_load_state case-weather-backup.timer
    if [ "$UNIT_LOAD_STATE" = loaded ]; then
        "$SYSTEMCTL_BIN" stop case-weather-backup.timer
    fi
    wait_for_backup_completion
    RUNTIME_QUIESCE_STARTED=1
    # 备份已稳定后再停其余调度入口与 writer。
    for unit in "${SCHEDULER_UNITS[@]}"; do
        [ "$unit" = case-weather-backup.timer ] && continue
        query_unit_load_state "$unit"
        if [ "$UNIT_LOAD_STATE" = loaded ]; then
            "$SYSTEMCTL_BIN" stop "$unit"
        fi
    done
    wait_for_backup_completion
    for unit in "${STOPPABLE_SERVICE_UNITS[@]}"; do
        query_unit_load_state "$unit"
        if [ "$UNIT_LOAD_STATE" = loaded ]; then
            "$SYSTEMCTL_BIN" stop "$unit"
        fi
    done
    for unit in "${SCHEDULER_UNITS[@]}" "${STOPPABLE_SERVICE_UNITS[@]}"; do
        query_unit_load_state "$unit"
        if [ "$UNIT_LOAD_STATE" = loaded ]; then
            query_unit_active_state "$unit"
            case "$UNIT_ACTIVE_STATE" in
                active|activating|reloading|deactivating)
                    fail "systemd 单元仍在运行: $unit=$UNIT_ACTIVE_STATE"
                    return 1
                    ;;
            esac
        fi
    done
    verify_no_unmanaged_processes_after_quiesce
}

stop_units_best_effort() {
    local failed=0
    local unit
    if query_unit_load_state case-weather-backup.timer; then
        if [ "$UNIT_LOAD_STATE" = loaded ]; then
            "$SYSTEMCTL_BIN" stop case-weather-backup.timer >/dev/null 2>&1 || failed=1
        fi
    else
        failed=1
        "$SYSTEMCTL_BIN" stop case-weather-backup.timer >/dev/null 2>&1 || true
    fi
    # 备份状态不确定时仍继续停止其他固定业务单元，且绝不强停备份服务本身。
    wait_for_backup_completion || failed=1
    for unit in "${SCHEDULER_UNITS[@]}"; do
        [ "$unit" = case-weather-backup.timer ] && continue
        if query_unit_load_state "$unit"; then
            if [ "$UNIT_LOAD_STATE" = loaded ]; then
                "$SYSTEMCTL_BIN" stop "$unit" >/dev/null 2>&1 || failed=1
            fi
        else
            failed=1
            "$SYSTEMCTL_BIN" stop "$unit" >/dev/null 2>&1 || true
        fi
    done
    for unit in "${STOPPABLE_SERVICE_UNITS[@]}"; do
        if query_unit_load_state "$unit"; then
            if [ "$UNIT_LOAD_STATE" = loaded ]; then
                "$SYSTEMCTL_BIN" stop "$unit" >/dev/null 2>&1 || failed=1
            fi
        else
            failed=1
            "$SYSTEMCTL_BIN" stop "$unit" >/dev/null 2>&1 || true
        fi
    done
    for unit in "${SCHEDULER_UNITS[@]}" "${STOPPABLE_SERVICE_UNITS[@]}"; do
        if ! query_unit_load_state "$unit"; then
            failed=1
            continue
        fi
        if [ "$UNIT_LOAD_STATE" = loaded ]; then
            if ! query_unit_active_state "$unit"; then
                failed=1
                continue
            fi
            case "$UNIT_ACTIVE_STATE" in
                active|activating|reloading|deactivating) failed=1 ;;
            esac
        fi
    done
    return "$failed"
}

verify_backup_not_running() {
    local state_status=0
    backup_service_is_running || state_status=$?
    if [ "$state_status" -eq 0 ]; then
        fail "每日 SQLite 备份仍在运行；本次未中止备份，请完成后重试发布"
        return 1
    fi
    if [ "$state_status" -ne 1 ]; then
        fail "无法可靠读取每日 SQLite 备份的 LoadState/ActiveState"
        return 1
    fi
}

backup_service_is_running() {
    local state_output load_state="" active_state="" key value
    if ! state_output="$($SYSTEMCTL_BIN show \
        case-weather-backup.service \
        --property=LoadState \
        --property=ActiveState \
        2>/dev/null)"; then
        return 2
    fi
    while IFS='=' read -r key value; do
        case "$key" in
            LoadState)
                [ -z "$load_state" ] || return 2
                load_state="$value"
                ;;
            ActiveState)
                [ -z "$active_state" ] || return 2
                active_state="$value"
                ;;
            '') ;;
            *) return 2 ;;
        esac
    done <<< "$state_output"
    case "$load_state" in
        loaded)
            case "$active_state" in
                active|activating|reloading|deactivating) return 0 ;;
                inactive|failed) return 1 ;;
                *) return 2 ;;
            esac
            ;;
        not-found)
            [ "$active_state" = inactive ] && return 1
            return 2
            ;;
        *) return 2 ;;
    esac
}

wait_for_backup_completion() {
    local attempt state_status
    for ((attempt = 1; attempt <= BACKUP_WAIT_ATTEMPTS; attempt += 1)); do
        state_status=0
        backup_service_is_running || state_status=$?
        if [ "$state_status" -eq 1 ]; then
            return 0
        fi
        if [ "$state_status" -ne 0 ]; then
            fail "等待备份时无法可靠读取 ActiveState"
            return 1
        fi
        if [ "$attempt" -lt "$BACKUP_WAIT_ATTEMPTS" ]; then
            sleep "$BACKUP_WAIT_SLEEP_SECONDS"
        fi
    done
    fail "每日 SQLite 备份在等待窗口内仍未完成；公网服务保持原状态"
    return 1
}

validate_backup_database_config() {
    local status=0
    PROJECT_DIR="$STATE_DIR" \
    ENV_FILE="$STAGED_ENV_FILE" \
    DATABASE_URI= \
    BACKUP_DATABASE_FILE= \
        /bin/bash -c '
            source "$1"
            if load_database_uri; then
                exit 0
            else
                status=$?
                exit "$status"
            fi
        ' bash "$APP_DIR/scripts/backup.sh" || status=$?
    if [ "$status" -ne 0 ]; then
        fail "候选环境的 SQLite 日备份配置不唯一或格式无效"
        return "$status"
    fi
}

verify_activation_guard_dropins() {
    local unit dropin expected load_state need_reload loaded_config
    expected="$TRANSACTION_DIR/activation-guard.expected"
    {
        printf '%s\n' '[Unit]'
        printf 'ConditionPathExists=|!%s\n' "$ACTIVATION_BOOT_GUARD_FILE"
        printf 'ConditionPathExists=|%s\n' "$RUNTIME_BOOT_GUARD_FILE"
    } > "$expected"
    chmod 0600 "$expected"

    for unit in "${ALL_UNITS[@]}"; do
        dropin="$UNIT_DIR/$unit.d/$ACTIVATION_GUARD_DROPIN_NAME"
        if ! "$VENV_DIR/bin/python" - \
            "$dropin" \
            "$expected" \
            "$UNIT_DIR" \
            "$unit" \
            "$CONTROL_OWNER_UID" \
            "$CONTROL_OWNER_GID" <<'PY'
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
expected = Path(sys.argv[2])
unit_root = Path(sys.argv[3]).resolve(strict=True)
unit = sys.argv[4]
owner_uid = int(sys.argv[5])
owner_gid = int(sys.argv[6])
directory_stat = path.parent.lstat()
file_stat = path.lstat()
if (
    not stat.S_ISDIR(directory_stat.st_mode)
    or stat.S_ISLNK(directory_stat.st_mode)
    or path.parent.name != f'{unit}.d'
    or path.parent.parent.resolve(strict=True) != unit_root
    or not stat.S_ISREG(file_stat.st_mode)
    or stat.S_ISLNK(file_stat.st_mode)
    or file_stat.st_uid != owner_uid
    or file_stat.st_gid != owner_gid
    or stat.S_IMODE(file_stat.st_mode) != 0o644
    or path.read_bytes() != expected.read_bytes()
):
    raise SystemExit(1)
PY
        then
            fail "systemd 断电保护 drop-in 文件无效: $unit"
            return 1
        fi
        if ! load_state="$($SYSTEMCTL_BIN show \
            "$unit" \
            --property=LoadState \
            --value 2>/dev/null)"; then
            fail "无法读取 systemd 单元 LoadState: $unit"
            return 1
        fi
        case "$load_state" in
            not-found) continue ;;
            loaded) ;;
            *)
                fail "systemd 单元 LoadState 异常，拒绝在保护门未确认时修改生产: $unit=${load_state:-unknown}"
                return 1
                ;;
        esac
        if ! need_reload="$($SYSTEMCTL_BIN show \
            "$unit" \
            --property=NeedDaemonReload \
            --value 2>/dev/null)" \
            || [ "$need_reload" != no ]; then
            fail "systemd 单元尚未加载磁盘上的最新断电保护配置: $unit"
            return 1
        fi
        loaded_config="$TRANSACTION_DIR/systemctl-cat-$unit"
        if ! "$SYSTEMCTL_BIN" cat "$unit" > "$loaded_config" 2>/dev/null \
            || ! "$VENV_DIR/bin/python" - \
                "$loaded_config" \
                "$dropin" \
                "|!$ACTIVATION_BOOT_GUARD_FILE" \
                "|$RUNTIME_BOOT_GUARD_FILE" <<'PY'
from pathlib import Path
import sys

loaded = Path(sys.argv[1]).read_text(encoding='utf-8').splitlines()
dropin_header = f'# {sys.argv[2]}'
expected = {sys.argv[3], sys.argv[4]}
if dropin_header not in loaded:
    raise SystemExit(1)

section = ''
path_conditions = []
for raw_line in loaded:
    line = raw_line.strip()
    if not line or line.startswith('#'):
        continue
    if line.startswith('[') and line.endswith(']'):
        section = line[1:-1]
        continue
    if section != 'Unit' or '=' not in line:
        continue
    key, value = line.split('=', 1)
    key = key.strip()
    value = value.strip()
    if key.startswith('Condition') and not value:
        # systemd 的任意空 Condition 赋值都会重置完整 condition 列表。
        path_conditions = []
        continue
    if key == 'ConditionPathExists':
        path_conditions.append(value)
        continue
    if key.startswith('Condition') and value.startswith('|'):
        # 其他 trigger condition 会加入 OR 组，可能绕过发布开机门。
        raise SystemExit(1)

if set(path_conditions) != expected or any(
    value not in expected for value in path_conditions
):
    raise SystemExit(1)
PY
        then
            fail "systemd 尚未加载预期的断电保护 drop-in: $unit"
            return 1
        fi
        chmod 0600 "$loaded_config"
    done
    log "现有与候选 systemd 单元的共享断电保护门已核验"
}

prepare_activation_boot_guard() {
    if [ -L "$RUNTIME_BOOT_GUARD_DIR" ] \
        || [ -e "$RUNTIME_BOOT_GUARD_FILE" ] \
        || [ -L "$RUNTIME_BOOT_GUARD_FILE" ] \
        || [ -e "$ACTIVATION_BOOT_GUARD_FILE" ] \
        || [ -L "$ACTIVATION_BOOT_GUARD_FILE" ]; then
        fail "运行期发布开机门存在符号链接或遗留许可"
        return 1
    fi
    mkdir -p "$RUNTIME_BOOT_GUARD_DIR"
    "$CHOWN_BIN" root:root "$RUNTIME_BOOT_GUARD_DIR"
    chmod 0700 "$RUNTIME_BOOT_GUARD_DIR"
    # 先持久拒绝重启，再发放仅当前 boot 有效的运行许可。
    "$VENV_DIR/bin/python" - \
        "$ACTIVATION_BOOT_GUARD_FILE" \
        "$RELEASE_ID" \
        "$TRANSACTION_DIR" <<'PY'
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

target = Path(sys.argv[1])
payload = (
    f'release_id={sys.argv[2]}\n'
    f'transaction={sys.argv[3]}\n'
    f'started_at={datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}\n'
).encode('utf-8')
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_CLOEXEC', 0)
flags |= getattr(os, 'O_NOFOLLOW', 0)
descriptor = os.open(target, flags, 0o600)
try:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError('short write')
        view = view[written:]
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory = os.open(target.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
    {
        printf 'release_id=%s\n' "$RELEASE_ID"
        printf 'transaction=%s\n' "$TRANSACTION_DIR"
    } > "$RUNTIME_BOOT_GUARD_FILE"
    chmod 0600 "$RUNTIME_BOOT_GUARD_FILE"
}

remove_activation_boot_guard() {
    local expected_transaction="${1:-}"
    local guard_transaction=""
    if [ -z "$expected_transaction" ]; then
        fail "清除发布开机门必须绑定事务"
        return 1
    fi
    if [ -e "$ACTIVATION_BOOT_GUARD_FILE" ] \
        || [ -L "$ACTIVATION_BOOT_GUARD_FILE" ]; then
        if ! guard_transaction="$(read_activation_guard_transaction)" \
            || [ "$guard_transaction" != "$expected_transaction" ]; then
            fail "拒绝清除不属于本事务的持久发布开机门"
            return 1
        fi
    fi
    if ! validate_runtime_guard_permit "$expected_transaction"; then
        fail "拒绝清除不属于本事务的运行期开机许可"
        return 1
    fi
    if [ -L "$ACTIVATION_BOOT_GUARD_FILE" ]; then
        fail "持久发布开机门不得为符号链接"
        return 1
    fi
    if [ -L "$RUNTIME_BOOT_GUARD_FILE" ]; then
        fail "运行期发布开机门不得为符号链接"
        return 1
    fi
    if [ -f "$RUNTIME_BOOT_GUARD_FILE" ]; then
        rm -f -- "$RUNTIME_BOOT_GUARD_FILE"
        fsync_directory "$RUNTIME_BOOT_GUARD_DIR"
    fi
    if [ -f "$ACTIVATION_BOOT_GUARD_FILE" ]; then
        rm -f -- "$ACTIVATION_BOOT_GUARD_FILE"
        fsync_directory "$STATE_DIR/deployments"
    fi
}

revoke_runtime_activation_permit() {
    local expected_transaction="${1:-}"
    if [ -z "$expected_transaction" ]; then
        fail "撤销运行期发布许可必须绑定事务"
        return 1
    fi
    if ! validate_runtime_guard_permit "$expected_transaction"; then
        fail "拒绝撤销不属于本事务的运行期开机许可"
        return 1
    fi
    if [ -L "$RUNTIME_BOOT_GUARD_FILE" ]; then
        fail "运行期发布许可不得为符号链接"
        return 1
    fi
    if [ -f "$RUNTIME_BOOT_GUARD_FILE" ]; then
        rm -f -- "$RUNTIME_BOOT_GUARD_FILE"
        fsync_directory "$RUNTIME_BOOT_GUARD_DIR"
    fi
}

verify_no_retired_processes() {
    local pattern rc
    for pattern in \
        "$STATE_DIR/backup.sh" \
        "$STATE_DIR/services/pipelines/sync_weather_data.py"; do
        if "$PGREP_BIN" -f -- "$pattern" >/dev/null 2>&1; then
            fail "检测到仍在运行的旧调度进程: $pattern"
            return 1
        else
            rc=$?
            if [ "$rc" -ne 1 ]; then
                fail "无法确认旧调度进程已停止: $pattern"
                return 1
            fi
        fi
    done
}

verify_no_unmanaged_processes_after_quiesce() {
    local rc pattern="$CURRENT_LINK/app/scripts/backup.sh"
    verify_no_retired_processes
    if "$PGREP_BIN" -f -- "$pattern" >/dev/null 2>&1; then
        fail "受管备份已静默后仍检测到未归属的备份进程: $pattern"
        return 1
    else
        rc=$?
        if [ "$rc" -ne 1 ]; then
            fail "无法确认受管备份进程已静默: $pattern"
            return 1
        fi
    fi
}

resolve_database_file() {
    local config_file="${1:-$ENV_FILE}"
    (
        cd "$APP_DIR"
        CASE_WEATHER_ENV_FILE="$config_file" "$VENV_DIR/bin/python" - <<'PY'
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

validate_managed_backup_database_path() {
    if ! "$VENV_DIR/bin/python" - "$DATABASE_FILE" "$STATE_DIR" <<'PY'
from pathlib import Path
import sys

database_file = Path(sys.argv[1]).resolve(strict=False)
state_dir = Path(sys.argv[2]).resolve(strict=False)
allowed_roots = (
    (state_dir / 'instance').resolve(strict=False),
    (state_dir / 'storage').resolve(strict=False),
)
if not any(
    database_file == root or database_file.is_relative_to(root)
    for root in allowed_roots
):
    raise SystemExit(1)
PY
    then
        fail "托管备份要求 SQLite 位于受控 instance 或 storage 目录"
        return 1
    fi
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

sqlite_logical_digest() {
    "$VENV_DIR/bin/python" - "$1" <<'PY'
import hashlib
import sqlite3
import sys

connection = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)
digest = hashlib.sha256()
try:
    for line in connection.iterdump():
        digest.update(line.encode('utf-8'))
        digest.update(b'\n')
finally:
    connection.close()
print(digest.hexdigest())
PY
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
from pathlib import Path
import stat
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
source_stat = source.lstat()
if stat.S_ISREG(source_stat.st_mode):
    descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
os.replace(source, destination)
if destination.is_file() and not destination.is_symlink():
    descriptor = os.open(
        destination,
        os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
for directory_path in {source.parent, destination.parent}:
    descriptor = os.open(
        directory_path,
        os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
}

durably_sync_release_state() {
    local sync_mode="${1:-}"
    case "$sync_mode" in
        commit|forward|rollback) ;;
        *) fail "durability barrier 必须声明 commit、forward 或 rollback"; return 1 ;;
    esac
    "$VENV_DIR/bin/python" - \
        "$sync_mode" \
        "$ENV_FILE" \
        "$BACKUP_RUNTIME_ENV_FILE" \
        "$DATABASE_FILE" \
        "$STATE_DIR/deployments/current-release" \
        "$CURRENT_LINK" \
        "$NEW_RELEASE" \
        "$STAGED_ENV_FILE" \
        "$UNIT_DIR" \
        "${INSTALL_UNITS[@]}" \
        -- "${LEGACY_UNITS[@]}" <<'PY'
import os
from pathlib import Path
import stat
import sys

separator = sys.argv.index('--')
mode = sys.argv[1]
env_file = Path(sys.argv[2])
backup_env = Path(sys.argv[3])
database = Path(sys.argv[4])
current_release = Path(sys.argv[5])
current_link = Path(sys.argv[6])
new_release = Path(sys.argv[7])
staged_env = Path(sys.argv[8])
unit_dir = Path(sys.argv[9])
install_units = sys.argv[10:separator]
legacy_units = sys.argv[separator + 1:]
unit_names = install_units + legacy_units


def fsync_regular(path):
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


for path in (
    env_file,
    backup_env,
    database,
    Path(f'{database}-wal'),
    Path(f'{database}-shm'),
    current_release,
):
    fsync_regular(path)
for unit in unit_names:
    fsync_regular(unit_dir / unit)

if mode in {'commit', 'forward'}:
    required_paths = [env_file, backup_env, database]
    if mode == 'commit':
        required_paths.append(current_release)
    for path in required_paths:
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
            raise SystemExit(1)
    if not current_link.is_symlink() or current_link.resolve(strict=True) != new_release.resolve(strict=True):
        raise SystemExit(1)
    if staged_env.exists() or staged_env.is_symlink():
        raise SystemExit(1)
    for unit in install_units:
        unit_path = unit_dir / unit
        file_stat = unit_path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
            raise SystemExit(1)
    for unit in legacy_units:
        unit_path = unit_dir / unit
        if unit_path.exists() or unit_path.is_symlink():
            raise SystemExit(1)

directories = {
    env_file.parent,
    backup_env.parent,
    database.parent,
    current_release.parent,
    current_link.parent,
    staged_env.parent,
    unit_dir,
}
if unit_dir.is_dir():
    for child in unit_dir.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        if child.name.endswith(('.wants', '.requires')):
            directories.add(child)
for directory_path in directories:
    if not directory_path.is_dir():
        continue
    descriptor = os.open(
        directory_path,
        os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
    "$SYNC_BIN"
}

durably_sync_database_state() {
    sqlite_quick_check "$DATABASE_FILE"
    "$VENV_DIR/bin/python" - "$DATABASE_FILE" <<'PY'
import os
from pathlib import Path
import stat
import sys

database = Path(sys.argv[1])
for path in (database, Path(f'{database}-wal'), Path(f'{database}-shm')):
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        if path == database:
            raise SystemExit(1) from None
        continue
    except OSError:
        raise SystemExit(1) from None
    if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
        raise SystemExit(1)
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
descriptor = os.open(
    database.parent,
    os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0),
)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
    "$SYNC_BIN"
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

backup_backup_runtime_environment() {
    if [ ! -f "$BACKUP_RUNTIME_ENV_FILE" ]; then
        BACKUP_RUNTIME_ENV_EXISTED=0
        return
    fi
    if [ -L "$BACKUP_RUNTIME_ENV_FILE" ]; then
        fail "日备份运行配置不得为符号链接"
        return 1
    fi
    BACKUP_RUNTIME_ENV_EXISTED=1
    cp -a "$BACKUP_RUNTIME_ENV_FILE" "$BACKUP_RUNTIME_ENV_BACKUP"
    chmod 0600 "$BACKUP_RUNTIME_ENV_BACKUP"
    BACKUP_RUNTIME_ENV_BACKUP_READY=1
}

apply_backup_runtime_environment() {
    local staged="$TRANSACTION_DIR/backup-runtime.next"
    {
        printf 'BACKUP_DATABASE_FILE=%s\n' "$DATABASE_FILE"
        printf 'BACKUP_PRUNE=1\n'
    } > "$staged"
    chmod 0600 "$staged"
    BACKUP_RUNTIME_ENV_MUTATION_STARTED=1
    atomic_replace "$staged" "$BACKUP_RUNTIME_ENV_FILE"
    "$CHOWN_BIN" root:root "$BACKUP_RUNTIME_ENV_FILE"
    chmod 0600 "$BACKUP_RUNTIME_ENV_FILE"
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
        atomic_replace "$temporary" "$UNIT_DIR/$unit"
    done
    mkdir -p "$TRANSACTION_DIR/retired-legacy-units"
    fsync_directory "$TRANSACTION_DIR"
    for unit in "${LEGACY_UNITS[@]}"; do
        query_unit_load_state "$unit"
        if [ "$UNIT_LOAD_STATE" = loaded ]; then
            "$SYSTEMCTL_BIN" disable "$unit" >/dev/null
        fi
        if [ -e "$UNIT_DIR/$unit" ] || [ -L "$UNIT_DIR/$unit" ]; then
            atomic_replace \
                "$UNIT_DIR/$unit" \
                "$TRANSACTION_DIR/retired-legacy-units/$unit"
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
    fsync_directory "$STATE_DIR/deployments"
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
            write_durable_marker \
                "$TRANSACTION_DIR/CACHE_SMOKE_VERIFIED" \
                "$(printf 'snapshot_id=%s\nmode=reused_completed_receipt' "$snapshot_id")"
            log "已复用同一冻结发布的 completed 天气烟测 receipt，未再次请求上游"
            return 0
        fi
        fail "同一冻结 commit 与配置已有 started 天气烟测 receipt；禁止自动重试，请人工核对上游计数与数据库"
        return 1
    fi
    mkdir "$FORMAL_SMOKE_RECEIPT_DIR"
    chmod 0700 "$FORMAL_SMOKE_RECEIPT_DIR"
    fsync_directory "$FORMAL_SMOKE_RECEIPT_ROOT"
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    write_durable_marker \
        "$binding_file" \
        "$(printf 'release_commit=%s\nconfig_fingerprint=%s' \
            "$FORMAL_RELEASE_COMMIT" \
            "$FORMAL_RELEASE_CONFIG_FINGERPRINT")"
    write_durable_marker "$started_file" "started_at=$now"
    # started receipt 完整落盘后，才允许打开唯一一次正式天气出网窗口。
    fsync_directory "$FORMAL_SMOKE_RECEIPT_DIR"
    fsync_directory "$FORMAL_SMOKE_RECEIPT_ROOT"
    fsync_directory "$STATE_DIR/deployments"
    "$SYNC_BIN"
}

complete_formal_smoke_receipt() {
    local snapshot_id="$1"
    local completed_file="$FORMAL_SMOKE_RECEIPT_DIR/completed"
    write_durable_marker \
        "$completed_file" \
        "$(printf 'snapshot_id=%s\ncompleted_at=%s' \
            "$snapshot_id" \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
    fsync_directory "$FORMAL_SMOKE_RECEIPT_DIR"
    fsync_directory "$FORMAL_SMOKE_RECEIPT_ROOT"
    "$SYNC_BIN"
}

run_formal_cache_smoke() {
    local previous_snapshot current_snapshot gate_open_status=0 smoke_status=0 gate_close_status=0
    [ "$REQUIRE_WECHAT_READY" = 1 ] || return 0
    prepare_formal_smoke_receipt
    if [ "$FORMAL_SMOKE_REUSED" = 1 ]; then
        return 0
    fi
    previous_snapshot="$(latest_snapshot_id)"
    # started receipt 已落盘。从这里开始即使请求结果未知，也只允许向前恢复。
    FORMAL_SMOKE_IRREVERSIBLE=1
    FORWARD_ONLY=1
    FORMAL_NETWORK_GATE_OPEN=1
    printf '0' \
        | "$VENV_DIR/bin/python" "$APP_DIR/scripts/update_env_value.py" \
            --file "$ENV_FILE" \
            --key QWEATHER_NETWORK_NOT_BEFORE_EPOCH \
            --mode always \
        || gate_open_status=$?
    if [ "$gate_open_status" -eq 0 ]; then
        tighten_environment_permissions || gate_open_status=$?
    fi
    if [ "$gate_open_status" -eq 0 ]; then
        (
            cd "$APP_DIR"
            runtime_exec /bin/bash scripts/weather_cache_sync.sh --skip-nowcast
        ) || smoke_status=$?
    fi
    # 无论上游调用结果如何，都立即恢复从当前时刻起 30 分钟的出网保护。
    arm_qweather_network_gate || gate_close_status=$?
    if [ "$gate_close_status" -eq 0 ]; then
        FORMAL_NETWORK_GATE_OPEN=0
    fi
    if [ "$gate_open_status" -ne 0 ]; then
        fail "唯一一次天气烟测的出网闸门未能安全打开"
        return "$gate_open_status"
    fi
    if [ "$smoke_status" -ne 0 ]; then
        fail "唯一一次天气同步烟测执行失败，禁止自动重试"
        return "$smoke_status"
    fi
    if [ "$gate_close_status" -ne 0 ]; then
        fail "天气烟测结束后未能恢复 30 分钟出网保护"
        return "$gate_close_status"
    fi
    current_snapshot="$(latest_snapshot_id)"
    if [ -z "$current_snapshot" ] || [ "$current_snapshot" = "$previous_snapshot" ]; then
        fail "唯一一次天气同步烟测未生成新的持久化快照"
        return 1
    fi
    verify_fresh_qweather_snapshot "$current_snapshot"
    # receipt 成为完成态之前，先确保对应 SQLite 快照及 sidecar 已持久化。
    durably_sync_database_state
    write_durable_marker \
        "$TRANSACTION_DIR/FORMAL_SMOKE_DB_DURABLE" \
        "snapshot_id=$current_snapshot"
    complete_formal_smoke_receipt "$current_snapshot"
    write_durable_marker \
        "$TRANSACTION_DIR/CACHE_SMOKE_VERIFIED" \
        "$(printf 'snapshot_id=%s\nmode=new_request' "$current_snapshot")"
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
        query_unit_load_state "$unit"
        if [ "$UNIT_LOAD_STATE" != loaded ]; then
            fail "延迟 timer 未正确安装: $unit"
            return 1
        fi
        unit_file_state="$($SYSTEMCTL_BIN is-enabled "$unit" 2>/dev/null || true)"
        if [ "$unit_file_state" != disabled ]; then
            fail "延迟 timer 状态应为 disabled，实际为 ${unit_file_state:-unknown}: $unit"
            return 1
        fi
        query_unit_active_state "$unit"
        case "$UNIT_ACTIVE_STATE" in
            active|activating|reloading|deactivating)
                fail "延迟 timer 在首轮等待前已运行: $unit=$UNIT_ACTIVE_STATE"
                return 1
                ;;
        esac
    done
    for unit in "${START_TIMER_UNITS[@]}"; do
        "$SYSTEMCTL_BIN" enable "$unit"
        unit_file_state="$($SYSTEMCTL_BIN is-enabled "$unit" 2>/dev/null || true)"
        if [ "$unit_file_state" != enabled ]; then
            fail "开机 timer 状态应为 enabled，实际为 ${unit_file_state:-unknown}: $unit"
            return 1
        fi
        query_unit_active_state "$unit"
        case "$UNIT_ACTIVE_STATE" in
            active|activating|reloading|deactivating)
                fail "开机 timer 在正式提交前不应运行: $unit=$UNIT_ACTIVE_STATE"
                return 1
                ;;
        esac
    done
}

validate_managed_backup_service() {
    local service_status=0
    local extracted_backup="$TRANSACTION_DIR/managed-backup-validation.db"
    local -a archives=()

    # transient unit 只携带本次解析出的精确数据库与事务目录，不落持久覆盖配置。
    if [ -e "$BACKUP_VALIDATION_DIR" ] \
        || [ -L "$BACKUP_VALIDATION_DIR" ]; then
        fail "本事务备份验证目录已存在，拒绝覆盖"
        return 1
    fi
    mkdir "$BACKUP_VALIDATION_DIR"
    "$CHOWN_BIN" root:root "$BACKUP_VALIDATION_DIR"
    chmod 0700 "$BACKUP_VALIDATION_DIR"
    "$SYSTEMD_RUN_BIN" \
        --quiet \
        --wait \
        --collect \
        --unit="case-weather-backup-validation-$$" \
        --property=Type=oneshot \
        --property=User=root \
        --property=Group=root \
        --property=UMask=0077 \
        --property=NoNewPrivileges=yes \
        --property=PrivateTmp=yes \
        --property=PrivateDevices=yes \
        --property=PrivateNetwork=yes \
        --property=ProtectSystem=strict \
        --property=ProtectHome=yes \
        --property=ProtectKernelTunables=yes \
        --property=ProtectKernelModules=yes \
        --property=ProtectKernelLogs=yes \
        --property=ProtectControlGroups=yes \
        --property=ProtectClock=yes \
        --property=ProtectHostname=yes \
        --property=ProtectProc=invisible \
        --property=ProcSubset=pid \
        --property=RestrictSUIDSGID=yes \
        --property=RestrictNamespaces=yes \
        --property=RestrictRealtime=yes \
        --property=LockPersonality=yes \
        --property=MemoryDenyWriteExecute=yes \
        --property=TimeoutStartSec=15min \
        --property=SystemCallArchitectures=native \
        --property=RestrictAddressFamilies=AF_UNIX \
        --property="CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_SETUID CAP_SETGID" \
        --property="ReadOnlyPaths=$APP_DIR $ENV_FILE" \
        --property="ReadWritePaths=$BACKUP_VALIDATION_DIR $STATE_DIR/instance $STATE_DIR/storage" \
        --property="InaccessiblePaths=$TRANSACTION_ROOT $STATE_DIR/deployments $STATE_DIR/run" \
        --working-directory="$APP_DIR" \
        --setenv="PROJECT_DIR=$STATE_DIR" \
        --setenv="ENV_FILE=$ENV_FILE" \
        --setenv="BACKUP_DIR=$BACKUP_VALIDATION_DIR" \
        --setenv=BACKUP_PRUNE=0 \
        --setenv="BACKUP_DATABASE_FILE=$DATABASE_FILE" \
        --setenv="DEFAULT_DB_FILE=$STATE_DIR/instance/health_weather.db" \
        --setenv="BACKUP_RUNTIME_USER=$RUNTIME_USER" \
        --setenv=RUNUSER_BIN=runuser \
        --setenv=SQLITE3_BIN=sqlite3 \
        --setenv=MKTEMP_BIN=mktemp \
        --setenv=INSTALL_BIN=install \
        /bin/bash "$APP_DIR/scripts/backup.sh" \
        || service_status=$?
    if [ "$service_status" -ne 0 ]; then
        archive_backup_validation_artifacts
        fail "托管 SQLite 备份 transient unit 验证失败"
        return "$service_status"
    fi
    while IFS= read -r -d '' archive; do
        archives+=("$archive")
    done < <(find "$BACKUP_VALIDATION_DIR" -maxdepth 1 -type f -name '*.db.gz' -print0)
    if [ "${#archives[@]}" -ne 1 ] || ! gzip -t "${archives[0]}"; then
        archive_backup_validation_artifacts
        fail "托管 SQLite 备份验证未生成唯一且完整的压缩备份"
        return 1
    fi
    if ! gzip -cd "${archives[0]}" > "$extracted_backup" \
        || ! sqlite_quick_check "$extracted_backup"; then
        archive_backup_validation_artifacts
        fail "托管 SQLite 备份验证产物未通过 SQLite quick_check"
        return 1
    fi
    chmod 0600 "$extracted_backup"
    archive_backup_validation_artifacts
    log "托管 SQLite 备份 transient unit 已在事务隔离目录验证通过"
}

validate_installed_backup_service() {
    local before_snapshot="$TRANSACTION_DIR/daily-backup.before.json"
    local loaded_config="$TRANSACTION_DIR/systemctl-cat-case-weather-backup.service.installed"
    local extracted_backup="$TRANSACTION_DIR/managed-daily-backup-validation.db"
    local load_state fragment_path need_reload unit_result exec_status new_archive
    local source_digest backup_digest

    load_state="$($SYSTEMCTL_BIN show \
        case-weather-backup.service \
        --property=LoadState \
        --value)"
    fragment_path="$($SYSTEMCTL_BIN show \
        case-weather-backup.service \
        --property=FragmentPath \
        --value)"
    need_reload="$($SYSTEMCTL_BIN show \
        case-weather-backup.service \
        --property=NeedDaemonReload \
        --value)"
    if [ "$load_state" != loaded ] \
        || [ "$fragment_path" != "$UNIT_DIR/case-weather-backup.service" ] \
        || [ "$need_reload" != no ]; then
        fail "正式日备份 unit 未从预期路径完整加载"
        return 1
    fi
    if ! "$SYSTEMCTL_BIN" cat case-weather-backup.service > "$loaded_config" \
        || ! grep -Fqx "EnvironmentFile=$BACKUP_RUNTIME_ENV_FILE" "$loaded_config" \
        || ! grep -Fqx "ExecStart=/bin/bash $CURRENT_LINK/app/scripts/backup.sh" "$loaded_config" \
        || ! grep -Fqx 'TimeoutStartSec=15min' "$loaded_config"; then
        fail "正式日备份 unit 缺少精确运行配置"
        return 1
    fi
    chmod 0600 "$loaded_config"
    "$VENV_DIR/bin/python" - "$STATE_DIR/backups/daily" "$before_snapshot" <<'PY'
import json
from pathlib import Path
import sys

directory = Path(sys.argv[1])
snapshot = sorted(
    path.name
    for path in directory.glob('health_weather_*.db.gz')
    if path.is_file() and not path.is_symlink()
)
Path(sys.argv[2]).write_text(json.dumps(snapshot), encoding='utf-8')
PY
    chmod 0600 "$before_snapshot"

    if ! "$SYSTEMCTL_BIN" start case-weather-backup.service; then
        fail "正式日备份 unit 实际执行失败"
        return 1
    fi
    unit_result="$($SYSTEMCTL_BIN show \
        case-weather-backup.service \
        --property=Result \
        --value)"
    exec_status="$($SYSTEMCTL_BIN show \
        case-weather-backup.service \
        --property=ExecMainStatus \
        --value)"
    if [ "$unit_result" != success ] || [ "$exec_status" != 0 ]; then
        fail "正式日备份 unit 执行结果异常: result=${unit_result:-unknown}, status=${exec_status:-unknown}"
        return 1
    fi
    if ! new_archive="$($VENV_DIR/bin/python - \
        "$STATE_DIR/backups/daily" \
        "$before_snapshot" <<'PY'
import json
from pathlib import Path
import sys

directory = Path(sys.argv[1])
before = set(json.loads(Path(sys.argv[2]).read_text(encoding='utf-8')))
after = [
    path
    for path in directory.glob('health_weather_*.db.gz')
    if path.is_file() and not path.is_symlink() and path.name not in before
]
if len(after) != 1:
    raise SystemExit(1)
print(after[0])
PY
    )"; then
        fail "正式日备份 unit 未生成唯一的新归档"
        return 1
    fi
    if ! gzip -t "$new_archive" \
        || ! gzip -cd "$new_archive" > "$extracted_backup" \
        || ! sqlite_quick_check "$extracted_backup"; then
        fail "正式日备份 unit 的新归档未通过 gzip 与 SQLite 校验"
        return 1
    fi
    source_digest="$(sqlite_logical_digest "$DATABASE_FILE")"
    backup_digest="$(sqlite_logical_digest "$extracted_backup")"
    if [ "$source_digest" != "$backup_digest" ]; then
        fail "正式日备份 unit 归档内容与冻结源数据库不一致"
        return 1
    fi
    chmod 0600 "$extracted_backup"
    write_durable_marker \
        "$TRANSACTION_DIR/ACTUAL_BACKUP_UNIT_VERIFIED" \
        "$(printf 'archive=%s\nsha256=%s' "$new_archive" "$backup_digest")"
    log "正式 case-weather-backup.service 已实际运行并通过内容一致性校验"
}

verify_pre_request_quiescence() {
    local unit
    for unit in "${INSTALL_UNITS[@]}"; do
        query_unit_load_state "$unit"
        if [ "$UNIT_LOAD_STATE" != loaded ]; then
            fail "正式天气请求前新 unit 未完整加载: $unit"
            return 1
        fi
        query_unit_active_state "$unit"
        case "$UNIT_ACTIVE_STATE" in
            active|activating|reloading|deactivating)
                fail "正式天气请求前业务单元仍在运行: $unit=$UNIT_ACTIVE_STATE"
                return 1
                ;;
        esac
    done
    for unit in "${LEGACY_UNITS[@]}"; do
        query_unit_load_state "$unit"
        if [ "$UNIT_LOAD_STATE" != not-found ]; then
            fail "正式天气请求前旧 unit 仍被加载: $unit"
            return 1
        fi
    done
    [ -f "$TRANSACTION_DIR/ACTUAL_BACKUP_UNIT_VERIFIED" ] || {
        fail "正式天气请求前缺少已安装备份 unit 的验证票据"
        return 1
    }
    log "正式天气请求前所有公网服务、writer 与 timer 均保持停止"
}

archive_backup_validation_artifacts() {
    [ -d "$BACKUP_VALIDATION_DIR" ] || return 0
    if [ -e "$BACKUP_VALIDATION_ARCHIVE_DIR" ] \
        || [ -L "$BACKUP_VALIDATION_ARCHIVE_DIR" ]; then
        fail "事务中的备份验证归档目录已存在"
        return 1
    fi
    mv "$BACKUP_VALIDATION_DIR" "$BACKUP_VALIDATION_ARCHIVE_DIR"
}

start_release_timers() {
    local unit failed=0
    for unit in "${START_TIMER_UNITS[@]}"; do
        if ! "$SYSTEMCTL_BIN" restart "$unit"; then
            failed=1
            continue
        fi
        if ! "$SYSTEMCTL_BIN" is-active --quiet "$unit"; then
            failed=1
        fi
    done
    if [ "$failed" -ne 0 ]; then
        fail "一个或多个发布 timer 启动失败，已继续尝试其余 timer"
        return 1
    fi
}

repair_release_timers_best_effort() {
    local unit failed=0
    # 向前修复阶段必须逐个补齐，单个失败不能阻断其他关键调度。
    for unit in "${START_TIMER_UNITS[@]}"; do
        "$SYSTEMCTL_BIN" enable "$unit" >/dev/null 2>&1 || failed=1
        "$SYSTEMCTL_BIN" restart "$unit" >/dev/null 2>&1 || failed=1
        "$SYSTEMCTL_BIN" is-active --quiet "$unit" >/dev/null 2>&1 || failed=1
    done
    for unit in "${DEFERRED_TIMER_UNITS[@]}"; do
        "$SYSTEMCTL_BIN" disable "$unit" >/dev/null 2>&1 || failed=1
        if "$SYSTEMCTL_BIN" is-active --quiet "$unit" >/dev/null 2>&1; then
            "$SYSTEMCTL_BIN" stop "$unit" >/dev/null 2>&1 || failed=1
        fi
    done
    return "$failed"
}

verify_release_state() {
    local unit unit_file_state on_success next_us uptime_us remaining_us link_target

    for unit in case-weather.service \
        case-weather-backup.timer \
        case-weather-cache-bootstrap.timer \
        case-weather-risk-precompute.timer \
        case-weather-usage-cleanup.timer; do
        if ! "$SYSTEMCTL_BIN" is-active --quiet "$unit"; then
            fail "发布后单元未处于 active: $unit"
            return 1
        fi
    done

    unit_file_state="$($SYSTEMCTL_BIN is-enabled case-weather-backup.timer 2>/dev/null || true)"
    if [ "$unit_file_state" != enabled ]; then
        fail "备份 timer 状态应为 enabled，实际为 ${unit_file_state:-unknown}"
        return 1
    fi
    unit_file_state="$($SYSTEMCTL_BIN is-enabled case-weather-cache-bootstrap.timer 2>/dev/null || true)"
    if [ "$unit_file_state" != enabled ]; then
        fail "bootstrap timer 状态应为 enabled，实际为 ${unit_file_state:-unknown}"
        return 1
    fi
    query_unit_load_state case-weather-cache.timer
    if [ "$UNIT_LOAD_STATE" != loaded ]; then
        fail "常规天气缓存 timer 未正确安装"
        return 1
    fi
    unit_file_state="$($SYSTEMCTL_BIN is-enabled case-weather-cache.timer 2>/dev/null || true)"
    if [ "$unit_file_state" != disabled ]; then
        fail "常规天气缓存 timer 状态应为 disabled，实际为 ${unit_file_state:-unknown}"
        return 1
    fi
    query_unit_active_state case-weather-cache.timer
    case "$UNIT_ACTIVE_STATE" in
        active|activating|reloading|deactivating)
            fail "常规天气缓存 timer 在首轮等待期间不应提前运行: $UNIT_ACTIVE_STATE"
            return 1
            ;;
    esac

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
    for unit in "${LEGACY_UNITS[@]}"; do
        query_unit_load_state "$unit"
        if [ "$UNIT_LOAD_STATE" = loaded ]; then
            fail "旧 systemd 单元仍存在: $unit"
            return 1
        fi
    done
    # Persistent timer 可能在启用后立即补跑一次合法备份，先等待其自然完成。
    wait_for_backup_completion
    verify_no_retired_processes
    verify_root_crontab_retired

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
    if [ "$remaining_us" -lt 1750000000 ] || [ "$remaining_us" -gt 1810000000 ]; then
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

restore_backup_runtime_environment() {
    local failed_env="$TRANSACTION_DIR/backup-runtime-from-failed-release.env"
    [ "$BACKUP_RUNTIME_ENV_MUTATION_STARTED" -eq 1 ] || return 0
    if [ -e "$BACKUP_RUNTIME_ENV_FILE" ]; then
        mv "$BACKUP_RUNTIME_ENV_FILE" "$failed_env" || return 1
        chmod 0600 "$failed_env" || return 1
    fi
    if [ "$BACKUP_RUNTIME_ENV_EXISTED" -eq 1 ]; then
        [ "$BACKUP_RUNTIME_ENV_BACKUP_READY" -eq 1 ] || return 1
        cp -a "$BACKUP_RUNTIME_ENV_BACKUP" "$BACKUP_RUNTIME_ENV_FILE.restore.$$" \
            || return 1
        chmod 0600 "$BACKUP_RUNTIME_ENV_FILE.restore.$$" || return 1
        atomic_replace \
            "$BACKUP_RUNTIME_ENV_FILE.restore.$$" \
            "$BACKUP_RUNTIME_ENV_FILE" \
            || return 1
        "$CHOWN_BIN" root:root "$BACKUP_RUNTIME_ENV_FILE" || return 1
        chmod 0600 "$BACKUP_RUNTIME_ENV_FILE" || return 1
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

    for unit in case-weather-backup.timer \
        case-weather-risk-precompute.timer \
        case-weather-usage-cleanup.timer \
        "${LEGACY_TIMER_UNITS[@]}"; do
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
        query_unit_load_state case-weather-cache-bootstrap.timer || return 1
        if [ "$UNIT_LOAD_STATE" != loaded ]; then
            fail "无法恢复被中断的天气同步：bootstrap timer 未加载"
            return 1
        fi
        restore_start_unit case-weather-cache-bootstrap.timer || return 1
        log "检测到被中断的天气同步，已改为 30 分钟后安全重试"
    fi
}

restore_backup_timer_state_only() {
    local unit exists enabled active
    while IFS=$'\t' read -r unit exists enabled active; do
        [ "$unit" = case-weather-backup.timer ] || continue
        [ "$exists" = 1 ] || return 0
        case "$enabled" in
            enabled) "$SYSTEMCTL_BIN" enable "$unit" >/dev/null || return 1 ;;
            enabled-runtime) "$SYSTEMCTL_BIN" enable --runtime "$unit" >/dev/null || return 1 ;;
            disabled) "$SYSTEMCTL_BIN" disable "$unit" >/dev/null || return 1 ;;
        esac
        if captured_unit_active "$unit"; then
            restore_start_unit "$unit" || return 1
        else
            "$SYSTEMCTL_BIN" stop "$unit" >/dev/null 2>&1 || return 1
        fi
        return 0
    done < "$STATE_FILE"
}

rollback_release() {
    local failed=0
    log "激活失败，开始恢复部署前状态"
    set +e
    if [ "$RUNTIME_QUIESCE_STARTED" -eq 0 ]; then
        # 只触碰过 backup timer 时，不停止公网服务或其他调度。
        restore_backup_timer_state_only || failed=1
        if [ "$failed" -eq 0 ]; then
            durably_sync_release_state rollback || failed=1
        fi
        if [ "$failed" -eq 0 ]; then
            write_durable_marker "$ROLLED_BACK_MARKER" success || failed=1
        fi
        if [ "$failed" -eq 0 ]; then
            remove_activation_boot_guard "$TRANSACTION_DIR" || failed=1
        fi
        set -e
        if [ "$failed" -ne 0 ]; then
            {
                echo '发布在运行时静默前失败，backup timer 未能完整恢复。'
                echo "事务目录: $TRANSACTION_DIR"
                echo '公网应用与其他调度未被停止，请人工核对备份调度。'
            } > "$FAILURE_MARKER"
            return 1
        fi
        log "已恢复发布前的 backup timer，公网服务未中断"
        return 0
    fi
    if ! stop_units_best_effort; then
        failed=1
    fi
    if [ "$failed" -eq 0 ]; then
        restore_database || failed=1
        restore_backup_runtime_environment || failed=1
        restore_environment || failed=1
        restore_current_link || failed=1
        if [ "$UNITS_MUTATED" -eq 1 ]; then
            restore_unit_files || failed=1
        fi
        restore_unit_states || failed=1
    fi
    if [ "$failed" -eq 0 ]; then
        durably_sync_release_state rollback || failed=1
    fi
    if [ "$failed" -eq 0 ]; then
        write_durable_marker "$ROLLED_BACK_MARKER" success || failed=1
    fi
    if [ "$failed" -eq 0 ]; then
        remove_activation_boot_guard "$TRANSACTION_DIR" || failed=1
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
    log "已恢复部署前配置、数据库、代码入口与 systemd 状态"
}

on_exit() {
    local rc=$?
    local timer_repair_status=0
    local forward_quiesce_status=0
    local forward_gate_status=0
    local forward_sync_status=0
    local marker_status=0
    local marker_payload
    trap - EXIT INT TERM HUP
    stop_candidate_release
    archive_backup_validation_artifacts || true
    if [ "$rc" -eq 0 ]; then
        exit 0
    fi
    if [ "$COMMITTED" -eq 1 ]; then
        repair_release_timers_best_effort || timer_repair_status=$?
        durably_sync_release_state commit || forward_sync_status=$?
        marker_payload="$(
            echo '新版本已通过首次公网健康检查并进入向前提交阶段；timer 启动或完整状态复核失败，为避免覆盖用户写入，本次不会回滚数据库。'
            echo "事务目录: $TRANSACTION_DIR"
            if [ "$timer_repair_status" -eq 0 ]; then
                echo '已逐个补齐并复核 backup、bootstrap、risk 与 cleanup timer。'
            else
                echo '已逐个尝试修复全部 timer，仍有单元失败，请立即人工检查。'
            fi
            if [ "$forward_sync_status" -ne 0 ]; then
                echo '向前状态未能完成 durability barrier，请保持开机门并人工核对磁盘状态。'
            fi
            echo '请检查 systemctl status、应用日志与 timer 状态，并在当前版本上向前修复。'
        )"
        write_durable_marker "$POST_COMMIT_MARKER" "$marker_payload" || marker_status=$?
        log "向前修复阶段失败，已保留新版本并写入标记: $POST_COMMIT_MARKER" >&2
        if [ "$forward_sync_status" -ne 0 ] || [ "$marker_status" -ne 0 ]; then
            exit 70
        fi
        exit "$rc"
    fi
    if [ "$FORWARD_ONLY" -eq 1 ]; then
        # 请求已开始或公网已尝试启动。保留新快照与 receipt，停住所有入口等待人工确认。
        stop_units_best_effort || forward_quiesce_status=$?
        if [ "$FORMAL_NETWORK_GATE_OPEN" -eq 1 ]; then
            arm_qweather_network_gate || forward_gate_status=$?
            if [ "$forward_gate_status" -eq 0 ]; then
                FORMAL_NETWORK_GATE_OPEN=0
            fi
        fi
        revoke_runtime_activation_permit "$TRANSACTION_DIR" || forward_quiesce_status=1
        durably_sync_release_state forward || forward_sync_status=$?
        marker_payload="$(
            echo '唯一一次正式天气请求已经开始，或公网服务已经尝试启动；本次保留新数据库、环境、代码入口与 systemd unit。'
            echo "事务目录: $TRANSACTION_DIR"
            echo '全部业务入口已尽力停止，持久开机门保持启用；禁止自动重试天气请求。'
            if [ "$forward_quiesce_status" -ne 0 ]; then
                echo '仍有单元未能确认停止，请立即人工检查。'
            fi
            if [ "$forward_gate_status" -ne 0 ]; then
                echo '30 分钟出网保护未能确认恢复，请勿手工启动天气同步。'
            fi
            if [ "$forward_sync_status" -ne 0 ]; then
                echo '向前状态未能完成 durability barrier，请人工核对磁盘状态。'
            fi
            echo '确认 receipt、QWeather 计数、SQLite 快照和 unit 状态后，再显式确认本事务继续发布。'
        )"
        write_durable_marker "$POST_COMMIT_MARKER" "$marker_payload" || marker_status=$?
        log "不可逆发布阶段失败，已保持停机与开机门: $POST_COMMIT_MARKER" >&2
        if [ "$forward_quiesce_status" -ne 0 ] \
            || [ "$forward_gate_status" -ne 0 ] \
            || [ "$forward_sync_status" -ne 0 ] \
            || [ "$marker_status" -ne 0 ]; then
            exit 70
        fi
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
validate_absolute_path RUNTIME_BOOT_GUARD_DIR "$RUNTIME_BOOT_GUARD_DIR"
validate_runtime_boot_guard_location
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
case "$BACKUP_WAIT_ATTEMPTS" in
    ''|0|*[!0-9]*)
        echo 'BACKUP_WAIT_ATTEMPTS 必须是 1 至 900 的整数' >&2
        exit 2
        ;;
esac
if [ "$BACKUP_WAIT_ATTEMPTS" -gt 900 ]; then
    echo 'BACKUP_WAIT_ATTEMPTS 必须是 1 至 900 的整数' >&2
    exit 2
fi
case "$BACKUP_WAIT_SLEEP_SECONDS" in
    ''|*[!0-9]*)
        echo 'BACKUP_WAIT_SLEEP_SECONDS 必须是 0 至 60 的整数' >&2
        exit 2
        ;;
esac
if [ "$BACKUP_WAIT_SLEEP_SECONDS" -gt 60 ]; then
    echo 'BACKUP_WAIT_SLEEP_SECONDS 必须是 0 至 60 的整数' >&2
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
command -v "$SYSTEMD_RUN_BIN" >/dev/null 2>&1 || require_executable "$SYSTEMD_RUN_BIN"
command -v "$SQLITE3_BIN" >/dev/null 2>&1 || require_executable "$SQLITE3_BIN"
command -v "$CURL_BIN" >/dev/null 2>&1 || require_executable "$CURL_BIN"
command -v "$FLOCK_BIN" >/dev/null 2>&1 || require_executable "$FLOCK_BIN"
command -v "$BUSCTL_BIN" >/dev/null 2>&1 || require_executable "$BUSCTL_BIN"
command -v "$CRONTAB_BIN" >/dev/null 2>&1 || require_executable "$CRONTAB_BIN"
command -v "$PGREP_BIN" >/dev/null 2>&1 || require_executable "$PGREP_BIN"
command -v gzip >/dev/null 2>&1 || require_executable gzip
if [ "$(id -u)" != "$(id -u "$RUNTIME_USER")" ]; then
    command -v "$RUNUSER_BIN" >/dev/null 2>&1 || require_executable "$RUNUSER_BIN"
fi
command -v "$CHOWN_BIN" >/dev/null 2>&1 || require_executable "$CHOWN_BIN"
command -v "$ENV_BIN" >/dev/null 2>&1 || require_executable "$ENV_BIN"
require_file "$UPTIME_FILE"
validate_release_dependencies
validate_formal_release_identity
if [ -n "$INHERITED_DATABASE_FILE" ] || [ -n "$INHERITED_DATABASE_URI" ]; then
    echo '禁止继承 DATABASE_FILE 或 DATABASE_URI；数据库只能由冻结的候选配置决定' >&2
    exit 2
fi

mkdir -p "$RELEASE_ROOT"
prepare_control_directories
validate_recovery_transaction_realpath
exec 9> "$RELEASE_ROOT/deploy.lock"
if ! "$FLOCK_BIN" -n 9; then
    echo '已有另一个部署事务正在运行，本次发布未修改生产状态。' >&2
    exit 73
fi

acknowledge_recovery_transaction
recover_activation_boot_guard_if_acknowledged
detect_unfinished_transactions
mkdir -p "$TRANSACTION_DIR"
fsync_directory "$TRANSACTION_ROOT"
capture_previous_state
durably_checkpoint_recovery_materials captured-state
verify_activation_guard_dropins
validate_backup_database_config
DATABASE_FILE="$(resolve_database_file "$STAGED_ENV_FILE")"
validate_absolute_path DATABASE_FILE "$DATABASE_FILE"
validate_managed_backup_database_path
preflight_root_crontab
verify_backup_not_running
verify_root_crontab_retired_before_activation
write_durable_marker "$STARTED_MARKER" "$NEW_RELEASE"

MUTATION_STARTED=1
prepare_activation_boot_guard
stop_units_strictly
backup_environment
backup_backup_runtime_environment
backup_database
durably_checkpoint_recovery_materials recovery-backups
apply_staged_environment
apply_backup_runtime_environment
prepare_runtime_permissions
# 候选进程、迁移和正式烟测必须读取刚刚应用的同一份外置配置。
export CASE_WEATHER_ENV_FILE="$ENV_FILE"

export DATABASE_FILE
tighten_database_permissions

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

LINK_MUTATED=1
switch_current_link "$NEW_RELEASE"
install_new_units
prepare_release_timer_states
validate_managed_backup_service
validate_installed_backup_service
verify_pre_request_quiescence
run_formal_cache_smoke
arm_qweather_network_gate
start_new_release

mkdir -p "$STATE_DIR/deployments"
printf '%s\n' "$NEW_RELEASE" > "$STATE_DIR/deployments/current-release.next.$$"
chmod 0600 "$STATE_DIR/deployments/current-release.next.$$"
atomic_replace \
    "$STATE_DIR/deployments/current-release.next.$$" \
    "$STATE_DIR/deployments/current-release"
COMMITTED=1
start_release_timers
verify_release_state
observe_post_commit_stability
durably_sync_release_state commit
write_durable_marker "$TRANSACTION_DIR/COMMITTED" success
remove_activation_boot_guard "$TRANSACTION_DIR"
log "发布已提交: $NEW_RELEASE"
log "运维提示：后续只清理临时 preflight/activate 单元，禁止停止或禁用天气缓存 timer"
