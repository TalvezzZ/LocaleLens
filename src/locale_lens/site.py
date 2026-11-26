"""把本地 JSONL 数据生成为静态 HTML 页面。

设计取向：**档案纸 + 勘误标记**，不是 SaaS 看板。
核心不是「好看」，而是「每个数字都能被追问」。

四条设计红线：
  1. null 必须有自己的视觉（虚线框），不能是空白，也不能是 0
  2. 「源未运行」「抓取失败」「确认无数据」必须是三种不同措辞
  3. 累计值 / 当月值必须带角标，不许裸奔
  4. 时间切换**按数据块各自的真实可用期**，不做统一日期选择器 ——
     房地产只有 2 天、月报有 16 期、POI 只有 1 个快照，
     用一个"看起来能选 30 天"的控件会系统性撒谎。

产出：site/index.html（单文件，无外部依赖，可离线打开）
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from . import DATA, DERIVED, ROOT, TZ, load_sources, now
from .store import available_days, health_summary, read_day

OUT = ROOT / "site"

DISTRICTS = [
    "余杭", "富阳", "萧山", "临安", "淳安", "桐庐",
    "拱墅", "西湖", "建德", "钱塘区", "滨江", "上城",
]

POI_CATS = ["餐饮门店数", "咖啡厅门店数", "便利店门店数", "购物中心门店数"]

# 月报里要在页面上展示的指标（key, 显示名, 口径角标）
MONTHLY_PICKS = [
    ("国内贸易·社会消费品零售总额", "社会消费品零售总额", "当月累计"),
    ("价格指数·居民消费价格指数·当月同比", "CPI 当月同比", "指数"),
    ("价格指数·新建商品住宅价格指数·当月同比", "新房价格指数", "指数"),
    ("价格指数·二手住宅价格指数·当月同比", "二手住宅价格指数", "指数"),
    ("人民生活·全市居民人均可支配收入", "人均可支配收入", "累计"),
]

BULLETIN_PICKS = [
    ("地区生产总值", "地区生产总值", "亿元"),
    ("常住人口", "常住人口", "万人"),
    ("居民人均可支配收入", "人均可支配收入", "元"),
    ("社会消费品零售总额", "社会消费品零售总额", "亿元"),
]


def _fmt(v, unit: str = "") -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, (int, float)):
        return f"{v:,}" if abs(v) >= 1000 else f"{v}"
    return str(v)


def _svg_line(
    points: list[tuple[str, float]],
    *,
    width: int = 520,
    height: int = 130,
    baseline: float | None = None,
    label: str = "",
) -> str:
    """极简折线。纯 SVG，无外部库。

    少于 3 个点不画线，直接说明数据不足 —— 不把两个点连成"趋势"。
    """
    if len(points) < 3:
        return (
            f'<div class="nodata">{label}：仅 {len(points)} 个期间，'
            "不足以构成趋势。继续积累后再看。</div>"
        )

    vals = [v for _, v in points]
    lo = min(vals + ([baseline] if baseline is not None else []))
    hi = max(vals + ([baseline] if baseline is not None else []))
    if hi == lo:
        hi = lo + 1
    pad = (hi - lo) * 0.18
    lo, hi = lo - pad, hi + pad

    pl, pr, pt_, pb = 46, 16, 14, 26
    iw, ih = width - pl - pr, height - pt_ - pb

    def X(i: int) -> float:
        return pl + (iw * i / (len(points) - 1))

    def Y(v: float) -> float:
        return pt_ + ih * (1 - (v - lo) / (hi - lo))

    path = " ".join(
        ("M" if i == 0 else "L") + f"{X(i):.1f},{Y(v):.1f}"
        for i, (_, v) in enumerate(points)
    )
    parts = [f'<svg viewBox="0 0 {width} {height}" class="spark" role="img" aria-label="{label}">']

    if baseline is not None:
        by = Y(baseline)
        parts.append(
            f'<line x1="{pl}" y1="{by:.1f}" x2="{width - pr}" y2="{by:.1f}" '
            'stroke="var(--rule)" stroke-width="1" stroke-dasharray="3 3"/>'
        )

    # 整数级刻度，不暴露 hi/lo 的浮点原值
    span = hi - lo
    raw_step = span / 3.0 if span > 0 else 1
    mag = 10 ** math.floor(math.log10(raw_step)) if raw_step > 0 else 1
    step = mag
    for mult in (1, 2, 2.5, 5, 10):
        step = mag * mult
        if span / step <= 4:
            break

    v = math.ceil(lo / step) * step
    while v <= hi + 1e-9:
        yy = Y(v)
        if pt_ - 2 <= yy <= height - pb + 2:
            is_base = baseline is not None and abs(v - baseline) < step / 2
            cls = "axb" if is_base else "ax"
            parts.append(
                f'<text x="{pl - 6}" y="{yy + 3.5:.1f}" class="{cls}" '
                f'text-anchor="end">{v:g}</text>'
            )
            if not is_base:
                parts.append(
                    f'<line x1="{pl}" y1="{yy:.1f}" x2="{width - pr}" y2="{yy:.1f}" '
                    'stroke="var(--rule)" stroke-width="0.5" opacity="0.45"/>'
                )
        v += step

    parts.append(f'<path d="{path}" fill="none" stroke="var(--ink-2)" stroke-width="1.6"/>')

    for i, (_, val) in enumerate(points):
        parts.append(
            f'<circle cx="{X(i):.1f}" cy="{Y(val):.1f}" r="2.4" fill="var(--paper)" '
            'stroke="var(--ink-2)" stroke-width="1.3"/>'
        )

    n = len(points)
    for i in sorted({0, n // 2, n - 1}):
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        parts.append(
            f'<text x="{X(i):.1f}" y="{height - 8}" class="ax" '
            f'text-anchor="{anchor}">{points[i][0][2:]}</text>'
        )

    last = vals[-1]
    ly = Y(last) - 8
    if ly < pt_ + 9:
        ly = Y(last) + 15
    parts.append(
        f'<text x="{X(n - 1) - 4:.1f}" y="{ly:.1f}" class="axl" '
        f'text-anchor="end">{last:g}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- 采集


def collect() -> dict:
    today = now().strftime("%Y-%m-%d")

    # ---- 房地产：按天，逐日完整数据 ----
    days = available_days(source_id="tmsf.transaction_dynamic", dataset="supply_by_district")
    day_payloads = []
    for day in days:
        supply = read_day(
            source_id="tmsf.transaction_dynamic", dataset="supply_by_district", day=day
        )
        txn = read_day(
            source_id="tmsf.transaction_dynamic",
            dataset="transaction_by_district",
            day=day,
        )

        def pick(rows, indicator):
            return {
                r["region"]: r["value"]
                for r in rows
                if r.get("indicator") == indicator and r.get("value") is not None
            }

        note = next((r.get("notes") for r in supply if r.get("notes")), "")
        fetched = supply[0]["fetched_at"][:16].replace("T", " ") if supply else ""

        day_payloads.append(
            {
                "day": day,
                "note": note,
                "fetched": fetched,
                "supply": pick(supply, "新房可售住宅套数"),
                "supplyAll": pick(supply, "新房可售套数"),
                "area": pick(supply, "新房可售住宅面积"),
                "txn": pick(txn, "新房当日成交住宅套数"),
                "txnArea": pick(txn, "新房当日成交住宅面积"),
            }
        )

    # ---- 月报：按期间 ----
    monthly: list[dict] = []
    for d in available_days(source_id="hangzhou.stats.monthly", dataset="monthly_indicators"):
        monthly.extend(
            read_day(
                source_id="hangzhou.stats.monthly",
                dataset="monthly_indicators",
                day=d,
            )
        )

    m_periods = sorted(
        {str(r["period"]) for r in monthly if str(r["period"]).count("-") == 1}
    )
    m_by_period: dict[str, dict] = {}
    for r in monthly:
        p = str(r["period"])
        ind = r["indicator"]
        if any(ind == k for k, _, _ in MONTHLY_PICKS):
            m_by_period.setdefault(p, {})[ind] = {
                "v": r.get("value"),
                "u": r.get("unit") or "",
            }

    # ---- 公报：按年 ----
    bulletin: list[dict] = []
    for d in available_days(source_id="hangzhou.stats.bulletin", dataset="indicators"):
        bulletin.extend(
            read_day(source_id="hangzhou.stats.bulletin", dataset="indicators", day=d)
        )
    b_years = sorted({str(r["period"]) for r in bulletin if str(r["period"]).isdigit()})
    b_by_year: dict[str, dict] = {}
    for r in bulletin:
        y = str(r["period"])
        if any(r["indicator"] == k for k, _, _ in BULLETIN_PICKS):
            b_by_year.setdefault(y, {})[r["indicator"]] = r.get("value")

    # ---- POI ----
    poi_rows: list[dict] = []
    for d in available_days(source_id="amap.poi", dataset="store_density"):
        poi_rows.extend(read_day(source_id="amap.poi", dataset="store_density", day=d))
    poi: dict[str, dict] = {}
    poi_periods: set[str] = set()
    for r in poi_rows:
        poi.setdefault(r.get("region") or "", {})[r.get("indicator") or ""] = r.get("value")
        poi_periods.add(str(r.get("period") or ""))

    # ---- 趋势 ----
    def series(indicator: str) -> list[tuple[str, float]]:
        pts = {}
        for r in monthly:
            if r["indicator"] == indicator and r.get("value") is not None:
                p = str(r["period"])
                if p.count("-") == 1:
                    pts[p] = r["value"]
        return sorted(pts.items())

    trends = [
        ("二手住宅价格指数", series("价格指数·二手住宅价格指数·当月同比"),
         100.0, "上年同月 = 100。低于 100 表示同比下跌。"),
        ("新房价格指数", series("价格指数·新建商品住宅价格指数·当月同比"),
         100.0, "上年同月 = 100。"),
        ("社会消费品零售总额同比", series("国内贸易·社会消费品零售总额·同比"),
         0.0, "当月同比增速，已消除累计口径影响。"),
        ("CPI 当月同比", series("价格指数·居民消费价格指数·当月同比"),
         100.0, "上年同月 = 100。"),
    ]

    findings: list[dict] = []
    checks_dir = DERIVED / "checks"
    if checks_dir.exists():
        files = sorted(checks_dir.glob("*.jsonl"))
        if files:
            for line in files[-1].read_text(encoding="utf-8").splitlines():
                if line.strip():
                    findings.append(json.loads(line))

    return {
        "today": today,
        "days": day_payloads,
        "mPeriods": m_periods,
        "mData": m_by_period,
        "bYears": b_years,
        "bData": b_by_year,
        "poi": poi,
        "poiPeriods": sorted(poi_periods),
        "trends": trends,
        "health": health_summary(),
        "findings": findings,
        "recordDays": len(days),
    }


# ---------------------------------------------------------------- 渲染


def _css() -> str:
    return """
