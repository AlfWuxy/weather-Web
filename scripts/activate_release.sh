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
DEPLOY_INTENT="${DEPLOY_INTENT:-web_backend_only}"
EXPECTED_WECHAT_FORMAL_RUNTIME="${EXPECTED_WECHAT_FORMAL_RUNTIME:-}"
EXPECTED_WEB_PRIVATE_FEATURES_ENABLED="${EXPECTED_WEB_PRIVATE_FEATURES_ENABLED:-}"
EXPECTED_RELEASE_COMMIT="${EXPECTED_RELEASE_COMMIT:-}"
EXPECTED_RELEASE_BRANCH="${EXPECTED_RELEASE_BRANCH:-}"
QWEATHER_BUDGET_SNAPSHOT_HELPER="${QWEATHER_BUDGET_SNAPSHOT_HELPER:-}"
QWEATHER_PENDING_KEY_PATH="${QWEATHER_PENDING_KEY_PATH:-}"
QWEATHER_KEY_TRANSITION_FAIL_AT="${QWEATHER_KEY_TRANSITION_FAIL_AT:-}"
FORMAL_SMOKE_LEASE_HELPER="${FORMAL_SMOKE_LEASE_HELPER:-}"
# 仅供非 root 测试夹具替代真实 Nginx；正式发布禁止覆盖。
RUNTIME_LOG_BOUNDARY_TEST_HELPER="${RUNTIME_LOG_BOUNDARY_TEST_HELPER:-}"
RUNTIME_USER="${RUNTIME_USER:-case-weather}"
RUNTIME_GROUP="${RUNTIME_GROUP:-case-weather}"
CONTROL_OWNER_UID="${CONTROL_OWNER_UID:-0}"
CONTROL_OWNER_GID="${CONTROL_OWNER_GID:-0}"
EXPECTED_REQUIREMENTS_LOCK_SHA256="c7e450c30d7d3c56bdf210f69a58620cba9d99e462e0e2c254ab45456271f853"

