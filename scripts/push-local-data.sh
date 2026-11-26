#!/bin/bash
# 把本地采集的数据（统计局）推送到 GitHub 的 data 分支。
#
# 为什么需要：*.hangzhou.gov.cn 在 GitHub runner 被阻断，统计局只能本地跑。
# 而这些数据目前只存在本机 —— 机器坏了就永久丢失。
# 这个脚本把它们推到 data 分支，跟云端采集的数据合在一起。
#
# 合并策略（重要）：
#   不是用本地数据整体覆盖远端，而是「把本地文件叠加到远端已有的基础上」，
#   这样云端跑出来的 tmsf / POI 数据不会被本地的旧版本覆盖掉。
#
# 用法：
#   bash scripts/push-local-data.sh              # 推送
#   bash scripts/push-local-data.sh --dry-run    # 只看会新增/修改什么

set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT" || exit 1

DRY_RUN=false
[ "${1:-}" = "--dry-run" ] && DRY_RUN=true

# 只有这些源由本地负责，其余不动
LOCAL_SOURCES=("hangzhou.stats" "hangzhou.stats.bulletin")

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "✗ 当前目录不是 git 仓库"
  exit 1
fi

echo "→ 拉取 data 分支…"
if ! git fetch --depth=1 origin data 2>/dev/null; then
  echo "✗ 拉取失败，检查网络"
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

git archive FETCH_HEAD | tar -x -C "$TMP"

echo "→ 比对本地与远端（仅本地负责的源）…"
NEW=0
CHANGED=0
UNCHANGED=0

for src in "${LOCAL_SOURCES[@]}"; do
  for base in raw data derived digest; do
    [ -d "$base/$src" ] || continue
    while IFS= read -r f; do
      rel="${f}"
      if [ ! -e "$TMP/$rel" ]; then
        NEW=$((NEW + 1))
        echo "  + $rel"
      elif ! cmp -s "$f" "$TMP/$rel"; then
        CHANGED=$((CHANGED + 1))
        echo "  ~ $rel"
      else
        UNCHANGED=$((UNCHANGED + 1))
      fi
    done < <(find "$base/$src" -type f 2>/dev/null)
  done
done

echo
echo "  新增: $NEW   修改: $CHANGED   未变: $UNCHANGED"

if [ "$NEW" -eq 0 ] && [ "$CHANGED" -eq 0 ]; then
  echo "  ○ 本地没有新数据，无需推送"
  exit 0
fi

if [ "$DRY_RUN" = true ]; then
  echo
  echo "（--dry-run，未做任何改动）"
  exit 0
fi

# ---- 在远端基础上叠加本地数据 ----
echo
echo "→ 构建提交…"
cd "$TMP"
git init -q
git remote add origin "https://x-access-token:$(gh auth token)@github.com/$(git -C "$PROJECT" remote get-url origin | sed 's|.*github.com[:/]||;s|\.git$||').git"
git fetch -q --depth=1 origin data
git reset -q --hard FETCH_HEAD

for src in "${LOCAL_SOURCES[@]}"; do
  for base in raw data derived digest; do
    [ -d "$PROJECT/$base/$src" ] || continue
    mkdir -p "$base/$src"
    cp -R "$PROJECT/$base/$src/." "$base/$src/" 2>/dev/null
  done
done

git add -A
if git diff --cached --quiet; then
  echo "  ○ 远端已是这些内容，无需提交"
  exit 0
fi

git -c user.name="locale-lens-local" \
    -c user.email="local@users.noreply.github.com" \
    commit -q -m "data(local): 统计局 $(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M')"
git push -q origin HEAD:data

echo
echo "✓ 已推送到 data 分支"
echo
echo "远端现在包含的源："
git ls-tree -d --name-only HEAD data/ | sed 's|data/||' | sed 's/^/  /'