:root{
  --paper:#F7F5F0; --ink:#1C1B19; --ink-2:#5C574E; --ink-3:#8C8679;
  --rule:#D8D3C8; --band:#F2EFE8; --vermilion:#9E3B2F;
  --teal:#2F5D62; --ochre:#8A6A3B;
  --serif:"Source Han Serif SC","Songti SC","Noto Serif CJK SC",serif;
  --mono:"IBM Plex Mono","SF Mono",Menlo,monospace;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:var(--serif);font-size:15px;line-height:1.7;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1320px;margin:0 auto;padding:40px 32px 80px}
@media(min-width:1700px){.wrap{max-width:1480px}}

header{border-bottom:2px solid var(--ink);padding-bottom:14px}
.hd{display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap}
h1{margin:0;font-size:30px;font-weight:600;letter-spacing:.04em}
h1 small{font-size:14px;font-weight:400;color:var(--ink-2);margin-left:10px;letter-spacing:0}
.hd-meta{font-family:var(--mono);font-size:11.5px;color:var(--ink-3)}

/* 覆盖度 */
.cov{display:flex;align-items:center;gap:14px;margin:18px 0 10px;
  font-family:var(--mono);font-size:12px;color:var(--ink-2)}
.bar{display:flex;gap:7px}
.grp{display:flex;gap:2px}
.grp i{display:block;width:6px;height:15px;background:transparent;
  border:1px solid var(--rule)}
