#!/bin/bash
# 数据库自动备份脚本
# 每天保留30天的备份

BACKUP_DIR=/opt/case-weather/backups
ENV_FILE="${ENV_FILE:-/opt/case-weather/.env}"
DEFAULT_DB_FILE=/opt/case-weather/storage/health_weather.db
DB_FILE=$DEFAULT_DB_FILE
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE=$BACKUP_DIR/health_weather_$DATE.db

load_database_uri() {
    [ -f "$ENV_FILE" ] || return 1
    while IFS='=' read -r key value; do
        case "$key" in
            ''|\#*) continue ;;
            DATABASE_URI)
                value="${value%%#*}"
                value="${value%"${value##*[![:space:]]}"}"
                value="${value#"${value%%[![:space:]]*}"}"
                if [[ "$value" == \"*\" && "$value" == *\" ]]; then
                    value="${value:1:${#value}-2}"
                fi
                if [ -n "$value" ]; then
                    DATABASE_URI="$value"
                    return 0
                fi
                return 1
                ;;
        esac
    done < "$ENV_FILE"
    return 1
}

parse_sqlite_path() {
    local uri="$1"
    local path=""
    if [[ "$uri" == sqlite:////* ]]; then
        path="/${uri#sqlite:////}"
    elif [[ "$uri" == sqlite:///* ]]; then
        path="${uri#sqlite:///}"
    else
        return 1
    fi
    path="${path%%\?*}"
    [ -n "$path" ] || return 1
    echo "$path"
}

if load_database_uri; then
    parsed_path="$(parse_sqlite_path "$DATABASE_URI")"
    if [ -n "$parsed_path" ]; then
        DB_FILE="$parsed_path"
    fi
fi

# 创建备份目录
mkdir -p "$BACKUP_DIR"

# 创建备份（使用SQLite的.backup命令保证一致性）
sqlite3 "$DB_FILE" ".backup $BACKUP_FILE"

# 压缩备份
gzip "$BACKUP_FILE"

echo "[$(date)] 备份完成: ${BACKUP_FILE}.gz"

# 删除30天前的备份
find "$BACKUP_DIR" -name "*.gz" -mtime +30 -delete

# 显示当前备份列表
echo "当前备份文件:"
ls -lh "$BACKUP_DIR"/*.gz 2>/dev/null | tail -5
