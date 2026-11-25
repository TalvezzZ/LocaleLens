"""每日 Markdown 摘要。这是人和 AI 读这个项目的最佳入口。"""

from __future__ import annotations

import json
from pathlib import Path

from . import DERIVED, DIGEST, load_sources, now
from .store import available_days, health_summary, read_day

SUPPLY = ("tmsf.transaction_dynamic", "supply_by_district")
TXN = ("tmsf.transaction_dynamic", "transaction_by_district")
APPROVAL = ("tmsf.presale_approval", "presale_approval")
CANCEL = ("tmsf.cancel_contract", "cancel_contract")
STATS_COLUMNS = [
    ("月报", "hangzhou.stats", "monthly_report"),
    ("公报", "hangzhou.stats", "stat_bulletin"),
    ("年鉴", "hangzhou.stats", "stat_yearbook"),
]
BULLETIN_IND = ("hangzhou.stats.bulletin", "indicators")


def _latest(rows: list[dict], indicator: str) -> tuple[dict[str, float], str | None]:
    """取某指标在该日最晚时点的各区县值。"""
    items = [r for r in rows if r.get("indicator") == indicator and r.get("value") is not None]
    if not items:
        return {}, None
    latest = max(r["period"] for r in items)
    snap = {r["region"]: r["value"] for r in items if r["period"] == latest and r.get("region")}
    return snap, latest


def _prev_day(dataset: tuple[str, str], day: str) -> str | None:
    days = available_days(source_id=dataset[0], dataset=dataset[1])
    earlier = [d for d in days if d < day]
    return earlier[-1] if earlier else None


def _seen_before(source_id: str, dataset: str, day: str) -> set[str]:
    """之前各天已经记录过的 event_id，用于标出"新增"。"""
    seen: set[str] = set()
    for d in available_days(source_id=source_id, dataset=dataset):
        if d >= day:
            continue
        for r in read_day(source_id=source_id, dataset=dataset, day=d):
            eid = r.get("event_id")
            if eid:
                seen.add(eid)
    return seen


def _read_checks(day: str) -> list[dict]:
    path = DERIVED / "checks" / f"{day}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _fmt(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _delta(cur: float | None, prev: float | None) -> str:
    if cur is None or prev is None:
        return "—"
    d = cur - prev
    if abs(d) < 1e-9:
        return "0"
    sign = "+" if d > 0 else ""
    return f"{sign}{_fmt(d)}"


