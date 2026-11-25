"""统计公报 → 结构化指标。

统计公报是 **HTML 正文**（不是 PDF），里面直接写着人口、收入、社零、CPI。
这里用保守正则抽取少量高价值指标，并且**把原文句子一起存下来**——
没有原文句子的数字，等于不可核查，按 SCHEMA 的口径红线不允许入库。

注意三个坑：
1. 公报里有脚注标记，如「地区生产总值[2]20059亿元」「社会消费品零售总额[4]9151亿元」
2. 「城镇居民人均可支配收入」包含子串「居民人均可支配收入」，必须用负向断言排除
3. 各年措辞略有差异（「居民消费价格（CPI）比上年上涨」vs「居民消费价格比上年上涨」）
"""

from __future__ import annotations

import re
import time

from lxml import html as LH

from .. import now, rel
from ..fetch import fetch, save_raw
from ..schema import Observation
from ..store import all_events, read_day, available_days
from . import NothingToDo

SOURCE_ID = "hangzhou.stats.bulletin"
BULLETIN_SOURCE = "hangzhou.stats"
BULLETIN_DATASET = "stat_bulletin"
DATASET = "indicators"

# 脚注标记：[2] ［2］ [ 12 ]
_FOOT = r"(?:[\[［]\s*\d+\s*[\]］])?"

PATTERNS: list[tuple[str, str, str]] = [
    ("地区生产总值", rf"地区生产总值\s*{_FOOT}\s*(\d+(?:\.\d+)?)\s*亿元", "亿元"),
    ("人均地区生产总值", r"人均地区生产总值\s*为?\s*(\d+(?:\.\d+)?)\s*元", "元"),
    (
        "居民人均可支配收入",
        r"(?<!城镇)(?<!农村)居民人均可支配收入\s*(\d+(?:\.\d+)?)\s*元",
        "元",
    ),
    ("城镇居民人均可支配收入", r"城镇居民人均可支配收入\s*(\d+(?:\.\d+)?)\s*元", "元"),
    ("农村居民人均可支配收入", r"农村居民人均可支配收入\s*(\d+(?:\.\d+)?)\s*元", "元"),
    (
        "社会消费品零售总额",
        rf"社会消费品零售总额\s*{_FOOT}\s*(\d+(?:\.\d+)?)\s*亿元",
        "亿元",
    ),
    (
        "居民消费价格指数同比",
        r"居民消费价格(?:（CPI）)?\s*比上?年\s*上涨\s*(\d+(?:\.\d+)?)\s*[%％]",
        "%",
    ),
    ("常住人口", r"常住人口\s*(\d+(?:\.\d+)?)\s*万人", "万人"),
    ("户籍人口", r"户籍人口\s*(\d+(?:\.\d+)?)\s*万人", "万人"),
    ("城镇新增就业", r"城镇新增就业\s*(\d+(?:\.\d+)?)\s*万人", "万人"),
]

YEAR_RE = re.compile(r"(\d{4})年杭州市国民经济和社会发展统计公报")

# 有些年份不写百分比，直接说「与上年持平」——那就是 0%，不是缺失。
SPECIAL_PATTERNS: list[tuple[str, str, float, str]] = [
    ("居民消费价格指数同比", r"居民消费价格与上?年持平", 0.0, "%"),
]

# 同比增速：公报里几乎每个主要指标都带「比上年增长 X%」。
# 抽出来存成 `<指标>同比`，这是口径校验的基础——
# 用「本期值 ÷ (1+增速)」反推上年基数，再和库里存的实值比对。
GROWTH_PATTERNS: list[tuple[str, str]] = [
    ("地区生产总值", r"地区生产总值[^。]{0,40}?增长\s*(\d+(?:\.\d+)?)\s*[%％]"),
    ("社会消费品零售总额", r"社会消费品零售总额[^。]{0,40}?增长\s*(\d+(?:\.\d+)?)\s*[%％]"),
    (
        "居民人均可支配收入",
        r"(?<!城镇)(?<!农村)居民人均可支配收入[^。]{0,40}?增长\s*(\d+(?:\.\d+)?)\s*[%％]",
    ),
    ("城镇居民人均可支配收入", r"城镇居民人均可支配收入[^。]{0,40}?增长\s*(\d+(?:\.\d+)?)\s*[%％]"),
    ("农村居民人均可支配收入", r"农村居民人均可支配收入[^。]{0,40}?增长\s*(\d+(?:\.\d+)?)\s*[%％]"),
]


