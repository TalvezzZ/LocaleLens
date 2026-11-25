"""适配器注册表。新增源 = 在这里注册一个 adapter。"""

from __future__ import annotations

from typing import Protocol

from ..schema import Event, Observation


class NothingToDo(Exception):
    """没有新工作可做（不是失败）。例如公报都已提取过。

    用它把「空转」和「解析失败」区分开——否则健康度会被误报污染。
    """


class Adapter(Protocol):
    source_id: str

    def run(self, backfill: bool = False) -> list[Observation] | list[Event]:
        """抓取 + 解析。原始快照由 adapter 自己调用 fetch.save_raw 保存。"""
        ...


def get_adapters() -> dict[str, Adapter]:
    from .amap_poi import AmapPoi
    from .hangzhou_bulletin import HangzhouBulletinIndicators
    from .hangzhou_opendata import HangzhouOpenData
    from .hangzhou_stats import HangzhouStats
    from .tmsf_lists import TmsfCancelContract, TmsfPresaleApproval
    from .tmsf_transaction_dynamic import TmsfTransactionDynamic

    instances = [
        TmsfTransactionDynamic(),
        TmsfPresaleApproval(),
        TmsfCancelContract(),
        HangzhouStats(),
        HangzhouBulletinIndicators(),
        HangzhouOpenData(),
        AmapPoi(),
    ]
    return {a.source_id: a for a in instances}
