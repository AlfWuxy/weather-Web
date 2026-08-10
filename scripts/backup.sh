#!/bin/bash
# 数据库自动备份脚本
# 每天保留30天的备份
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# 项目目录只由脚本位置或显式变量决定，外置 ENV_FILE 不应改变数据库根目录。
PROJECT_DIR="${PROJECT_DIR:-$ROOT_DIR}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
DEFAULT_DB_FILE="${DEFAULT_DB_FILE:-$PROJECT_DIR/instance/health_weather.db}"
BACKUP_RUNTIME_USER="${BACKUP_RUNTIME_USER:-}"
RUNUSER_BIN="${RUNUSER_BIN:-runuser}"
SQLITE3_BIN="${SQLITE3_BIN:-sqlite3}"
MKTEMP_BIN="${MKTEMP_BIN:-mktemp}"
INSTALL_BIN="${INSTALL_BIN:-install}"
BACKUP_PRUNE="${BACKUP_PRUNE:-1}"
BACKUP_DATABASE_FILE="${BACKUP_DATABASE_FILE:-}"
DB_FILE="$DEFAULT_DB_FILE"
DATE="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/health_weather_$DATE.db"
STAGING_FILE=""

cleanup_staging_file() {
    if [ -n "$STAGING_FILE" ] \
        && [[ "$STAGING_FILE" == /tmp/case-weather-backup.* ]] \
        && [ -f "$STAGING_FILE" ] \
        && [ ! -L "$STAGING_FILE" ]; then
        rm -f -- "$STAGING_FILE"
    fi
}

trap cleanup_staging_file EXIT

