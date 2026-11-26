#!/bin/bash
# 本地统计局采集。
#
# 为什么要在本地跑：*.hangzhou.gov.cn 在 GitHub runner（境外 IP）被阻断，
# 本机可以正常访问。统计局数据发布后永久留档、节奏固定（月报每月 28-30 日发上月），
# 所以滞后抓取零损失，本地低频定时完全够用。
#
# 采集前会做比对校验：如果库里的最新一期已经和网站列表页一致，直接跳过，不重复拉取。

set -uo pipefail

PROJECT="/Users/zhouxinping/PycharmProjects/LocaleLens"
cd "$PROJECT" || exit 1

export PATH="/Users/zhouxinping/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p logs

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %z') 本地统计局采集开始 ==="
  uv run locale-lens run --source hangzhou.stats
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %z') 结束 (exit=$?) ==="
  echo
} >> logs/local-stats.log 2>&1