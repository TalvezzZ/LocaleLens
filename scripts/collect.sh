#!/bin/bash
# LocaleLens 定时采集入口。
# 由 launchd 在 9/13/18/22 点调用；也可手动执行。

set -uo pipefail

PROJECT="/Users/zhouxinping/PycharmProjects/LocaleLens"
cd "$PROJECT" || exit 1

# launchd 的 PATH 很干净，显式补上 uv 和 homebrew
export PATH="/Users/zhouxinping/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p logs

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %z') 开始 ==="
  uv run locale-lens run
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %z') 结束 (exit=$?) ==="
  echo
} >> logs/collect.log 2>&1
