"""透明售房网 · 交易动态。

页面声明数据来源于杭州市住保房管网；该栏目为每小时统计一次，
可能有延迟，以网签备案数据为准。

页面结构（实测 2026-11）：
  可售       div.dynamicTable + div.mytableLine（滚动区，全部行都在 HTML 里）
  成交区域   div.dynamicTable + div.myFivetableLine
  成交用途   <table> + <tr><td>
  批准预售   div.dynamicTable，表头 6 列但行只有 3 列（全部 / 住宅 两个子块叠放）

抓取 4 个数据集：
  supply_by_district       新房可售（按区县，时点快照）
  transaction_by_district  新房当日成交（按区县，当日累计）
  transaction_by_type      新房当日成交（按房屋用途）
  presale                  批准预售（本月 / 本年）
"""

from __future__ import annotations

import re
from datetime import datetime

from lxml import html as LH

from .. import now
from ..fetch import fetch, save_raw
from ..schema import Observation

SOURCE_ID = "tmsf.transaction_dynamic"
BASE_URL = "https://tmsf.com/newhouse/transactionDynamic_index.htm"
CITY = "杭州"
CONFIDENCE = "official"  # 数据源为住保房管局，属官方口径


# ---------- 文本 / 数值工具 ----------


def _clean(s: str | None) -> str:
    return " ".join((s or "").split())


def _cell(el) -> str:
    return _clean(el.text_content())


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    t = _clean(s).replace(",", "").replace("，", "")
    if not t:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group()) if m else None


def _area(s: str | None) -> float | None:
    """'116.94万m²' -> 1169400.0 ; '2457.98㎡' -> 2457.98"""
    if s is None:
        return None
    t = _clean(s).replace(",", "")
    v = _num(t)
    if v is None:
        return None
    if "万" in t:
        v *= 10_000
    return v


# ---------- 区块抽取 ----------


def _block_from_dynamic(dt) -> tuple[list[str], list[list[str]]] | None:
    header = [_cell(li) for li in dt.xpath(".//ul[contains(@class,'firstLine')]/li")]
    if not header:
        return None

    rows: list[list[str]] = []
    # 样式 A：滚动区里的 div.xxxTableLine（mytableLine / myFivetableLine）
    for ln in dt.xpath(".//div[contains(@class,'tableLine')]"):
        cells = [_cell(c) for c in ln.xpath("./div")]
        if cells:
            rows.append(cells)
    # 样式 B：直接的 ul > li 行（公告、合计行等）
    if not rows:
        for ul in dt.xpath("./ul[not(contains(@class,'firstLine'))]"):
            cells = [_cell(li) for li in ul.xpath("./li")]
            if cells:
                rows.append(cells)

    if not rows:
        return None
    return header, rows


def _block_from_table(tbl) -> tuple[list[str], list[list[str]]] | None:
    rows: list[list[str]] = []
    for tr in tbl.xpath(".//tr"):
        cells = tr.xpath("./th|./td")
        if cells:
            rows.append([_cell(c) for c in cells])
    if len(rows) < 2:
        return None
    return rows[0], rows[1:]


def _iter_blocks(doc):
    """产出 (header, rows)。同时支持 div 版和 table 版。"""
    for dt in doc.xpath("//div[contains(@class,'dynamicTable')]"):
        b = _block_from_dynamic(dt)
        if b:
            yield b
    for tbl in doc.xpath("//table[not(ancestor::table)]"):
        b = _block_from_table(tbl)
        if b:
            yield b


def _classify(header: list[str]) -> str | None:
    h = " ".join(header)
    if "房屋用途" in h:
        return "transaction_by_type"
    if "可售套数" in h:
        return "supply_by_district"
    if "成交套数" in h and "区域" in h:
        return "transaction_by_district"
    if "批准预售" in h:
        return "presale"
    return None


def _indicator_for(dataset: str, col: str) -> tuple[str, str] | None:
    """列名 -> (指标名, 单位)。"""
    is_resi = "住宅" in col
    is_area = "面积" in col
    if dataset == "supply_by_district":
        base = "新房可售"
    elif dataset == "transaction_by_district":
        base = "新房当日成交"
    else:
        return None
    if is_area:
        return base + ("住宅面积" if is_resi else "面积"), "m²"
    return base + ("住宅套数" if is_resi else "套数"), "套"


def _capture_note(ts: datetime) -> str:
    """记录抓取时刻，并标明是否已接近日终。

    如果两次晚间抓取都失败，残留的会是日内快照——这个标记能让下游识别出来，
    避免把半天的数据当成全天。
    """
    tag = "日终值" if ts.hour >= 22 else "日内快照（非日终）"
    return f"抓取于 {ts.strftime('%H:%M')}，{tag}"


def _region_name(label: str) -> str | None:
    t = _clean(label)
    if not t:
        return None
    if t in {"合计", "全市", "总计"}:
        return CITY
    return t


# ---------- 适配器 ----------