APP_DIR="$NEW_RELEASE/app"
VENV_DIR="$NEW_RELEASE/venv"
CANDIDATE_BASE_STATE_FILE="$NEW_RELEASE/private-metadata/candidate-base-state.json"
CI_PROOF_FILE="$NEW_RELEASE/private-metadata/ci-proof.json"
MINIPROGRAM_CI_PROOF_FILE="$NEW_RELEASE/private-metadata/miniprogram-ci-proof.json"
MODEL_ARTIFACT_RECEIPT_FILE="$NEW_RELEASE/private-metadata/model-artifacts.json"
RUNTIME_SMOKE_RECEIPT_FILE="$NEW_RELEASE/private-metadata/runtime-smoke.json"
DEPENDENCY_RECEIPT_FILE="$NEW_RELEASE/private-metadata/dependency-receipt.json"
RELEASE_ID="${NEW_RELEASE##*/}"
TRANSACTION_ROOT="$STATE_DIR/backups/deploy-transactions"
FORMAL_SMOKE_RECEIPT_ROOT="$STATE_DIR/deployments/formal-cache-smokes"
TRANSACTION_DIR="$TRANSACTION_ROOT/${RELEASE_ID}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
QWEATHER_PRIVATE_DIR="$STATE_DIR/private"
QWEATHER_KEY_PLAN="$TRANSACTION_DIR/qweather-key-transition.json"
QWEATHER_KEY_ARCHIVE_DIR="$TRANSACTION_DIR/qweather-key-recovery"
QWEATHER_KEY_FINAL_CREATED_MARKER="$TRANSACTION_DIR/QWEATHER_KEY_FINAL_CREATED"
QWEATHER_KEY_PENDING_CLEANED_MARKER="$TRANSACTION_DIR/QWEATHER_KEY_PENDING_CLEANED"
QWEATHER_KEY_RECOVERED_MARKER="$TRANSACTION_DIR/QWEATHER_KEY_RECOVERED"
FORWARD_ONLY_MARKER="$TRANSACTION_DIR/FORWARD_ONLY_REQUIRED"
PUBLIC_START_MARKER="$TRANSACTION_DIR/PUBLIC_START_ATTEMPTED"
FORMAL_SMOKE_LEASE_JOURNAL="$TRANSACTION_DIR/formal-smoke-lease.journal"
STATE_FILE="$TRANSACTION_DIR/unit-state.tsv"
OLD_LINK_FILE="$TRANSACTION_DIR/old-current-link"
DB_BACKUP="$TRANSACTION_DIR/database-before.db"
ENV_BACKUP="$TRANSACTION_DIR/environment-before.env"
ENV_METADATA="$TRANSACTION_DIR/environment-before.metadata"
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
ACTIVATION_GUARD_DROPIN_STATE="$TRANSACTION_DIR/activation-guard-dropins.tsv"
ACTIVATION_GUARD_DROPIN_BACKUP_DIR="$TRANSACTION_DIR/activation-guard-dropins"
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
RETIRED_BOOTSTRAP_UNITS=(
    case-weather-cache-bootstrap.service
)
SERVICE_UNITS=(
    case-weather-backup.service
    case-weather-cache.service
    case-weather-dispatch.service
    case-weather-risk-precompute.service
    case-weather-usage-cleanup.service
    case-weather.service
)
INSTALL_UNITS=("${MANAGED_TIMER_UNITS[@]}" "${SERVICE_UNITS[@]}")
LEGACY_UNITS=("${LEGACY_TIMER_UNITS[@]}" "${LEGACY_SERVICE_UNITS[@]}" "${RETIRED_BOOTSTRAP_UNITS[@]}")
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
ACTIVATION_GUARD_DROPINS_MUTATED=0
RUNTIME_QUIESCE_STARTED=0
RUNTIME_KEY_QUIESCENCE_PROVEN=0
QWEATHER_KEY_TRANSITION_REQUIRED=0
QWEATHER_KEY_TRANSITION_ACTION=""
QWEATHER_FINAL_KEY_PATH=""
QWEATHER_KEY_SHA256=""
QWEATHER_KEY_FINAL_CREATED=0
QWEATHER_KEY_PENDING_CLEANED=0
CANDIDATE_PID=""
RELEASE_COMMIT=""
FORMAL_RELEASE_COMMIT=""
FORMAL_RELEASE_CONFIG_FINGERPRINT=""
FORMAL_SMOKE_RECEIPT_DIR=""
FORMAL_SMOKE_REUSED=0
FORMAL_SMOKE_IRREVERSIBLE=0
FORMAL_NETWORK_GATE_OPEN=0
FORMAL_SMOKE_TOKEN=""
FORMAL_SMOKE_TOKEN_SHA256=""
FORMAL_SMOKE_BINDING=""
FORMAL_SMOKE_TICKET=""
FORMAL_SMOKE_LEASE_TOKEN=""
FORMAL_SMOKE_LEASE_TOKEN_SHA256=""
FORMAL_SMOKE_REDIS_BACKEND_SHA256=""
FORMAL_SMOKE_LEASE_RESERVED=0
FORMAL_SMOKE_RECEIPT_REUSE_CANDIDATE=0

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
    if [[ "$value" != /* \
        || "$value" = "/" \
        || "$value" == *"'"* \
        || "$value" == *$'\t'* \
        || "$value" == *$'\r'* \
        || "$value" == *$'\n'* ]]; then
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
import os
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
                || [[ ! "$enabled" =~ ^(enabled|enabled-runtime|disabled|static)$ ]]; then
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
    capture_activation_guard_dropin_state
}

capture_activation_guard_dropin_state() {
    local unit directory dropin directory_existed file_existed
    local directory_metadata directory_uid directory_gid directory_mode
    local temporary_name
    mkdir -p "$ACTIVATION_GUARD_DROPIN_BACKUP_DIR"
    : > "$ACTIVATION_GUARD_DROPIN_STATE"
    chmod 0600 "$ACTIVATION_GUARD_DROPIN_STATE"

    for unit in "${ALL_UNITS[@]}"; do
        directory="$UNIT_DIR/$unit.d"
        dropin="$directory/$ACTIVATION_GUARD_DROPIN_NAME"
        directory_existed=0
        file_existed=0
        directory_uid=-
        directory_gid=-
        directory_mode=-
        temporary_name=".$ACTIVATION_GUARD_DROPIN_NAME.next"
        if [ -e "$directory" ] || [ -L "$directory" ]; then
            if ! directory_metadata="$("$VENV_DIR/bin/python" - \
                "$directory" \
                "$UNIT_DIR" \
                "$unit" \
                "$CONTROL_OWNER_UID" \
                "$CONTROL_OWNER_GID" \
                "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" <<'PY'
import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
root = Path(sys.argv[2]).resolve(strict=True)
unit = sys.argv[3]
file_stat = path.lstat()
directory_mode = stat.S_IMODE(file_stat.st_mode)
listxattr = getattr(os, 'listxattr', None)
if listxattr is None:
    if sys.argv[6] != '1' or os.geteuid() == 0:
        raise SystemExit(1)
    extended_attributes = []
else:
    try:
        extended_attributes = listxattr(path, follow_symlinks=False)
    except OSError:
        raise SystemExit(1) from None
if (
    not stat.S_ISDIR(file_stat.st_mode)
    or stat.S_ISLNK(file_stat.st_mode)
    or path.name != f'{unit}.d'
    or path.parent.resolve(strict=True) != root
    or file_stat.st_uid != int(sys.argv[4])
    or file_stat.st_gid != int(sys.argv[5])
    or directory_mode & 0o022
    or any(
        name.startswith('system.posix_acl_')
        for name in extended_attributes
    )
):
    raise SystemExit(1)
print(
    f'{file_stat.st_uid}\t'
    f'{file_stat.st_gid}\t'
    f'{directory_mode:04o}'
)
PY
            )"; then
                fail "systemd drop-in 目录身份异常: $directory"
                return 1
            fi
            IFS=$'\t' read -r \
                directory_uid \
                directory_gid \
                directory_mode <<< "$directory_metadata"
            directory_existed=1
        fi
        if [ -e "$dropin" ] || [ -L "$dropin" ]; then
            if ! "$VENV_DIR/bin/python" - \
                "$dropin" \
                "$directory" \
                "$CONTROL_OWNER_UID" \
                "$CONTROL_OWNER_GID" <<'PY'
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
directory = Path(sys.argv[2]).resolve(strict=True)
file_stat = path.lstat()
if (
    not stat.S_ISREG(file_stat.st_mode)
    or stat.S_ISLNK(file_stat.st_mode)
    or path.parent.resolve(strict=True) != directory
    or file_stat.st_uid != int(sys.argv[3])
    or file_stat.st_gid != int(sys.argv[4])
):
    raise SystemExit(1)
PY
            then
                fail "systemd 断电保护 drop-in 身份异常: $dropin"
                return 1
            fi
            cp -a "$dropin" "$ACTIVATION_GUARD_DROPIN_BACKUP_DIR/$unit"
            file_existed=1
        fi
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$unit" \
            "$directory_existed" \
            "$file_existed" \
            "$directory_uid" \
            "$directory_gid" \
            "$directory_mode" \
            "$temporary_name" >> "$ACTIVATION_GUARD_DROPIN_STATE"
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

transaction_requires_forward_only() {
    local transaction="$1" state
    if ! state="$($VENV_DIR/bin/python - \
        "$transaction" \
        "$CONTROL_OWNER_UID" \
        "$CONTROL_OWNER_GID" <<'PY'
from pathlib import Path
import stat
import sys

transaction = Path(sys.argv[1]).resolve(strict=True)
owner_uid = int(sys.argv[2])
owner_gid = int(sys.argv[3])
forward = transaction / 'FORWARD_ONLY_REQUIRED'
public = transaction / 'PUBLIC_START_ATTEMPTED'


def read_marker(path, allowed):
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or stat.S_ISLNK(file_stat.st_mode)
        or stat.S_IMODE(file_stat.st_mode) != 0o600
        or file_stat.st_uid != owner_uid
        or file_stat.st_gid != owner_gid
        or path.parent.resolve(strict=True) != transaction
    ):
        raise SystemExit(1)
    value = path.read_text(encoding='utf-8').strip()
    if value not in allowed:
        raise SystemExit(1)
    return value


forward_value = read_marker(
    forward,
    {'phase=formal-smoke-started', 'phase=public-service-start'},
)
public_value = read_marker(public, {'phase=public-service-start'})
if public_value is not None and forward_value is None:
    raise SystemExit(1)
print('forward' if forward_value is not None else 'rollback-safe')
PY
    )"; then
        fail "事务 forward-only/public-start 阶段标记无效"
        return 2
    fi
    case "$state" in
        forward) return 0 ;;
        rollback-safe) return 1 ;;
        *) fail "事务 forward-only 阶段判定异常"; return 2 ;;
    esac
}

record_forward_only_phase() {
    local phase="$1" status=0
    case "$phase" in
        formal-smoke-started|public-service-start) ;;
        *) fail "forward-only 阶段名称无效: $phase"; return 1 ;;
    esac
    if [ ! -e "$FORWARD_ONLY_MARKER" ] && [ ! -L "$FORWARD_ONLY_MARKER" ]; then
        write_durable_marker "$FORWARD_ONLY_MARKER" "phase=$phase"
    else
        transaction_requires_forward_only "$TRANSACTION_DIR" || status=$?
        [ "$status" -eq 0 ] || {
            fail "已有 forward-only 阶段标记无法安全复用"
            return 1
        }
    fi
    if [ "$phase" = public-service-start ]; then
        if [ ! -e "$PUBLIC_START_MARKER" ] && [ ! -L "$PUBLIC_START_MARKER" ]; then
            write_durable_marker "$PUBLIC_START_MARKER" 'phase=public-service-start'
        else
            transaction_requires_forward_only "$TRANSACTION_DIR" || status=$?
            [ "$status" -eq 0 ] || {
                fail "已有 public-start 阶段标记无法安全复用"
                return 1
            }
        fi
    fi
    # 阶段标记已先于不可逆动作耐久落盘，进程内退出路径随后切换到向前恢复。
    FORWARD_ONLY=1
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
        "$ACTIVATION_GUARD_DROPIN_STATE" \
        "$ACTIVATION_GUARD_DROPIN_BACKUP_DIR" \
        "$CAPTURED_STATE_CHECKPOINT" \
        "$ENV_BACKUP" \
        "$ENV_METADATA" \
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
    dropin_state_raw,
    dropin_backup_dir_raw,
    captured_checkpoint_raw,
    env_backup_raw,
    env_metadata_raw,
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
dropin_state = Path(dropin_state_raw)
dropin_backup_dir = Path(dropin_backup_dir_raw)


def ensure_transaction_path(path):
    try:
        parent = path.parent.resolve(strict=True)
    except OSError:
        raise SystemExit(1) from None
    if parent not in {transaction, units_backup_dir, dropin_backup_dir}:
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
dropin_backup_stat = dropin_backup_dir.lstat()
if (
    not stat.S_ISDIR(transaction_stat.st_mode)
    or stat.S_ISLNK(transaction_stat.st_mode)
    or not stat.S_ISDIR(units_stat.st_mode)
    or stat.S_ISLNK(units_stat.st_mode)
    or not stat.S_ISDIR(dropin_backup_stat.st_mode)
    or stat.S_ISLNK(dropin_backup_stat.st_mode)
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

dropin_lines = require_regular(dropin_state).read_text(
    encoding='utf-8'
).splitlines()
if len(dropin_lines) != len(expected_units):
    raise SystemExit(1)
dropin_backups = []
for line, expected_unit in zip(dropin_lines, expected_units, strict=True):
    fields = line.split('\t')
    expected_temporary_name = f'.{os.path.basename("10-case-weather-activation-guard.conf")}.next'
    if (
        len(fields) != 7
        or fields[0] != expected_unit
        or fields[1] not in {'0', '1'}
        or fields[2] not in {'0', '1'}
        or (fields[2] == '1' and fields[1] != '1')
        or fields[6] != expected_temporary_name
    ):
        raise SystemExit(1)
    if fields[1] == '1':
        if (
            not fields[3].isdigit()
            or not fields[4].isdigit()
            or len(fields[5]) != 4
            or fields[5][0] != '0'
            or any(character not in '01234567' for character in fields[5])
            or int(fields[5], 8) & 0o022
        ):
            raise SystemExit(1)
    elif fields[3:6] != ['-', '-', '-']:
        raise SystemExit(1)
    backup = dropin_backup_dir / expected_unit
    if fields[2] == '1':
        dropin_backups.append(require_regular(backup))
    elif path_exists(backup):
        raise SystemExit(1)
for child in dropin_backup_dir.iterdir():
    if child.name not in expected_units:
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

files_to_sync = [
    state_file,
    old_link_file,
    dropin_state,
    *dropin_backups,
    *backup_units,
]
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
    env_metadata = Path(env_metadata_raw)
    if env_existed_raw == '1':
        metadata_fields = require_regular(env_metadata).read_text(
            encoding='utf-8'
        ).split()
        if (
            len(metadata_fields) != 3
            or not all(value.isdigit() for value in metadata_fields)
            or metadata_fields[2] not in {'600', '640'}
        ):
            raise SystemExit(1)
        files_to_sync.append(env_metadata)
    elif path_exists(env_metadata):
        raise SystemExit(1)
elif phase != 'captured-state':
    raise SystemExit(1)

for file_path in files_to_sync:
    fsync_regular(file_path)
for directory_path in (
    units_backup_dir,
    dropin_backup_dir,
    transaction,
    transaction.parent,
):
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

read_validated_activation_guard_terminal_count() {
    local guard_transaction="$1"
    "$VENV_DIR/bin/python" - \
        "$guard_transaction" \
        "$CONTROL_OWNER_UID" \
        "$CONTROL_OWNER_GID" <<'PY'
from pathlib import Path
import stat
import sys

transaction = Path(sys.argv[1]).resolve(strict=True)
owner_uid = int(sys.argv[2])
owner_gid = int(sys.argv[3])
terminal_count = 0
allowed_payloads = {
    'COMMITTED': {b'success\n'},
    'ROLLED_BACK': {b'success\n', b'pre-mutation\n'},
}
for name in ('COMMITTED', 'ROLLED_BACK'):
    marker = transaction / name
    try:
        marker_stat = marker.lstat()
    except FileNotFoundError:
        continue
    if (
        not stat.S_ISREG(marker_stat.st_mode)
        or stat.S_ISLNK(marker_stat.st_mode)
        or stat.S_IMODE(marker_stat.st_mode) != 0o600
        or marker_stat.st_uid != owner_uid
        or marker_stat.st_gid != owner_gid
        or marker.read_bytes() not in allowed_payloads[name]
    ):
        raise SystemExit(1)
    terminal_count += 1
if terminal_count > 1:
    raise SystemExit(1)
print(terminal_count)
PY
}

formal_runtime_stopped_probe_is_allowed() {
    local guard_transaction="" terminal_count=""
    [ -e "$ACTIVATION_BOOT_GUARD_FILE" ] \
        || [ -L "$ACTIVATION_BOOT_GUARD_FILE" ] \
        || return 1
    if ! guard_transaction="$(read_activation_guard_transaction)"; then
        fail "Nginx 停机恢复探针发现无效的持久开机门"
        return 2
    fi
    if ! terminal_count="$(
        read_validated_activation_guard_terminal_count "$guard_transaction"
    )"; then
        fail "Nginx 停机恢复探针发现无效或冲突的事务终态标记"
        return 2
    fi
    if [ -n "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ]; then
        if [ "$guard_transaction" != "$RECOVERY_ACKNOWLEDGED_TRANSACTION" ]; then
            fail "Nginx 停机恢复探针与持久开机门事务不匹配"
            return 2
        fi
        return 0
    fi
    # 只有唯一、可信的终态标记可以在无人工作确认时进入既有自动归档流程。
    [ "$terminal_count" = 1 ] || return 1
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
    'FORWARD_ONLY_REQUIRED',
        'PUBLIC_START_ATTEMPTED',
        'qweather-key-transition.json',
        'formal-smoke-lease.journal',
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
            or (
                name in {
                    'FORWARD_ONLY_REQUIRED',
                    'PUBLIC_START_ATTEMPTED',
                    'qweather-key-transition.json',
                    'formal-smoke-lease.journal',
                }
                and stat.S_IMODE(marker_stat.st_mode) != 0o600
            )
        ):
            raise SystemExit(1)
    if (
        started.exists()
        or (transaction / 'qweather-key-transition.json').exists()
        or (transaction / 'formal-smoke-lease.journal').exists()
        or (transaction / 'ROLLBACK_REQUIRED.txt').exists()
        or (transaction / 'POST_COMMIT_ATTENTION.txt').exists()
    ):
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
        if { [ ! -f "$RECOVERY_ACKNOWLEDGED_TRANSACTION/ACTIVATION_STARTED" ] \
                || [ -L "$RECOVERY_ACKNOWLEDGED_TRANSACTION/ACTIVATION_STARTED" ]; } \
            && { [ ! -f "$RECOVERY_ACKNOWLEDGED_TRANSACTION/qweather-key-transition.json" ] \
                || [ -L "$RECOVERY_ACKNOWLEDGED_TRANSACTION/qweather-key-transition.json" ]; } \
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
    # 人工确认只能在本事务的私钥计划已回收或已验证为向前保留状态后落盘。
    reconcile_acknowledged_qweather_key_plan "$RECOVERY_ACKNOWLEDGED_TRANSACTION"
    # forward-only 事务已经保留新入口；精确人工确认时同步修复历史账本。
    reconcile_acknowledged_current_release_ledger \
        "$RECOVERY_ACKNOWLEDGED_TRANSACTION"
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
    local guard_transaction recovered_guard terminal_count=""
    [ -e "$ACTIVATION_BOOT_GUARD_FILE" ] \
        || [ -L "$ACTIVATION_BOOT_GUARD_FILE" ] \
        || return 0
    if ! guard_transaction="$(read_activation_guard_transaction)"; then
        fail "持久开机门内容、权限或事务路径无效"
        return 1
    fi
    if ! terminal_count="$(
        read_validated_activation_guard_terminal_count "$guard_transaction"
    )"; then
        fail "持久开机门对应事务的终态标记无效或冲突"
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
    reconcile_acknowledged_qweather_key_plan "$guard_transaction"
    if ! validate_runtime_guard_permit "$guard_transaction"; then
        quarantine_runtime_activation_permit "$guard_transaction" || true
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

prepare_qweather_key_transition_plan() {
    local expected_owner_uid=0 expected_owner_gid=0 runtime_group_gid plan_summary
    if [ "$REQUIRE_WECHAT_READY" != 1 ]; then
        if [ -n "$QWEATHER_PENDING_KEY_PATH" ]; then
            fail "非正式激活不得携带 QWeather pending 私钥"
            return 1
        fi
        return 0
    fi
    if [ "$DEPLOY_INTENT" = web_backend_only ]; then
        if [ -n "$QWEATHER_PENDING_KEY_PATH" ]; then
            fail "网页/后端发布不得携带 QWeather pending 私钥"
            return 1
        fi
        log "正式运行态复用服务器现有 QWeather 私钥，不执行私钥转换"
        return 0
    fi
    if [ -z "$QWEATHER_PENDING_KEY_PATH" ]; then
        fail "正式 JWT 激活缺少 QWEATHER_PENDING_KEY_PATH"
        return 1
    fi
    if [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" = 1 ]; then
        expected_owner_uid="$CONTROL_OWNER_UID"
        expected_owner_gid="$CONTROL_OWNER_GID"
    fi
    runtime_group_gid="$(id -g "$RUNTIME_USER")"
    if ! plan_summary="$($VENV_DIR/bin/python - \
        "$STAGED_ENV_FILE" \
        "$QWEATHER_PENDING_KEY_PATH" \
        "$QWEATHER_PRIVATE_DIR" \
        "$RELEASE_ID" \
        "$expected_owner_uid" \
        "$expected_owner_gid" \
        "$runtime_group_gid" \
        "$QWEATHER_KEY_PLAN" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

(
    staged_env_raw,
    pending_raw,
    private_dir_raw,
    release_id,
    expected_owner_uid_raw,
    expected_owner_gid_raw,
    runtime_group_gid_raw,
    plan_raw,
) = sys.argv[1:]
expected_owner_uid = int(expected_owner_uid_raw)
expected_owner_gid = int(expected_owner_gid_raw)
runtime_group_gid = int(runtime_group_gid_raw)

if not re.fullmatch(r'[A-Za-z0-9._-]+', release_id):
    raise SystemExit(1)
if any(value != os.path.normpath(value) for value in (pending_raw, private_dir_raw, plan_raw)):
    raise SystemExit(1)
if any('\n' in value or '\r' in value or '\t' in value for value in sys.argv[1:]):
    raise SystemExit(1)

staged_env = Path(staged_env_raw)
staged_stat = staged_env.lstat()
if not stat.S_ISREG(staged_stat.st_mode) or stat.S_ISLNK(staged_stat.st_mode):
    raise SystemExit(1)
values = {}
for raw_line in staged_env.read_text(encoding='utf-8').splitlines():
    line = raw_line.strip()
    if not line or line.startswith('#'):
        continue
    key, separator, value = line.partition('=')
    if not separator or not key or key in values:
        raise SystemExit(1)
    values[key] = value
if values.get('QWEATHER_AUTH_MODE', '').strip().lower() != 'jwt':
    raise SystemExit(1)
final_raw = values.get('QWEATHER_JWT_PRIVATE_KEY_PATH', '')
if not final_raw or not os.path.isabs(final_raw) or final_raw != os.path.normpath(final_raw):
    raise SystemExit(1)
if '\n' in final_raw or '\r' in final_raw or '\t' in final_raw:
    raise SystemExit(1)

private_dir = Path(private_dir_raw)
private_stat = private_dir.lstat()
private_real = private_dir.resolve(strict=True)
if (
    not stat.S_ISDIR(private_stat.st_mode)
    or stat.S_ISLNK(private_stat.st_mode)
    or str(private_real) != private_dir_raw
    or private_stat.st_uid != expected_owner_uid
    or stat.S_IMODE(private_stat.st_mode) not in {0o700, 0o750}
):
    raise SystemExit(1)
if stat.S_IMODE(private_stat.st_mode) == 0o700:
    if private_stat.st_gid != expected_owner_gid:
        raise SystemExit(1)
elif private_stat.st_gid != runtime_group_gid:
    raise SystemExit(1)

pending = Path(pending_raw)
final = Path(final_raw)
expected_pending_name = f'.qweather-jwt.pending-{release_id}'
if pending.name != expected_pending_name or pending.parent != private_dir:
    raise SystemExit(1)
if final.parent != private_dir or final == pending or final.name.startswith('.qweather-jwt.pending-'):
    raise SystemExit(1)
if pending.parent.resolve(strict=True) != private_real or final.parent.resolve(strict=True) != private_real:
    raise SystemExit(1)


def stable_read(path, *, mode, uid, gid):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_uid != uid
            or before.st_gid != gid
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > 16 * 1024
        ):
            raise SystemExit(1)
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            total += len(chunk)
            if total > 16 * 1024:
                raise SystemExit(1)
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_nlink,
            before.st_size,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
            after.st_size,
        ):
            raise SystemExit(1)
        return b''.join(chunks), before
    finally:
        os.close(descriptor)


pending_payload, pending_stat = stable_read(
    pending,
    mode=0o600,
    uid=expected_owner_uid,
    gid=expected_owner_gid,
)
digest = hashlib.sha256(pending_payload).hexdigest()
try:
    final.lstat()
except FileNotFoundError:
    action = 'create'
    final_device_before = None
    final_inode_before = None
else:
    final_payload, final_stat = stable_read(
        final,
        mode=0o640,
        uid=expected_owner_uid,
        gid=runtime_group_gid,
    )
    if hashlib.sha256(final_payload).hexdigest() != digest:
        raise SystemExit(1)
    action = 'reuse'
    final_device_before = final_stat.st_dev
    final_inode_before = final_stat.st_ino
    if stat.S_IMODE(private_stat.st_mode) != 0o750 or private_stat.st_gid != runtime_group_gid:
        raise SystemExit(1)

plan = Path(plan_raw)
if plan.parent.resolve(strict=True) != plan.parent or plan.exists() or plan.is_symlink():
    raise SystemExit(1)
payload = {
    'version': 2,
    'release_id': release_id,
    'action': action,
    'pending_path': pending_raw,
    'final_path': final_raw,
    'sha256': digest,
    'pending_device': pending_stat.st_dev,
    'pending_inode': pending_stat.st_ino,
    'pending_nlink': pending_stat.st_nlink,
    'pending_size': pending_stat.st_size,
    'final_device_before': final_device_before,
    'final_inode_before': final_inode_before,
    'final_nlink_before': None if action == 'create' else final_stat.st_nlink,
    'private_device_before': private_stat.st_dev,
    'private_inode_before': private_stat.st_ino,
    'private_uid_before': private_stat.st_uid,
    'private_gid_before': private_stat.st_gid,
    'private_mode_before': stat.S_IMODE(private_stat.st_mode),
}
encoded = (json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_CLOEXEC', 0)
flags |= getattr(os, 'O_NOFOLLOW', 0)
descriptor = os.open(plan, flags, 0o600)
try:
    view = memoryview(encoded)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError('short write')
        view = view[written:]
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory = os.open(plan.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
try:
    os.fsync(directory)
finally:
    os.close(directory)
print(f'{action}\t{final_raw}\t{digest}')
PY
    )"; then
        fail "QWeather pending/final 私钥或转换计划校验失败"
        return 1
    fi
    IFS=$'\t' read -r \
        QWEATHER_KEY_TRANSITION_ACTION \
        QWEATHER_FINAL_KEY_PATH \
        QWEATHER_KEY_SHA256 <<< "$plan_summary"
    if [ -z "$QWEATHER_KEY_TRANSITION_ACTION" ] \
        || [ -z "$QWEATHER_FINAL_KEY_PATH" ] \
        || [[ ! "$QWEATHER_KEY_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
        fail "QWeather 私钥转换计划摘要无效"
        return 1
    fi
    QWEATHER_KEY_TRANSITION_REQUIRED=1
    log "已耐久记录 QWeather 私钥转换计划: $QWEATHER_KEY_TRANSITION_ACTION"
}

qweather_key_fault() {
    local point="$1"
    if [ "$QWEATHER_KEY_TRANSITION_FAIL_AT" = "$point" ] \
        || [ "$QWEATHER_KEY_TRANSITION_FAIL_AT" = "$point-cleanup" ]; then
        fail "测试注入 QWeather 私钥转换故障: $point"
        return 1
    fi
}

verify_qweather_key_quiescence() {
    local unit
    RUNTIME_KEY_QUIESCENCE_PROVEN=0
    for unit in "${SCHEDULER_UNITS[@]}" "${STOPPABLE_SERVICE_UNITS[@]}"; do
        query_unit_load_state "$unit"
        if [ "$UNIT_LOAD_STATE" = loaded ]; then
            query_unit_active_state "$unit"
            case "$UNIT_ACTIVE_STATE" in
                active|activating|reloading|deactivating)
                    fail "私钥提升前 systemd 单元仍在运行: $unit=$UNIT_ACTIVE_STATE"
                    return 1
                    ;;
            esac
        fi
    done
    verify_no_unmanaged_processes_after_quiesce
    RUNTIME_KEY_QUIESCENCE_PROVEN=1
}

promote_qweather_key_after_quiesce() {
    local expected_owner_uid=0 expected_owner_gid=0 runtime_group_gid
    [ "$QWEATHER_KEY_TRANSITION_REQUIRED" -eq 1 ] || return 0
    [ "$RUNTIME_QUIESCE_STARTED" -eq 1 ] || {
        fail "QWeather 私钥只能在运行时完全静默后提升"
        return 1
    }
    if [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" = 1 ]; then
        expected_owner_uid="$CONTROL_OWNER_UID"
        expected_owner_gid="$CONTROL_OWNER_GID"
    fi
    runtime_group_gid="$(id -g "$RUNTIME_USER")"
    verify_qweather_key_quiescence
    qweather_key_fault before-promotion
    if ! "$VENV_DIR/bin/python" - \
        "$QWEATHER_KEY_PLAN" \
        "$QWEATHER_KEY_TRANSITION_ACTION" \
        "$QWEATHER_PENDING_KEY_PATH" \
        "$QWEATHER_FINAL_KEY_PATH" \
        "$QWEATHER_KEY_SHA256" \
        "$expected_owner_uid" \
        "$expected_owner_gid" \
        "$runtime_group_gid" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

(
    plan_raw,
    expected_action,
    pending_raw,
    final_raw,
    expected_digest,
    owner_uid_raw,
    owner_gid_raw,
    runtime_gid_raw,
) = sys.argv[1:]
owner_uid = int(owner_uid_raw)
owner_gid = int(owner_gid_raw)
runtime_gid = int(runtime_gid_raw)
plan_path = Path(plan_raw)
plan_stat = plan_path.lstat()
if (
    not stat.S_ISREG(plan_stat.st_mode)
    or stat.S_ISLNK(plan_stat.st_mode)
    or plan_stat.st_nlink != 1
):
    raise SystemExit(1)
plan = json.loads(plan_path.read_text(encoding='utf-8'))
if (
    plan.get('version') != 2
    or plan.get('action') != expected_action
    or plan.get('pending_path') != pending_raw
    or plan.get('final_path') != final_raw
    or plan.get('sha256') != expected_digest
):
    raise SystemExit(1)


def digest(path, *, expected_mode, expected_uid, expected_gid, expected_nlink):
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0),
    )
    try:
        before = os.fstat(descriptor)
        payload = b''
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            payload += chunk
            if len(payload) > 16 * 1024:
                raise SystemExit(1)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != expected_mode
            or before.st_uid != expected_uid
            or before.st_gid != expected_gid
            or before.st_nlink != expected_nlink
            or before.st_size <= 0
            or before.st_ino != after.st_ino
            or before.st_mode != after.st_mode
            or before.st_nlink != after.st_nlink
            or before.st_size != after.st_size
        ):
            raise SystemExit(1)
        return hashlib.sha256(payload).hexdigest(), before
    finally:
        os.close(descriptor)


pending = Path(pending_raw)
final = Path(final_raw)
private_stat = final.parent.lstat()
if (
    not stat.S_ISDIR(private_stat.st_mode)
    or stat.S_ISLNK(private_stat.st_mode)
    or (private_stat.st_dev, private_stat.st_ino)
    != (plan.get('private_device_before'), plan.get('private_inode_before'))
    or (private_stat.st_uid, private_stat.st_gid, stat.S_IMODE(private_stat.st_mode))
    != (
        plan.get('private_uid_before'),
        plan.get('private_gid_before'),
        plan.get('private_mode_before'),
    )
):
    raise SystemExit(1)
pending_digest, pending_stat = digest(
    pending,
    expected_mode=0o600,
    expected_uid=owner_uid,
    expected_gid=owner_gid,
    expected_nlink=1,
)
if (
    pending_digest != expected_digest
    or plan.get('pending_nlink') != 1
    or (pending_stat.st_dev, pending_stat.st_ino) != (
    plan.get('pending_device'),
    plan.get('pending_inode'),
    )
):
    raise SystemExit(1)
if expected_action == 'create':
    if final.exists() or final.is_symlink():
        raise SystemExit(1)
    os.link(pending, final, follow_symlinks=False)
    final_digest, final_stat = digest(
        final,
        expected_mode=0o600,
        expected_uid=owner_uid,
        expected_gid=owner_gid,
        expected_nlink=2,
    )
    if final_digest != expected_digest or (final_stat.st_dev, final_stat.st_ino) != (
        plan.get('pending_device'),
        plan.get('pending_inode'),
    ):
        raise SystemExit(1)
elif expected_action == 'reuse':
    final_digest, final_stat = digest(
        final,
        expected_mode=0o640,
        expected_uid=owner_uid,
        expected_gid=runtime_gid,
        expected_nlink=1,
    )
    if final_digest != expected_digest or (final_stat.st_dev, final_stat.st_ino) != (
        plan.get('final_device_before'),
        plan.get('final_inode_before'),
    ):
        raise SystemExit(1)
else:
    raise SystemExit(1)
directory = os.open(final.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
    then
        fail "QWeather 私钥提升前重校验或 no-clobber link 失败"
        return 1
    fi
    if [ "$QWEATHER_KEY_TRANSITION_ACTION" = create ]; then
        QWEATHER_KEY_FINAL_CREATED=1
        write_durable_marker "$QWEATHER_KEY_FINAL_CREATED_MARKER" "$QWEATHER_FINAL_KEY_PATH"
        qweather_key_fault after-link
    fi
    if ! "$VENV_DIR/bin/python" - \
        "$QWEATHER_KEY_PLAN" \
        "$QWEATHER_PENDING_KEY_PATH" \
        "$QWEATHER_FINAL_KEY_PATH" \
        "$QWEATHER_KEY_SHA256" \
        "$QWEATHER_KEY_TRANSITION_ACTION" \
        "$expected_owner_uid" \
        "$expected_owner_gid" \
        "$runtime_group_gid" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

plan = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
pending = Path(sys.argv[2])
final = Path(sys.argv[3])
expected_digest = sys.argv[4]
action = sys.argv[5]
owner_uid = int(sys.argv[6])
owner_gid = int(sys.argv[7])
runtime_gid = int(sys.argv[8])
private_stat = final.parent.lstat()
if (
    plan.get('version') != 2
    or not stat.S_ISDIR(private_stat.st_mode)
    or stat.S_ISLNK(private_stat.st_mode)
    or (private_stat.st_dev, private_stat.st_ino)
    != (plan.get('private_device_before'), plan.get('private_inode_before'))
    or (private_stat.st_uid, private_stat.st_gid, stat.S_IMODE(private_stat.st_mode))
    != (
        plan.get('private_uid_before'),
        plan.get('private_gid_before'),
        plan.get('private_mode_before'),
    )
):
    raise SystemExit(1)


def read_digest(path, *, expected_mode, expected_gid, expected_nlink):
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0),
    )
    try:
        file_stat = os.fstat(descriptor)
        payload = b''
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            payload += chunk
            if len(payload) > 16 * 1024:
                raise SystemExit(1)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or stat.S_IMODE(file_stat.st_mode) != expected_mode
            or file_stat.st_uid != owner_uid
            or file_stat.st_gid != expected_gid
            or file_stat.st_nlink != expected_nlink
            or file_stat.st_size <= 0
            or (
                file_stat.st_dev,
                file_stat.st_ino,
                file_stat.st_mode,
                file_stat.st_nlink,
                file_stat.st_size,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_nlink,
                after.st_size,
            )
        ):
            raise SystemExit(1)
        return hashlib.sha256(payload).hexdigest(), file_stat
    finally:
        os.close(descriptor)


pending_digest, pending_stat = read_digest(
    pending,
    expected_mode=0o600,
    expected_gid=owner_gid,
    expected_nlink=2 if action == 'create' else 1,
)
final_mode = 0o600 if action == 'create' else 0o640
final_gid = owner_gid if action == 'create' else runtime_gid
final_digest, final_stat = read_digest(
    final,
    expected_mode=final_mode,
    expected_gid=final_gid,
    expected_nlink=2 if action == 'create' else 1,
)
if pending_digest != expected_digest or final_digest != expected_digest:
    raise SystemExit(1)
if (pending_stat.st_dev, pending_stat.st_ino) != (
    plan.get('pending_device'),
    plan.get('pending_inode'),
):
    raise SystemExit(1)
expected_final_identity = (
    (plan.get('pending_device'), plan.get('pending_inode'))
    if action == 'create'
    else (plan.get('final_device_before'), plan.get('final_inode_before'))
)
if (final_stat.st_dev, final_stat.st_ino) != expected_final_identity:
    raise SystemExit(1)
pending.unlink()
directory = os.open(final.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
    then
        fail "QWeather pending 私钥 root-only 清理失败"
        return 1
    fi
    QWEATHER_KEY_PENDING_CLEANED=1
    write_durable_marker "$QWEATHER_KEY_PENDING_CLEANED_MARKER" "$QWEATHER_PENDING_KEY_PATH"
    qweather_key_fault after-pending-unlink
    if [ "$QWEATHER_KEY_TRANSITION_ACTION" = create ]; then
        "$CHOWN_BIN" "root:$RUNTIME_GROUP" "$QWEATHER_FINAL_KEY_PATH"
        chmod 0640 "$QWEATHER_FINAL_KEY_PATH"
        "$CHOWN_BIN" "root:$RUNTIME_GROUP" "$QWEATHER_PRIVATE_DIR"
        chmod 0750 "$QWEATHER_PRIVATE_DIR"
        fsync_directory "$QWEATHER_PRIVATE_DIR"
        qweather_key_fault after-permissions
    fi
    if ! "$VENV_DIR/bin/python" - \
        "$QWEATHER_KEY_PLAN" \
        "$QWEATHER_PENDING_KEY_PATH" \
        "$QWEATHER_FINAL_KEY_PATH" \
        "$QWEATHER_KEY_SHA256" \
        "$QWEATHER_KEY_TRANSITION_ACTION" \
        "$expected_owner_uid" \
        "$runtime_group_gid" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

plan = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
pending = Path(sys.argv[2])
final = Path(sys.argv[3])
expected_digest = sys.argv[4]
action = sys.argv[5]
owner_uid = int(sys.argv[6])
runtime_gid = int(sys.argv[7])
if pending.exists() or pending.is_symlink():
    raise SystemExit(1)
descriptor = os.open(
    final,
    os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0),
)
try:
    before = os.fstat(descriptor)
    payload = b''
    while True:
        chunk = os.read(descriptor, 4096)
        if not chunk:
            break
        payload += chunk
        if len(payload) > 16 * 1024:
            raise SystemExit(1)
    after = os.fstat(descriptor)
finally:
    os.close(descriptor)
expected_identity = (
    (plan.get('pending_device'), plan.get('pending_inode'))
    if action == 'create'
    else (plan.get('final_device_before'), plan.get('final_inode_before'))
)
if (
    not stat.S_ISREG(before.st_mode)
    or stat.S_IMODE(before.st_mode) != 0o640
    or before.st_uid != owner_uid
    or before.st_gid != runtime_gid
    or before.st_nlink != 1
    or before.st_size <= 0
    or hashlib.sha256(payload).hexdigest() != expected_digest
    or (before.st_dev, before.st_ino) != expected_identity
    or (before.st_dev, before.st_ino, before.st_mode, before.st_nlink, before.st_size)
    != (after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size)
):
    raise SystemExit(1)
PY
    then
        fail "QWeather final 私钥终态校验失败"
        return 1
    fi
    log "QWeather 私钥已在运行时静默后完成 $QWEATHER_KEY_TRANSITION_ACTION"
}

reconcile_qweather_key_plan() {
    local transaction="$1" mode="$2" expected_owner_uid=0 expected_owner_gid=0 runtime_group_gid
    local plan="$transaction/qweather-key-transition.json"
    [ -e "$plan" ] || [ -L "$plan" ] || return 0
    if [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" = 1 ]; then
        expected_owner_uid="$CONTROL_OWNER_UID"
        expected_owner_gid="$CONTROL_OWNER_GID"
    fi
    runtime_group_gid="$(id -g "$RUNTIME_USER")"
    if { [ "$mode" = rollback ] || [ "$mode" = pre-mutation ]; } \
        && { [ "$QWEATHER_KEY_TRANSITION_FAIL_AT" = cleanup ] \
            || [[ "$QWEATHER_KEY_TRANSITION_FAIL_AT" == *-cleanup ]]; }; then
        fail "测试注入 QWeather 私钥回收故障"
        return 1
    fi
    if ! "$VENV_DIR/bin/python" - \
        "$plan" \
        "$transaction" \
        "$QWEATHER_PRIVATE_DIR" \
        "$mode" \
        "$expected_owner_uid" \
        "$expected_owner_gid" \
        "$runtime_group_gid" \
        "$RUNTIME_KEY_QUIESCENCE_PROVEN" \
        "$QWEATHER_KEY_TRANSITION_FAIL_AT" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

plan_path = Path(sys.argv[1])
transaction = Path(sys.argv[2]).resolve(strict=True)
private_path = Path(sys.argv[3])
private_stat = private_path.lstat()
if not stat.S_ISDIR(private_stat.st_mode) or stat.S_ISLNK(private_stat.st_mode):
    raise SystemExit(1)
private_dir = private_path.resolve(strict=True)
mode = sys.argv[4]
owner_uid = int(sys.argv[5])
owner_gid = int(sys.argv[6])
runtime_gid = int(sys.argv[7])
quiescence_proven = sys.argv[8] == '1'
fault_point = sys.argv[9]
if mode not in {'rollback', 'committed', 'pre-mutation'}:
    raise SystemExit(1)
if plan_path.parent.resolve(strict=True) != transaction:
    raise SystemExit(1)
plan_stat = plan_path.lstat()
if (
    not stat.S_ISREG(plan_stat.st_mode)
    or stat.S_ISLNK(plan_stat.st_mode)
    or stat.S_IMODE(plan_stat.st_mode) != 0o600
    or plan_stat.st_uid != owner_uid
    or plan_stat.st_gid != owner_gid
    or plan_stat.st_nlink != 1
):
    raise SystemExit(1)
plan = json.loads(plan_path.read_text(encoding='utf-8'))
required = {
    'version', 'release_id', 'action', 'pending_path', 'final_path', 'sha256',
    'pending_device', 'pending_inode', 'pending_nlink', 'pending_size',
    'final_device_before', 'final_inode_before', 'final_nlink_before',
    'private_device_before', 'private_inode_before', 'private_uid_before',
    'private_gid_before', 'private_mode_before',
}
if set(plan) != required or plan['version'] != 2 or plan['action'] not in {'create', 'reuse'}:
    raise SystemExit(1)
expected_digest = plan['sha256']


def plain_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


if (
    not isinstance(expected_digest, str)
    or len(expected_digest) != 64
    or not all(character in '0123456789abcdef' for character in expected_digest)
    or not plain_int(plan['pending_device'])
    or not plain_int(plan['pending_inode'])
    or plan['pending_nlink'] != 1
    or not plain_int(plan['pending_size'])
    or plan['pending_size'] <= 0
    or not plain_int(plan['private_device_before'])
    or not plain_int(plan['private_inode_before'])
    or not plain_int(plan['private_uid_before'])
    or not plain_int(plan['private_gid_before'])
    or plan['private_mode_before'] not in {0o700, 0o750}
):
    raise SystemExit(1)
if plan['action'] == 'create':
    if (
        plan['final_device_before'] is not None
        or plan['final_inode_before'] is not None
        or plan['final_nlink_before'] is not None
    ):
        raise SystemExit(1)
else:
    if (
        not plain_int(plan['final_device_before'])
        or not plain_int(plan['final_inode_before'])
        or plan['final_nlink_before'] != 1
    ):
        raise SystemExit(1)
if (private_stat.st_dev, private_stat.st_ino) != (
    plan['private_device_before'],
    plan['private_inode_before'],
):
    raise SystemExit(1)
private_before = (
    plan['private_uid_before'],
    plan['private_gid_before'],
    plan['private_mode_before'],
)
private_now = (
    private_stat.st_uid,
    private_stat.st_gid,
    stat.S_IMODE(private_stat.st_mode),
)
private_promoted = (owner_uid, runtime_gid, 0o750)
if mode == 'committed':
    expected_private_states = {private_promoted} if plan['action'] == 'create' else {private_before}
elif mode == 'pre-mutation' or plan['action'] == 'reuse':
    expected_private_states = {private_before}
else:
    # create 可能在 chown 与 chmod 两条命令之间被 SIGKILL。
    expected_private_states = {
        private_before,
        (owner_uid, runtime_gid, plan['private_mode_before']),
        private_promoted,
    }
if private_now not in expected_private_states:
    raise SystemExit(1)
pending = Path(plan['pending_path'])
final = Path(plan['final_path'])
if (
    pending.parent.resolve(strict=True) != private_dir
    or final.parent.resolve(strict=True) != private_dir
    or pending == final
):
    raise SystemExit(1)


def read_file(path):
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0),
    )
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 0:
            raise SystemExit(1)
        payload = b''
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            payload += chunk
            if len(payload) > 16 * 1024:
                raise SystemExit(1)
        after = os.fstat(descriptor)
        if (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_mode,
            file_stat.st_uid,
            file_stat.st_gid,
            file_stat.st_nlink,
            file_stat.st_size,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
            after.st_size,
        ):
            raise SystemExit(1)
        return hashlib.sha256(payload).hexdigest(), file_stat
    finally:
        os.close(descriptor)


pending_identity = (plan['pending_device'], plan['pending_inode'])
final_identity = (
    pending_identity
    if plan['action'] == 'create'
    else (plan['final_device_before'], plan['final_inode_before'])
)


def require_final():
    digest, file_stat = read_file(final)
    if (
        digest != expected_digest
        or stat.S_IMODE(file_stat.st_mode) != 0o640
        or file_stat.st_uid != owner_uid
        or file_stat.st_gid != runtime_gid
        or file_stat.st_nlink != 1
        or (file_stat.st_dev, file_stat.st_ino) != final_identity
    ):
        raise SystemExit(1)


def exists(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


archive = transaction / 'qweather-key-recovery'
if mode == 'committed':
    require_final()
    if exists(pending):
        # 允许人工确认前由部署器重新 provision 同一把私钥，但它必须是独立、root-only 的新对象。
        pending_digest, pending_stat = read_file(pending)
        if (
            pending_digest != expected_digest
            or pending_stat.st_size != plan['pending_size']
            or stat.S_IMODE(pending_stat.st_mode) != 0o600
            or pending_stat.st_uid != owner_uid
            or pending_stat.st_gid != owner_gid
            or pending_stat.st_nlink != 1
            or (pending_stat.st_dev, pending_stat.st_ino) == final_identity
        ):
            raise SystemExit(1)
else:
    if archive.exists() and (archive.is_symlink() or not archive.is_dir()):
        raise SystemExit(1)
    archive.mkdir(mode=0o700, exist_ok=True)
    os.chown(archive, owner_uid, owner_gid)
    os.chmod(archive, 0o700)

    def inspect_controlled(path, expected_identity, allowed_nlinks):
        digest, file_stat = read_file(path)
        if (
            digest != expected_digest
            or (file_stat.st_dev, file_stat.st_ino) != expected_identity
            or file_stat.st_nlink not in allowed_nlinks
        ):
            raise SystemExit(1)
        if path == final and stat.S_IMODE(file_stat.st_mode) & 0o040 and not quiescence_proven:
            raise SystemExit(1)
        return file_stat

    def secure_archive(path, name, expected_identity):
        if not exists(path):
            return
        inspect_controlled(path, expected_identity, {1})
        destination = archive / name
        if destination.exists() or destination.is_symlink():
            raise SystemExit(1)
        os.chown(path, owner_uid, owner_gid)
        os.chmod(path, 0o600)
        os.replace(path, destination)

    if mode == 'pre-mutation':
        if plan['action'] == 'create' and exists(final):
            # mutation 前从未创建 final；出现任何对象都属于并发或身份歧义，禁止删除。
            raise SystemExit(1)
        if plan['action'] == 'reuse':
            require_final()
        secure_archive(pending, 'pending.pem', pending_identity)
        if exists(pending):
            raise SystemExit(1)
    elif plan['action'] == 'create':
        final_exists = exists(final)
        pending_exists = exists(pending)
        if final_exists and pending_exists:
            final_stat = inspect_controlled(final, final_identity, {2})
            pending_stat = inspect_controlled(pending, pending_identity, {2})
            if (final_stat.st_dev, final_stat.st_ino) != (
                pending_stat.st_dev,
                pending_stat.st_ino,
            ):
                raise SystemExit(1)
            # 两个名称确认属于本计划的同一 inode 后，先解除 final 别名，再归档唯一剩余名称。
            final.unlink()
            directory = os.open(private_dir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            secure_archive(pending, 'pending.pem', pending_identity)
        elif final_exists:
            secure_archive(final, 'final-created.pem', final_identity)
        elif pending_exists:
            secure_archive(pending, 'pending.pem', pending_identity)
        if exists(final) or exists(pending):
            raise SystemExit(1)
    else:
        require_final()
        secure_archive(pending, 'pending.pem', pending_identity)
        if exists(pending):
            raise SystemExit(1)
        require_final()

    expected_children = {
        'pending.pem': pending_identity,
        **({'final-created.pem': final_identity} if plan['action'] == 'create' else {}),
    }
    for child in archive.iterdir():
        child_stat = child.lstat()
        child_digest, stable_child_stat = read_file(child)
        if (
            child.name not in expected_children
            or not stat.S_ISREG(child_stat.st_mode)
            or stat.S_ISLNK(child_stat.st_mode)
            or stat.S_IMODE(child_stat.st_mode) != 0o600
            or child_stat.st_uid != owner_uid
            or child_stat.st_gid != owner_gid
            or child_stat.st_nlink != 1
            or child_digest != expected_digest
            or (stable_child_stat.st_dev, stable_child_stat.st_ino)
            != expected_children[child.name]
        ):
            raise SystemExit(1)
        descriptor = os.open(
            child,
            os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    if mode == 'rollback' and plan['action'] == 'create':
        # 先撤销 group traverse，再恢复属组；SIGKILL 中间态仍属于允许的安全集合。
        os.chmod(private_dir, plan['private_mode_before'])
        descriptor = os.open(private_dir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if fault_point == 'during-directory-restore':
            raise SystemExit(1)
        os.chown(private_dir, plan['private_uid_before'], plan['private_gid_before'])
        restored = private_dir.lstat()
        if (
            (restored.st_dev, restored.st_ino)
            != (plan['private_device_before'], plan['private_inode_before'])
            or (
                restored.st_uid,
                restored.st_gid,
                stat.S_IMODE(restored.st_mode),
            )
            != private_before
        ):
            raise SystemExit(1)

for directory_path in {private_dir, transaction, archive if archive.exists() else transaction}:
    descriptor = os.open(directory_path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
    then
        fail "QWeather 私钥转换计划的 $mode 状态校验或回收失败"
        return 1
    fi
}

recover_qweather_key_for_rollback() {
    [ "$QWEATHER_KEY_TRANSITION_REQUIRED" -eq 1 ] || return 0
    if ! reconcile_qweather_key_plan "$TRANSACTION_DIR" rollback; then
        return 1
    fi
    if [ ! -e "$QWEATHER_KEY_RECOVERED_MARKER" ]; then
        write_durable_marker "$QWEATHER_KEY_RECOVERED_MARKER" rollback
    fi
    log "已在恢复旧单元前收回本轮 QWeather 私钥"
}

recover_qweather_key_before_mutation() {
    [ "$QWEATHER_KEY_TRANSITION_REQUIRED" -eq 1 ] || return 0
    if ! reconcile_qweather_key_plan "$TRANSACTION_DIR" pre-mutation; then
        return 1
    fi
    if [ ! -e "$QWEATHER_KEY_RECOVERED_MARKER" ] \
        && [ ! -L "$QWEATHER_KEY_RECOVERED_MARKER" ]; then
        write_durable_marker "$QWEATHER_KEY_RECOVERED_MARKER" pre-mutation
    fi
    log "已收回尚未进入生产变更阶段的 QWeather pending 私钥"
}

qweather_ack_recovery_needs_quiescence() {
    local transaction="$1" plan="$transaction/qweather-key-transition.json" state runtime_group_gid
    runtime_group_gid="$(id -g "$RUNTIME_USER")"
    if ! state="$($VENV_DIR/bin/python - \
        "$plan" \
        "$transaction" \
        "$QWEATHER_PRIVATE_DIR" \
        "$CONTROL_OWNER_UID" \
        "$CONTROL_OWNER_GID" \
        "$runtime_group_gid" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

plan_path = Path(sys.argv[1])
transaction = Path(sys.argv[2]).resolve(strict=True)
private_path = Path(sys.argv[3])
private_stat = private_path.lstat()
if not stat.S_ISDIR(private_stat.st_mode) or stat.S_ISLNK(private_stat.st_mode):
    raise SystemExit(1)
private_dir = private_path.resolve(strict=True)
owner_uid = int(sys.argv[4])
owner_gid = int(sys.argv[5])
runtime_gid = int(sys.argv[6])
if plan_path.parent.resolve(strict=True) != transaction:
    raise SystemExit(1)
plan_stat = plan_path.lstat()
if (
    not stat.S_ISREG(plan_stat.st_mode)
    or stat.S_ISLNK(plan_stat.st_mode)
    or stat.S_IMODE(plan_stat.st_mode) != 0o600
    or plan_stat.st_uid != owner_uid
    or plan_stat.st_gid != owner_gid
    or plan_stat.st_nlink != 1
):
    raise SystemExit(1)
plan = json.loads(plan_path.read_text(encoding='utf-8'))
required = {
    'version', 'release_id', 'action', 'pending_path', 'final_path', 'sha256',
    'pending_device', 'pending_inode', 'pending_nlink', 'pending_size',
    'final_device_before', 'final_inode_before', 'final_nlink_before',
    'private_device_before', 'private_inode_before', 'private_uid_before',
    'private_gid_before', 'private_mode_before',
}
if set(plan) != required or plan['version'] != 2 or plan['action'] not in {'create', 'reuse'}:
    raise SystemExit(1)
if (
    not isinstance(plan['pending_device'], int)
    or isinstance(plan['pending_device'], bool)
    or not isinstance(plan['pending_inode'], int)
    or isinstance(plan['pending_inode'], bool)
    or plan['pending_nlink'] != 1
    or not isinstance(plan['sha256'], str)
    or len(plan['sha256']) != 64
    or not all(character in '0123456789abcdef' for character in plan['sha256'])
):
    raise SystemExit(1)
if (private_stat.st_dev, private_stat.st_ino) != (
    plan['private_device_before'],
    plan['private_inode_before'],
):
    raise SystemExit(1)
final = Path(plan['final_path'])
if final.parent.resolve(strict=True) != private_dir:
    raise SystemExit(1)
try:
    descriptor = os.open(
        final,
        os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0),
    )
except FileNotFoundError:
    print('safe' if plan['action'] == 'create' else 'quiesce')
    raise SystemExit(0)
except OSError:
    print('quiesce')
    raise SystemExit(0)
try:
    before = os.fstat(descriptor)
    payload = b''
    while True:
        chunk = os.read(descriptor, 4096)
        if not chunk:
            break
        payload += chunk
        if len(payload) > 16 * 1024:
            print('quiesce')
            raise SystemExit(0)
    after = os.fstat(descriptor)
finally:
    os.close(descriptor)
expected_identity = (
    (plan['pending_device'], plan['pending_inode'])
    if plan['action'] == 'create'
    else (plan['final_device_before'], plan['final_inode_before'])
)
stable = (
    stat.S_ISREG(before.st_mode)
    and before.st_nlink in {1, 2}
    and (before.st_dev, before.st_ino, before.st_mode, before.st_nlink, before.st_size)
    == (after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size)
    and (before.st_dev, before.st_ino) == expected_identity
    and hashlib.sha256(payload).hexdigest() == plan['sha256']
)
if not stable:
    print('quiesce')
elif plan['action'] == 'create':
    print(
        'safe'
        if stat.S_IMODE(before.st_mode) == 0o600
        and before.st_uid == owner_uid
        and before.st_gid == owner_gid
        and before.st_nlink in {1, 2}
        else 'quiesce'
    )
else:
    print(
        'safe'
        if stat.S_IMODE(before.st_mode) == 0o640
        and before.st_uid == owner_uid
        and before.st_gid == runtime_gid
        and before.st_nlink == 1
        else 'quiesce'
    )
PY
    )"; then
        fail "无法判定已确认事务的 QWeather 私钥静默要求"
        return 2
    fi
    case "$state" in
        quiesce) return 0 ;;
        safe) return 1 ;;
        *) fail "QWeather 私钥静默要求判定异常"; return 2 ;;
    esac
}

reconcile_acknowledged_qweather_key_plan() {
    local transaction="$1" mode=rollback marker_state=0 quiesce_state=0
    [ -e "$transaction/qweather-key-transition.json" ] \
        || [ -L "$transaction/qweather-key-transition.json" ] \
        || return 0
    transaction_requires_forward_only "$transaction" || marker_state=$?
    if [ "$marker_state" -eq 2 ]; then
        # 阶段方向不可信时保持最保守状态，先停入口并拒绝触碰任何私钥对象。
        stop_units_best_effort >/dev/null 2>&1 || true
        revoke_or_quarantine_runtime_activation_permit "$transaction" || true
        return 1
    fi
    if [ "$marker_state" -eq 0 ] \
        || [ -f "$transaction/COMMITTED" ] \
        || [ -f "$transaction/POST_COMMIT_ATTENTION.txt" ]; then
        mode=committed
    elif [ ! -f "$transaction/ACTIVATION_STARTED" ] \
        || [ -L "$transaction/ACTIVATION_STARTED" ]; then
        mode=pre-mutation
    fi
    if [ "$mode" = rollback ]; then
        qweather_ack_recovery_needs_quiescence "$transaction" || quiesce_state=$?
        case "$quiesce_state" in
            0)
                if ! stop_units_best_effort; then
                    revoke_or_quarantine_runtime_activation_permit "$transaction" || true
                    fail "已确认事务无法证明业务单元和运行 UID 完全静默"
                    return 1
                fi
                ;;
            1) ;;
            *)
                stop_units_best_effort >/dev/null 2>&1 || true
                revoke_or_quarantine_runtime_activation_permit "$transaction" || true
                return 1
                ;;
        esac
    fi
    if ! reconcile_qweather_key_plan "$transaction" "$mode"; then
        if [ "$mode" != pre-mutation ]; then
            stop_units_best_effort >/dev/null 2>&1 || true
            revoke_or_quarantine_runtime_activation_permit "$transaction" || true
        fi
        return 1
    fi
}

reconcile_acknowledged_current_release_ledger() {
    local transaction="$1" marker_state=0 target marker
    transaction_requires_forward_only "$transaction" || marker_state=$?
    case "$marker_state" in
        0) ;;
        1) return 0 ;;
        *) fail "已确认事务的 forward-only 阶段证据无效"; return 1 ;;
    esac

    if ! target="$($VENV_DIR/bin/python - \
        "$transaction" \
        "$CURRENT_LINK" \
        "$RELEASE_ROOT" \
        "$CONTROL_OWNER_UID" \
        "$CONTROL_OWNER_GID" <<'PY'
from pathlib import Path
import stat
import sys

transaction = Path(sys.argv[1]).resolve(strict=True)
current_link = Path(sys.argv[2])
release_root = Path(sys.argv[3]).resolve(strict=True)
owner_uid = int(sys.argv[4])
owner_gid = int(sys.argv[5])
activation = transaction / 'ACTIVATION_STARTED'
activation_stat = activation.lstat()
if (
    not stat.S_ISREG(activation_stat.st_mode)
    or stat.S_ISLNK(activation_stat.st_mode)
    or activation_stat.st_uid != owner_uid
    or activation_stat.st_gid != owner_gid
    or stat.S_IMODE(activation_stat.st_mode) != 0o600
):
    raise SystemExit(1)
lines = activation.read_text(encoding='utf-8').splitlines()
if len(lines) != 1 or not lines[0] or '\x00' in lines[0]:
    raise SystemExit(1)
target = Path(lines[0])
release_directory = (release_root / 'releases').resolve(strict=True)
if (
    not target.is_absolute()
    or str(target) != str(target.resolve(strict=True))
    or target.parent.resolve(strict=True) != release_directory
    or not target.is_dir()
    or target.is_symlink()
    or not current_link.is_symlink()
    or current_link.resolve(strict=True) != target.resolve(strict=True)
):
    raise SystemExit(1)
print(target)
PY
    )"; then
        fail "已确认 forward-only 事务与当前代码入口不匹配"
        return 1
    fi

    write_current_release_ledger "$target"
    marker="$transaction/CURRENT_RELEASE_RECONCILED"
    if [ -e "$marker" ] || [ -L "$marker" ]; then
        if ! "$VENV_DIR/bin/python" - \
            "$marker" \
            "$target" \
            "$CONTROL_OWNER_UID" \
            "$CONTROL_OWNER_GID" <<'PY'
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
expected = f'release={sys.argv[2]}\n'
file_stat = path.lstat()
if (
    not stat.S_ISREG(file_stat.st_mode)
    or stat.S_ISLNK(file_stat.st_mode)
    or file_stat.st_uid != int(sys.argv[3])
    or file_stat.st_gid != int(sys.argv[4])
    or stat.S_IMODE(file_stat.st_mode) != 0o600
    or path.read_text(encoding='utf-8') != expected
):
    raise SystemExit(1)
PY
        then
            fail "历史 current-release 对账标记无效"
            return 1
        fi
    else
        write_durable_marker "$marker" "release=$target"
    fi
    log "已将明确确认的 forward-only 版本与 current-release 账本对齐"
}

stop_units_strictly() {
    local unit
    RUNTIME_KEY_QUIESCENCE_PROVEN=0
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
    RUNTIME_KEY_QUIESCENCE_PROVEN=1
}

stop_units_best_effort() {
    local failed=0
    local unit
    RUNTIME_KEY_QUIESCENCE_PROVEN=0
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
    verify_no_unmanaged_processes_after_quiesce || failed=1
    if [ "$failed" -eq 0 ]; then
        RUNTIME_KEY_QUIESCENCE_PROVEN=1
    fi
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

install_activation_guard_dropins() {
    local unit directory dropin temporary expected
    expected="$TRANSACTION_DIR/activation-guard.expected"
    {
        printf '%s\n' '[Unit]'
        printf 'ConditionPathExists=|!%s\n' "$ACTIVATION_BOOT_GUARD_FILE"
        printf 'ConditionPathExists=|%s\n' "$RUNTIME_BOOT_GUARD_FILE"
    } > "$expected"
    chmod 0600 "$expected"

    # 所有精确临时路径必须在任何目录权限修改前确认不存在；事务快照已
    # 耐久记录同一固定文件名，崩溃恢复时只会处理本轮可证明的临时文件。
    for unit in "${ALL_UNITS[@]}"; do
        directory="$UNIT_DIR/$unit.d"
        temporary="$directory/.$ACTIVATION_GUARD_DROPIN_NAME.next"
        if [ -L "$directory" ]; then
            fail "systemd drop-in 目录不得为符号链接: $directory"
            return 1
        fi
        if [ -e "$temporary" ] || [ -L "$temporary" ]; then
            fail "systemd drop-in 临时路径已被占用: $temporary"
            return 1
        fi
    done

    ACTIVATION_GUARD_DROPINS_MUTATED=1
    for unit in "${ALL_UNITS[@]}"; do
        directory="$UNIT_DIR/$unit.d"
        dropin="$directory/$ACTIVATION_GUARD_DROPIN_NAME"
        mkdir -p "$directory"
        "$CHOWN_BIN" "$CONTROL_OWNER_UID:$CONTROL_OWNER_GID" "$directory"
        chmod 0755 "$directory"
        temporary="$directory/.$ACTIVATION_GUARD_DROPIN_NAME.next"
        "$VENV_DIR/bin/python" - \
            "$expected" \
            "$directory" \
            "$(basename "$temporary")" \
            "$UNIT_DIR" \
            "$unit" \
            "$CONTROL_OWNER_UID" \
            "$CONTROL_OWNER_GID" \
            "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" <<'PY'
import os
from pathlib import Path
import stat
import sys

expected = Path(sys.argv[1])
directory = Path(sys.argv[2])
temporary_name = sys.argv[3]
unit_root = Path(sys.argv[4]).resolve(strict=True)
unit = sys.argv[5]
expected_uid = int(sys.argv[6])
expected_gid = int(sys.argv[7])
test_mode = sys.argv[8]

directory_stat = directory.lstat()
listxattr = getattr(os, 'listxattr', None)
if listxattr is None:
    if test_mode != '1' or os.geteuid() == 0:
        raise SystemExit(1)
    extended_attributes = []
else:
    try:
        extended_attributes = listxattr(directory, follow_symlinks=False)
    except OSError:
        raise SystemExit(1) from None
if (
    not stat.S_ISDIR(directory_stat.st_mode)
    or stat.S_ISLNK(directory_stat.st_mode)
    or directory.parent.resolve(strict=True) != unit_root
    or directory.name != f'{unit}.d'
    or directory_stat.st_uid != expected_uid
    or directory_stat.st_gid != expected_gid
    or stat.S_IMODE(directory_stat.st_mode) != 0o755
    or any(
        name.startswith('system.posix_acl_')
        for name in extended_attributes
    )
    or temporary_name != '.10-case-weather-activation-guard.conf.next'
):
    raise SystemExit(1)

open_flags = (
    os.O_RDONLY
    | getattr(os, 'O_CLOEXEC', 0)
    | getattr(os, 'O_NOFOLLOW', 0)
)
expected_fd = os.open(expected, open_flags)
try:
    expected_stat = os.fstat(expected_fd)
    if not stat.S_ISREG(expected_stat.st_mode):
        raise SystemExit(1)
    chunks = []
    while True:
        chunk = os.read(expected_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    expected_bytes = b''.join(chunks)
finally:
    os.close(expected_fd)

directory_fd = os.open(
    directory,
    os.O_RDONLY
    | getattr(os, 'O_DIRECTORY', 0)
    | getattr(os, 'O_CLOEXEC', 0)
    | getattr(os, 'O_NOFOLLOW', 0),
)
temporary_fd = None
created = False
try:
    try:
        os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise SystemExit(1)
    temporary_fd = os.open(
        temporary_name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, 'O_CLOEXEC', 0)
        | getattr(os, 'O_NOFOLLOW', 0),
        0o600,
        dir_fd=directory_fd,
    )
    created = True
    view = memoryview(expected_bytes)
    while view:
        written = os.write(temporary_fd, view)
        if written <= 0:
            raise OSError('short write')
        view = view[written:]
    temporary_stat = os.fstat(temporary_fd)
    if (
        temporary_stat.st_uid != expected_uid
        or temporary_stat.st_gid != expected_gid
    ):
        os.fchown(temporary_fd, expected_uid, expected_gid)
    os.fchmod(temporary_fd, 0o644)
    os.fsync(temporary_fd)
except BaseException:
    if temporary_fd is not None:
        os.close(temporary_fd)
        temporary_fd = None
    if created:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError:
            pass
    raise
finally:
    if temporary_fd is not None:
        os.close(temporary_fd)
    os.close(directory_fd)
PY
        mv -f "$temporary" "$dropin"
        fsync_directory "$directory"
    done
    "$SYSTEMCTL_BIN" daemon-reload
    fsync_directory "$UNIT_DIR"
    verify_activation_guard_dropins
    log "systemd 断电保护 drop-in 已在激活事务内安装"
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

quarantine_runtime_activation_permit() {
    local expected_transaction="$1" evidence marker
    [ -e "$RUNTIME_BOOT_GUARD_FILE" ] \
        || [ -L "$RUNTIME_BOOT_GUARD_FILE" ] \
        || return 0
    evidence="$RUNTIME_BOOT_GUARD_DIR/activation-permit.quarantined.$$"
    if ! "$VENV_DIR/bin/python" - \
        "$RUNTIME_BOOT_GUARD_FILE" \
        "$evidence" \
        "$RUNTIME_BOOT_GUARD_DIR" <<'PY'
import os
from pathlib import Path
import stat
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
runtime_dir = Path(sys.argv[3])
directory_stat = runtime_dir.lstat()
if (
    not stat.S_ISDIR(directory_stat.st_mode)
    or stat.S_ISLNK(directory_stat.st_mode)
    or source.parent != runtime_dir
    or destination.parent != runtime_dir
    or destination.exists()
    or destination.is_symlink()
):
    raise SystemExit(1)
source.lstat()
os.rename(source, destination)
descriptor = os.open(runtime_dir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
    then
        fail "运行期开机许可无法从生效路径安全隔离"
        return 1
    fi
    marker="$expected_transaction/RUNTIME_PERMIT_QUARANTINED.$$"
    write_durable_marker \
        "$marker" \
        "$(printf 'active_path=%s\nevidence_path=%s' \
            "$RUNTIME_BOOT_GUARD_FILE" \
            "$evidence")"
    log "已将无效或错配的运行期开机许可移出生效路径: $evidence" >&2
}

revoke_or_quarantine_runtime_activation_permit() {
    local expected_transaction="$1"
    if revoke_runtime_activation_permit "$expected_transaction"; then
        return 0
    fi
    quarantine_runtime_activation_permit "$expected_transaction"
}

verify_no_retired_processes() {
    local pattern rc
    for pattern in \
        "$STATE_DIR/backup.sh" \
        "$STATE_DIR/services/pipelines/sync_weather_data.py" \
        "services.pipelines.sync_weather_cache" \
        "$CURRENT_LINK/app/scripts/weather_cache_sync.sh" \
        "$CURRENT_LINK/app/services/pipelines/sync_weather_cache.py"; do
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

verify_weather_sync_lock_quiescent() {
    local lock_path="$STATE_DIR/run/case-weather-sync.lock"
    if [ -L "$lock_path" ]; then
        fail "天气同步锁不得为符号链接"
        return 1
    fi
    [ -e "$lock_path" ] || return 0
    if [ ! -f "$lock_path" ]; then
        fail "天气同步锁不是常规文件"
        return 1
    fi
    exec 8<> "$lock_path" || {
        fail "无法打开天气同步锁进行静默检查"
        return 1
    }
    if ! "$FLOCK_BIN" -n 8; then
        exec 8>&-
        fail "正式天气请求前仍有同步周期持有同机锁"
        return 1
    fi
    if ! "$FLOCK_BIN" -u 8; then
        exec 8>&-
        fail "天气同步静默检查后无法释放锁"
        return 1
    fi
    exec 8>&-
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
    # argv 模式只能发现已知旧进程。安全边界必须枚举运行 UID，阻止改名或逃逸进程读取新私钥。
    if "$PGREP_BIN" -u "$RUNTIME_USER" >/dev/null 2>&1; then
        fail "运行账户仍有未归属进程，拒绝授予或回收运行组可读私钥"
        return 1
    else
        rc=$?
        if [ "$rc" -ne 1 ]; then
            fail "无法完整枚举运行账户进程，拒绝继续私钥转换"
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
    required_paths = [env_file, backup_env, database, current_release]
    for path in required_paths:
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
            raise SystemExit(1)
    if not current_link.is_symlink() or current_link.resolve(strict=True) != new_release.resolve(strict=True):
        raise SystemExit(1)
    if current_release.read_text(encoding='utf-8').strip() != str(new_release):
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
    local metadata
    if [ ! -f "$ENV_FILE" ]; then
        ENV_EXISTED=0
        return
    fi
    if [ -L "$ENV_FILE" ]; then
        fail "活动环境文件不得为符号链接"
        return 1
    fi
    metadata="$("$VENV_DIR/bin/python" - "$ENV_FILE" <<'PY'
from pathlib import Path
import stat
import sys

file_stat = Path(sys.argv[1]).lstat()
if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
    raise SystemExit(1)
print(file_stat.st_uid, file_stat.st_gid, f'{stat.S_IMODE(file_stat.st_mode):o}')
PY
)"
    if [[ ! "$metadata" =~ ^[0-9]+\ [0-9]+\ (600|640)$ ]]; then
        fail "活动环境文件权限不安全，拒绝在事务外修正"
        return 1
    fi
    printf '%s\n' "$metadata" > "$ENV_METADATA"
    chmod 0600 "$ENV_METADATA"
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

write_current_release_ledger() {
    local target="$1"
    local ledger="$STATE_DIR/deployments/current-release"
    local temporary="$ledger.next.$$"
    local current_target=""
    case "$target" in
        "$RELEASE_ROOT"/releases/*) ;;
        *) fail "current-release 目标不在受控发布目录: $target"; return 1 ;;
    esac
    if [ ! -d "$target" ] || [ -L "$target" ]; then
        fail "current-release 目标不是普通发布目录: $target"
        return 1
    fi
    if [ ! -L "$CURRENT_LINK" ] \
        || ! current_target="$(readlink "$CURRENT_LINK")" \
        || [ "$current_target" != "$target" ]; then
        fail "current-release 写入前 current 链接与目标版本不一致"
        return 1
    fi
    if [ -L "$ledger" ] || [ -e "$temporary" ] || [ -L "$temporary" ]; then
        fail "current-release 账本或临时路径状态异常"
        return 1
    fi
    printf '%s\n' "$target" > "$temporary"
    chmod 0600 "$temporary"
    atomic_replace "$temporary" "$ledger"
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

validate_candidate_ml_contract() {
    local base_url="http://$CANDIDATE_BIND"
    local ml_body

    ml_body="$(
        "$CURL_BIN" --fail --silent --show-error --max-time 5 \
            "$base_url/api/ml/status" 2>/dev/null
    )" || {
        fail "候选应用 ML 状态接口不可用"
        return 1
    }
    if ! printf '%s' "$ml_body" | "$VENV_DIR/bin/python" -c '
import json
import sys

payload = json.load(sys.stdin)
status = payload.get("status") if isinstance(payload, dict) else None
valid = (
    payload.get("success") is True
    and isinstance(status, dict)
    and status.get("model_loaded") is True
    and status.get("runtime_sklearn_version") == "1.7.2"
    and status.get("expected_sklearn_version") == "1.7.2"
    and status.get("sklearn_compatible") is True
)
raise SystemExit(0 if valid else 1)
'; then
        fail "候选应用 ML 运行态版本或模型状态异常"
        return 1
    fi
}

validate_candidate_weather_contracts() {
    local base_url="http://$CANDIDATE_BIND"
    local bootstrap_body risk_body

    bootstrap_body="$(
        "$CURL_BIN" --fail --silent --show-error --max-time 5 \
            "$base_url/mp/api/v1/bootstrap" 2>/dev/null
    )" || {
        fail "候选应用小程序天气快照接口不可用"
        return 1
    }
    if ! printf '%s' "$bootstrap_body" | "$VENV_DIR/bin/python" -c '
import json
import sys

payload = json.load(sys.stdin)
data = payload.get("data") if isinstance(payload, dict) else None
current = data.get("current") if isinstance(data, dict) else None
risk = data.get("risk") if isinstance(data, dict) else None
source_status = data.get("source_status") if isinstance(data, dict) else None
weather_status = (
    source_status.get("weather") if isinstance(source_status, dict) else None
)
provider = ""
if isinstance(weather_status, dict):
    provider = str(weather_status.get("provider") or "").strip()
if not provider and isinstance(current, dict):
    provider = str(
        current.get("data_source") or current.get("source") or ""
    ).strip()
score = risk.get("score") if isinstance(risk, dict) else None
summary = str(risk.get("summary") or "") if isinstance(risk, dict) else ""
pending_markers = ("待刷新", "尚未生成", "正在更新")
valid = (
    payload.get("success") is True
    and isinstance(data, dict)
    and data.get("available") is True
    and data.get("stale") is False
    and bool(str(data.get("snapshot_id") or "").strip())
    and provider == "QWeather"
    and isinstance(score, (int, float))
    and not isinstance(score, bool)
    and bool(summary.strip())
    and not any(marker in summary for marker in pending_markers)
)
raise SystemExit(0 if valid else 1)
'; then
        fail "候选应用天气快照仍处于缺失、陈旧或待刷新状态"
        return 1
    fi

    risk_body="$(
        "$CURL_BIN" --fail --silent --show-error --max-time 5 \
            "$base_url/risk" 2>/dev/null
    )" || {
        fail "候选应用公开风险页不可用"
        return 1
    }
    if printf '%s' "$risk_body" | grep -Eq '天气更新中|风险待刷新'; then
        fail "候选应用公开风险页仍显示待刷新状态"
        return 1
    fi
    if ! printf '%s' "$risk_body" | grep -Fq '当前风险：'; then
        fail "候选应用公开风险页缺少已生成的风险结果"
        return 1
    fi
}

stop_candidate_release() {
    if [ -n "$CANDIDATE_PID" ]; then
        kill "$CANDIDATE_PID" >/dev/null 2>&1 || true
        wait "$CANDIDATE_PID" >/dev/null 2>&1 || true
        CANDIDATE_PID=""
    fi
}

start_candidate_release() {
    local contract_phase="${1:-base}"
    case "$contract_phase" in
        base)
            log "在仅本机可访问的端口验证候选版本基础运行态"
            ;;
        weather)
            log "在正式天气烟测后验证候选版本天气与风险展示"
            ;;
        *)
            fail "候选版本验证阶段无效: $contract_phase"
            return 2
            ;;
    esac
    (
        cd "$APP_DIR"
        runtime_exec "$VENV_DIR/bin/python" -m gunicorn \
            --workers 1 \
            --bind "$CANDIDATE_BIND" \
            --timeout 60 \
            app:app
    ) > "$TRANSACTION_DIR/candidate-gunicorn.log" 2>&1 &
    CANDIDATE_PID=$!
    wait_for_health "$CANDIDATE_HEALTH_URL" "$CANDIDATE_PID"
    case "$contract_phase" in
        base) validate_candidate_ml_contract ;;
        weather) validate_candidate_weather_contracts ;;
    esac
    stop_candidate_release
}

validate_release_dependencies() {
    local actual_lock_sha recorded_lock_sha
    require_file "$APP_DIR/requirements.lock"
    require_file "$NEW_RELEASE/private-metadata/requirements-lock.sha256"
    require_file "$NEW_RELEASE/private-metadata/python-version.txt"
    require_file "$NEW_RELEASE/private-metadata/pip-inspect.json"
    require_file "$DEPENDENCY_RECEIPT_FILE"
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
    if ! "$VENV_DIR/bin/python" - \
        "$APP_DIR/requirements.lock" \
        "$NEW_RELEASE/private-metadata/requirements-lock.sha256" \
        "$NEW_RELEASE/private-metadata/python-version.txt" \
        "$NEW_RELEASE/private-metadata/pip-inspect.json" \
        "$DEPENDENCY_RECEIPT_FILE" \
        "$EXPECTED_REQUIREMENTS_LOCK_SHA256" \
        "$CONTROL_OWNER_UID" \
        "$CONTROL_OWNER_GID" <<'PY'
import hashlib
import json
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys


def reject(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


lock_file, lock_receipt, python_receipt, inspect_receipt, dependency_receipt = map(
    Path, sys.argv[1:6]
)
expected_sha, expected_uid, expected_gid = (
    sys.argv[6],
    int(sys.argv[7]),
    int(sys.argv[8]),
)
for private_file in (
    lock_receipt,
    python_receipt,
    inspect_receipt,
    dependency_receipt,
):
    info = private_file.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or (info.st_uid, info.st_gid) != (expected_uid, expected_gid)
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        reject("依赖私有收据类型、硬链接、所有者或权限异常")

def canonical_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def locked_packages():
    packages = {}
    pattern = re.compile(
        r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?==([^\s\\;]+)"
    )
    for line in lock_file.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or line[0].isspace():
            continue
        match = pattern.match(line)
        if match is None:
            reject("requirements.lock 含无法核对的顶层条目")
        name = canonical_name(match.group(1))
        if name in packages:
            reject("requirements.lock 含重复包")
        packages[name] = match.group(2)
    return packages


def inspected_packages(payload):
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, list):
        reject("pip inspect 收据结构异常")
    packages = {}
    for item in installed:
        metadata = item.get("metadata") if isinstance(item, dict) else None
        name = metadata.get("name") if isinstance(metadata, dict) else None
        version = metadata.get("version") if isinstance(metadata, dict) else None
        if not isinstance(name, str) or not isinstance(version, str):
            reject("pip inspect 包名或版本异常")
        name = canonical_name(name)
        if name in packages:
            reject("pip inspect 含重复包")
        packages[name] = version
    return packages


inspect_bytes = inspect_receipt.read_bytes()
try:
    recorded_inspect = json.loads(inspect_bytes)
    receipt = json.loads(dependency_receipt.read_text(encoding="utf-8"))
except (UnicodeError, json.JSONDecodeError):
    reject("依赖私有收据无法解析")
python_version = f"Python {platform.python_version()}"
if (
    lock_receipt.read_text(encoding="utf-8").splitlines() != [expected_sha]
    or python_receipt.read_text(encoding="utf-8").splitlines()
    != [python_version]
    or not isinstance(receipt, dict)
    or receipt.get("schema_version") != 1
    or receipt.get("method")
    not in {"fresh-install", "verified-current-clone"}
    or receipt.get("requirements_lock_sha256") != expected_sha
    or receipt.get("pip_inspect_sha256")
    != hashlib.sha256(inspect_bytes).hexdigest()
    or receipt.get("python_major_minor")
    != f"{sys.version_info.major}.{sys.version_info.minor}"
    or receipt.get("python_version") != python_version
):
    reject("依赖来源收据与候选解释器不一致")
if receipt["method"] == "fresh-install":
    if (
        receipt.get("source_release_id") is not None
        or receipt.get("source_pip_inspect_sha256") is not None
    ):
        reject("全新安装收据不得声明复用来源")
else:
    if (
        not isinstance(receipt.get("source_release_id"), str)
        or receipt["source_release_id"] in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9._-]+", receipt["source_release_id"])
        is None
        or not isinstance(receipt.get("source_pip_inspect_sha256"), str)
        or re.fullmatch(
            r"[0-9a-f]{64}", receipt["source_pip_inspect_sha256"]
        )
        is None
    ):
        reject("依赖复用来源收据不完整")
live_inspect_result = subprocess.run(
    [sys.executable, "-m", "pip", "inspect", "--local"],
    check=False,
    capture_output=True,
    text=True,
)
if live_inspect_result.returncode != 0:
    reject("候选虚拟环境 pip inspect 失败")
try:
    live_inspect = json.loads(live_inspect_result.stdout)
except json.JSONDecodeError:
    reject("候选虚拟环境 pip inspect 输出异常")
expected_packages = locked_packages()
if (
    inspected_packages(recorded_inspect) != expected_packages
    or inspected_packages(live_inspect) != expected_packages
):
    reject("锁、记录收据与候选 live 包集合或版本不一致")
if subprocess.run(
    [sys.executable, "-m", "pip", "check"],
    check=False,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
).returncode != 0:
    reject("候选虚拟环境依赖完整性检查失败")
if subprocess.run(
    [sys.executable, "-m", "gunicorn", "--version"],
    check=False,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
).returncode != 0:
    reject("候选虚拟环境 Gunicorn 模块或入口不可用")
PY
    then
        fail "部署依赖 Python、来源收据或 pip check 验证失败"
        return 1
    fi
}

compute_effective_redis_backend_sha256() {
    local environment_path="$1"
    "$VENV_DIR/bin/python" - "$environment_path" <<'PY'
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import stat
import sys
from urllib.parse import parse_qsl, unquote, urlsplit

from dotenv import dotenv_values


MAX_ENV_BYTES = 1024 * 1024
path = Path(sys.argv[1])
descriptor = None
try:
    path_before = path.lstat()
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, 'O_CLOEXEC', 0)
        | getattr(os, 'O_NOFOLLOW', 0),
    )
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(path_before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(path_before.st_mode)
        or before.st_size > MAX_ENV_BYTES
        or (path_before.st_dev, path_before.st_ino)
        != (before.st_dev, before.st_ino)
    ):
        raise ValueError
    chunks = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(65536, MAX_ENV_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_ENV_BYTES:
            raise ValueError
    payload = b''.join(chunks)
    after = os.fstat(descriptor)
    path_after = path.lstat()
    stable_fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(
        getattr(before, field) != getattr(after, field)
        or getattr(after, field) != getattr(path_after, field)
        for field in stable_fields
    ):
        raise ValueError
    text = payload.decode('utf-8-sig')
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
finally:
    if descriptor is not None:
        os.close(descriptor)

values = dotenv_values(stream=io.StringIO(text), interpolate=True)
raw = str(
    values.get('WEATHER_CACHE_REDIS_URL')
    or values.get('REDIS_URL')
    or ''
).strip()
if not raw:
    print(hashlib.sha256(b'absent').hexdigest())
    raise SystemExit(0)

try:
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {'redis', 'rediss'} or not parsed.hostname or parsed.fragment:
        raise ValueError
    host = parsed.hostname.lower().rstrip('.')
    try:
        host = ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    port = parsed.port or 6379
    if port < 1 or port > 65535:
        raise ValueError
    username = unquote(parsed.username or '')
    password = unquote(parsed.password or '')
    path_value = unquote(parsed.path or '').strip('/')
    if path_value and (not path_value.isdigit() or int(path_value) < 0):
        raise ValueError
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    # redis-py 对重复参数采用首值；直接拒绝重复键，避免排序后身份哈希碰撞。
    query_keys = [key for key, _value in query_items]
    if len(query_keys) != len(set(query_keys)):
        raise ValueError
    db_values = [value for key, value in query_items if key == 'db']
    if len(db_values) > 1 or any(
        not value.isdigit() or int(value) < 0 for value in db_values
    ):
        raise ValueError
    database = int(db_values[0]) if db_values else int(path_value or '0')
    connection_options = sorted(
        (key, value) for key, value in query_items if key != 'db'
    )
except (TypeError, ValueError):
    raise SystemExit(2) from None

# 只输出不可逆哈希；认证信息和原始 Redis URL 不进入 journal 或日志。
identity = {
    'scheme': scheme,
    'host': host,
    'port': port,
    'username': username,
    'password': password,
    'database': database,
    'connection_options': connection_options,
}
canonical = json.dumps(
    identity,
    ensure_ascii=False,
    sort_keys=True,
    separators=(',', ':'),
).encode('utf-8')
print(hashlib.sha256(canonical).hexdigest())
PY
}

verify_effective_redis_backend_identity() {
    local active_hash staged_hash
    active_hash="$(compute_effective_redis_backend_sha256 "$ENV_FILE")" || {
        fail "活动环境 Redis 后端身份无法安全解析"
        return 1
    }
    staged_hash="$(compute_effective_redis_backend_sha256 "$STAGED_ENV_FILE")" || {
        fail "候选环境 Redis 后端身份无法安全解析"
        return 1
    }
    if [[ ! "$active_hash" =~ ^[0-9a-f]{64}$ ]] \
        || [[ ! "$staged_hash" =~ ^[0-9a-f]{64}$ ]] \
        || [ "$active_hash" != "$staged_hash" ]; then
        fail "活动与候选环境的有效 Redis 后端不同；请先独立迁移并验证 Redis，再重新创建发布"
        return 1
    fi
    FORMAL_SMOKE_REDIS_BACKEND_SHA256="$active_hash"
}

verify_candidate_base_state() {
    if ! "$VENV_DIR/bin/python" - \
        "$CANDIDATE_BASE_STATE_FILE" \
        "$ENV_FILE" \
        "$STAGED_ENV_FILE" \
        "$CURRENT_LINK" \
        "$DEPLOY_INTENT" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


MAX_ENV_BYTES = 1024 * 1024
HASH_PATTERN = re.compile(r'^[0-9a-f]{64}$')
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
    raise SystemExit(1)


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


def read_regular_stably(path, max_bytes):
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
            or before.st_size > max_bytes
        ):
            fail()
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
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
            # 只兼容旧活动环境缺字段；候选环境仍必须显式且唯一。
            candidates = ['0']
        if len(candidates) != 1 or candidates[0] not in {'0', '1'}:
            fail()
        values[key] = candidates[0]
    return values


metadata_path = Path(sys.argv[1])
active_env = Path(sys.argv[2])
staged_env = Path(sys.argv[3])
current_link = Path(sys.argv[4])
deployment_intent = sys.argv[5]
try:
    metadata = json.loads(
        read_regular_stably(metadata_path, 4096).decode('utf-8')
    )
except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
    fail()
if (
    not isinstance(metadata, dict)
    or set(metadata) != {
        'active_env_sha256',
        'current_link_state_sha256',
        'deployment_intent',
        'qweather_config_sha256',
        'wechat_formal_runtime',
        'web_private_features_enabled',
        'version',
    }
    or metadata.get('version') != 3
    or not isinstance(metadata.get('active_env_sha256'), str)
    or not HASH_PATTERN.fullmatch(metadata['active_env_sha256'])
    or not isinstance(metadata.get('current_link_state_sha256'), str)
    or not HASH_PATTERN.fullmatch(metadata['current_link_state_sha256'])
    or metadata.get('deployment_intent') != deployment_intent
    or deployment_intent not in {'web_backend_only', 'wechat_formal'}
    or not isinstance(metadata.get('qweather_config_sha256'), str)
    or not HASH_PATTERN.fullmatch(metadata['qweather_config_sha256'])
    or metadata.get('wechat_formal_runtime') not in {'0', '1'}
    or metadata.get('web_private_features_enabled') not in {'0', '1'}
):
    fail()
active_content = read_regular_stably(active_env, MAX_ENV_BYTES)
active_runtime_flags = runtime_gate_values(
    active_content,
    allow_missing_web_private=True,
)
if (
    hashlib.sha256(active_content).hexdigest()
    != metadata['active_env_sha256']
    or current_link_state(current_link)
    != metadata['current_link_state_sha256']
    or active_runtime_flags['WECHAT_FORMAL_RUNTIME']
    != metadata['wechat_formal_runtime']
    or active_runtime_flags['WEB_PRIVATE_FEATURES_ENABLED']
    != metadata['web_private_features_enabled']
):
    fail()
if deployment_intent == 'web_backend_only':
    staged_content = read_regular_stably(staged_env, MAX_ENV_BYTES)
    staged_runtime_flags = runtime_gate_values(staged_content)
    if (
        qweather_configuration_hash(active_content)
        != metadata['qweather_config_sha256']
        or qweather_configuration_hash(staged_content)
        != metadata['qweather_config_sha256']
        or staged_runtime_flags != active_runtime_flags
    ):
        fail()
PY
    then
        fail "候选配置基线已变化，请从最新活动环境重新创建发布"
        return 1
    fi
}

verify_effective_runtime_gate() {
    local gate_state=""
    local base_runtime=""
    local base_web_private=""
    local staged_runtime=""
    local staged_web_private=""
    if ! gate_state="$("$VENV_DIR/bin/python" - \
        "$STAGED_ENV_FILE" \
        "$CANDIDATE_BASE_STATE_FILE" \
        "$DEPLOY_INTENT" <<'PY'
import json
import os
from pathlib import Path
import stat
import sys


def read_regular_stably(path, max_bytes):
    descriptor = None
    try:
        path_before = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, 'O_CLOEXEC', 0)
            | getattr(os, 'O_NOFOLLOW', 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(path_before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(path_before.st_mode)
            or before.st_size > max_bytes
        ):
            raise SystemExit(1)
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise SystemExit(1)
        after = os.fstat(descriptor)
        path_after = path.lstat()
        fingerprint = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_gid,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if (
            total != before.st_size
            or fingerprint(path_before) != fingerprint(before)
            or fingerprint(before) != fingerprint(after)
            or fingerprint(after) != fingerprint(path_after)
        ):
            raise SystemExit(1)
        return b''.join(chunks)
    except OSError:
        raise SystemExit(1) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def runtime_gate_values(payload):
    try:
        text = payload.decode('utf-8')
    except UnicodeDecodeError:
        raise SystemExit(1) from None
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
            matches[normalized_key].append(
                value.strip().strip('"').strip("'")
            )
    values = []
    for key in ('WECHAT_FORMAL_RUNTIME', 'WEB_PRIVATE_FEATURES_ENABLED'):
        candidates = matches[key]
        if len(candidates) != 1 or candidates[0] not in {'0', '1'}:
            raise SystemExit(1)
        values.append(candidates[0])
    return values


staged_env = Path(sys.argv[1])
metadata_path = Path(sys.argv[2])
deployment_intent = sys.argv[3]
try:
    metadata = json.loads(
        read_regular_stably(metadata_path, 4096).decode('utf-8')
    )
except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1) from None
expected_keys = {
    'active_env_sha256',
    'current_link_state_sha256',
    'deployment_intent',
    'qweather_config_sha256',
    'wechat_formal_runtime',
    'web_private_features_enabled',
    'version',
}
if (
    not isinstance(metadata, dict)
    or set(metadata) != expected_keys
    or metadata.get('version') != 3
    or metadata.get('deployment_intent') != deployment_intent
    or metadata.get('wechat_formal_runtime') not in {'0', '1'}
    or metadata.get('web_private_features_enabled') not in {'0', '1'}
):
    raise SystemExit(1)
staged_runtime, staged_web_private = runtime_gate_values(
    read_regular_stably(staged_env, 1024 * 1024)
)
print(
    ':'.join(
        (
            staged_runtime,
            staged_web_private,
            metadata['wechat_formal_runtime'],
            metadata['web_private_features_enabled'],
        )
    )
)
PY
)"; then
        fail "候选正式态、双端网页开关或活动基线不完整"
        return 1
    fi
    IFS=: read -r \
        staged_runtime \
        staged_web_private \
        base_runtime \
        base_web_private <<< "$gate_state"
    if [ "$staged_runtime" != "$REQUIRE_WECHAT_READY" ]; then
        fail "部署门禁与候选 WECHAT_FORMAL_RUNTIME 不一致"
        return 1
    fi
    case "$DEPLOY_INTENT" in
        wechat_formal)
            if [ "$staged_runtime:$staged_web_private" != "1:1" ]; then
                fail "微信正式部署候选必须保持正式态与双端网页开关均为 1"
                return 1
            fi
            ;;
        web_backend_only)
            if [ "$staged_runtime:$staged_web_private" \
                != "$base_runtime:$base_web_private" ]; then
                fail "网页/后端发布必须继承活动环境的正式态与双端网页开关"
                return 1
            fi
            ;;
    esac
    if [ "$staged_runtime" != "$EXPECTED_WECHAT_FORMAL_RUNTIME" ] \
        || [ "$staged_web_private" != "$EXPECTED_WEB_PRIVATE_FEATURES_ENABLED" ]; then
        fail "候选正式态与部署预期不一致"
        return 1
    fi
}

validate_release_identity() {
    local metadata_file="$NEW_RELEASE/private-metadata/source-commit.txt"
    local metadata_commit=""
    if [[ ! "$EXPECTED_RELEASE_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
        fail "发布缺少有效的冻结 commit 票据"
        return 1
    fi
    if [ -L "$metadata_file" ]; then
        fail "发布 commit metadata 不得为符号链接"
        return 1
    fi
    require_file "$metadata_file"
    IFS= read -r metadata_commit < "$metadata_file"
    if [[ ! "$metadata_commit" =~ ^[0-9a-f]{40}$ ]] \
        || [ "$metadata_commit" != "$EXPECTED_RELEASE_COMMIT" ]; then
        fail "上传 release 与冻结 commit 票据不一致"
        return 1
    fi
    RELEASE_COMMIT="$metadata_commit"
    if [ "$REQUIRE_WECHAT_READY" = 1 ]; then
        FORMAL_RELEASE_COMMIT="$metadata_commit"
    fi
}

validate_model_artifacts() {
    local helper="$APP_DIR/scripts/model_artifact.py"
    local manifest="$APP_DIR/models/feature_config.json"
    require_file "$helper"
    require_file "$manifest"
    require_file "$MODEL_ARTIFACT_RECEIPT_FILE"
    if ! "$VENV_DIR/bin/python" "$helper" verify \
        --artifact-dir "$APP_DIR/models" \
        --manifest "$manifest" \
        --receipt "$MODEL_ARTIFACT_RECEIPT_FILE" \
        --commit "$EXPECTED_RELEASE_COMMIT" \
        --expected-owner "$CONTROL_OWNER_UID" \
        --expected-group "$RUNTIME_GROUP" \
        --expected-file-mode 0640 \
        --expected-dir-mode 0750; then
        fail "模型制品收据或运行制品与待激活 release 不一致"
        return 1
    fi
}

validate_release_proofs() {
    require_file "$APP_DIR/scripts/verify_github_ci.py"
    require_file "$APP_DIR/scripts/release_runtime_smoke.py"
    require_file "$CI_PROOF_FILE"
    require_file "$RUNTIME_SMOKE_RECEIPT_FILE"

    if ! "$VENV_DIR/bin/python" \
        "$APP_DIR/scripts/verify_github_ci.py" verify-receipt \
        --receipt "$CI_PROOF_FILE" \
        --repo AlfWuxy/weather-Web \
        --workflow .github/workflows/ci.yml \
        --commit "$EXPECTED_RELEASE_COMMIT" \
        --branch "$EXPECTED_RELEASE_BRANCH" \
        --proof-job "可发布提交证明"; then
        fail "GitHub Python CI 收据与待激活 release 不一致"
        return 1
    fi

    if [ "$REQUIRE_WECHAT_READY" = 1 ]; then
        require_file "$MINIPROGRAM_CI_PROOF_FILE"
        if ! "$VENV_DIR/bin/python" \
            "$APP_DIR/scripts/verify_github_ci.py" verify-receipt \
            --receipt "$MINIPROGRAM_CI_PROOF_FILE" \
            --repo AlfWuxy/weather-Web \
            --workflow .github/workflows/miniprogram.yml \
            --commit "$EXPECTED_RELEASE_COMMIT" \
            --branch "$EXPECTED_RELEASE_BRANCH" \
            --proof-job "小程序可发布提交证明"; then
            fail "GitHub 小程序 CI 收据与待激活 release 不一致"
            return 1
        fi
    fi

    if ! "$VENV_DIR/bin/python" \
        "$APP_DIR/scripts/release_runtime_smoke.py" verify-receipt \
        --receipt "$RUNTIME_SMOKE_RECEIPT_FILE" \
        --repo-root "$APP_DIR" \
        --expected-commit "$EXPECTED_RELEASE_COMMIT" \
        --expected-python "$VENV_DIR/bin/python" \
        --expected-python-minor 3.11 \
        --expected-lock-sha "$EXPECTED_REQUIREMENTS_LOCK_SHA256"; then
        fail "服务器低内存运行态收据与待激活 release 不一致"
        return 1
    fi
}

verify_formal_runtime_healthz_probe() {
    local access_log="$1"
    local error_log="$2"
    local stat_bin="$3"
    local access_before access_after error_before error_after
    local probe_status="" stopped_probe_result=1
    local allow_stopped_recovery=0

    if [ ! -f "$access_log" ] || [ -L "$access_log" ] \
        || [ ! -f "$error_log" ] || [ -L "$error_log" ]; then
        fail "Nginx 运行态日志必须是固定路径下的普通文件"
        return 1
    fi
    access_before="$("$stat_bin" -c %s "$access_log")"
    error_before="$("$stat_bin" -c %s "$error_log")"

    if formal_runtime_stopped_probe_is_allowed; then
        allow_stopped_recovery=1
    else
        stopped_probe_result=$?
        if [ "$stopped_probe_result" -ne 1 ]; then
            return 1
        fi
    fi

    # 正式 Nginx 只监听回环 HTTP，公网 HTTPS 由边缘层终止。
    # 固定命中边缘隧道使用的 8080 入口，避免误连同机其他 443 服务或绕回公网。
    if ! probe_status="$("$CURL_BIN" \
        --silent \
        --show-error \
        --noproxy '*' \
        --connect-timeout 5 \
        --max-time 10 \
        --header 'Host: yilaoweather.org' \
        --output /dev/null \
        --write-out '%{http_code}' \
        http://127.0.0.1:8080/healthz
    )"; then
        fail "Nginx 本机 healthz 请求失败"
        return 1
    fi
    case "$probe_status" in
        200) ;;
        502)
            if [ "$allow_stopped_recovery" != 1 ]; then
                fail "Nginx 本机 healthz 返回非预期状态: $probe_status"
                return 1
            fi
            log "已验证恢复事务，接受持久开机门下的预期 502 停机态"
            ;;
        *)
            fail "Nginx 本机 healthz 返回非预期状态: $probe_status"
            return 1
            ;;
    esac

    access_after="$("$stat_bin" -c %s "$access_log")"
    error_after="$("$stat_bin" -c %s "$error_log")"
    if [ "$access_after" != "$access_before" ] \
        || [ "$error_after" != "$error_before" ]; then
        fail "本机 healthz 请求导致 Nginx 访问者日志增长"
        return 1
    fi
    log "正式 Nginx 日志边界与本机 healthz 验证通过"
}

verify_formal_runtime_log_boundary() {
    local access_log=/var/log/nginx/access.log
    local error_log=/var/log/nginx/error.log

    [ "$REQUIRE_WECHAT_READY" = 1 ] || return 0

    if [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" = 1 ]; then
        # 测试 helper 只能观察锁与零变更边界，不能进入正式 root 路径。
        "$RUNTIME_LOG_BOUNDARY_TEST_HELPER" \
            "$RELEASE_ROOT/deploy.lock" \
            "$TRANSACTION_DIR" \
            "$MUTATION_STARTED"
        return 0
    fi

    [ -x /usr/sbin/nginx ] || {
        fail "正式发布缺少固定的 /usr/sbin/nginx"
        return 1
    }
    [ -x /usr/bin/systemctl ] || {
        fail "正式发布缺少固定的 /usr/bin/systemctl"
        return 1
    }
    [ -x /usr/bin/stat ] || {
        fail "正式发布缺少固定的 /usr/bin/stat"
        return 1
    }
    require_file "$APP_DIR/scripts/verify_runtime_log_boundary.py"

    log "重新加载并验证正式 Nginx 日志边界"
    /usr/sbin/nginx -t >/dev/null
    /usr/bin/systemctl reload nginx
    /usr/bin/systemctl is-active --quiet nginx || {
        fail "Nginx reload 后未保持 active"
        return 1
    }
    "$VENV_DIR/bin/python" \
        "$APP_DIR/scripts/verify_runtime_log_boundary.py" \
        --active-nginx >/dev/null

    verify_formal_runtime_healthz_probe "$access_log" "$error_log" /usr/bin/stat
}

compute_formal_release_config_fingerprint() {
    local environment_path="${1:-$ENV_FILE}"
    local private_key_digest_override="${2:-}"
    local qweather_key_owner_uid=0
    local qweather_key_group_gid=""
    if [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" = 1 ]; then
        qweather_key_owner_uid="$CONTROL_OWNER_UID"
    fi
    qweather_key_group_gid="$(id -g "$RUNTIME_USER")"
    "$VENV_DIR/bin/python" - \
        "$environment_path" \
        "$qweather_key_owner_uid" \
        "$qweather_key_group_gid" \
        "$private_key_digest_override" <<'PY'
import hashlib
import json
import os
import stat
import sys

path = sys.argv[1]
expected_key_owner_uid = int(sys.argv[2])
expected_key_group_gid = int(sys.argv[3])
private_key_digest_override = sys.argv[4]
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
    'WEATHER_CACHE_REDIS_URL',
    'REDIS_URL',
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
            or stat.S_IMODE(before.st_mode) != 0o640
            or before.st_uid != expected_key_owner_uid
            or before.st_gid != expected_key_group_gid
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
    if private_key_digest_override:
        if (
            len(private_key_digest_override) != 64
            or any(
                character not in '0123456789abcdef'
                for character in private_key_digest_override
            )
        ):
            raise SystemExit('正式 JWT 私钥摘要覆盖值无效')
        payload['QWEATHER_JWT_PRIVATE_KEY_SHA256'] = private_key_digest_override
    else:
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

preflight_formal_qweather_jwt_runtime() {
    local state qweather_key_owner_uid=0 qweather_key_group_gid=""
    [ "$REQUIRE_WECHAT_READY" = 1 ] || return 0
    if [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" = 1 ]; then
        qweather_key_owner_uid="$CONTROL_OWNER_UID"
    fi
    qweather_key_group_gid="$(id -g "$RUNTIME_USER")"
    if ! state="$(
        cd "$APP_DIR"
        runtime_exec "$VENV_DIR/bin/python" - \
            "$qweather_key_owner_uid" \
            "$qweather_key_group_gid" <<'PY'
import os
import stat
import sys
from pathlib import Path

try:
    expected_key_owner_uid = int(sys.argv[1])
    expected_key_group_gid = int(sys.argv[2])
    from core.app import create_app
    from services.qweather_auth import get_qweather_request_headers

    app = create_app(register_blueprints=False)
    with app.app_context():
        config = app.config
        if str(config.get('QWEATHER_AUTH_MODE') or '').strip().lower() != 'jwt':
            raise RuntimeError('mode')
        key_path = Path(str(config.get('QWEATHER_JWT_PRIVATE_KEY_PATH') or ''))
        if not key_path.is_absolute():
            raise RuntimeError('path')
        protected_roots = (Path('/home'), Path('/root'), Path('/run/user'))
        if any(key_path == root or root in key_path.parents for root in protected_roots):
            raise RuntimeError('protected-home')
        key_stat = os.stat(key_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(key_stat.st_mode)
            or stat.S_IMODE(key_stat.st_mode) != 0o640
            or key_stat.st_uid != expected_key_owner_uid
            or key_stat.st_gid != expected_key_group_gid
            or expected_key_group_gid
            not in {os.getegid(), *os.getgroups()}
        ):
            raise RuntimeError('owner-or-mode')
        headers = get_qweather_request_headers(
            config,
            api_base=str(config.get('QWEATHER_API_BASE') or ''),
        )
        authorization = headers.get('Authorization', '')
        if set(headers) != {'Authorization'} or not authorization.startswith('Bearer '):
            raise RuntimeError('headers')
except Exception:
    raise SystemExit(2) from None

print('ready')
PY
    )"; then
        fail "正式 JWT 运行用户离线签名预检失败；未写入 started receipt，也未消耗天气预算"
        return 1
    fi
    [ "$state" = ready ] || {
        fail "正式 JWT 运行用户离线签名预检没有返回 ready"
        return 1
    }
    log "正式 JWT 已由运行用户完成离线签名预检，未发起网络请求"
}

capture_qweather_budget_snapshot() {
    local destination="$1"
    local snapshot normalized
    if [ -n "$QWEATHER_BUDGET_SNAPSHOT_HELPER" ]; then
        if [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" != 1 ]; then
            fail "生产激活禁止覆盖 QWeather 预算快照实现"
            return 1
        fi
        snapshot="$(runtime_exec "$QWEATHER_BUDGET_SNAPSHOT_HELPER")" || {
            fail "离线测试预算快照 helper 执行失败"
            return 1
        }
    else
        if ! snapshot="$(
            cd "$APP_DIR"
            runtime_exec "$VENV_DIR/bin/python" - <<'PY'
import json

try:
    from core.app import create_app
    from services.qweather_budget import get_qweather_budget_snapshot

    app = create_app(register_blueprints=False)
    with app.app_context():
        source = get_qweather_budget_snapshot()
    snapshot = {
        'backend': source.get('backend'),
        'month': source.get('month'),
        'used': source.get('used'),
        'endpoints': source.get('endpoints'),
    }
except Exception:
    raise SystemExit(2) from None

print(json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(',', ':')))
PY
        )"; then
            fail "无法读取正式 QWeather 持久预算快照"
            return 1
        fi
    fi
    if ! normalized="$("$VENV_DIR/bin/python" - "$snapshot" <<'PY'
import json
import re
import sys

try:
    value = json.loads(sys.argv[1])
    month = value['month']
    used = value['used']
    endpoints = value['endpoints']
    if value.get('backend') != 'redis' or not re.fullmatch(r'\d{4}-\d{2}', month):
        raise ValueError
    if isinstance(used, bool) or not isinstance(used, int) or used < 0:
        raise ValueError
    if not isinstance(endpoints, dict):
        raise ValueError
    normalized_endpoints = {}
    for key, count in endpoints.items():
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', key):
            raise ValueError
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError
        normalized_endpoints[key] = count
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None

print(json.dumps(
    {'backend': 'redis', 'month': month, 'used': used, 'endpoints': normalized_endpoints},
    ensure_ascii=True,
    sort_keys=True,
    separators=(',', ':'),
))
PY
    )"; then
        fail "QWeather 持久预算快照结构异常"
        return 1
    fi
    write_durable_marker "$destination" "$normalized"
}

budget_snapshot_started_fields() {
    "$VENV_DIR/bin/python" - "$1" <<'PY'
import json
import sys

with open(sys.argv[1], encoding='ascii') as stream:
    value = json.load(stream)
print(f"budget_month={value['month']}")
print(f"budget_used_before={value['used']}")
print('budget_endpoints_before_json=' + json.dumps(
    value['endpoints'], ensure_ascii=True, sort_keys=True, separators=(',', ':')
))
PY
}

compare_qweather_budget_snapshots() {
    "$VENV_DIR/bin/python" - "$1" "$2" <<'PY'
import json
import sys

with open(sys.argv[1], encoding='ascii') as stream:
    before = json.load(stream)
with open(sys.argv[2], encoding='ascii') as stream:
    after = json.load(stream)

if before['backend'] != 'redis' or after['backend'] != 'redis':
    raise SystemExit(2)
if before['month'] != after['month']:
    raise SystemExit(2)
total_delta = after['used'] - before['used']
endpoint_deltas = {}
allowed_endpoints = {
    'weather_now',
    'weather_7d_forecast',
    'weatheralert_v1_current',
}
for endpoint in sorted(set(before['endpoints']) | set(after['endpoints'])):
    delta = after['endpoints'].get(endpoint, 0) - before['endpoints'].get(endpoint, 0)
    if delta < 0 or delta > 1:
        raise SystemExit(2)
    if delta:
        endpoint_deltas[endpoint] = delta
if sum(endpoint_deltas.values()) != total_delta:
    raise SystemExit(2)
if total_delta != len(allowed_endpoints):
    raise SystemExit(2)
if set(endpoint_deltas) != allowed_endpoints:
    raise SystemExit(2)
if not all(delta == 1 for delta in endpoint_deltas.values()):
    raise SystemExit(2)

compact = lambda value: json.dumps(
    value, ensure_ascii=True, sort_keys=True, separators=(',', ':')
)
print(f"budget_month={before['month']}")
print(f"budget_used_before={before['used']}")
print(f"budget_used_after={after['used']}")
print(f"budget_total_delta={total_delta}")
print('budget_endpoints_after_json=' + compact(after['endpoints']))
print('budget_endpoint_deltas_json=' + compact(endpoint_deltas))
PY
}

verify_completed_budget_receipt() {
    "$VENV_DIR/bin/python" - "$1" "$2" "$3" <<'PY'
import hashlib
import json
import re
import sys

def fields(path):
    values = {}
    with open(path, encoding='utf-8') as stream:
        for raw_line in stream:
            key, separator, value = raw_line.rstrip('\n').partition('=')
            if separator:
                values[key] = value
    return values

try:
    started = fields(sys.argv[1])
    completed = fields(sys.argv[2])
    binding = fields(sys.argv[3])
    month = completed['budget_month']
    if month != started['budget_month'] or not re.fullmatch(r'\d{4}-\d{2}', month):
        raise ValueError
    used_before = int(started['budget_used_before'])
    if used_before != int(completed['budget_used_before']):
        raise ValueError
    used_after = int(completed['budget_used_after'])
    total_delta = int(completed['budget_total_delta'])
    before_endpoints = json.loads(started['budget_endpoints_before_json'])
    after_endpoints = json.loads(completed['budget_endpoints_after_json'])
    endpoint_deltas = json.loads(completed['budget_endpoint_deltas_json'])
    allowed_endpoints = {
        'weather_now',
        'weather_7d_forecast',
        'weatheralert_v1_current',
    }
    formal_binding = started['formal_smoke_binding']
    formal_token_sha256 = started['formal_smoke_token_sha256']
    formal_lease_token_sha256 = started['formal_smoke_lease_token_sha256']
    if not re.fullmatch(r'[0-9a-f]{64}', formal_binding):
        raise ValueError
    expected_binding = hashlib.sha256(
        f"{binding['release_commit']}:{binding['config_fingerprint']}".encode('ascii')
    ).hexdigest()
    if formal_binding != expected_binding:
        raise ValueError
    if not re.fullmatch(r'[0-9a-f]{64}', formal_token_sha256):
        raise ValueError
    if not re.fullmatch(r'[0-9a-f]{64}', formal_lease_token_sha256):
        raise ValueError
    if total_delta != len(allowed_endpoints) or used_after - used_before != total_delta:
        raise ValueError
    if set(endpoint_deltas) != allowed_endpoints:
        raise ValueError
    if not all(isinstance(value, int) and value == 1 for value in endpoint_deltas.values()):
        raise ValueError
    if sum(endpoint_deltas.values()) != total_delta:
        raise ValueError
    for endpoint in set(before_endpoints) | set(after_endpoints):
        if after_endpoints.get(endpoint, 0) - before_endpoints.get(endpoint, 0) != endpoint_deltas.get(endpoint, 0):
            raise ValueError
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
PY
}

prepare_formal_smoke_token_material() {
    local generated
    generated="$("$VENV_DIR/bin/python" - \
        "$FORMAL_RELEASE_COMMIT" \
        "$FORMAL_RELEASE_CONFIG_FINGERPRINT" <<'PY'
import hashlib
import secrets
import sys

commit, fingerprint = sys.argv[1:]
token = secrets.token_hex(32)
binding = hashlib.sha256(f'{commit}:{fingerprint}'.encode('ascii')).hexdigest()
lease_token = secrets.token_hex(32)
print(token)
print(binding)
print(hashlib.sha256(token.encode('ascii')).hexdigest())
print(lease_token)
print(hashlib.sha256(lease_token.encode('ascii')).hexdigest())
PY
    )" || {
        fail "正式天气烟测一次性票据材料生成失败"
        return 1
    }
    FORMAL_SMOKE_TOKEN="$(printf '%s\n' "$generated" | sed -n '1p')"
    FORMAL_SMOKE_BINDING="$(printf '%s\n' "$generated" | sed -n '2p')"
    FORMAL_SMOKE_TOKEN_SHA256="$(printf '%s\n' "$generated" | sed -n '3p')"
    FORMAL_SMOKE_LEASE_TOKEN="$(printf '%s\n' "$generated" | sed -n '4p')"
    FORMAL_SMOKE_LEASE_TOKEN_SHA256="$(printf '%s\n' "$generated" | sed -n '5p')"
    if [[ ! "$FORMAL_SMOKE_TOKEN" =~ ^[0-9a-f]{64}$ ]] \
        || [[ ! "$FORMAL_SMOKE_BINDING" =~ ^[0-9a-f]{64}$ ]] \
        || [[ ! "$FORMAL_SMOKE_TOKEN_SHA256" =~ ^[0-9a-f]{64}$ ]] \
        || [[ ! "$FORMAL_SMOKE_LEASE_TOKEN" =~ ^[0-9a-f]{64}$ ]] \
        || [[ ! "$FORMAL_SMOKE_LEASE_TOKEN_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
        fail "正式天气烟测一次性票据材料格式异常"
        return 1
    fi
    FORMAL_SMOKE_TICKET="$STATE_DIR/run/formal-weather-smoke-$RELEASE_ID-$FORMAL_SMOKE_BINDING.ticket"
}

verify_formal_smoke_ticket_path_available() {
    if [ -e "$FORMAL_SMOKE_TICKET" ] || [ -L "$FORMAL_SMOKE_TICKET" ]; then
        fail "正式天气烟测一次性票据已存在，禁止覆盖"
        return 1
    fi
}

run_formal_smoke_cycle_lease_control() {
    local action="$1" environment_path="$2" option=""
    case "$action" in
        reserve) option=--reserve-formal-lease-only ;;
        renew) option=--renew-formal-lease-only ;;
        release) option=--release-formal-lease-only ;;
        *) fail "正式天气烟测租约控制动作无效"; return 1 ;;
    esac
    if [ -n "$FORMAL_SMOKE_LEASE_HELPER" ]; then
        if [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" != 1 ]; then
            fail "生产激活禁止覆盖正式天气烟测租约实现"
            return 1
        fi
        (
            cd "$APP_DIR"
            runtime_exec "$ENV_BIN" \
                "CASE_WEATHER_ENV_FILE=$environment_path" \
                "CASE_WEATHER_FORMAL_SMOKE_LEASE_ACTION=$action" \
                "CASE_WEATHER_FORMAL_SMOKE_LEASE_TOKEN=$FORMAL_SMOKE_LEASE_TOKEN" \
                "$FORMAL_SMOKE_LEASE_HELPER"
        )
        return $?
    fi
    (
        cd "$APP_DIR"
        runtime_exec "$ENV_BIN" \
            "CASE_WEATHER_ENV_FILE=$environment_path" \
            "CASE_WEATHER_FORMAL_SMOKE_LEASE_TOKEN=$FORMAL_SMOKE_LEASE_TOKEN" \
            "$VENV_DIR/bin/python" \
            -m services.pipelines.sync_weather_cache \
            "$option"
    )
}

write_formal_smoke_lease_journal() {
    local transaction_id="${TRANSACTION_DIR##*/}"
    if [[ ! "$FORMAL_SMOKE_LEASE_TOKEN" =~ ^[0-9a-f]{64}$ ]] \
        || [[ ! "$FORMAL_SMOKE_REDIS_BACKEND_SHA256" =~ ^[0-9a-f]{64}$ ]] \
        || [ -z "$transaction_id" ] \
        || [[ "$transaction_id" == *$'\n'* ]]; then
        fail "正式天气烟测 lease journal 材料无效"
        return 1
    fi
    if [ -e "$FORMAL_SMOKE_LEASE_JOURNAL" ] \
        || [ -L "$FORMAL_SMOKE_LEASE_JOURNAL" ]; then
        fail "正式天气烟测 lease journal 已存在，禁止覆盖"
        return 1
    fi
    write_durable_marker \
        "$FORMAL_SMOKE_LEASE_JOURNAL" \
        "$(printf 'transaction_id=%s\nredis_backend_sha256=%s\nlease_token=%s' \
            "$transaction_id" \
            "$FORMAL_SMOKE_REDIS_BACKEND_SHA256" \
            "$FORMAL_SMOKE_LEASE_TOKEN")"
    log "正式天气烟测 lease journal 已在预占前耐久落盘"
}

