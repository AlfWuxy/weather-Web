#!/bin/bash
# 只解析部署脚本需要的简单 dotenv 值，保留引号内的 # 和空格。

normalize_env_value() {
    local raw="$1"
    raw="${raw#"${raw%%[![:space:]]*}"}"
    raw="${raw%"${raw##*[![:space:]]}"}"
    if [[ "$raw" =~ ^\"([^\"]*)\"[[:space:]]*(#.*)?$ ]]; then
        NORMALIZED_ENV_VALUE="${BASH_REMATCH[1]}"
        return 0
    fi
    if [[ "$raw" =~ ^\'([^\']*)\'[[:space:]]*(#.*)?$ ]]; then
        NORMALIZED_ENV_VALUE="${BASH_REMATCH[1]}"
        return 0
    fi
    if [[ "$raw" =~ ^(.*[^[:space:]])[[:space:]]+#.*$ ]]; then
        raw="${BASH_REMATCH[1]}"
    fi
    NORMALIZED_ENV_VALUE="${raw%"${raw##*[![:space:]]}"}"
}
