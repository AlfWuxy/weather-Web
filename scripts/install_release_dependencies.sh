#!/usr/bin/env bash
# 在受限 systemd transient unit 中创建发布虚拟环境并生成依赖证据。

set -euo pipefail

if [ "$#" -ne 4 ]; then
    echo "用法: $0 <release-app> <release-venv> <metadata-dir> <expected-lock-sha>" >&2
    exit 64
fi

RELEASE_APP="$1"
RELEASE_VENV="$2"
METADATA_DIR="$3"
EXPECTED_LOCK_SHA="$4"
LOCK_FILE="$RELEASE_APP/requirements.lock"

for path in "$RELEASE_APP" "$RELEASE_VENV" "$METADATA_DIR"; do
    case "$path" in
        /*) ;;
        *)
            echo "发布依赖安装只接受绝对路径。" >&2
            exit 64
            ;;
    esac
done

case "$EXPECTED_LOCK_SHA" in
    *[!0-9a-f]*|"")
        echo "requirements.lock 预期摘要格式不合法。" >&2
        exit 64
        ;;
esac
if [ "${#EXPECTED_LOCK_SHA}" -ne 64 ]; then
    echo "requirements.lock 预期摘要长度不合法。" >&2
    exit 64
fi
if [ ! -f "$LOCK_FILE" ] || [ -L "$LOCK_FILE" ]; then
    echo "requirements.lock 缺失或不是普通文件。" >&2
    exit 64
fi
if [ -e "$RELEASE_VENV" ] || [ -L "$RELEASE_VENV" ]; then
    echo "发布虚拟环境目标已存在，拒绝覆盖。" >&2
    exit 64
fi

umask 077
ACTUAL_LOCK_SHA="$(
    python3 -c \
        'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
        "$LOCK_FILE"
)"
if [ "$ACTUAL_LOCK_SHA" != "$EXPECTED_LOCK_SHA" ]; then
    echo "requirements.lock 摘要不匹配。" >&2
    exit 65
fi

python3 -m venv "$RELEASE_VENV"
"$RELEASE_VENV/bin/python" -m pip install \
    --index-url https://pypi.org/simple \
    --no-cache-dir \
    --no-compile \
    --require-hashes \
    --only-binary=:all: \
    -r "$LOCK_FILE"

if [ ! -x "$RELEASE_VENV/bin/gunicorn" ]; then
    echo "锁定依赖安装后缺少 gunicorn。" >&2
    exit 65
fi

install -d -o root -g root -m 0700 "$METADATA_DIR"
"$RELEASE_VENV/bin/python" --version \
    > "$METADATA_DIR/python-version.txt" 2>&1
printf '%s\n' "$ACTUAL_LOCK_SHA" \
    > "$METADATA_DIR/requirements-lock.sha256"
"$RELEASE_VENV/bin/python" -m pip inspect --local \
    > "$METADATA_DIR/pip-inspect.json"
chmod 0600 \
    "$METADATA_DIR/python-version.txt" \
    "$METADATA_DIR/requirements-lock.sha256" \
    "$METADATA_DIR/pip-inspect.json"