remove_formal_smoke_lease_journal() {
    local journal_path="$1"
    "$VENV_DIR/bin/python" - \
        "$journal_path" \
        "$TRANSACTION_ROOT" \
        "$CONTROL_OWNER_UID" \
        "$CONTROL_OWNER_GID" <<'PY'
import os
from pathlib import Path
import stat
import sys

journal = Path(sys.argv[1])
root = Path(sys.argv[2]).resolve(strict=True)
owner_uid = int(sys.argv[3])
owner_gid = int(sys.argv[4])
parent = journal.parent.resolve(strict=True)
file_stat = journal.lstat()
if (
    parent.parent != root
    or journal.name != 'formal-smoke-lease.journal'
    or not stat.S_ISREG(file_stat.st_mode)
    or stat.S_ISLNK(file_stat.st_mode)
    or file_stat.st_uid != owner_uid
    or file_stat.st_gid != owner_gid
    or stat.S_IMODE(file_stat.st_mode) != 0o600
):
    raise SystemExit(1)
os.unlink(journal)
directory = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
}

recover_abandoned_formal_smoke_lease_journals() {
    local journal_rows="" journal_path lease_token backend_hash transaction_path
    local marker_status=0 saved_token="$FORMAL_SMOKE_LEASE_TOKEN"
    [ "$REQUIRE_WECHAT_READY" = 1 ] || return 0
    if [[ ! "$FORMAL_SMOKE_REDIS_BACKEND_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
        fail "无法在恢复历史租约前确认活动 Redis 后端身份"
        return 1
    fi
    if ! journal_rows="$("$VENV_DIR/bin/python" - \
        "$TRANSACTION_ROOT" \
        "$CONTROL_OWNER_UID" \
        "$CONTROL_OWNER_GID" <<'PY'
from pathlib import Path
import re
import stat
import sys

root = Path(sys.argv[1]).resolve(strict=True)
owner_uid = int(sys.argv[2])
owner_gid = int(sys.argv[3])
hash_pattern = re.compile(r'^[0-9a-f]{64}$')
for transaction in sorted(root.iterdir()):
    transaction_stat = transaction.lstat()
    if not stat.S_ISDIR(transaction_stat.st_mode) or stat.S_ISLNK(transaction_stat.st_mode):
        raise SystemExit(1)
    journal = transaction / 'formal-smoke-lease.journal'
    try:
        journal_stat = journal.lstat()
    except FileNotFoundError:
        continue
    if (
        not stat.S_ISREG(journal_stat.st_mode)
        or stat.S_ISLNK(journal_stat.st_mode)
        or journal_stat.st_uid != owner_uid
        or journal_stat.st_gid != owner_gid
        or stat.S_IMODE(journal_stat.st_mode) != 0o600
        or journal.parent.resolve(strict=True) != transaction.resolve(strict=True)
    ):
        raise SystemExit(1)
    values = {}
    try:
        for line in journal.read_text(encoding='utf-8').splitlines():
            key, separator, value = line.partition('=')
            if not separator or not key or not value or key in values:
                raise ValueError
            values[key] = value
    except (OSError, UnicodeError, ValueError):
        raise SystemExit(1) from None
    if (
        set(values)
        != {'transaction_id', 'redis_backend_sha256', 'lease_token'}
        or values['transaction_id'] != transaction.name
        or not hash_pattern.fullmatch(values['redis_backend_sha256'])
        or not hash_pattern.fullmatch(values['lease_token'])
        or any(
            character in str(journal)
            for character in ('\t', '\r', '\n')
        )
    ):
        raise SystemExit(1)
    print(
        f"{journal}\t{values['lease_token']}\t"
        f"{values['redis_backend_sha256']}"
    )
PY
    )"; then
        fail "历史正式天气烟测 lease journal 内容、权限或路径无效"
        return 1
    fi
    [ -n "$journal_rows" ] || return 0

    # 先完整验证全部 journal，再执行任何 compare-delete，避免半清理。
    while IFS=$'\t' read -r journal_path lease_token backend_hash; do
        [ -n "$journal_path" ] || continue
        transaction_path="${journal_path%/formal-smoke-lease.journal}"
        if [ "$backend_hash" != "$FORMAL_SMOKE_REDIS_BACKEND_SHA256" ]; then
            fail "历史天气租约的 Redis 后端身份与当前活动环境不一致，保留 journal 等待人工核对"
            return 1
        fi
        marker_status=0
        transaction_requires_forward_only "$transaction_path" || marker_status=$?
        case "$marker_status" in
            1) ;;
            0)
                fail "历史天气租约已进入不可逆请求阶段，禁止自动释放"
                return 1
                ;;
            *)
                fail "历史天气租约的阶段证据损坏，禁止自动释放"
                return 1
                ;;
        esac
    done <<< "$journal_rows"

    while IFS=$'\t' read -r journal_path lease_token backend_hash; do
        [ -n "$journal_path" ] || continue
        FORMAL_SMOKE_LEASE_TOKEN="$lease_token"
        if ! run_formal_smoke_cycle_lease_control release "$ENV_FILE"; then
            FORMAL_SMOKE_LEASE_TOKEN="$saved_token"
            fail "历史天气租约无法用 owner token 安全回收，保留 journal 等待 TTL"
            return 1
        fi
        if ! remove_formal_smoke_lease_journal "$journal_path"; then
            FORMAL_SMOKE_LEASE_TOKEN="$saved_token"
            fail "历史天气租约已核验释放，但 lease journal 无法安全收口"
            return 1
        fi
        log "已用 owner token 安全回收可识别的中断天气租约: ${journal_path%/*}"
    done <<< "$journal_rows"
    FORMAL_SMOKE_LEASE_TOKEN="$saved_token"
}

