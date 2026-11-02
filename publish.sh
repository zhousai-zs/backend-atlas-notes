#!/usr/bin/env bash
# 用法: ./publish.sh [提交说明]   重新生成站点并推送；无改动则直接退出
set -euo pipefail
cd "$(dirname "$0")"
python3 build.py
git add -A
if git diff --cached --quiet; then echo "无改动，跳过推送"; exit 0; fi
git commit -qm "${1:-update: $(date '+%F %T')}"
git push -q origin main
echo "已推送: https://zhousai-zs.github.io/backend-atlas-notes/"
