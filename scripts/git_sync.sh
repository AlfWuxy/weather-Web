#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Not a git repository: $ROOT"
  exit 1
fi

branch=$(git symbolic-ref --quiet --short HEAD || true)
if [ -z "$branch" ]; then
  echo "Detached HEAD. Please checkout a branch."
  exit 1
fi

has_changes=0
if ! git diff --quiet; then
  has_changes=1
fi
if ! git diff --cached --quiet; then
  has_changes=1
fi
if [ -n "$(git ls-files --others --exclude-standard)" ]; then
  has_changes=1
fi

if [ "$has_changes" -eq 1 ]; then
  git add -A
  msg=${1:-"sync: $(date '+%Y-%m-%d %H:%M:%S')"}
  git commit -m "$msg" || true
else
  echo "No local changes to commit."
fi

git pull --rebase origin "$branch"
git push origin "$branch"
