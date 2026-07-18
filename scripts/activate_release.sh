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
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SQLITE3_BIN="${SQLITE3_BIN:-sqlite3}"
CURL_BIN="${CURL_BIN:-curl}"
FLOCK_BIN="${FLOCK_BIN:-flock}"
BUSCTL_BIN="${BUSCTL_BIN:-busctl}"
UPTIME_FILE="${UPTIME_FILE:-/proc/uptime}"
DATABASE_FILE="${DATABASE_FILE:-}"
RECOVERY_ACKNOWLEDGED_TRANSACTION="${RECOVERY_ACKNOWLEDGED_TRANSACTION:-}"

APP_DIR="$NEW_RELEASE/app"
VENV_DIR="$NEW_RELEASE/venv"
RELEASE_ID="${NEW_RELEASE##*/}"
TRANSACTION_ROOT="$STATE_DIR/backups/deploy-transactions"
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
    {
        printf 'confirmed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'confirmed_before_release=%s\n' "$NEW_RELEASE"
    } > "$RECOVERY_ACKNOWLEDGED_TRANSACTION/$RECOVERY_CONFIRMED_MARKER_NAME"
    chmod 0600 "$RECOVERY_ACKNOWLEDGED_TRANSACTION/$RECOVERY_CONFIRMED_MARKER_NAME"
    log "已登记人工恢复确认: $RECOVERY_ACKNOWLEDGED_TRANSACTION"
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
            ;;
        *)
            [ -d "$database_dir" ] || fail "外置 SQLite 目录不存在: $database_dir"
            ;;
    esac
    for suffix in '' -wal -shm; do
        if [ -e "$DATABASE_FILE$suffix" ]; then
            chmod 0600 "$DATABASE_FILE$suffix"
        fi
    done
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
    chmod 0600 "$STAGED_ENV_FILE"
    ENV_MUTATION_STARTED=1
    atomic_replace "$STAGED_ENV_FILE" "$ENV_FILE"
    chmod 0600 "$ENV_FILE"
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
    chmod 0600 "$ENV_FILE"
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
        exec "$VENV_DIR/bin/gunicorn" \
            --workers 1 \
            --bind "$CANDIDATE_BIND" \
            --timeout 60 \
            app:app
    ) > "$TRANSACTION_DIR/candidate-gunicorn.log" 2>&1 &
    CANDIDATE_PID=$!
    wait_for_health "$CANDIDATE_HEALTH_URL" "$CANDIDATE_PID"
    stop_candidate_release
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
require_file "$ENV_FILE"
require_file "$STAGED_ENV_FILE"
require_file "$APP_DIR/scripts/server_migrate.sh"
require_file "$APP_DIR/scripts/update_env_value.py"
require_executable "$VENV_DIR/bin/python"
require_executable "$VENV_DIR/bin/gunicorn"
command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1 || require_executable "$SYSTEMCTL_BIN"
command -v "$SQLITE3_BIN" >/dev/null 2>&1 || require_executable "$SQLITE3_BIN"
command -v "$CURL_BIN" >/dev/null 2>&1 || require_executable "$CURL_BIN"
command -v "$FLOCK_BIN" >/dev/null 2>&1 || require_executable "$FLOCK_BIN"
command -v "$BUSCTL_BIN" >/dev/null 2>&1 || require_executable "$BUSCTL_BIN"
require_file "$UPTIME_FILE"

mkdir -p "$RELEASE_ROOT" "$TRANSACTION_ROOT"
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

LINK_MUTATED=1
switch_current_link "$NEW_RELEASE"
install_new_units
prepare_release_timer_states
arm_qweather_network_gate
start_new_release

mkdir -p "$STATE_DIR/deployments"
printf '%s\n' "$NEW_RELEASE" > "$STATE_DIR/deployments/current-release.next.$$"
mv -f "$STATE_DIR/deployments/current-release.next.$$" "$STATE_DIR/deployments/current-release"
COMMITTED=1
start_release_timers
verify_release_state
printf '%s\n' 'success' > "$TRANSACTION_DIR/COMMITTED"
log "发布已提交: $NEW_RELEASE"