trim_whitespace() {
    local value="${1:-}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

normalize_env_value() {
    local value
    local remainder
    value="$(trim_whitespace "${1:-}")"

    case "$value" in
        \"*)
            value="${value#\"}"
            [[ "$value" == *\"* ]] || return 1
            remainder="${value#*\"}"
            value="${value%%\"*}"
            ;;
        \'*)
            value="${value#\'}"
            [[ "$value" == *\'* ]] || return 1
            remainder="${value#*\'}"
            value="${value%%\'*}"
            ;;
        *)
            value="${value%%#*}"
            remainder=""
            ;;
    esac

    remainder="$(trim_whitespace "$remainder")"
    if [ -n "$remainder" ] && [[ "$remainder" != \#* ]]; then
        return 1
    fi
    trim_whitespace "$value"
}

load_database_uri() {
    local line
    local key
    local value
    local found=0

    # 显式环境变量优先于配置文件。
    if [ -n "${DATABASE_URI:-}" ]; then
        return 0
    fi
    [ -f "$ENV_FILE" ] || return 1

    while IFS= read -r line || [ -n "$line" ]; do
        line="$(trim_whitespace "$line")"
        case "$line" in
            ''|\#*) continue ;;
        esac
        [[ "$line" == *=* ]] || continue
        key="$(trim_whitespace "${line%%=*}")"
        key="${key#export }"
        key="$(trim_whitespace "$key")"
        [ "$key" = "DATABASE_URI" ] || continue
        if ! value="$(normalize_env_value "${line#*=}")"; then
            echo "DATABASE_URI 格式无效: 引号或行尾内容不完整" >&2
            return 2
        fi
        if [ -z "$value" ]; then
            echo "DATABASE_URI 已配置但值为空" >&2
            return 2
        fi
        found=$((found + 1))
        if [ "$found" -gt 1 ]; then
            echo "DATABASE_URI 重复配置，拒绝猜测日备份数据库" >&2
            return 2
        fi
        DATABASE_URI="$value"
    done < "$ENV_FILE"
    [ "$found" -eq 1 ]
}

parse_sqlite_path() {
    local uri
    local path=""
    local parent_dir
    local file_name
    uri="$(trim_whitespace "${1:-}")"

    case "$uri" in
        sqlite+pysqlite:///*) path="${uri#sqlite+pysqlite:///}" ;;
        sqlite:///*) path="${uri#sqlite:///}" ;;
        *)
            echo "仅支持 sqlite 或 sqlite+pysqlite DATABASE_URI: $uri" >&2
            return 2
            ;;
    esac

    path="${path%%\?*}"
    if [ -z "$path" ] || [ "$path" = ":memory:" ]; then
        echo "DATABASE_URI 未指向可备份的 SQLite 文件" >&2
        return 2
    fi
    if [[ "$path" != /* ]]; then
        if [[ "$path" == */* || "$path" == *\\* ]]; then
            path="$PROJECT_DIR/$path"
        else
            path="$PROJECT_DIR/instance/$path"
        fi
    fi

    # 发布目录的 instance/storage 可能是持久化软链接，统一输出真实父目录便于审计与校验。
    parent_dir="$(dirname "$path")"
    file_name="$(basename "$path")"
    if [ -d "$parent_dir" ]; then
        parent_dir="$(cd "$parent_dir" && pwd -P)"
        path="${parent_dir%/}/$file_name"
    fi
    printf '%s\n' "$path"
}

usage() {
    echo "用法: $0 [--if-present]" >&2
}

create_sqlite_backup() {
    local sqlite3_path="$1"
    local runuser_path=""
    local mktemp_path=""
    local install_path=""

    if [ -z "$BACKUP_RUNTIME_USER" ]; then
        "$sqlite3_path" -readonly "$DB_FILE" ".backup '$BACKUP_FILE'"
        return 0
    fi
    if [ "$(id -u)" -ne 0 ]; then
        echo "BACKUP_RUNTIME_USER 仅允许 root 管理的备份服务使用" >&2
        return 4
    fi
    if [[ ! "$BACKUP_RUNTIME_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
        echo "BACKUP_RUNTIME_USER 格式无效" >&2
        return 4
    fi
    runuser_path="$(command -v "$RUNUSER_BIN")" || {
        echo "缺少 runuser 命令，无法切换到数据库运行账户" >&2
        return 127
    }
    mktemp_path="$(command -v "$MKTEMP_BIN")" || {
        echo "缺少 mktemp 命令，无法创建隔离备份暂存文件" >&2
        return 127
    }
    install_path="$(command -v "$INSTALL_BIN")" || {
        echo "缺少 install 命令，无法固化 root 备份文件" >&2
        return 127
    }

    # 运行账户可在自己的 WAL 目录创建必要 sidecar；备份正文只落到 PrivateTmp。
    STAGING_FILE="$(
        "$runuser_path" -u "$BACKUP_RUNTIME_USER" -- \
            "$mktemp_path" /tmp/case-weather-backup.XXXXXX
    )"
    if [[ "$STAGING_FILE" != /tmp/case-weather-backup.* ]] \
        || [[ "$STAGING_FILE" == *$'\n'* ]] \
        || [ ! -f "$STAGING_FILE" ] \
        || [ -L "$STAGING_FILE" ]; then
        echo "隔离备份暂存文件校验失败" >&2
        return 4
    fi
    "$runuser_path" -u "$BACKUP_RUNTIME_USER" -- \
        "$sqlite3_path" -readonly "$DB_FILE" ".backup '$STAGING_FILE'"
    "$install_path" -m 0600 "$STAGING_FILE" "$BACKUP_FILE"
    cleanup_staging_file
    STAGING_FILE=""
}

main() {
    local if_present=0
    local load_status=0
    local parse_status=0
    local parsed_path=""

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --if-present) if_present=1 ;;
            -h|--help)
                usage
                return 0
                ;;
            *)
                echo "未知参数: $1" >&2
                usage
                return 2
                ;;
        esac
        shift
    done

    case "$BACKUP_PRUNE" in
        0|1) ;;
        *)
            echo "BACKUP_PRUNE 必须是 0 或 1" >&2
            return 2
            ;;
    esac

    if [ -n "$BACKUP_RUNTIME_USER" ] && [ -z "$BACKUP_DATABASE_FILE" ]; then
        echo "托管备份必须显式提供 BACKUP_DATABASE_FILE，拒绝回退猜测数据库" >&2
        return 2
    fi

    if [ -n "$BACKUP_DATABASE_FILE" ]; then
        if [[ "$BACKUP_DATABASE_FILE" != /* ]] \
            || [[ "$BACKUP_DATABASE_FILE" == *$'\n'* ]]; then
            echo "BACKUP_DATABASE_FILE 必须是安全的绝对路径" >&2
            return 2
        fi
        DB_FILE="$BACKUP_DATABASE_FILE"
    elif load_database_uri; then
        if parsed_path="$(parse_sqlite_path "$DATABASE_URI")"; then
            DB_FILE="$parsed_path"
        else
            parse_status=$?
            return "$parse_status"
        fi
    else
        load_status=$?
        if [ "$load_status" -ne 1 ]; then
            return "$load_status"
        fi
    fi

    if [ ! -f "$DB_FILE" ]; then
        if [ "$if_present" -eq 1 ]; then
            echo "未发现源数据库，按 --if-present 跳过备份: $DB_FILE"
            return 0
        fi
        echo "源数据库不存在，拒绝创建空备份: $DB_FILE" >&2
        return 3
    fi

    command -v "$SQLITE3_BIN" >/dev/null 2>&1 || {
        echo "缺少 sqlite3 命令，无法执行备份" >&2
        return 127
    }
    command -v gzip >/dev/null 2>&1 || {
        echo "缺少 gzip 命令，无法压缩备份" >&2
        return 127
    }

    # 创建备份目录
    mkdir -p "$BACKUP_DIR"

    # 使用 SQLite 在线备份保证一致性，源连接始终保持只读。
    create_sqlite_backup "$(command -v "$SQLITE3_BIN")"

    # 压缩备份
    gzip "$BACKUP_FILE"

    echo "[$(date)] 备份完成: ${BACKUP_FILE}.gz"

    # 正式日备份保留 30 天；发布验证目录禁止触碰正式保留集。
    if [ "$BACKUP_PRUNE" -eq 1 ]; then
        find "$BACKUP_DIR" \
            -maxdepth 1 \
            -type f \
            -name "health_weather_*.db.gz" \
            -mtime +30 \
            -delete
    fi

    # 显示当前备份列表
    echo "当前备份文件:"
    ls -lh "$BACKUP_DIR"/*.gz 2>/dev/null | tail -5
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