def build_daily(day: str) -> Path:
    city = load_sources().get("city", "杭州")
    supply_rows = read_day(source_id=SUPPLY[0], dataset=SUPPLY[1], day=day)
    txn_rows = read_day(source_id=TXN[0], dataset=TXN[1], day=day)

    prev_supply_day = _prev_day(SUPPLY, day)
    prev_supply = (
        read_day(source_id=SUPPLY[0], dataset=SUPPLY[1], day=prev_supply_day)
        if prev_supply_day
        else []
    )

    supply_now, supply_period = _latest(supply_rows, "新房可售住宅套数")
    supply_prev, _ = _latest(prev_supply, "新房可售住宅套数")
    supply_area_now, _ = _latest(supply_rows, "新房可售住宅面积")
    supply_all_now, _ = _latest(supply_rows, "新房可售套数")

    txn_cnt_now, txn_period = _latest(txn_rows, "新房当日成交住宅套数")
    txn_area_now, _ = _latest(txn_rows, "新房当日成交住宅面积")

    lines: list[str] = []
    lines.append(f"# {city} · 地区观察日报 {day}")
    lines.append("")
    lines.append(f"> 生成时间：{now().strftime('%Y-%m-%d %H:%M:%S %Z')}  ")
    lines.append("> 数据源：透明售房网 · 交易动态（数据来源：杭州市住保房管网）  ")
    supply_note = next((r.get("notes") for r in supply_rows if r.get("notes")), None)
    txn_note = next((r.get("notes") for r in txn_rows if r.get("notes")), None)
    if supply_note:
        lines.append(f"> 可售：{supply_note}  ")
    if txn_note:
        lines.append(f"> 当日成交：{txn_note}  ")
    lines.append("")

    # ---- 一、可售 ----
    lines.append("## 一、新房可售（住宅）")
    lines.append("")
    if not supply_now:
        lines.append("**本日未取到可售数据。** 请查看「源健康度」，不要当成 0。")
    else:
        prev_label = f"较 {prev_supply_day}" if prev_supply_day else "较上一观测日"
        lines.append(f"| 区县 | 可售住宅套数 | {prev_label} | 住宅面积(万m²) | 全部可售套数 |")
        lines.append("|---|---:|---:|---:|---:|")
        for region in sorted(supply_now):
            area = supply_area_now.get(region)
            lines.append(
                "| {} | {} | {} | {} | {} |".format(
                    region,
                    _fmt(supply_now.get(region)),
                    _delta(supply_now.get(region), supply_prev.get(region)),
                    _fmt(area / 10_000) if area is not None else "—",
                    _fmt(supply_all_now.get(region)),
                )
            )
    lines.append("")

    # ---- 二、成交 ----
    lines.append("## 二、当日成交（住宅）")
    lines.append("")
    if not txn_cnt_now:
        lines.append("**本日未取到成交数据。**")
    else:
        lines.append("| 区县 | 成交套数 | 成交面积(m²) |")
        lines.append("|---|---:|---:|")
        for region in sorted(txn_cnt_now):
            lines.append(
                f"| {region} | {_fmt(txn_cnt_now.get(region))} | "
                f"{_fmt(txn_area_now.get(region))} |"
            )
        total = sum(v for v in txn_cnt_now.values() if v)
        lines.append(f"| **合计（未含全市行）** | {_fmt(total)} | — |")
    lines.append("")

    # ---- 三、健康度 ----
    lines.append("## 三、源健康度")
    lines.append("")
    hs = health_summary()
    if not hs:
        lines.append("暂无健康度记录。")
    else:
        lines.append("| 源 | 数据集 | 最近成功 | 连续失败 | 失败率 |")
        lines.append("|---|---|---|---:|---:|")
        for h in hs:
            lines.append(
                "| {} | {} | {} | {} | {} |".format(
                    h["source_id"],
                    h["dataset"],
                    (h["last_ok"] or "从未")[:19],
                    h["consecutive_fails"],
                    h["fail_rate"],
                )
            )
    lines.append("")

    # ---- 四、事件 ----
    lines.append("## 四、公告与退房")
    lines.append("")

    approval = read_day(source_id=APPROVAL[0], dataset=APPROVAL[1], day=day)
    cancel = read_day(source_id=CANCEL[0], dataset=CANCEL[1], day=day)

    seen_a = _seen_before(*APPROVAL, day)
    new_a = [r for r in approval if r.get("event_id") not in seen_a]
    lines.append(f"### 预售审批公告：本日抓取 {len(approval)} 条，其中**新增 {len(new_a)} 条**")
    lines.append("")
    if approval:
        lines.append("| 区县 | 公告名称 | 核发日期 | 开盘日期 |")
        lines.append("|---|---|---|---|")
        for r in sorted(approval, key=lambda x: (x.get("announced_date") or ""), reverse=True)[:15]:
            mark = " 🆕" if r.get("event_id") not in seen_a else ""
            lines.append(
                "| {} | {}{} | {} | {} |".format(
                    r.get("region") or "—",
                    r.get("title") or "",
                    mark,
                    r.get("announced_date") or "—",
                    r.get("event_date") or "—",
                )
            )
    else:
        lines.append("本日未取到公告数据。")
    lines.append("")

    seen_c = _seen_before(*CANCEL, day)
    new_c = [r for r in cancel if r.get("event_id") not in seen_c]
    lines.append(f"### 解除合同（退房）：本日抓取 {len(cancel)} 条，其中**新增 {len(new_c)} 条**")
    lines.append("")
    if cancel:
        lines.append("| 区县 | 坐落 | 解除时间 |")
        lines.append("|---|---|---|")
        for r in sorted(cancel, key=lambda x: (x.get("event_date") or ""), reverse=True)[:20]:
            mark = " 🆕" if r.get("event_id") not in seen_c else ""
            lines.append(
                "| {} | {}{} | {} |".format(
                    r.get("region") or "—",
                    r.get("title") or "",
                    mark,
                    r.get("event_date") or "—",
                )
            )
    else:
        lines.append("本日未取到退房数据。")
    lines.append("")

    # ---- 五、统计与开放数据 ----
    lines.append("## 五、统计与开放数据")
    lines.append("")

    new_total = 0
    has_history = False
    stats_rows: list[tuple[str, dict]] = []
    for label, sid, ds in STATS_COLUMNS:
        rows = read_day(source_id=sid, dataset=ds, day=day)
        seen = _seen_before(sid, ds, day)
        if seen:
            has_history = True
        fresh = [r for r in rows if r.get("event_id") not in seen]
        new_total += len(fresh)
        stats_rows.extend((label, r) for r in fresh)

    if has_history:
        lines.append(f"### 新发布：本日新增 **{new_total}** 条")
    else:
        lines.append(f"### 新发布：首次入库 **{new_total}** 条（历史回填，不是今日新发）")
    lines.append("")
    if stats_rows:
        lines.append("| 栏目 | 标题 | 发布日期 | 原件 |")
        lines.append("|---|---|---|---|")
        for label, r in sorted(
            stats_rows, key=lambda x: (x[1].get("announced_date") or ""), reverse=True
        )[:12]:
            payload = r.get("payload") or {}
            if payload.get("pdf_path"):
                has = "已归档"
            elif payload.get("pdf_error"):
                has = f"失败：{str(payload['pdf_error'])[:20]}"
            else:
                has = "待归档"
            lines.append(
                "| {} | {} | {} | {} |".format(
                    label,
                    r.get("title") or "",
                    r.get("announced_date") or "—",
                    has,
                )
            )
    else:
        lines.append("本日无新增发布。")
    lines.append("")

    # 公报关键指标（最近三年）
    ind_rows = []
    for d in available_days(source_id=BULLETIN_IND[0], dataset=BULLETIN_IND[1]):
        ind_rows.extend(read_day(source_id=BULLETIN_IND[0], dataset=BULLETIN_IND[1], day=d))
    if ind_rows:
        periods = sorted({r["period"] for r in ind_rows})[-3:]
        by_ind: dict[str, dict[str, float]] = {}
        units: dict[str, str] = {}
        for r in ind_rows:
            by_ind.setdefault(r["indicator"], {})[r["period"]] = r["value"]
            units[r["indicator"]] = r["unit"]

        lines.append("### 统计公报关键指标")
        lines.append("")
        lines.append("| 指标 | 单位 | " + " | ".join(periods) + " |")
        lines.append("|---|---|" + "---:|" * len(periods))
        for ind in by_ind:
            cells = " | ".join(
                _fmt(by_ind[ind].get(p)) if by_ind[ind].get(p) is not None else "—"
                for p in periods
            )
            lines.append(f"| {ind} | {units[ind]} | {cells} |")
        lines.append("")
        lines.append("> 数值来自统计公报原文，`notes` 字段保存了对应原句，可逐条核查。")
        lines.append("")

    # 开放平台新上架数据集
    od_rows = read_day(source_id="hangzhou.opendata", dataset="new_datasets", day=day)
    od_seen = _seen_before("hangzhou.opendata", "new_datasets", day)
    od_fresh = [r for r in od_rows if r.get("event_id") not in od_seen]
    lines.append(f"### 开放平台新上架数据集：本日 {len(od_rows)} 条，新增 **{len(od_fresh)}** 条")
    lines.append("")
    if od_rows:
        lines.append("| 区县 | 数据集 | 提供部门 | 上架日 |")
        lines.append("|---|---|---|---|")
        for r in sorted(
            od_fresh or od_rows, key=lambda x: (x.get("event_date") or ""), reverse=True
        )[:10]:
            p = r.get("payload") or {}
            lines.append(
                "| {} | {} | {} | {} |".format(
                    r.get("region") or "—",
                    (r.get("title") or "")[:36],
                    (p.get("提供部门") or "—")[:22],
                    r.get("event_date") or "—",
                )
            )
        lines.append("")
        lines.append("> 这是「自动发现新数据源」的入口：平台上了新数据集，你会先知道。")
    else:
        lines.append("本日未取到开放平台数据。")
    lines.append("")

    # ---- 六、商圈门店密度 ----
    lines.append("## 六、商圈门店密度（高德 POI）")
    lines.append("")
    poi_rows = read_day(source_id="amap.poi", dataset="store_density", day=day)
    if poi_rows:
        cats = ["餐饮门店数", "咖啡厅门店数", "便利店门店数", "购物中心门店数"]
        regions = sorted({r["region"] for r in poi_rows if r.get("region")})
        lines.append("| 区县 | " + " | ".join(c.replace("门店数", "") for c in cats) + " |")
        lines.append("|---|" + "---:|" * len(cats))
        for reg in regions:
            cells = []
            for c in cats:
                vals = [
                    r["value"]
                    for r in poi_rows
                    if r.get("region") == reg and r.get("indicator") == c
                ]
                v = vals[0] if vals else None
                cells.append("—" if v is None else _fmt(v))
            lines.append(f"| {reg} | " + " | ".join(cells) + " |")
        lines.append("")
        unk = sum(1 for r in poi_rows if r.get("value") is None)
        lines.append(
            f"> 共 {len(poi_rows)} 项，其中 **{unk} 项为「—」（未知）**。"
            "`—` 不是 0，是高德接口返回封顶值（600/1000）后仍无法拆到可信粒度。"
        )
        lines.append(
            "> 口径：这是高德 POI 库里的门店存量，**不是工商注册数、不是实际营业数**，"
            "更不是客流或营收。只能做同类门店的区县横向对比与自身趋势。"
        )
    else:
        lines.append("本日未取到 POI 数据（可能未配置 AMAP_KEY）。")
    lines.append("")

    # ---- 七、数据检查 ----
    lines.append("## 七、口径与数据修订检查")
    lines.append("")
    findings = _read_checks(day)
    if findings:
        for f in sorted(findings, key=lambda x: -abs(x.get("diff_pct", 0))):
            lines.append(f"- **[{f.get('severity', '?')}]** {f.get('message', '')}")
            ev = f.get("evidence_value") or ""
            if ev:
                lines.append(f"  - 原文依据：{ev[:140]}")
        lines.append("")
        lines.append(
            "> 这些指标**不能直接跨年算增长率**，否则会得出错误结论。"
            "要么只用同一口径区间，要么以最新公报的反推值为基数。"
        )
    else:
        lines.append("未发现口径变更或数据修订。")
    lines.append("")
    lines.append(
        "> 检测方法：用「本期值 ÷ (1+公布增速)」反推上年基数，"
        "与库中实存值比对，差异 ≥2% 即视为该年数据被修订。"
    )
    lines.append("")

    # ---- 八、口径提醒 ----
    lines.append("## 八、口径提醒")
    lines.append("")
    lines.append("- 可售套数是**时点快照**，不能求和，也不能当流量用。")
    lines.append("- 当日成交是**抓取时点的累计值**，不是日终值；不同时点之间不可直接相加。")
    lines.append("- `—` 表示未取到，**不等于 0**。")
    lines.append("- 本页只有官方口径，未混入平台挂牌数据。")
    lines.append("")

    out = DIGEST / "daily" / f"{day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
