#!/bin/bash
# Quick fix script for critical issues

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

echo "======================================"
echo "Starting quick fixes"
echo "======================================"

# 1. Ensure database directory exists
echo ""
echo "[1/4] Checking database directory..."
mkdir -p storage

if [ ! -f storage/health_weather.db ] && [ -f instance/health_weather.db ]; then
    cp instance/health_weather.db storage/health_weather.db
    echo "✅ Database copied to storage/"
elif [ -f storage/health_weather.db ]; then
    echo "✅ storage/health_weather.db exists"
else
    echo "⚠️  No existing database found"
fi

# 2. Configure PAIR_TOKEN_PEPPER
echo ""
echo "[2/4] Checking PAIR_TOKEN_PEPPER..."
if [ ! -f .env ]; then
    touch .env
fi

if ! grep -q "^PAIR_TOKEN_PEPPER=" .env 2>/dev/null; then
    PEPPER=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    echo "PAIR_TOKEN_PEPPER=$PEPPER" >> .env
    echo "✅ PAIR_TOKEN_PEPPER configured"
else
    echo "✅ PAIR_TOKEN_PEPPER exists"
fi

# 3. Check SECRET_KEY
echo ""
echo "[3/4] Checking SECRET_KEY..."
if ! grep -q "^SECRET_KEY=" .env 2>/dev/null; then
    SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    echo "SECRET_KEY=$SECRET" >> .env
    echo "✅ SECRET_KEY configured"
else
    echo "✅ SECRET_KEY exists"
fi

echo ""
echo "[4/4] Verifying security config..."
python3 - <<'PY'
from pathlib import Path

def load_env(path):
    data = {}
    if not Path(path).exists():
        return data
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, val = line.split('=', 1)
        data[key.strip()] = val.strip()
    return data

env = load_env('.env')
issues = []
for key in ('SECRET_KEY', 'PAIR_TOKEN_PEPPER'):
    val = env.get(key, '')
    if not val:
        issues.append(f\"{key} missing\")
    elif len(val) < 32:
        issues.append(f\"{key} too short\")
if issues:
    print(\"⚠️  Config check issues:\", \", \".join(issues))
else:
    print(\"✅ Security config looks good\")
PY

echo ""
echo "======================================"
echo "Fixes completed!"
echo "======================================"
