#!/usr/bin/env bash
# Vault 로컬 git 스냅샷. 원격 저장소는 없다 (개인 노트를 외부로 보내지 않음).
# 실수로 지우거나 Syncthing 충돌로 덮어쓴 노트를 git log / git checkout 으로 복구하기 위한 것.
set -euo pipefail

vault="${AGENT_VAULT:-$HOME/Obsidian}"
vault="${vault/#\~/$HOME}"
cd "$vault"

if [ ! -d .git ]; then
    echo "not a git repository: $vault (run: git -C \"$vault\" init)" >&2
    exit 1
fi

git add -A
if git diff --cached --quiet; then
    echo "no changes"
    exit 0
fi
git commit -q -m "auto snapshot $(date '+%Y-%m-%d %H:%M')"
git log --oneline -1 --stat | tail -1