reserve_formal_smoke_cycle_lease() {
    if ! run_formal_smoke_cycle_lease_control reserve "$STAGED_ENV_FILE"; then
        fail "正式天气烟测无法在生产变更前取得全局租约"
        return 1
    fi
}

renew_formal_smoke_cycle_lease() {
    if ! run_formal_smoke_cycle_lease_control renew "$ENV_FILE"; then
        fail "正式天气烟测租约在不可逆 receipt 前已过期或不再属于本事务"
        return 1
    fi
    log "正式天气烟测全局租约已由同一 token 原子续回 30 分钟"
}

release_reversible_formal_smoke_cycle_lease() {
    local environment_path="$ENV_FILE"
    [ "$FORMAL_SMOKE_IRREVERSIBLE" = 0 ] || return 0
    [ "$FORWARD_ONLY" = 0 ] || return 0
    if [ ! -e "$FORMAL_SMOKE_LEASE_JOURNAL" ] \
        && [ ! -L "$FORMAL_SMOKE_LEASE_JOURNAL" ]; then
        if [ "$FORMAL_SMOKE_LEASE_RESERVED" = 1 ]; then
            fail "可回滚天气租约缺少耐久 lease journal"
            return 1
        fi
        return 0
    fi
    if [ -L "$FORMAL_SMOKE_LEASE_JOURNAL" ] \
        || [ ! -f "$FORMAL_SMOKE_LEASE_JOURNAL" ]; then
        fail "可回滚天气租约的 lease journal 状态异常"
        return 1
    fi
    if [ -f "$STAGED_ENV_FILE" ] && [ ! -L "$STAGED_ENV_FILE" ]; then
        environment_path="$STAGED_ENV_FILE"
    fi
    if ! run_formal_smoke_cycle_lease_control release "$environment_path"; then
        log "可回滚事务未能核验并释放自身天气租约；保留 Redis 状态等待 TTL 到期" >&2
        return 1
    fi
    if ! remove_formal_smoke_lease_journal "$FORMAL_SMOKE_LEASE_JOURNAL"; then
        log "可回滚事务已核验租约，但 lease journal 无法安全删除" >&2
        return 1
    fi
    FORMAL_SMOKE_LEASE_RESERVED=0
    log "可回滚事务已原子释放自身天气租约；陌生租约保持原状"
}