def _sentence(text: str, m: re.Match) -> str:
    """取匹配所在的那一句，作为可核查的原文证据。"""
    start = text.rfind("。", 0, m.start()) + 1
    end = text.find("。", m.end())
    if end < 0:
        end = min(len(text), m.end() + 80)
    return text[start : end + 1].strip()[:240]


def extract(text: str) -> list[tuple[str, float, str, str]]:
    """返回 [(指标, 数值, 单位, 原文句子)]。同一指标只取第一处。"""
    out: list[tuple[str, float, str, str]] = []
    found: set[str] = set()

    for name, pattern, unit in PATTERNS:
        m = re.search(pattern, text)
        if not m:
            continue
        try:
            value = float(m.group(1))
        except (TypeError, ValueError):
            continue
        out.append((name, value, unit, _sentence(text, m)))
        found.add(name)

    for name, pattern, fixed, unit in SPECIAL_PATTERNS:
        if name in found:
            continue
        m = re.search(pattern, text)
        if not m:
            continue
        out.append((name, fixed, unit, _sentence(text, m)))
        found.add(name)

    # 同比增速（口径校验用）
    for name, pattern in GROWTH_PATTERNS:
        m = re.search(pattern, text)
        if not m:
            continue
        try:
            growth = float(m.group(1))
        except (TypeError, ValueError):
            continue
        out.append((f"{name}同比", growth, "%", _sentence(text, m)))

    return out


class HangzhouBulletinIndicators:
    """从已登记的统计公报事件里抽取结构化指标。"""

    source_id = SOURCE_ID

    def run(self, backfill: bool = False) -> list[Observation]:
        ts = now()
        events = all_events(source_id=BULLETIN_SOURCE, dataset=BULLETIN_DATASET)
        if not events:
            return []

        done = self._done_years()
        out: list[Observation] = []

        for ev in events.values():
            title = ev.get("title") or ""
            m = YEAR_RE.search(title)
            if not m:
                continue
            year = m.group(1)
            if year in done and not backfill:
                continue

            url = ev.get("url")
            if not url:
                continue

            try:
                text_html, _st = fetch(url, timeout=40)
            except Exception:  # noqa: BLE001
                continue

            snap = save_raw(SOURCE_ID, DATASET, url, text_html, ts)
            doc = LH.fromstring(text_html)
            body = " ".join(doc.text_content().split())

            for name, value, unit, quote in extract(body):
                out.append(
                    Observation(
                        source_id=SOURCE_ID,
                        dataset=DATASET,
                        region="杭州",
                        indicator=name,
                        value=value,
                        unit=unit,
                        period=year,
                        period_type="year",
                        published_at=ev.get("announced_date"),
                        fetched_at=ts.isoformat(),
                        source_url=url,
                        snapshot=rel(snap.snapshot_path),
                        sha256=snap.sha256,
                        confidence="official",
                        notes=quote,
                    )
                )

            time.sleep(3.0)

        if not out:
            raise NothingToDo("各年度公报均已提取，无新增")
        return out

    def _done_years(self) -> set[str]:
        done: set[str] = set()
        for d in available_days(source_id=SOURCE_ID, dataset=DATASET):
            for r in read_day(source_id=SOURCE_ID, dataset=DATASET, day=d):
                p = r.get("period")
                if p:
                    done.add(str(p))
        return done
