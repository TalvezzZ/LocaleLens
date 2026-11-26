"""统计局月报 PDF → L1 指标（适配器）。

输入是已归档的月报 PDF（`raw/hangzhou.stats/monthly_report/`），
输出是结构化指标。不重复下载 PDF —— 沿用已有的原始快照，
这正是三层留存的价值：PDF 早就在 L0 里，解析器可以随时更换重跑。
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import RAW, now, rel
from ..schema import Observation
from ..store import available_days
from . import NothingToDo
from ..pdf_monthly import (
    DATASET,
    SOURCE_ID,
    infer_period_from_content,
    parse_pdf,
    section_to_records,
)

PDF_SOURCE = "hangzhou.stats"
PDF_DATASET = "monthly_report"


class HangzhouMonthlyPdf:
    source_id = SOURCE_ID

    def run(self, backfill: bool = False) -> list[Observation]:
        ts = now()
        pdfs = self._find_pdfs()
        if not pdfs:
            raise NothingToDo("未找到月报 PDF（先跑 hangzhou.stats 归档原件）")

        done = self._done_periods()
        out: list[Observation] = []
        skipped = 0

        for pdf_path, meta in pdfs:
            year, month = self._period_of(pdf_path, meta)
            if year is None:
                continue

            # 比对校验：这个期间已经解析过就跳过
            key = f"{year}-{month:02d}"
            if not backfill and key in done:
                skipped += 1
                continue

            sections = parse_pdf(pdf_path)
            for sec in sections:
                records, _warnings = section_to_records(
                    sec,
                    source_url=(meta or {}).get("url") or "",
                    snapshot=rel(pdf_path),
                    sha256=(meta or {}).get("sha256") or "",
                    fetched_at=ts.isoformat(),
                    year=year,
                    data_month=month,
                )
                for rec in records:
                    out.append(Observation(**rec))

        if skipped:
            print(f"    已解析过 {skipped} 份，跳过")
        if not out:
            raise NothingToDo("月报 PDF 均已解析，无新增")
        return out

    # -- 找 PDF --

    def _find_pdfs(self) -> list[tuple[Path, dict | None]]:
        """找所有已归档的月报 PDF，附上同目录的 meta.json。"""
        base = RAW / PDF_SOURCE / PDF_DATASET
        if not base.exists():
            return []

        found: list[tuple[Path, dict | None]] = []
        for pdf in sorted(base.rglob("*.pdf")):
            meta_path = pdf.with_name(pdf.name + ".meta.json")
            meta = None
            if meta_path.exists():
                import json

                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    meta = None
            found.append((pdf, meta))
        return found

    def _period_of(self, pdf: Path, meta: dict | None) -> tuple[int | None, int | None]:
        """判定 PDF 属于哪一期。优先文件名，其次 meta 里的 fileName，最后看内容。"""
        for name in (pdf.name, (meta or {}).get("filename") or ""):
            m = re.search(r"(20\d{2})年(\d{1,2})月报", name)
            if m:
                return int(m.group(1)), int(m.group(2))

        # 文件名不可靠时，从内容推断月份，年份取最近一次可用
        sections = parse_pdf(pdf)
        _y, month = infer_period_from_content(sections)
        if month is None:
            return None, None
        # 年份：文件名/目录里通常只有归档日，月份<=归档月则同年，否则上一年
        archive_day = re.search(r"/(20\d{2})/(\d{2})/", str(pdf))
        year = 2025
        if archive_day:
            ay = int(archive_day.group(1))
            am = int(archive_day.group(2))
            year = ay if month <= am else ay - 1
        return year, month

    # -- 已解析期间 --

    def _done_periods(self) -> set[str]:
        done: set[str] = set()
        for d in available_days(source_id=SOURCE_ID, dataset=DATASET):
            from ..store import read_day

            for r in read_day(source_id=SOURCE_ID, dataset=DATASET, day=d):
                p = r.get("period")
                if p:
                    done.add(str(p))
        return done