preflight_formal_smoke_cycle_lease() {
    local binding_file started_file completed_file snapshot_id
    [ "$REQUIRE_WECHAT_READY" = 1 ] || return 0

    FORMAL_RELEASE_CONFIG_FINGERPRINT="$(
        compute_formal_release_config_fingerprint \
            "$STAGED_ENV_FILE" \
            "$QWEATHER_KEY_SHA256"
    )"
    if [[ ! "$FORMAL_RELEASE_CONFIG_FINGERPRINT" =~ ^[0-9a-f]{64}$ ]]; then
        fail "生产变更前无法生成正式发布配置指纹"
        return 1
    fi
    FORMAL_SMOKE_RECEIPT_DIR="$FORMAL_SMOKE_RECEIPT_ROOT/${FORMAL_RELEASE_COMMIT}-${FORMAL_RELEASE_CONFIG_FINGERPRINT}"
    binding_file="$FORMAL_SMOKE_RECEIPT_DIR/binding"
    started_file="$FORMAL_SMOKE_RECEIPT_DIR/started"
    completed_file="$FORMAL_SMOKE_RECEIPT_DIR/completed"

    if [ -L "$FORMAL_SMOKE_RECEIPT_ROOT" ] \
        || { [ -e "$FORMAL_SMOKE_RECEIPT_ROOT" ] \
            && [ ! -d "$FORMAL_SMOKE_RECEIPT_ROOT" ]; }; then
        fail "正式天气烟测 receipt 根目录状态异常"
        return 1
    fi
    if [ -e "$FORMAL_SMOKE_RECEIPT_DIR" ] \
        || [ -L "$FORMAL_SMOKE_RECEIPT_DIR" ]; then
        verify_receipt_binding
        if [ -L "$started_file" ] || [ ! -f "$started_file" ]; then
            fail "正式天气烟测 receipt 状态不完整，必须人工核对"
            return 1
        fi
        if [ -L "$completed_file" ] || [ ! -f "$completed_file" ]; then
            fail "同一冻结 commit 与配置已有 started 天气烟测 receipt；禁止自动重试，请人工核对上游计数与数据库"
            return 1
        fi
        if ! verify_completed_budget_receipt \
            "$started_file" \
            "$completed_file" \
            "$binding_file"; then
            fail "正式天气烟测 completed receipt 缺少可信预算差值"
            return 1
        fi
        snapshot_id="$(receipt_value "$completed_file" snapshot_id || true)"
        verify_fresh_qweather_snapshot "$snapshot_id"
        FORMAL_SMOKE_RECEIPT_REUSE_CANDIDATE=1
        log "生产变更前已识别可复核的 completed 天气烟测 receipt，无需预占新租约"
        return 0
    fi

    prepare_formal_smoke_token_material
    verify_formal_smoke_ticket_path_available
    write_formal_smoke_lease_journal
    reserve_formal_smoke_cycle_lease
    FORMAL_SMOKE_LEASE_RESERVED=1
    log "正式天气烟测全局租约已在生产变更前预占"
}

