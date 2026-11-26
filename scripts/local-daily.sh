#!/bin/bash
# 本地每日维护：拉取云端数据 → 采集统计局 → 推回云端。
#
# 为什么要串成一条：
#   1. 先拉取，拿到云端最新的 tmsf / POI 数据
#   2. 再采统计局（只有本机能访问 *.hangzhou.gov.cn）
#   3. 再推回，把统计局数据叠加到 data 分支
#
# 每一步都幂等，中途失败不影响已完成的步骤：
#   拉取失败 → 用本地已有数据继续采统计局
#   采集失败 → 不推送，下轮再试
#   推送失败 → 本地数据完整保留，下轮重试

set -uo pipefail

PROJECT="/Users/zhouxinping/PycharmProjects/LocaleLens"
cd "$PROJECT" || exit 1

export PATH="/Users/zhouxinping/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p logs
LOG="logs/local-daily.log"

{
  echo "================================================================"
  echo "本地每日维护开始：$(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "================================================================"

  echo
  echo "[1/3] 拉取云端数据"
  if bash scripts/sync-data.sh 2>&1; then
    echo "  ✓ 拉取完成"
  else
    echo "  ✗ 拉取失败（用本地已有数据继续）"
  fi

  echo
  echo "[2/3] 采集统计局"
  if uv run locale-lens run --source hangzhou.stats 2>&1 | tee /tmp/stats-run.log; then
    STATS_OK=0
  else
    STATS_OK=1
  fi
  if grep -q "无新增\|已是最新" /tmp/stats-run.log 2>/dev/null; then
    echo "  ○ 统计局无新增"
  elif [ "$STATS_OK" = "0" ]; then
    echo "  ✓ 统计局有更新"
  else
    echo "  ✗ 统计局采集失败"
  fi

  echo
  echo "[3/3] 推送本地数据到云端"
  if bash scripts/push-local-data.sh 2>&1; then
    echo "  ✓ 推送完成"
  else
    echo "  ✗ 推送失败（本地数据未受影响）"
  fi

  echo
  echo "结束：$(date '+%Y-%m-%d %H:%M:%S %z')"
  echo
} >> "$LOG" 2>&1

# 日志只留最近 2000 行，避免无限增长
if [ -f "$LOG" ]; then
  LINES=$(wc -l < "$LOG" | tr -d ' ')
  if [ "$LINES" -gt 2000 ]; then
    tail -1500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
fi