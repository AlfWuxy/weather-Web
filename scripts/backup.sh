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
DB_FILE="$DEFAULT_DB_FILE"
DATE="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/health_weather_$DATE.db"

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
        [ "$key" = "DATABASE_URI" ] || continue
        if ! value="$(normalize_env_value "${line#*=}")"; then
            echo "DATABASE_URI 格式无效: 引号或行尾内容不完整" >&2
            return 2
        fi
        if [ -z "$value" ]; then
            echo "DATABASE_URI 已配置但值为空" >&2
            return 2
        fi
        DATABASE_URI="$value"
        return 0
    done < "$ENV_FILE"
    return 1
}

parse_sqlite_path() {
    local uri
    local path=""
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
    printf '%s\n' "$path"
}

usage() {
    echo "用法: $0 [--if-present]" >&2
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

    if load_database_uri; then
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

    command -v sqlite3 >/dev/null 2>&1 || {
        echo "缺少 sqlite3 命令，无法执行备份" >&2
        return 127
    }
    command -v gzip >/dev/null 2>&1 || {
        echo "缺少 gzip 命令，无法压缩备份" >&2
        return 127
    }

    # 创建备份目录
    mkdir -p "$BACKUP_DIR"

    # 创建备份（使用SQLite的.backup命令保证一致性）
    sqlite3 "$DB_FILE" ".backup '$BACKUP_FILE'"

    # 压缩备份
    gzip "$BACKUP_FILE"

    echo "[$(date)] 备份完成: ${BACKUP_FILE}.gz"

    # 删除30天前的备份
    find "$BACKUP_DIR" -name "*.gz" -mtime +30 -delete

    # 显示当前备份列表
    echo "当前备份文件:"
    ls -lh "$BACKUP_DIR"/*.gz 2>/dev/null | tail -5
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