issue_formal_smoke_ticket() {
    verify_formal_smoke_ticket_path_available
    write_durable_marker \
        "$FORMAL_SMOKE_TICKET" \
        "$(printf 'binding=%s\ntoken_sha256=%s\nlease_token_sha256=%s' \
            "$FORMAL_SMOKE_BINDING" \
            "$FORMAL_SMOKE_TOKEN_SHA256" \
            "$FORMAL_SMOKE_LEASE_TOKEN_SHA256")"
    "$CHOWN_BIN" "root:$RUNTIME_GROUP" "$FORMAL_SMOKE_TICKET"
    chmod 0640 "$FORMAL_SMOKE_TICKET"
    fsync_directory "$STATE_DIR/run"
}

revoke_formal_smoke_ticket() {
    [ -n "$FORMAL_SMOKE_TICKET" ] || return 0
    if [ -L "$FORMAL_SMOKE_TICKET" ]; then
        fail "正式天气烟测一次性票据被替换为符号链接"
        return 1
    fi
    if [ -f "$FORMAL_SMOKE_TICKET" ]; then
        rm -f -- "$FORMAL_SMOKE_TICKET"
        fsync_directory "$STATE_DIR/run"
    fi
}

prepare_formal_smoke_receipt() {
    local budget_before_file="$1"
    local binding_file started_file completed_file snapshot_id now budget_fields
    local active_fingerprint
    active_fingerprint="$(compute_formal_release_config_fingerprint "$ENV_FILE")"
    if [[ ! "$active_fingerprint" =~ ^[0-9a-f]{64}$ ]] \
        || [ "$active_fingerprint" != "$FORMAL_RELEASE_CONFIG_FINGERPRINT" ]; then
        fail "正式发布配置指纹在租约预占后发生变化"
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
        if [ "$FORMAL_SMOKE_RECEIPT_REUSE_CANDIDATE" != 1 ]; then
            fail "正式天气烟测 receipt 在生产变更后意外出现"
            return 1
        fi
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
            if ! verify_completed_budget_receipt \
                "$started_file" \
                "$completed_file" \
                "$binding_file"; then
                fail "正式天气烟测 completed receipt 缺少可信预算差值"
                return 1
            fi
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
    if [ "$FORMAL_SMOKE_RECEIPT_REUSE_CANDIDATE" = 1 ]; then
        fail "生产变更前识别的 completed 天气烟测 receipt 已消失"
        return 1
    fi
    if [ "$FORMAL_SMOKE_LEASE_RESERVED" != 1 ]; then
        fail "正式天气烟测缺少生产变更前预占的全局租约"
        return 1
    fi
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    budget_fields="$(budget_snapshot_started_fields "$budget_before_file")" || {
        fail "正式天气烟测预算前值无法写入 receipt"
        return 1
    }
    verify_formal_smoke_ticket_path_available
    # started 前只允许本事务 token 原子续租，避免慢迁移耗尽预占窗口。
    renew_formal_smoke_cycle_lease
    # 续租成功后先耐久标记不可逆边界，覆盖 receipt 任一步写入失败。
    record_forward_only_phase formal-smoke-started
    FORMAL_SMOKE_IRREVERSIBLE=1
    # 不可逆边界已经耐久，journal 不再具备自动回收资格，立即收口避免误释放。
    remove_formal_smoke_lease_journal "$FORMAL_SMOKE_LEASE_JOURNAL"
    # 租约已续回完整窗口；这里将同一 token 绑定到不可重试 receipt。
    mkdir "$FORMAL_SMOKE_RECEIPT_DIR"
    chmod 0700 "$FORMAL_SMOKE_RECEIPT_DIR"
    fsync_directory "$FORMAL_SMOKE_RECEIPT_ROOT"
    write_durable_marker \
        "$binding_file" \
        "$(printf 'release_commit=%s\nconfig_fingerprint=%s' \
            "$FORMAL_RELEASE_COMMIT" \
            "$FORMAL_RELEASE_CONFIG_FINGERPRINT")"
    write_durable_marker \
        "$started_file" \
        "$(printf 'started_at=%s\nformal_smoke_binding=%s\nformal_smoke_token_sha256=%s\nformal_smoke_lease_token_sha256=%s\n%s' \
            "$now" \
            "$FORMAL_SMOKE_BINDING" \
            "$FORMAL_SMOKE_TOKEN_SHA256" \
            "$FORMAL_SMOKE_LEASE_TOKEN_SHA256" \
            "$budget_fields")"
    # started receipt 完整落盘后，才允许打开唯一一次正式天气出网窗口。
    fsync_directory "$FORMAL_SMOKE_RECEIPT_DIR"
    fsync_directory "$FORMAL_SMOKE_RECEIPT_ROOT"
    fsync_directory "$STATE_DIR/deployments"
    # started receipt 已经不可变落盘，之后才签发唯一一次运行票据。
    issue_formal_smoke_ticket
    "$SYNC_BIN"
}

