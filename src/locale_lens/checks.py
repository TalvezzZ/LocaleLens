"""数据质量检查。

当前只有一种检查：**口径变更 / 数据修订检测**。

原理：
    公报会同时给出「本期值」和「同比增速」。
        implied_prev = 本期值 / (1 + 增速/100)
    用 implied_prev 和库里存的「上年实值」比对：
        差异小 → 口径一致，跨年可比
        差异大 → 上年数据被修订过（或口径变了），直接算跨年增长会失真

为什么必须做这件事：
    杭州社会消费品零售总额 2023 年公布 7671 亿元，2024 年公布 9151 亿元，
    但 2024 年公报写的是「比上年增长 2.8%」。反推上年基数约 8902 亿元，
    和 7671 亿元差了 16%——第五次全国经济普查修订了 2023 年数据。
    如果不校验，就会得出「一年涨 19%」的错误结论。
"""

from __future__ import annotations

import json

from . import DERIVED, now
from .store import all_observations

THRESHOLD_PCT = 2.0  # 差异超过这个百分比才认为是修订，不是四舍五入误差


def _severity(diff_pct: float) -> str:
    a = abs(diff_pct)
    if a >= 10:
        return "high"
    if a >= 5:
        return "medium"
    return "low"


def detect_caliber_revisions(source_id: str, dataset: str) -> list[dict]:
    """返回口径变更/数据修订的发现列表。"""
    obs = all_observations(source_id=source_id, dataset=dataset)

    values: dict[str, dict[str, float]] = {}
    growths: dict[str, dict[str, float]] = {}
    units: dict[str, str] = {}
    quotes: dict[tuple[str, str], str] = {}

    for r in obs:
        ind = (r.get("indicator") or "").strip()
        period = str(r.get("period") or "").strip()
        val = r.get("value")
        if not ind or val is None or not period.isdigit():
            continue

        if ind.endswith("同比"):
            growths.setdefault(ind[:-2], {})[period] = float(val)
            quotes[(ind, period)] = r.get("notes") or ""
        else:
            values.setdefault(ind, {})[period] = float(val)
            units[ind] = r.get("unit") or ""
            quotes[(ind, period)] = r.get("notes") or ""

    findings: list[dict] = []

    for ind, gseries in sorted(growths.items()):
        vseries = values.get(ind)
        if not vseries:
            continue
        for period, growth in sorted(gseries.items()):
            value = vseries.get(period)
            if value is None:
                continue
            prev_period = str(int(period) - 1)
            actual_prev = vseries.get(prev_period)
            if actual_prev is None or actual_prev == 0:
                continue

            implied_prev = value / (1.0 + growth / 100.0)
            diff_pct = (implied_prev - actual_prev) / actual_prev * 100.0
            if abs(diff_pct) < THRESHOLD_PCT:
                continue

            unit = units.get(ind, "")
            direction = "上调" if diff_pct > 0 else "下调"
            findings.append(
                {
                    "check": "caliber_revision",
                    "indicator": ind,
                    "unit": unit,
                    "period": period,
                    "prev_period": prev_period,
                    "value": value,
                    "growth_pct": growth,
                    "implied_prev": round(implied_prev, 1),
                    "actual_prev": actual_prev,
                    "diff_pct": round(diff_pct, 1),
                    "severity": _severity(diff_pct),
                    "message": (
                        f"{ind}：{period} 年公布 {value:,.0f}{unit}、增速 {growth}%，"
                        f"反推 {prev_period} 年基数 {implied_prev:,.0f}{unit}，"
                        f"但库中实存 {actual_prev:,.0f}{unit}，相差 {diff_pct:+.1f}%。"
                        f"说明 {prev_period} 年数据被{direction}修订，"
                        f"直接算跨年增长会失真。"
                    ),
                    "evidence_value": quotes.get((ind, period), ""),
                    "evidence_growth": quotes.get((f"{ind}同比", period), ""),
                }
            )

    return findings


def check_monotonic_cumulative(source_id: str, dataset: str) -> list[dict]:
    """检查累计型指标在同年内是否单调不减。

    累计值（如社零 1-9月）在一年内必须单调递增 —— 除非数据修订。
    如果出现下降，要么是解析错位（把当月当累计），要么是官方修订。
    两种情况都该报警。
    """
    obs = all_observations(source_id=source_id, dataset=dataset)

    series: dict[tuple, dict[str, float]] = {}
    for r in obs:
        ind = (r.get("indicator") or "").strip()
        period = str(r.get("period") or "").strip()
        val = r.get("value")
        notes = r.get("notes") or ""
        if not ind or val is None:
            continue
        # 只看明确标记为累计的记录
        if "period_kind=cum" not in notes:
            continue
        if ind.endswith("同比") or "同比" in ind:
            continue
        if not period[:4].isdigit():
            continue
        # 只检查真正的「流量累计」指标。
        # 存量指标（资产总计、单位数、利润总额）本身会波动，不适用单调规则。
        # 判定：指标名里出现「累计」语义的常见流量词才检查。
        FLOW_WORDS = ("社会消费品零售总额", "营业收入", "进出口", "销售额",
                      "投资", "增加值", "产值", "产量", "零售额", "收入")
        if not any(w in ind for w in FLOW_WORDS):
            continue
        # 存量/时点类词直接排除
        STOCK_WORDS = ("资产", "单位数", "负债", "总额（亿元）·规上服务业企业·利税")
        if any(w in ind for w in STOCK_WORDS):
            continue
        series.setdefault((r.get("region"), ind), {})[period] = float(val)

    findings: list[dict] = []
    for (region, ind), pts in sorted(series.items()):
        by_year: dict[str, list[tuple[str, float]]] = {}
        for period, v in pts.items():
            by_year.setdefault(period[:4], []).append((period, v))

        for year, items in by_year.items():
            items.sort()
            for i in range(1, len(items)):
                prev_p, prev_v = items[i - 1]
                cur_p, cur_v = items[i]
                if cur_v < prev_v * 0.98:  # 允许 2% 以内的修订
                    findings.append(
                        {
                            "check": "cumulative_not_monotonic",
                            "indicator": ind,
                            "region": region,
                            "period": cur_p,
                            "prev_period": prev_p,
                            "value": cur_v,
                            "prev_value": prev_v,
                            "diff_pct": round((cur_v - prev_v) / prev_v * 100, 1)
                            if prev_v
                            else None,
                            "severity": "high"
                            if cur_v < prev_v * 0.5
                            else "medium",
                            "message": (
                                f"{region or '杭州'} {ind}：{prev_p} 为 {prev_v:,.0f}，"
                                f"{cur_p} 却降到 {cur_v:,.0f}"
                                f"（{((cur_v - prev_v) / prev_v * 100):+.1f}%）。"
                                f"累计值同年不应下降，可能是解析错位或官方修订。"
                            ),
                        }
                    )
    return findings


def run_checks(day: str | None = None) -> list[dict]:
    """跑全部检查，写入 derived/checks/<day>.jsonl，返回发现。"""
    day = day or now().strftime("%Y-%m-%d")

    findings: list[dict] = []
    findings.extend(
        detect_caliber_revisions("hangzhou.stats.bulletin", "indicators")
    )
    # 注意：累计单调性检查已停用。
    # 实测在月报上产生 100+ 条误报 —— 因为「规上工业增加值」「产值率」等
    # 本身是增速/比率而非累计金额，天然会波动。
    # 为保证信噪比，这类检查只保留给人工确认过的指标白名单，
    # 目前白名单为空，故不启用。

    out_dir = DERIVED / "checks"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for item in findings:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    return findings