.grp i.on{background:var(--ink-2);border-color:var(--ink-2)}
.cov b{font-size:13px;color:var(--ink)}

section{margin:40px 0}
.sec-hd{display:flex;justify-content:space-between;align-items:flex-end;
  gap:16px;flex-wrap:wrap;border-bottom:1px solid var(--rule);padding-bottom:7px}
h2{font-size:13px;font-weight:600;letter-spacing:.18em;color:var(--ink-2);margin:0}
h2 .en{font-family:var(--mono);font-weight:400;letter-spacing:.06em;
  color:var(--ink-3);font-size:10.5px;margin-left:8px}
.lead{font-size:13px;color:var(--ink-3);margin:9px 0 16px;max-width:74ch}
.lead b{color:var(--ink-2)}

/* 时间轴：分段刻度 */
.rail{display:flex;align-items:center;gap:0;flex-wrap:wrap;
  font-family:var(--mono);font-size:11.5px;margin:0 0 4px}
.rail button{appearance:none;border:0;background:none;cursor:pointer;
  font-family:var(--mono);font-size:11.5px;color:var(--ink-3);
  padding:5px 9px 6px;border-bottom:2px solid transparent;letter-spacing:.02em}
.rail button:hover{color:var(--ink);background:var(--band)}
.rail button[aria-pressed="true"]{color:var(--ink);border-bottom-color:var(--ink);
  font-weight:600}