complete_formal_smoke_receipt() {
    local snapshot_id="$1"
    local budget_delta_fields="$2"
    local completed_file="$FORMAL_SMOKE_RECEIPT_DIR/completed"
    write_durable_marker \
        "$completed_file" \
        "$(printf 'snapshot_id=%s\ncompleted_at=%s\n%s' \
            "$snapshot_id" \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            "$budget_delta_fields")"
    fsync_directory "$FORMAL_SMOKE_RECEIPT_DIR"
    fsync_directory "$FORMAL_SMOKE_RECEIPT_ROOT"
    "$SYNC_BIN"
}

run_formal_cache_smoke() {
    local previous_snapshot current_snapshot budget_delta_fields
    local gate_open_status=0 smoke_status=0 gate_close_status=0 ticket_revoke_status=0
    local budget_before_file="$TRANSACTION_DIR/qweather-budget-before.json"
    local budget_after_file="$TRANSACTION_DIR/qweather-budget-after.json"
    [ "$REQUIRE_WECHAT_READY" = 1 ] || return 0
    preflight_formal_qweather_jwt_runtime
    capture_qweather_budget_snapshot "$budget_before_file"
    prepare_formal_smoke_receipt "$budget_before_file"
    if [ "$FORMAL_SMOKE_REUSED" = 1 ]; then
        return 0
    fi
    previous_snapshot="$(latest_snapshot_id)"
    # started receipt 已落盘。从这里开始即使请求结果未知，也只允许向前恢复。
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
            runtime_exec "$ENV_BIN" \
                "CASE_WEATHER_FORMAL_SMOKE_TOKEN=$FORMAL_SMOKE_TOKEN" \
                "CASE_WEATHER_FORMAL_SMOKE_BINDING=$FORMAL_SMOKE_BINDING" \
                "CASE_WEATHER_FORMAL_SMOKE_TICKET=$FORMAL_SMOKE_TICKET" \
                "CASE_WEATHER_FORMAL_SMOKE_LEASE_TOKEN=$FORMAL_SMOKE_LEASE_TOKEN" \
                /bin/bash scripts/weather_cache_sync.sh --skip-nowcast
        ) || smoke_status=$?
    fi
    # 无论子进程是否走到消费逻辑，都撤销磁盘票据，禁止激活事务自动重试。
    revoke_formal_smoke_ticket || ticket_revoke_status=$?
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
    if [ "$ticket_revoke_status" -ne 0 ]; then
        fail "唯一一次天气同步烟测票据未能安全撤销"
        return "$ticket_revoke_status"
    fi
    if [ "$gate_close_status" -ne 0 ]; then
        fail "天气烟测结束后未能恢复 30 分钟出网保护"
        return "$gate_close_status"
    fi
    capture_qweather_budget_snapshot "$budget_after_file"
    if ! budget_delta_fields="$(
        compare_qweather_budget_snapshots "$budget_before_file" "$budget_after_file"
    )"; then
        fail "正式天气烟测预算差值异常；必须由 now、7d 与 weatheralert 三项各增加 1 次"
        return 1
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
    complete_formal_smoke_receipt "$current_snapshot" "$budget_delta_fields"
    write_durable_marker \
        "$TRANSACTION_DIR/CACHE_SMOKE_VERIFIED" \
        "$(printf 'snapshot_id=%s\nmode=new_request' "$current_snapshot")"
    log "唯一一次天气同步烟测与持久化快照校验通过"
}

