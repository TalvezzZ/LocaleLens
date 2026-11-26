"""杭州市统计局月报 PDF → 结构化指标。

为什么不用 parse-agent：WPS 生成的 PDF 文本层完整（非扫描件），
pypdf 的 layout 模式能给出列对齐的文本，纯 Python 足够，
不必引入外部服务，也让整个项目保持「一个 uv sync 就能跑」。

解析策略（按脆弱性从低到高）：
  1. 章节切分    按章节标题（国内贸易/价格指数…）切块。语义锚点，最稳。
  2. 表头合并    表头可能跨 1~2 行（如「9 月 / 上月=100」叠在「上年同期=100」下），
                  合并后再按列位置映射到数据行。
  3. 期间解析    「1-9月」= 累计、「9月」= 当月、「前三季度」= 季度累计。
                  混淆期间是这类报表最容易出的错，必须逐行判定。
  4. 行分类      无数字且非「其中」开头 → 分组标题，跳过不入库。
                  「—」→ null，不是 0。
  5. 列语义特别处理：
                 - 价格指数：值是「指数」（上年同期=100），单位空，且有三列期间
                 - 区县经济：第一列是区县名，指标名在表头里

明确不做：
  - 不推断嵌套层级（限额以上→其中→其中，缩进不可靠）
  - 不做跨页表格合并
  - 不猜测列含义：表头无法确定语义的列，直接丢弃
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

SOURCE_ID = "hangzhou.stats.monthly"
DATASET = "monthly_indicators"

# 章节标题 → (section_key, 默认单位, 数值语义)
# 数值语义：
#   level  绝对值
#   index  指数（上年同期=100），无单位
SECTIONS: dict[str, tuple[str, str, str]] = {
    "地区生产总值": ("gdp", "亿元", "level"),
    "重点产业": ("key_industry", "亿元", "level"),
    "工业": ("industry", "", "level"),
    "国内贸易": ("domestic_trade", "亿元", "level"),
    "对外贸易": ("foreign_trade", "亿元", "level"),
    "工业企业效益": ("industry_profit", "", "level"),
    "服务业企业效益": ("service_profit", "", "level"),
    "人民生活": ("living", "元", "level"),
    "价格指数": ("price_index", "", "index"),
    "区、县（市）经济": ("district_economy", "亿元", "level"),
    "财政": ("finance", "亿元", "level"),
    "金融": ("finance_bank", "亿元", "level"),
    "投资": ("investment", "", "level"),
    "房地产": ("realestate", "", "level"),
    "农业": ("agriculture", "", "level"),
}

_HEADER_HINT = ("指标", "同比", "上年同期", "上月", "累计")
_DASH_VALUES = {"—", "-", "－", "–", "——"}

# 区县名，用于识别「第一列是地区」的行
DISTRICTS = [
    "上城区", "拱墅区", "西湖区", "滨江区", "萧山区", "余杭区", "临平区",
    "富阳区", "临安区", "桐庐县", "淳安县", "建德市", "钱塘区",
]
REGION_LABELS = {"全国", "全省", "全市", "市区", "杭州"}


@dataclass
class Row:
    raw: str
    name: str
    numbers: list[str] = field(default_factory=list)
    indent: int = 0
    parent: str = ""


@dataclass
class Section:
    key: str
    title: str
    page: int
    header_lines: list[str] = field(default_factory=list)
    header_cells: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _split_row(line: str) -> Row:
    indent = len(line) - len(line.lstrip())
    parts = [p for p in re.split(r"\s{2,}", line.strip()) if p.strip()]
    if not parts:
        return Row(raw=line, name="", indent=indent)
    return Row(
        raw=line,
        name=_clean(parts[0]),
        numbers=[_clean(p) for p in parts[1:]],
        indent=indent,
    )


def _assign_parents(rows: list[Row], *, flat: bool = False) -> None:
    """用缩进建立父子关系。

    为什么必须做：同一个子项名会在不同父项下重复出现，
    例如「租赁和商务服务业」同时出现在「营业收入」和「利润总额」下面。
    不记录父级，这两个值就会被当成同一个指标，产生冲突数据。

    当 flat=True（区县/排行类表格）时不做父子推断：
    那类表格的缩进只是排版对齐，地区之间是同级关系，
    若按缩进推断会让「全市」错误地成为各区县的父级。

    父级的判定：一个行是父级，当且仅当**后面存在缩进比它更深的行**。
    不能只看「有没有数字」——「营业收入（亿元） 16358」本身带数字，
    但它下面还有「其中：…」，所以它同时也是父级。
    """
    if flat:
        for r in rows:
            r.parent = ""
            r._is_parent = False  # type: ignore[attr-defined]
        return

    for i, r in enumerate(rows):
        if not r.name:
            continue
        r.name = re.sub(r"^[*\s]+", "", r.name).strip()
        # 找下一个非空行，看缩进是否更深
        is_parent = False
        for nxt in rows[i + 1 :]:
            if not nxt.name:
                continue
            is_parent = nxt.indent > r.indent
            break

        # 「其中：」开头的行是同级列表的第一个元素，不是父级。
        # layout 模式下它的缩进常被文本长度影响（如 indent=4 vs 9），
        # 但后面跟的其实是同级条目，不是它的子项。
        if r.name.startswith("其中"):
            is_parent = False

        if is_parent:
            r.parent = ""  # 父级本身不挂父级，避免层层嵌套名过长
            r._is_parent = True  # type: ignore[attr-defined]
        else:
            # 找最近的、缩进更浅的父级
            for prev in reversed(rows[:i]):
                if prev.name and getattr(prev, "_is_parent", False) and prev.indent < r.indent:
                    r.parent = prev.name
                    break


def _is_dash(tok: str) -> bool:
    return tok.strip() in _DASH_VALUES


def _is_header_line(line: str) -> bool:
    s = _clean(line)
    if any(h in s for h in _HEADER_HINT):
        return True
    # 纯表头碎片：只有期间词、没有独立数值指标名
    if re.fullmatch(r"[指标\s]*(?:\d{1,2}\s*月|上月=\d+|上年同期=\d+|1-\d+月|同比[±%]*|[\s（）亿元元%±=]*)+", s):
        return True
    return False


def _merge_headers(lines: list[str]) -> list[str]:
    """把多行表头合并成一份列名列表。"""
    cells: list[str] = []
    for line in lines:
        for p in re.split(r"\s{2,}", line.strip()):
            p = _clean(p)
            if p:
                cells.append(p)
    return cells


def infer_period_from_content(sections: list[Section]) -> tuple[int | None, int | None]:
    """从 PDF 正文推断 (year, data_month)。

    有些 PDF 文件名是哈希（旧版链接没带 download 属性），
    必须靠内容判断。优先用「x-y月」的表头，其次用「YYYY年」。
    """
    for sec in sections:
        joined = " ".join(sec.header_cells)
        m = re.search(r"(\d{1,2})\s*[-—－]\s*(\d{1,2})\s*月", joined)
        if m:
            # 只有月，没有年 —— 年需要外部提供
            return None, int(m.group(2))
        m = re.search(r"前\s*([一二三四1-4])\s*季度", joined)
        if m:
            cn = {"一": 1, "二": 2, "三": 3, "四": 4}
            q = cn.get(m.group(1)) or int(m.group(1))
            return None, q * 3
        m = re.search(r"(20\d{2})\s*年", joined)
        if m:
            return int(m.group(1)), 12
    return None, None


def parse_pdf(path: Path) -> list[Section]:
    reader = PdfReader(str(path))
    sections: list[Section] = []
    current: Section | None = None

    for pno, page in enumerate(reader.pages):
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            text = page.extract_text() or ""

        for line in text.split("\n"):
            if not line.strip():
                continue
            stripped = _clean(line)

            hit = next((t for t in SECTIONS if stripped == t), None)
            if hit:
                current = Section(key=SECTIONS[hit][0], title=hit, page=pno + 1)
                sections.append(current)
                continue

            if current is None:
                continue

            if _is_header_line(line):
                current.header_lines.append(stripped)
                continue

            row = _split_row(line)
            if row.name:
                current.rows.append(row)
                if current.header_lines and not current.header_cells:
                    current.header_cells = _merge_headers(current.header_lines)

    for sec in sections:
        if sec.header_lines and not sec.header_cells:
            sec.header_cells = _merge_headers(sec.header_lines)
        _assign_parents(sec.rows, flat=(sec.key == "district_economy"))

    return sections


def _detect_period(header_cells: list[str], year: int, data_month: int) -> tuple[str, str, int]:
    """判定主值列对应的期间。返回 (period, period_type, 主值列下标_从1起)。

    保守策略：只有当表头能明确说清列含义时才采用；
    说不清就返回累计期间 + 第 1 列为数值，并在 notes 里标注不确定。
    """
    joined = " ".join(header_cells)

    # 情况 A0：整年数据，如「2025年（亿元）」—— 常见于年初的月报，
    # 此时放的是上一年度终值，绝不能记成当期值。
    m = re.search(r"(20\d{2})\s*年", joined)
    if m and not re.search(r"\d\s*[-—－]\s*\d\s*月", joined):
        return m.group(1), "year", 1

    # 情况 A：明确写着 x-y月（累计）
    m = re.search(r"(\d{1,2})\s*[-—－]\s*(\d{1,2})\s*月", joined)
    if m:
        return f"{year}-{int(m.group(2)):02d}", "cum", 1

    # 情况 B0：一季度 / 上半年 / 前三季度 等「自然累计期」
    if "上半年" in joined:
        return f"{year}-06", "cum", 1
    if "一季度" in joined:
        return f"{year}-03", "cum", 1
    if "下半年" in joined:
        return f"{year}-12", "cum", 1

    # 情况 B：前三季度/前x季度（「三」是中文数字，要一起认）
    qm = re.search(r"前\s*([一二三四1-4]?)\s*季度", joined)
    if qm:
        cn = {"一": 1, "二": 2, "三": 3, "四": 4}
        ch = qm.group(1) or "三"
        q = cn.get(ch) or int(ch) if ch.isdigit() else 3
        # 累计到第 q 季度末，用该季度最后一个月作为期间
        return f"{year}-{(int(q) * 3):02d}", "cum", 1

    # 情况 C：价格指数型 —— 表头是「上月=100 / 上年同期=100」叠「9月 / 1-9月」
    if "上月=100" in joined or "上年同期=100" in joined:
        # 列序：上月=100, 9月(当月), 1-9月(累计)。主值取「当月」，即第 2 列
        return f"{year}-{data_month:02d}", "month", 2

    # 情况 D：单一「N月」
    m = re.search(r"(?:^|[^\d])(\d{1,2})\s*月", joined)
    if m:
        return f"{year}-{int(m.group(1)):02d}", "month", 1

    return f"{year}-{data_month:02d}", "unknown", 1


def section_to_records(
    sec: Section,
    *,
    source_url: str,
    snapshot: str,
    sha256: str,
    fetched_at: str,
    year: int,
    data_month: int,
) -> tuple[list[dict], list[str]]:
    """把一个章节转成 L1 观测记录。返回 (记录, 警告)。"""
    unit = SECTIONS.get(sec.title, ("", "", "level"))[1]
    semantic = SECTIONS.get(sec.title, ("", "", "level"))[2]
    warnings: list[str] = []

    period, ptype, value_col = _detect_period(sec.header_cells, year, data_month)
    # period_type 只允许 SCHEMA 定义的值。
    # 累计 vs 当月的区分写进 notes（period_kind），不污染 period_type。
    if ptype == "year":
        period_type_db = "year"
    else:
        period_type_db = "month"
    hdr_text = " / ".join(sec.header_cells) if sec.header_cells else "(无表头)"
    if ptype == "unknown":
        warnings.append(f"{sec.title}：表头无法确定期间，按累计推测（{hdr_text[:40]}）")

    is_district_table = sec.key == "district_economy"
    is_price_index = semantic == "index"
    out: list[dict] = []

    for row in sec.rows:
        name = row.name.lstrip("*").strip()
        if not name:
            continue

        # 该行是否以地区名开头（区县经济页）
        region_name: str | None = None
        if is_district_table and (name in DISTRICTS or name in REGION_LABELS):
            region_name = name
            # 指标名在表头里，例如「*地区生产总值 1-9月（亿元） 同比±%」
            hdr_ind = next((c for c in sec.header_cells if c.startswith("*")), None)
            if hdr_ind:
                name = re.split(r"\s{2,}|\s(?=\d)", hdr_ind.lstrip("*").strip())[0].strip()
            else:
                name = "地区生产总值"

        # 带上父级，避免不同分组下的同名子项冲突
        if row.parent and row.parent != name:
            name = f"{row.parent}·{name}"

        numeric = [t for t in row.numbers if re.search(r"-?\d", t)]
        if not numeric and not name.startswith("其中"):
            continue  # 分组标题

        # 取主值列
        idx = value_col - 1
        if idx >= len(row.numbers):
            continue
        tok = row.numbers[idx]

        if _is_dash(tok):
            value: float | None = None
        else:
            m = re.search(r"-?\d+(?:\.\d+)?", tok)
            value = float(m.group()) if m else None

        record_unit = unit if semantic == "level" else ""
        if semantic == "index":
            record_unit = "指数"

        base = {
            "source_id": SOURCE_ID,
            "dataset": DATASET,
            "region": region_name or "杭州",
            "unit": record_unit,
            "period": period,
            "period_type": period_type_db,
            "published_at": None,
            "fetched_at": fetched_at,
            "source_url": source_url,
            "snapshot": snapshot,
            "sha256": sha256,
            "layer": "L1",
            "confidence": "official",
        }

        # 价格指数页不输出「主值」记录，它只有环比/当月同比/累计同比三列，下面统一处理
        if not is_price_index:
            out.append(
                {
                    **base,
                    "indicator": f"{sec.title}·{name}",
                    "value": value,
                    "notes": f"表头：{hdr_text[:80]}；period_kind={ptype}",
                }
            )

        # 价格指数页：三列分别是 环比 / 当月同比 / 累计同比，必须分开存，
        # 否则把环比当成同比会得出完全相反的结论。
        if is_price_index and len(row.numbers) >= 3:
            labels = [
                (0, "环比", "上月=100"),
                (1, "当月同比", "上年同月=100"),
                (2, "累计同比", "上年同期累计=100"),
            ]
            for col, suffix, desc in labels:
                tok2 = row.numbers[col]
                if _is_dash(tok2):
                    v2: float | None = None
                else:
                    m2 = re.search(r"-?\d+(?:\.\d+)?", tok2)
                    v2 = float(m2.group()) if m2 else None
                out.append(
                    {
                        **base,
                        "indicator": f"{sec.title}·{name}·{suffix}",
                        "value": v2,
                        "unit": "指数",
                        "period": period if suffix == "当月同比" else base["period"],
                        "notes": f"{desc}；表头：{hdr_text[:60]}",
                    }
                )
            continue

        # 同比列（非价格指数）
        if semantic != "index" and "同比" in " ".join(sec.header_cells) and len(row.numbers) > idx + 1:
            gtok = row.numbers[idx + 1]
            if not _is_dash(gtok):
                gm = re.search(r"-?\d+(?:\.\d+)?", gtok)
                if gm:
                    out.append(
                        {
                            **base,
                            "indicator": f"{sec.title}·{name}·同比",
                            "value": float(gm.group()),
                            "unit": "%",
                            "notes": f"表头：{hdr_text[:80]}",
                        }
                    )

    return out, warnings