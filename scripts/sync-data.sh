#!/bin/bash
# 把 GitHub Actions 采集的数据同步回本地。
#
# 数据在仓库的 data 分支（main 只有代码）。
#
# 同步语义：
#   - tar 解包是「覆盖同名文件、保留多余文件」，所以本地独有的数据（如本地跑的统计局）不会丢
#   - 但同一天同一文件，云端版本会覆盖本地版本 → 所以同步前自动备份
#   - 加 --dry-run 可以先看会发生什么，不实际改动
#
# 用法：
#   bash scripts/sync-data.sh              # 同步
#   bash scripts/sync-data.sh --dry-run    # 只看差异
#   bash scripts/sync-data.sh --no-backup  # 不备份（省空间）

set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT" || exit 1

DRY_RUN=false
DO_BACKUP=true
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --no-backup) DO_BACKUP=false ;;
  esac
done

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "✗ 当前目录不是 git 仓库"
  exit 1
fi

echo "→ 拉取 data 分支…"
if ! git fetch --depth=1 origin data 2>/dev/null; then
  echo "✗ 拉取失败。检查网络，或 git remote -v 是否指向正确仓库"
  exit 1
fi

REMOTE_SHA=$(git rev-parse FETCH_HEAD)
LOCAL_MARK=".sync-state"
echo "  data 分支版本: ${REMOTE_SHA:0:8}"
if [ -f "$LOCAL_MARK" ] && [ "$(cat "$LOCAL_MARK")" = "$REMOTE_SHA" ]; then
  echo "  ○ 已是最新，无需同步"
  exit 0
fi

# ---- 差异预览 ----
echo
echo "→ 差异预览…"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git archive FETCH_HEAD | tar -x -C "$TMP"

CHANGED=0
NEW=0
for d in raw data derived digest health; do
  [ -d "$TMP/$d" ] || continue
  while IFS= read -r f; do
    rel="${f#$TMP/}"
    if [ ! -f "$rel" ]; then
      NEW=$((NEW + 1))
    elif ! cmp -s "$f" "$rel"; then
      CHANGED=$((CHANGED + 1))
    fi
  done < <(find "$TMP/$d" -type f 2>/dev/null)
done

echo "  新增文件: $NEW"
echo "  内容变化: $CHANGED"
echo "  本地独有: 保留不动（如本地跑的统计局数据）"

if [ "$NEW" -eq 0 ] && [ "$CHANGED" -eq 0 ]; then
  echo "  ○ 没有实际变化"
  [ "$DRY_RUN" = false ] && echo "$REMOTE_SHA" > "$LOCAL_MARK"
  exit 0
fi

if [ "$DRY_RUN" = true ]; then
  echo
  echo "（--dry-run，未做任何改动）"
  exit 0
fi

# ---- 备份 ----
if [ "$DO_BACKUP" = true ] && { [ -d data ] || [ -d raw ]; }; then
  STAMP=$(date '+%Y%m%d-%H%M%S')
  BACKUP="../LocaleLens-data-backup-$STAMP"
  mkdir -p "$BACKUP"
  for d in raw data derived digest health; do
    [ -d "$d" ] && cp -R "$d" "$BACKUP/" 2>/dev/null
  done
  echo
  echo "→ 已备份到 $BACKUP"
  echo "  确认没问题后可以删：rm -rf $BACKUP"
fi

# ---- 应用 ----
echo
echo "→ 解包…"
git archive FETCH_HEAD | tar -x -C .
echo "$REMOTE_SHA" > "$LOCAL_MARK"

echo
echo "✓ 同步完成"
du -sh raw data derived digest health 2>/dev/null | sed 's/^/  /'
echo
echo "最近 7 天的数据文件："
find data -name '*.jsonl' -newermt '-7 days' 2>/dev/null | sort | tail -8 | sed 's/^/  /'