start_new_release() {
    "$SYSTEMCTL_BIN" enable case-weather.service
    # 公网启动阶段标记先耐久落盘，覆盖 restart 后到 COMMITTED 之间的进程崩溃。
    record_forward_only_phase public-service-start
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
    verify_no_retired_processes
    verify_weather_sync_lock_quiescent
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
    case " $on_success " in
        *" case-weather-cache.timer "*) ;;
        *) fail "天气缓存服务缺少 recurring timer OnSuccess"; return 1 ;;
    esac
    on_success="$($SYSTEMCTL_BIN show case-weather-cache.service --property=OnFailure --value)"
    case " $on_success " in
        *" case-weather-cache.timer "*) ;;
        *) fail "天气缓存服务缺少 recurring timer OnFailure"; return 1 ;;
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
    log "发布后服务、两阶段 30 分钟天气 timer、OnSuccess、OnFailure、链接与健康检查全部通过"
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
        $1 == wanted && $2 == "1" && ($4 == "active" || $4 == "activating" || $4 == "reloading" || $4 == "deactivating") { found = 1 }
        END { exit(found ? 0 : 1) }
    ' "$STATE_FILE"
}

captured_unit_running() {
    local wanted="$1"
    awk -F '\t' -v wanted="$wanted" '
        $1 == wanted && $2 == "1" && ($4 == "active" || $4 == "activating" || $4 == "reloading" || $4 == "deactivating") { found = 1 }
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
    local owner_uid owner_gid original_mode extra
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
        read -r owner_uid owner_gid original_mode extra < "$ENV_METADATA" \
            || return 1
        [ -z "${extra:-}" ] || return 1
        [[ "$owner_uid" =~ ^[0-9]+$ ]] || return 1
        [[ "$owner_gid" =~ ^[0-9]+$ ]] || return 1
        case "$original_mode" in
            600|640) ;;
            *) return 1 ;;
        esac
        "$CHOWN_BIN" "$owner_uid:$owner_gid" "$ENV_FILE" || return 1
        chmod "$original_mode" "$ENV_FILE" || return 1
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

restore_activation_guard_dropins() {
    local unit directory_existed file_existed directory_uid directory_gid
    local directory_mode temporary_name directory dropin backup temporary
    local removed_dir="$TRANSACTION_DIR/activation-guard-dropins-from-failed-release"
    [ "$ACTIVATION_GUARD_DROPINS_MUTATED" -eq 1 ] || return 0
    mkdir -p "$removed_dir"
    while IFS=$'\t' read -r \
        unit \
        directory_existed \
        file_existed \
        directory_uid \
        directory_gid \
        directory_mode \
        temporary_name; do
        case "$unit" in
            *[!A-Za-z0-9_.@-]*|'') return 1 ;;
        esac
        case "$directory_existed:$file_existed" in
            0:0|1:0|1:1) ;;
            *) return 1 ;;
        esac
        if [ "$temporary_name" != ".$ACTIVATION_GUARD_DROPIN_NAME.next" ]; then
            return 1
        fi
        if [ "$directory_existed" = 1 ]; then
            case "$directory_uid:$directory_gid:$directory_mode" in
                *[!0-9:]*|*::*|:*) return 1 ;;
            esac
            case "$directory_mode" in
                0[0-7][0-7][0-7]) ;;
                *) return 1 ;;
            esac
        elif [ "$directory_uid:$directory_gid:$directory_mode" != "-:-:-" ]; then
            return 1
        fi
        directory="$UNIT_DIR/$unit.d"
        dropin="$directory/$ACTIVATION_GUARD_DROPIN_NAME"
        backup="$ACTIVATION_GUARD_DROPIN_BACKUP_DIR/$unit"
        temporary="$directory/$temporary_name"
        if [ -e "$temporary" ] || [ -L "$temporary" ]; then
            if ! "$VENV_DIR/bin/python" - \
                "$temporary" \
                "$directory" <<'PY'
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
directory = Path(sys.argv[2]).resolve(strict=True)
file_stat = path.lstat()
if (
    not stat.S_ISREG(file_stat.st_mode)
    or stat.S_ISLNK(file_stat.st_mode)
    or path.parent.resolve(strict=True) != directory
):
    raise SystemExit(1)
PY
            then
                return 1
            fi
            mv "$temporary" "$removed_dir/$unit.temporary" || return 1
        fi
        if [ "$file_existed" = 1 ]; then
            [ -f "$backup" ] && [ ! -L "$backup" ] || return 1
            cp -a "$backup" "$directory/.$ACTIVATION_GUARD_DROPIN_NAME.restore.$$" \
                || return 1
            mv -f \
                "$directory/.$ACTIVATION_GUARD_DROPIN_NAME.restore.$$" \
                "$dropin" \
                || return 1
        elif [ -e "$dropin" ] || [ -L "$dropin" ]; then
            mv "$dropin" "$removed_dir/$unit" || return 1
        fi
        if [ "$directory_existed" = 0 ]; then
            rmdir "$directory" || return 1
        elif [ -d "$directory" ] && [ ! -L "$directory" ]; then
            "$CHOWN_BIN" "$directory_uid:$directory_gid" "$directory" || return 1
            chmod "$directory_mode" "$directory" || return 1
            fsync_directory "$directory" || return 1
        else
            return 1
        fi
    done < "$ACTIVATION_GUARD_DROPIN_STATE"
    "$SYSTEMCTL_BIN" daemon-reload || return 1
    fsync_directory "$UNIT_DIR" || return 1
    log "已恢复部署前 systemd 断电保护 drop-in"
}

restore_unit_states() {
    local unit exists enabled active
    while IFS=$'\t' read -r unit exists enabled active; do
        [ "$exists" = 1 ] || continue
        case "$enabled" in
            enabled) "$SYSTEMCTL_BIN" enable "$unit" >/dev/null || return 1 ;;
            enabled-runtime) "$SYSTEMCTL_BIN" enable --runtime "$unit" >/dev/null || return 1 ;;
            disabled) "$SYSTEMCTL_BIN" disable "$unit" >/dev/null || return 1 ;;
            static) : ;;
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
            static) : ;;
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
    local failed=0 runtime_permit_revoke_status=0
    log "激活失败，开始恢复部署前状态"
    set +e
    if [ "$RUNTIME_QUIESCE_STARTED" -eq 0 ]; then
        # 只触碰过 backup timer 时，不停止公网服务或其他调度。
        recover_qweather_key_before_mutation || failed=1
        if [ "$failed" -eq 0 ]; then
            restore_activation_guard_dropins || failed=1
        fi
        if [ "$failed" -eq 0 ]; then
            restore_backup_timer_state_only || failed=1
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
            revoke_or_quarantine_runtime_activation_permit "$TRANSACTION_DIR" \
                || runtime_permit_revoke_status=$?
            {
                echo '发布在运行时静默前失败，backup timer 未能完整恢复。'
                echo "事务目录: $TRANSACTION_DIR"
                echo '公网应用与其他调度未被停止，请人工核对备份调度。'
                if [ "$runtime_permit_revoke_status" -ne 0 ]; then
                    echo '运行期开机许可未能可靠撤销，请保持全部业务单元停止。'
                fi
            } > "$FAILURE_MARKER"
            return 1
        fi
        log "已恢复发布前的 backup timer，公网服务未中断"
        return 0
    fi
    if ! stop_units_best_effort; then
        failed=1
    fi
    # 私钥必须先回到 root-only 事务归档，之后才允许恢复任何旧单元。
    recover_qweather_key_for_rollback || failed=1
    if [ "$failed" -eq 0 ]; then
        restore_database || failed=1
        restore_backup_runtime_environment || failed=1
        restore_environment || failed=1
        restore_current_link || failed=1
        if [ "$UNITS_MUTATED" -eq 1 ]; then
            restore_unit_files || failed=1
        fi
        restore_activation_guard_dropins || failed=1
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
        revoke_or_quarantine_runtime_activation_permit "$TRANSACTION_DIR" \
            || runtime_permit_revoke_status=$?
        stop_units_best_effort >/dev/null 2>&1 || true
        {
            echo '自动回滚未完整成功。全部业务单元已尽力停止。'
            echo "事务目录: $TRANSACTION_DIR"
            echo '请人工核对数据库、current 链接和 systemd unit 后再启动服务。'
            if [ "$runtime_permit_revoke_status" -ne 0 ]; then
                echo '运行期开机许可未能可靠撤销，禁止尝试启动业务单元。'
            fi
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
    local forward_ledger_status=0
    local forward_sync_status=0
    local marker_status=0 forward_marker_status=0 pre_mutation_recovery_status=0
    local marker_payload
    trap - EXIT INT TERM HUP
    stop_candidate_release
    archive_backup_validation_artifacts || true
    if [ "$rc" -eq 0 ]; then
        exit 0
    fi
    if [ -d "$TRANSACTION_DIR" ] && [ ! -L "$TRANSACTION_DIR" ]; then
        transaction_requires_forward_only "$TRANSACTION_DIR" \
            || forward_marker_status=$?
        case "$forward_marker_status" in
            0) FORWARD_ONLY=1 ;;
            1) ;;
            *)
                # 损坏或歧义的阶段证据不得触发回滚，保留开机门并进入停机确认流程。
                FORWARD_ONLY=1
                ;;
        esac
    fi
    release_reversible_formal_smoke_cycle_lease || true
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
        revoke_or_quarantine_runtime_activation_permit "$TRANSACTION_DIR" \
            || forward_quiesce_status=1
        write_current_release_ledger "$NEW_RELEASE" || forward_ledger_status=$?
        if [ "$forward_ledger_status" -eq 0 ]; then
            durably_sync_release_state forward || forward_sync_status=$?
        fi
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
            if [ "$forward_ledger_status" -ne 0 ]; then
                echo 'current-release 账本未能与保留的新 current 链接对齐。'
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
            || [ "$forward_ledger_status" -ne 0 ] \
            || [ "$forward_sync_status" -ne 0 ] \
            || [ "$marker_status" -ne 0 ]; then
            exit 70
        fi
        exit "$rc"
    fi
    if [ "$MUTATION_STARTED" -eq 0 ]; then
        if [ "$QWEATHER_KEY_TRANSITION_REQUIRED" -eq 1 ]; then
            recover_qweather_key_before_mutation || pre_mutation_recovery_status=$?
        fi
        if [ "$pre_mutation_recovery_status" -eq 0 ] \
            && [ -d "$TRANSACTION_DIR" ] \
            && [ ! -L "$TRANSACTION_DIR" ] \
            && [ ! -e "$ROLLED_BACK_MARKER" ] \
            && [ ! -L "$ROLLED_BACK_MARKER" ]; then
            write_durable_marker "$ROLLED_BACK_MARKER" pre-mutation \
                || pre_mutation_recovery_status=$?
        fi
        if [ "$pre_mutation_recovery_status" -ne 0 ]; then
            marker_payload="$(
                echo '生产变更尚未开始，但预检事务未能安全收口。'
                echo "事务目录: $TRANSACTION_DIR"
                echo '如存在 QWeather pending 私钥，请保持 pending/final 文件原状并核对身份、所有者与权限。'
            )"
            write_durable_marker "$FAILURE_MARKER" "$marker_payload" \
                || exit 70
            exit 70
        fi
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
if [ -n "$QWEATHER_PENDING_KEY_PATH" ]; then
    validate_absolute_path QWEATHER_PENDING_KEY_PATH "$QWEATHER_PENDING_KEY_PATH"
fi
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
case "$DEPLOY_INTENT" in
    web_backend_only|wechat_formal) ;;
    *) echo 'DEPLOY_INTENT 必须是 web_backend_only 或 wechat_formal' >&2; exit 2 ;;
esac
case "$EXPECTED_WECHAT_FORMAL_RUNTIME" in
    0|1) ;;
    *)
        echo 'EXPECTED_WECHAT_FORMAL_RUNTIME 必须显式设置为 0 或 1' >&2
        exit 2
        ;;
esac
case "$EXPECTED_WEB_PRIVATE_FEATURES_ENABLED" in
    0|1) ;;
    *)
        echo 'EXPECTED_WEB_PRIVATE_FEATURES_ENABLED 必须显式设置为 0 或 1' >&2
        exit 2
        ;;
esac
if [[ ! "$EXPECTED_RELEASE_BRANCH" =~ ^(main|codex/[A-Za-z0-9._/-]+)$ ]] \
    || [[ "$EXPECTED_RELEASE_BRANCH" == *".."* ]]; then
    echo 'EXPECTED_RELEASE_BRANCH 只能是 main 或安全的 codex/* 分支' >&2
    exit 2
fi
case "$QWEATHER_KEY_TRANSITION_FAIL_AT" in
    ''|after-plan|before-promotion|after-link|after-pending-unlink|after-permissions|during-directory-restore|cleanup|after-plan-cleanup|after-link-cleanup|after-pending-unlink-cleanup|after-permissions-cleanup) ;;
    *) echo 'QWEATHER_KEY_TRANSITION_FAIL_AT 测试故障点无效' >&2; exit 2 ;;
esac
if [ -n "$QWEATHER_KEY_TRANSITION_FAIL_AT" ] \
    && [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" != 1 ]; then
    echo '生产激活禁止注入 QWeather 私钥转换故障' >&2
    exit 2
fi
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
if [ -n "$QWEATHER_BUDGET_SNAPSHOT_HELPER" ]; then
    if [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" != 1 ]; then
        echo '生产激活禁止覆盖 QWeather 预算快照实现' >&2
        exit 2
    fi
    validate_absolute_path QWEATHER_BUDGET_SNAPSHOT_HELPER "$QWEATHER_BUDGET_SNAPSHOT_HELPER"
    require_executable "$QWEATHER_BUDGET_SNAPSHOT_HELPER"
fi
if [ -n "$FORMAL_SMOKE_LEASE_HELPER" ]; then
    if [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" != 1 ]; then
        echo '生产激活禁止覆盖正式天气烟测租约实现' >&2
        exit 2
    fi
    validate_absolute_path FORMAL_SMOKE_LEASE_HELPER "$FORMAL_SMOKE_LEASE_HELPER"
    require_executable "$FORMAL_SMOKE_LEASE_HELPER"
fi
if [ -n "$RUNTIME_LOG_BOUNDARY_TEST_HELPER" ]; then
    if [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" != 1 ]; then
        echo '生产激活禁止覆盖 Nginx 运行态日志守卫' >&2
        exit 2
    fi
    validate_absolute_path \
        RUNTIME_LOG_BOUNDARY_TEST_HELPER \
        "$RUNTIME_LOG_BOUNDARY_TEST_HELPER"
    require_executable "$RUNTIME_LOG_BOUNDARY_TEST_HELPER"
elif [ "$REQUIRE_WECHAT_READY" = 1 ] \
    && [ "$ALLOW_NONROOT_TEST_RUNTIME_GUARD" = 1 ]; then
    echo '非 root 正式发布测试必须显式提供 Nginx 日志守卫 helper' >&2
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
verify_effective_runtime_gate
validate_release_dependencies
validate_release_identity
validate_model_artifacts
validate_release_proofs
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

# CAS 必须处于部署锁内，并先于事务目录、状态快照与全部发布 mutation。
verify_candidate_base_state
# Redis 后端必须在活动与候选环境间保持同一有效身份，避免两套 timer 各自持锁。
verify_effective_redis_backend_identity
# 部署锁内先安全回收可识别的可逆中断租约，再处理历史事务确认。
recover_abandoned_formal_smoke_lease_journals

acknowledge_recovery_transaction
recover_activation_boot_guard_if_acknowledged
detect_unfinished_transactions
mkdir -p "$TRANSACTION_DIR"
fsync_directory "$TRANSACTION_ROOT"
capture_previous_state
durably_checkpoint_recovery_materials captured-state
prepare_qweather_key_transition_plan
qweather_key_fault after-plan
validate_backup_database_config
DATABASE_FILE="$(resolve_database_file "$STAGED_ENV_FILE")"
validate_absolute_path DATABASE_FILE "$DATABASE_FILE"
validate_managed_backup_database_path
preflight_root_crontab
verify_backup_not_running
verify_root_crontab_retired_before_activation
preflight_formal_smoke_cycle_lease
# Nginx reload 也属于生产 mutation，必须晚于全局天气租约预检。
verify_formal_runtime_log_boundary
write_durable_marker "$STARTED_MARKER" "$NEW_RELEASE"

MUTATION_STARTED=1
install_activation_guard_dropins
prepare_activation_boot_guard
stop_units_strictly
promote_qweather_key_after_quiesce
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

start_candidate_release base

LINK_MUTATED=1
switch_current_link "$NEW_RELEASE"
install_new_units
prepare_release_timer_states
validate_managed_backup_service
validate_installed_backup_service
verify_pre_request_quiescence
run_formal_cache_smoke
if [ "$REQUIRE_WECHAT_READY" = 1 ]; then
    # 正式快照生成并持久化后，再验证依赖该快照的天气与风险页面。
    start_candidate_release weather
fi
arm_qweather_network_gate
start_new_release

write_current_release_ledger "$NEW_RELEASE"
# 私钥与目录身份先验证为可提交态；失败由 durable forward-only 路径停机并保留开机门。
reconcile_qweather_key_plan "$TRANSACTION_DIR" committed
COMMITTED=1
start_release_timers
verify_release_state
observe_post_commit_stability
durably_sync_release_state commit
write_durable_marker "$TRANSACTION_DIR/COMMITTED" success
remove_activation_boot_guard "$TRANSACTION_DIR"
log "发布已提交: $NEW_RELEASE"
log "运维提示：后续只清理临时 preflight/activate 单元，禁止停止或禁用天气缓存 timer"