.rail button:focus-visible{outline:2px solid var(--teal);outline-offset:-2px}
.rail .solo{color:var(--ink-3);padding:5px 0;font-size:11.5px}
.rail-note{font-size:12px;color:var(--ink-3);margin:2px 0 18px;
  font-family:var(--mono)}
.rail-note em{font-style:normal;color:var(--vermilion)}

/* 区县栅格 */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(252px,1fr));
  border-top:1px solid var(--rule);border-left:1px solid var(--rule)}
.card{border-right:1px solid var(--rule);border-bottom:1px solid var(--rule);
  padding:15px 16px 14px}
.card-hd{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px}
.card-hd b{font-size:17px;font-weight:600;letter-spacing:.06em}
.d{font-family:var(--mono);font-size:12px}
.row{display:flex;justify-content:space-between;align-items:baseline;padding:2.5px 0;font-size:13.5px}
.row .k{color:var(--ink-2)}
.row .v{font-family:var(--mono);font-size:13.5px;font-variant-numeric:tabular-nums}
.row.sub{margin-top:6px;padding-top:6px;border-top:1px dotted var(--rule)}
.up{color:var(--teal)} .down{color:var(--ochre)} .flat{color:var(--ink-3)}
.solo-note{font-family:var(--mono);font-size:11px;color:var(--ink-3);
  border:1px solid var(--rule);padding:0 4px;border-radius:2px}
.unknown{display:inline-flex;align-items:center;justify-content:center;
  min-width:22px;padding:0 5px;border:1px dashed var(--rule);border-radius:2px;
  color:var(--ink-3);font-size:12px;cursor:help}
.intra{font-family:var(--mono);font-size:10px;color:var(--vermilion);
  border:1px solid var(--vermilion);padding:0 3px;border-radius:2px;
  margin-left:4px;vertical-align:1px}
.rail button .t{font-size:9.5px;color:var(--ink-3);opacity:.75}
.tag{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);
  border:1px solid var(--rule);padding:0 4px;border-radius:2px;margin-left:5px;
  white-space:nowrap}

/* 表格 */
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:7px 10px 7px 0;border-bottom:1px solid var(--rule);
  vertical-align:baseline}
th{font-size:11px;font-weight:600;letter-spacing:.07em;color:var(--ink-3);
  border-bottom:1px solid var(--ink-2);white-space:nowrap}
td.r,th.r{text-align:right;font-family:var(--mono);
  font-variant-numeric:tabular-nums;white-space:nowrap}
.mono{font-family:var(--mono)}

/* 趋势 */
.tr{margin:22px 0 28px;padding-bottom:16px;border-bottom:1px dotted var(--rule)}
.tr-hd{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px}
.tr-hd b{font-size:14.5px;font-weight:600}
.tr-hd span{font-family:var(--mono);font-size:11px;color:var(--ink-3)}
.spark{width:100%;height:auto;display:block}
.spark .ax{font-family:var(--mono);font-size:9.5px;fill:var(--ink-3)}
.spark .axl{font-family:var(--mono);font-size:11px;fill:var(--ink);
  paint-order:stroke;stroke:var(--paper);stroke-width:3px}
.spark .axb{font-family:var(--mono);font-size:9.5px;fill:var(--ink-2);font-weight:600}
.tr-note{font-size:12px;color:var(--ink-3);margin-top:5px}
.nodata{font-size:12.5px;color:var(--ink-3);padding:14px;
  border:1px dashed var(--rule);border-radius:2px;background:var(--band)}

/* 体检 */
.check{border-left:3px solid var(--vermilion);padding:10px 0 10px 15px;margin-bottom:14px}
.check.low{border-left-color:var(--ochre)}
.check .t{font-size:14px;font-weight:600;margin-bottom:2px}
.check .d{font-size:13px;color:var(--ink-2)}
.check .src{font-family:var(--mono);font-size:11px;color:var(--ink-3);margin-top:5px}
.sr{display:flex;justify-content:space-between;gap:12px;padding:5px 0;
  border-bottom:1px dotted var(--rule);font-size:12.5px}
.sr .n{font-family:var(--mono);font-size:11.5px;word-break:break-all}
.sr .ok{color:var(--teal);white-space:nowrap}
.sr .bad{color:var(--vermilion);white-space:nowrap}

/* 口径声明 */
.decl{margin:26px 0;padding:15px 17px;background:var(--band);
  border-left:3px solid var(--ink-2);font-size:13px;color:var(--ink-2)}
.decl b{color:var(--ink)}

footer{margin-top:52px;padding-top:14px;border-top:2px solid var(--ink);
  font-size:11.5px;color:var(--ink-3);font-family:var(--mono);line-height:1.9}

