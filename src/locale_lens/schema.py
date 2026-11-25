"""L1 记录的数据契约。写入前必须通过校验。

两类记录：
  Observation  数值型事实（可售套数、成交面积……）
  Event        事件型事实（预售审批公告、解除合同……）
两者都落到同一个 JSONL 文件格式里，靠 dedupe_key 区分。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

PeriodType = Literal["hour", "day", "month", "quarter", "year", "instant"]
Confidence = Literal["official", "platform", "media", "poi", "unknown"]


class Observation(BaseModel):
    """一条数值事实。"""

    source_id: str
    dataset: str
    region: Optional[str] = None
    indicator: str
    value: Optional[float] = None
    unit: str
    period: str
    period_type: PeriodType
    published_at: Optional[str] = None
    fetched_at: str
    source_url: str
    snapshot: Optional[str] = None
    sha256: Optional[str] = None
    layer: Literal["L1"] = "L1"
    confidence: Confidence = "official"
    notes: Optional[str] = None

    def dedupe_key(self) -> tuple:
        """同一天内重跑按此键覆盖，保证幂等。"""
        return ("obs", self.region or "", self.indicator, self.period)


class Event(BaseModel):
    """一条事件事实。event_id 必须稳定，重跑不能产生重复。"""

    source_id: str
    dataset: str
    event_id: str
    event_type: str
    title: str
    region: Optional[str] = None
    event_date: Optional[str] = None
    announced_date: Optional[str] = None
    url: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    fetched_at: str
    snapshot: Optional[str] = None
    sha256: Optional[str] = None
    layer: Literal["L1"] = "L1"
    confidence: Confidence = "official"

    def dedupe_key(self) -> tuple:
        return ("event", self.event_id)


def row_key(row: dict) -> tuple:
    """从已落盘的 dict 反推去重键。"""
    if "event_id" in row:
        return ("event", row["event_id"])
    return (
        "obs",
        row.get("region") or "",
        row.get("indicator") or "",
        row.get("period") or "",
    )