class TmsfTransactionDynamic:
    source_id = SOURCE_ID

    def run(self, backfill: bool = False) -> list[Observation]:
        ts = now()
        text, _status = fetch(BASE_URL)
        snap = save_raw(SOURCE_ID, "transaction_dynamic", BASE_URL, text, ts)

        doc = LH.fromstring(text)
        obs: list[Observation] = []
        for header, rows in _iter_blocks(doc):
            dataset = _classify(header)
            if dataset is None:
                continue
            obs.extend(self._parse(dataset, header, rows, ts, snap.snapshot_rel, snap.sha256))
        return obs

    # -- 解析 --

    def _base(self, dataset: str, ts: datetime, snapshot: str, sha256: str) -> dict:
        return {
            "source_id": SOURCE_ID,
            "dataset": dataset,
            "fetched_at": ts.isoformat(),
            "source_url": BASE_URL,
            "snapshot": snapshot,
            "sha256": sha256,
            "confidence": CONFIDENCE,
        }

    def _parse(self, dataset, header, rows, ts, snapshot, sha256) -> list[Observation]:
        if dataset in {"supply_by_district", "transaction_by_district"}:
            return self._parse_district(dataset, header, rows, ts, snapshot, sha256)
        if dataset == "transaction_by_type":
            return self._parse_by_type(header, rows, ts, snapshot, sha256)
        if dataset == "presale":
            return self._parse_presale(header, rows, ts, snapshot, sha256)
        return []

    def _parse_district(self, dataset, header, rows, ts, snapshot, sha256) -> list[Observation]:
        base = self._base(dataset, ts, snapshot, sha256)
        # 当日累计成交是「一天的量」，不是「这一小时的量」：
        # period 用日期，一天内多次抓取落到同一个键，最后一次覆盖前面的 → 天然日终值。
        period = ts.strftime("%Y-%m-%d")
        out: list[Observation] = []

        for row in rows:
            if len(row) < 2:
                continue
            region = _region_name(row[0])
            if region is None:
                continue
            for idx, col in enumerate(header[1:], start=1):
                if idx >= len(row):
                    continue
                mapped = _indicator_for(dataset, col)
                if mapped is None:
                    continue
                indicator, unit = mapped
                raw = row[idx]
                out.append(
                    Observation(
                        **base,
                        region=region,
                        indicator=indicator,
                        value=_area(raw) if unit == "m²" else _num(raw),
                        unit=unit,
                        period=period,
                        period_type="day",
                        notes=_capture_note(ts),
                    )
                )
        return out

    def _parse_by_type(self, header, rows, ts, snapshot, sha256) -> list[Observation]:
        base = self._base("transaction_by_type", ts, snapshot, sha256)
        period = ts.strftime("%Y-%m-%d")
        out: list[Observation] = []

        for row in rows:
            if len(row) < 2:
                continue
            label = _clean(row[0])
            if not label or label in {"合计", "总计"}:
                continue
            for idx, col in enumerate(header[1:], start=1):
                if idx >= len(row):
                    continue
                if "面积" in col:
                    indicator, unit, parser = f"新房当日成交面积·{label}", "m²", _area
                elif "套数" in col:
                    indicator, unit, parser = f"新房当日成交套数·{label}", "套", _num
                else:
                    continue
                out.append(
                    Observation(
                        **base,
                        region=None,
                        indicator=indicator,
                        value=parser(row[idx]),
                        unit=unit,
                        period=period,
                        period_type="day",
                        notes=_capture_note(ts),
                    )
                )
        return out

    def _parse_presale(self, header, rows, ts, snapshot, sha256) -> list[Observation]:
        """表头可能是 6 列而每行只有 3 列：全部 / 住宅 两个子块纵向叠放。"""
        base = self._base("presale", ts, snapshot, sha256)
        out: list[Observation] = []

        if not rows:
            return out

        ncol = len(rows[0])
        chunks: list[tuple[list[str], list[list[str]]]] = []
        if ncol > 1 and len(header) == ncol * 2:
            size = len(rows) // 2
            chunks = [(header[:ncol], rows[:size]), (header[ncol:], rows[size:])]
        else:
            chunks = [(header, rows)]

        for cols, rs in chunks:
            for row in rs:
                if len(row) < 2:
                    continue
                label = _clean(row[0])
                if label == "本月":
                    period, ptype = ts.strftime("%Y-%m"), "month"
                elif label == "本年":
                    period, ptype = ts.strftime("%Y"), "year"
                else:
                    continue
                for idx, col in enumerate(cols[1:], start=1):
                    if idx >= len(row):
                        continue
                    if "面积" in col:
                        indicator, unit, parser = col, "m²", _area
                    elif "套数" in col:
                        indicator, unit, parser = col, "套", _num
                    else:
                        continue
                    out.append(
                        Observation(
                            **base,
                            region=CITY,
                            indicator=indicator,
                            value=parser(row[idx]),
                            unit=unit,
                            period=period,
                            period_type=ptype,
                            notes=f"批准预售累计（{label}）",
                        )
                    )
        return out
