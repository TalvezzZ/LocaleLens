"""透明售房网 · 列表型公示。

两个源共用同一套「表单 POST 分页」逻辑：
  presale_approval  商品房预售审批公告（新增供应的事件流）
  cancel_contract   解除合同公示（退房信号）

分页机制：页面上的表单是 POST，但实测 **POST 的 page 字段会被忽略**，
必须用 GET 查询串 `?page=N` 才生效。实测共 8 页。
日常只抓第 1 页（足够发现新增）；回填时抓全部页。
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime

from lxml import html as LH

from .. import now
from ..fetch import fetch, save_raw
from ..schema import Event

DISTRICTS = [
    "上城", "拱墅", "西湖", "滨江", "萧山", "余杭", "临平",
    "富阳", "临安", "桐庐", "淳安", "建德", "钱塘",
]


def _clean(s: str | None) -> str:
    return " ".join((s or "").split())


def _date(s: str | None) -> str | None:
    """把 '2026-11-25' 规范化；空或异常返回 None（不是 0，也不是空串）。"""
    t = _clean(s)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _region_in(text: str) -> str | None:
    for d in DISTRICTS:
        if d in text:
            return d
    return None


def _table_rows(doc, tokens: list[str]) -> tuple[list[str], list[list[str]]] | None:
    """挑出表头包含指定关键词的那张表。"""
    for tbl in doc.xpath("//table[not(ancestor::table)]"):
        rows: list[list[str]] = []
        for tr in tbl.xpath(".//tr"):
            cells = tr.xpath("./th|./td")
            if cells:
                rows.append([_clean(c.text_content()) for c in cells])
        if len(rows) < 2:
            continue
        header = " ".join(rows[0])
        if any(tok in header for tok in tokens):
            return rows[0], rows[1:]
    return None


# ---------- 基类 ----------


class _ListSource:
    source_id: str = ""
    url: str = ""
    dataset: str = ""
    event_type: str = ""
    header_tokens: list[str] = []
    max_pages: int = 8
    politeness_seconds: float = 3.0

    def run(self, backfill: bool = False) -> list[Event]:
        ts = now()
        last_page = self.max_pages if backfill else 1
        out: list[Event] = []

        for page in range(1, last_page + 1):
            page_url = f"{self.url}?page={page}"
            text, _status = fetch(page_url)
            snap = save_raw(self.source_id, self.dataset, page_url, text, ts)
            events = self._parse(text, ts, snap.snapshot_rel, snap.sha256)
            if not events:
                break
            out.extend(events)
            if page < last_page:
                time.sleep(self.politeness_seconds)
        return out

    def _base(self, ts: datetime, snapshot: str, sha256: str) -> dict:
        return {
            "source_id": self.source_id,
            "dataset": self.dataset,
            "event_type": self.event_type,
            "fetched_at": ts.isoformat(),
            "snapshot": snapshot,
            "sha256": sha256,
        }

    def _parse(self, text: str, ts: datetime, snapshot: str, sha256: str) -> list[Event]:
        raise NotImplementedError


# ---------- 预售审批公告 ----------


class TmsfPresaleApproval(_ListSource):
    source_id = "tmsf.presale_approval"
    url = "https://tmsf.com/newhouse/transactionDynamic_presaleapproval.htm"
    dataset = "presale_approval"
    event_type = "presale_approval"
    header_tokens = ["公告名称"]

    def _parse(self, text: str, ts: datetime, snapshot: str, sha256: str) -> list[Event]:
        blk = _table_rows(LH.fromstring(text), self.header_tokens)
        if blk is None:
            return []
        header, rows = blk
        base = self._base(ts, snapshot, sha256)

        out: list[Event] = []
        for r in rows:
            if len(r) < 3:
                continue
            title = _clean(r[0])
            if not title:
                continue
            region = _region_from_notice(title)
            out.append(
                Event(
                    **base,
                    event_id=title,
                    title=title,
                    region=region,
                    event_date=_date(r[1]),
                    announced_date=_date(r[2]),
                    payload={
                        "公告名称": title,
                        "开盘日期": r[1],
                        "核发日期": r[2],
                    },
                )
            )
        return out


def _region_from_notice(title: str) -> str | None:
    """'余杭房预许字（2026）第00686号' -> 余杭 ; '杭房预许字…' -> 杭州

    公告号有两种形态：房预许字（预售许可）、房现备字（现售备案）。
    """
    m = re.match(r"^(.+?)房(?:预许|现备)字", title)
    if not m:
        return None
    prefix = m.group(1)
    if prefix == "杭":
        return "杭州"
    if prefix in DISTRICTS:
        return prefix
    return None


# ---------- 解除合同公示 ----------


class TmsfCancelContract(_ListSource):
    source_id = "tmsf.cancel_contract"
    url = "https://tmsf.com/newhouse/transactionDynamic_cancel.htm"
    dataset = "cancel_contract"
    event_type = "contract_cancel"
    header_tokens = ["开发商名称", "解除合同"]

    def _parse(self, text: str, ts: datetime, snapshot: str, sha256: str) -> list[Event]:
        blk = _table_rows(LH.fromstring(text), self.header_tokens)
        if blk is None:
            return []
        header, rows = blk
        base = self._base(ts, snapshot, sha256)

        out: list[Event] = []
        for r in rows:
            if len(r) < 4:
                continue
            dev, addr, buyer, when = (_clean(x) for x in r[:4])
            if not addr:
                continue
            # 稳定 ID：同一房源同一解除日期只记一条
            eid = hashlib.sha1(f"{dev}|{addr}|{when}".encode("utf-8")).hexdigest()[:16]
            out.append(
                Event(
                    **base,
                    event_id=eid,
                    title=addr,
                    region=_region_in(addr),
                    event_date=_date(when),
                    payload={
                        "开发商": dev,
                        "坐落": addr,
                        "原购买人": buyer,  # 官网已脱敏为 ***
                        "解除时间": when,
                    },
                )
            )
        return out