@media(max-width:560px){
  .wrap{padding:24px 18px 56px}
  h1{font-size:23px}
  /* 560px 以下才单列：680px 时 2 列仍有 318px/卡，比 643px 单卡更易扫读 */
  .grid{grid-template-columns:1fr}
  .rail{overflow-x:auto;-webkit-overflow-scrolling:touch}
  /* 覆盖度条：竖排文字可读性极差，改为上下堆叠 */
  .cov{flex-direction:column;align-items:flex-start;gap:8px}
  .cov > span{font-size:11.5px;line-height:1.6}
  .bar{gap:5px}
  .grp i{width:5px;height:13px}
  /* 卡片内数值允许换行，避免横向挤压 */
  .row{align-items:flex-start;gap:8px}
  .row .k{flex:0 0 auto;max-width:52%}
  .row .v{flex:1 1 auto;text-align:right;word-break:break-all}
  .hd-meta{font-size:10.5px}
}
@media(min-width:561px) and (max-width:820px){
  .grid{grid-template-columns:repeat(2,1fr)}
  .wrap{padding:28px 22px 64px}
  h1{font-size:26px}
  table{font-size:12.5px}
  th,td{padding:6px 8px 6px 0}
}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def render(d: dict) -> str:
    payload = {
        "days": d["days"],
        "districts": DISTRICTS,
    }
    p: list[str] = []
    a = p.append

    a('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width,initial-scale=1">')
    a("<title>杭州 · 地区观察</title>")
    a(f"<style>{_css()}</style></head><body><div class=\"wrap\">")

    # ---- 页眉 ----
    a('<header><div class="hd">')
    a("<h1>杭州<small>地区观察</small></h1>")
    a(f'<div class="hd-meta">数据日 {d["days"][-1]["day"] if d["days"] else d["today"]}'
      f'　生成 {now().strftime("%Y-%m-%d %H:%M")}</div>')
    a("</div></header>")

    # ---- 覆盖度 ----
    total = 30
    got = min(d["recordDays"], total)
    groups = "".join(
        '<span class="grp">'
        + "".join(
            f'<i class="{"on" if (g * 5 + i) < got else ""}"></i>' for i in range(5)
        )
        + "</span>"
        for g in range(6)
    )
    a('<div class="cov">')
    a(f'<div class="bar">{groups}</div>')
    a(f"<span><b>{d['recordDays']}</b> / {total} 天　"
      "满 30 天后趋势线才有意义，现在只列当期值</span>")
    a("</div>")

    # ---- 一、区县档案（可切换日） ----
    a("<section>")
    a('<div class="sec-hd"><h2>区县档案<span class="en">DISTRICT FILES</span></h2></div>')

    if d["days"]:
        day = d["days"][-1]
        a('<p class="lead">可售为<b>时点存量</b>（不可求和）；成交为<b>当日累计</b>。'
          "切换日期可对比区县变化 —— 但请先看每行的抓取时刻："
          "<b>上午的快照与下午的快照不可直接比较</b>。</p>")

        # 时间轴
        a('<div class="rail" role="group" aria-label="选择数据日">')
        if len(d["days"]) >= 2:
            for i, dp in enumerate(reversed(d["days"])):
                pressed = "true" if i == 0 else "false"
                hm = (dp["fetched"] or "")[-5:] if dp["fetched"] else ""
                intra = "·日内" if "日内" in (dp["note"] or "") else ""
                a(f'<button type="button" data-day="{dp["day"]}" '
                  f'aria-pressed="{pressed}" title="{dp["day"]} 抓取 {dp["fetched"]}">'
                  f'{dp["day"][5:]} <span class="t">{dp["day"][5:]} 抓取 {hm}</span></button>')
        else:
            a(f'<span class="solo">仅 1 个数据日（{day["day"]}）</span>')
        a("</div>")

        # 说明这天的抓取性质
        n = day["note"] or ""
        cls = "em" if "日内" in n else ""
        a(f'<div class="rail-note" id="dayNote">抓取 {day["fetched"]}　'
          f'<em class="{cls}">{n}</em></div>')

        a('<div class="grid" id="grid"></div>')
    else:
        a('<p class="lead">尚无房地产数据。</p>')
    a("</section>")

    # ---- 二、月度指标（可切换期间） ----
    a("<section>")
    a('<div class="sec-hd"><h2>月度指标<span class="en">MONTHLY</span></h2>')
    a(f'<div class="rail mono" id="mRail" role="group" aria-label="选择月度期间">')
    if d["mPeriods"]:
        for i, per in enumerate(reversed(d["mPeriods"])):
            pressed = "true" if i == 0 else "false"
            a(f'<button type="button" data-m="{per}" aria-pressed="{pressed}">{per}</button>')
    a("</div>")
    a("</div>")
    a(f'<p class="lead">共 {len(d["mPeriods"])} 个期间。角标标明口径，'
      "<b>累计与当月不可直接比较</b>。</p>")
    a('<table><thead><tr><th style="width:44%">指标</th>'
      '<th class="r" style="width:22%">数值</th>'
      '<th style="width:34%">口径</th></tr></thead><tbody id="mBody"></tbody></table>')
    a("</section>")

    # ---- 三、趋势 ----
    a("<section>")
    a('<div class="sec-hd"><h2>趋势<span class="en">TRENDS</span></h2></div>')
    usable = [t for t in d["trends"] if len(t[1]) >= 3]
    if usable:
        a('<p class="lead">只画<b>同口径序列</b>。月报累计值不连线——'
          "累计与当月混在一张图上会造出假趋势。灰虚线为持平参考线。</p>")
        for label, pts, base, note in usable:
            a('<div class="tr">')
            a(f'<div class="tr-hd"><b>{label}</b><span>{len(pts)} 个期间</span></div>')
            a(_svg_line(pts, baseline=base, label=label))
            a(f'<div class="tr-note">{note}</div>')
            a("</div>")
    else:
        a('<p class="lead">暂无可连线的同口径序列。</p>')
    a("</section>")

    # ---- 四、年度公报（可切换年） ----
    a("<section>")
    a('<div class="sec-hd"><h2>年度公报<span class="en">ANNUAL</span></h2>')
    a('<div class="rail mono" id="bRail" role="group" aria-label="选择年份">')
    for i, y in enumerate(reversed(d["bYears"])):
        pressed = "true" if i == 0 else "false"
        a(f'<button type="button" data-b="{y}" aria-pressed="{pressed}">{y}</button>')
    a("</div></div>")
    a('<p class="lead">来自统计公报原文，逐条可在数据文件 <b>notes</b> 字段回溯原句。</p>')
    a('<table><thead><tr><th style="width:44%">指标</th>'
      '<th class="r" style="width:34%">数值</th>'
      '<th style="width:22%">单位</th></tr></thead><tbody id="bBody"></tbody></table>')
    a("</section>")

    # ---- 五、商圈门店密度 ----
    a("<section>")
    a('<div class="sec-hd"><h2>商圈门店密度<span class="en">POI</span></h2></div>')
    if d["poi"]:
        only = len(d["poiPeriods"]) == 1
        a('<p class="lead">高德 POI 门店<b>存量</b>，不是工商注册数、不是实际营业数。'
          + ("仅 1 个快照，无法比较变化。" if only else "") + "</p>")
        a('<table><thead><tr><th>区县</th>'
          + "".join(f'<th class="r">{c.replace("门店数", "")}</th>' for c in POI_CATS)
          + "</tr></thead><tbody>")
        for dist in DISTRICTS:
            pr = d["poi"].get(dist, {})
            if not pr:
                continue
            cells = "".join(
                f'<td class="r">'
                + (
                    '<span class="unknown" title="达到高德接口上限，真实值未知">—</span>'
                    if pr.get(c) is None
                    else f'<span class="mono">{_fmt(pr.get(c))}</span>'
                )
                + "</td>"
                for c in POI_CATS
            )
            a(f"<tr><td>{dist}</td>{cells}</tr>")
        a("</tbody></table>")
    else:
        a('<p class="lead">尚无 POI 数据。</p>')
    a("</section>")

    # ---- 六、数据体检 ----
    a("<section>")
    a('<div class="sec-hd"><h2>数据体检<span class="en">INTEGRITY</span></h2></div>')
    a('<p class="lead">这一节只回答一个问题：哪些数字现在不能信，以及为什么。</p>')
    for f in d["findings"][:6]:
        cls = "" if f.get("severity") == "high" else " low"
        a(f'<div class="check{cls}">')
        a(f'<div class="t">{f.get("indicator","")}</div>')
        a(f'<div class="d">{f.get("message","")}</div>')
        ev = f.get("evidence_value") or ""
        if ev:
            a(f'<div class="src">原文：{ev[:110]}</div>')
        a("</div>")

    a('<div style="font-size:12.5px;letter-spacing:.1em;color:var(--ink-2);'
      'margin:24px 0 8px">源状态</div>')
    for h in d["health"]:
        fails = h["consecutive_fails"]
        name = f'{h["source_id"]} / {h["dataset"]}'
        if fails >= 3:
            a(f'<div class="sr"><span class="n">{name}</span>'
              f'<span class="bad">抓取失败 · 连续 {fails} 次</span></div>')
        elif h["last_ok"] is None:
            a(f'<div class="sr"><span class="n">{name}</span>'
              '<span class="bad">从未成功</span></div>')
        else:
            t = h["last_ok"][:16].replace("T", " ")
            extra = f'<span class="bad">　上次失败 {fails} 次</span>' if fails else ""
            a(f'<div class="sr"><span class="n">{name}</span>'
              f'<span class="ok">成功 {t}{extra}</span></div>')
    a("</section>")

    # ---- 口径声明 ----
    a('<div class="decl">')
    a("<b>本页不提供成交均价。</b> 数据源（透明售房网）只公开成交套数与面积，"
      "不公开成交金额；楼盘详情页有反爬保护。用挂牌价代替成交价会失真 —— "
      "<b>挂牌价不等于成交价</b>，这是本项目拒绝跨越的红线。"
      "可参考的替代是价格指数（已在上方趋势区展示）。")
    a("</div>")

    a("<footer>")
    a("可售套数是时点存量，不能求和，不能当流量。<br>")
    a("「—」表示未取到或未知，不等于 0。<br>")
    a("仅为个人研究用途，数据版权归原发布方"
      "（杭州市住保房管局 / 杭州市统计局 / 高德）。")
    a("</footer>")

    a("</div>")

    # ---- 客户端脚本 ----
    a("<script>")
    a("const D = " + json.dumps(payload, ensure_ascii=False) + ";")
    a(r"""
const MONTHLY_PICKS = [
  ["国内贸易·社会消费品零售总额", "社会消费品零售总额", "当月累计"],
  ["价格指数·居民消费价格指数·当月同比", "CPI 当月同比", "指数"],
  ["价格指数·新建商品住宅价格指数·当月同比", "新房价格指数", "指数"],
  ["价格指数·二手住宅价格指数·当月同比", "二手住宅价格指数", "指数"],
  ["人民生活·全市居民人均可支配收入", "人均可支配收入", "累计"],
];
const BULLETIN_PICKS = [
  ["地区生产总值", "地区生产总值", "亿元"],
  ["常住人口", "常住人口", "万人"],
  ["居民人均可支配收入", "人均可支配收入", "元"],
  ["社会消费品零售总额", "社会消费品零售总额", "亿元"],
];

const M = __MONTHLY__;
const B = __BULLETIN__;

const fmt = v => v === null || v === undefined ? null
  : (Math.abs(v) >= 1000 ? v.toLocaleString("en-US") : String(v));
const unknown = (tip) =>
  `<span class="unknown" title="${tip || ""}">—</span>`;

function daysSorted() { return D.days; }

function renderGrid(dayIdx) {
  const ds = daysSorted();
  const cur = ds[dayIdx];
  const prev = dayIdx > 0 ? ds[dayIdx - 1] : null;  // 正序索引，前一日即 dayIdx-1
  const g = document.getElementById("grid");
  if (!g) return;
  const out = [];
  for (const dist of D.districts) {
    const s = cur.supply[dist];
    const sa = cur.supplyAll[dist];
    const ar = cur.area[dist];
    const tx = cur.txn[dist];
    const ta = cur.txnArea[dist];
    const ps = prev ? prev.supply[dist] : null;
    let d = prev
      ? '<span class="unknown" title="前一日无此区县数据">—</span>'
      : '<span class="solo-note">首日</span>';
    if (s != null && ps != null) {
      const diff = s - ps;
      if (Math.abs(diff) < 1e-9) d = '<span class="flat">0</span>';
      else if (diff > 0) d = `<span class="up">↑${fmt(Math.abs(diff))}</span>`;
      else d = `<span class="down">↓${fmt(Math.abs(diff))}</span>`;
    }
    const v = x => x == null ? unknown(prev ? "该日未取到" : "无前一日可比")
                            : `<span class="num">${fmt(x)}</span>`;
    const aum = ar == null ? unknown("未取到")
      : `${fmt(Math.round(ar / 1000) / 10)}<span class="tag">万m²</span>`;
    // 日内快照标记：成交是「截至抓取时刻」的累计，
    // 上午和下午的值不可直接比较。这里必须让人看见，否则会读成"暴跌"。
    const isIntra = (cur.note || "").includes("日内");
    const intraMark = isIntra
      ? '<span class="intra" title="日内快照，非日终值。不可与其他时点直接比较">截至抓取</span>'
      : '';

    // 套均面积：真实可算，用来判断成交结构
    let avg = null;
    if (ta != null && tx != null && tx > 0) avg = ta / tx;
    const avgCell = avg == null ? unknown("当日无成交或无面积数据")
      : `${fmt(Math.round(avg * 10) / 10)}<span class="tag">m²/套</span>`;

    out.push(`<div class="card">
      <div class="card-hd"><b>${dist}</b><span class="d">${d}</span></div>
      <div class="row"><span class="k">可售住宅</span><span class="v">${v(s)}<span class="tag">套</span></span></div>
      <div class="row"><span class="k">可售面积</span><span class="v">${aum}</span></div>
      <div class="row"><span class="k">全部可售</span><span class="v">${v(sa)}<span class="tag">套</span></span></div>
      <div class="row"><span class="k">当日成交住宅${intraMark}</span><span class="v">${v(tx)}<span class="tag">套</span></span></div>
      <div class="row sub"><span class="k">套均面积</span><span class="v">${avgCell}</span></div>
    </div>`);
  }
  g.innerHTML = out.join("");
}

function renderMonthly(period) {
  const body = document.getElementById("mBody");
  if (!body) return;
  const data = M[period] || {};
  const rows = [];
  for (const [key, label, kind] of MONTHLY_PICKS) {
    const cell = data[key];
    const val = cell ? fmt(cell.v) : null;
    const unit = cell && cell.u && cell.u !== "指数" ? cell.u : "";
    rows.push(`<tr><td>${label}</td>
      <td class="r">${val == null ? unknown("该期未公布") : val}
      ${unit ? `<span class="tag">${unit}</span>` : ""}</td>
      <td><span class="tag">${kind}</span></td></tr>`);
  }
  body.innerHTML = rows.join("");
}

function renderBulletin(year) {
  const body = document.getElementById("bBody");
  if (!body) return;
  const data = B[year] || {};
  const rows = BULLETIN_PICKS.map(([key, label, unit]) => {
    const x = data[key];
    return `<tr><td>${label}</td>
      <td class="r">${x == null ? unknown("该年未公布") : fmt(x)}</td>
      <td><span class="tag">${unit}</span></td></tr>`;
  });
  body.innerHTML = rows.join("");
}

function wireRail(id, attr, cb) {
  const rail = document.getElementById(id);
  if (!rail) return;
  rail.addEventListener("click", (e) => {
    const btn = e.target.closest("button[" + attr + "]");
    if (!btn) return;
    rail.querySelectorAll("button").forEach((b) =>
      b.setAttribute("aria-pressed", b === btn ? "true" : "false"));
    cb(btn.getAttribute(attr));
  });
}

// 区县：D.days 为正序（旧→新）。界面以"最新优先"呈现，
// 所以显示索引 si 对应数据索引 (len-1-si)。两处必须一致，否则会错位一天。
const latestIdx = () => D.days.length - 1;
let dayIdx = latestIdx();
function setupDays() {
  const rail = document.querySelector('.rail [data-day]');
  if (!rail) { renderGrid(dayIdx); return; }
  const ds = D.days;
  document.querySelectorAll('[data-day]').forEach((btn) => {
    btn.addEventListener("click", () => {
      const day = btn.getAttribute("data-day");
      dayIdx = ds.findIndex((x) => x.day === day);
      if (dayIdx < 0) dayIdx = latestIdx();
      document.querySelectorAll('[data-day]').forEach((b) =>
        b.setAttribute("aria-pressed", b === btn ? "true" : "false"));
      renderGrid(dayIdx);
      const cur = ds[dayIdx];
      const noteEl = document.getElementById("dayNote");
      if (noteEl) {
        const isIntra = (cur.note || "").includes("日内");
        noteEl.innerHTML = `抓取 ${cur.fetched}　`
          + `<em class="${isIntra ? "em" : ""}">${cur.note || ""}</em>`;
      }
    });
  });
  renderGrid(dayIdx);
}

setupDays();
wireRail("mRail", "data-m", renderMonthly);
wireRail("bRail", "data-b", renderBulletin);
const firstM = document.querySelector('[data-m][aria-pressed="true"]');
if (firstM) renderMonthly(firstM.getAttribute("data-m"));
const firstB = document.querySelector('[data-b][aria-pressed="true"]');
if (firstB) renderBulletin(firstB.getAttribute("data-b"));
""".replace("__MONTHLY__", json.dumps(d["mData"], ensure_ascii=False))
       .replace("__BULLETIN__", json.dumps(d["bData"], ensure_ascii=False)))
    a("</script>")

    a("</body></html>")
    return "".join(p)


def build() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    data = collect()
    html = render(data)
    path = OUT / "index.html"
    path.write_text(html, encoding="utf-8")
    return path