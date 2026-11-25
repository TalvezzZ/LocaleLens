#!/bin/bash
# 把 GitHub Actions 采集的数据同步回本地。
#
# 数据存在仓库的 data 分支里（main 分支只有代码）。
# 这个脚本把 data 分支的内容解包到当前目录，覆盖 raw/ data/ derived/ digest/ health/。
#
# 用法：bash scripts/sync-data.sh

set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT" || exit 1

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "✗ 当前目录不是 git 仓库"
  exit 1
fi

echo "→ 拉取 data 分支…"
if ! git fetch --depth=1 origin data 2>/dev/null; then
  echo "✗ 拉取失败。检查：git remote -v 是否指向正确仓库"
  exit 1
fi

echo "→ 解包到本地…"
# 先备份现有本地数据（避免被覆盖后无法找回）
STAMP=$(date '+%Y%m%d-%H%M%S')
if [ -d data ] && [ -n "$(ls -A data 2>/dev/null)" ]; then
  BACKUP="../LocaleLens-data-backup-$STAMP"
  mkdir -p "$BACKUP"
  for d in raw data derived digest health; do
    [ -d "$d" ] && cp -R "$d" "$BACKUP/" 2>/dev/null
  done
  echo "  已备份到 $BACKUP"
fi

git archive FETCH_HEAD | tar -x -C .

echo "✓ 同步完成"
echo
du -sh raw data derived digest health 2>/dev/null | sed 's/^/  /'
echo
echo "最近的数据文件："
find data -name '*.jsonl' -newermt '-7 days' 2>/dev/null | sort | tail -8 | sed 's/^/  /'
