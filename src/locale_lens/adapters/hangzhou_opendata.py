"""杭州市数据开放平台 · 新数据集发现 + 平台规模追踪。

平台是 Vue SPA，但首页用的是 Struts2 接口，返回标准 JSON：
  /dop/homePage/getNewData.action          最新上架数据集
  /dop/homePage/getPopularData.action      热门数据集
  /dop/openIndex/getPlatformSituation.action  平台规模（3825 数据集 / 4301 API …）

深度数据集检索接口藏在懒加载 chunk 里，暂不逆向——先用这两类：
  1. 新上架数据集 → 事件流，用来"自动发现新数据源"
  2. 平台规模 → 观测值，看开放进度
"""

from __future__ import annotations

import json
import re

from .. import now, rel
from ..fetch import fetch, save_raw
from ..schema import Event, Observation

SOURCE_ID = "hangzhou.opendata"
BASE = "https://data.hangzhou.gov.cn"
NEW_EP = "/dop/homePage/getNewData.action"
SITUATION_EP = "/dop/openIndex/getPlatformSituation.action"

DISTRICTS = [
    "上城", "拱墅", "西湖", "滨江", "萧山", "余杭", "临平",
    "富阳", "临安", "桐庐", "淳安", "建德", "钱塘",
]

# 平台规模字段 → (指标名, 单位)
SITUATION_FIELDS = [
    ("dataSetCount", "开放数据集总数", "个"),
    ("apiCount", "开放API总数", "个"),
    ("fileCount", "开放文件总数", "个"),
    ("colCount", "数据项总数", "个"),
    ("deptCount", "接入部门数", "个"),
    ("downloadUseCount", "累计下载调用量", "次"),
    ("callCount", "累计接口调用量", "次"),
]


def _region(text: str) -> str | None:
    for d in DISTRICTS:
        if d in text:
            return d
    return None


class HangzhouOpenData:
    source_id = SOURCE_ID

    def run(self, backfill: bool = False) -> list[Event] | list[Observation]:
        ts = now()
        out: list[Event | Observation] = []

        out.extend(self._new_datasets(ts))
        out.extend(self._situation(ts))

        return out  # type: ignore[return-value]

    # -- 新上架数据集 --

    def _new_datasets(self, ts) -> list[Event]:
        url = BASE + NEW_EP
        text, _st = fetch(url, timeout=30)
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not isinstance(items, list):
            return []

        snap = save_raw(SOURCE_ID, "new_datasets", url, text, ts)
        out: list[Event] = []
        for it in items:
            rid = it.get("resourceId")
            name = (it.get("resourceName") or "").strip()
            if rid is None or not name:
                continue
            dept = it.get("private_dept") or ""
            out.append(
                Event(
                    source_id=SOURCE_ID,
                    dataset="new_datasets",
                    event_id=f"od-{rid}",
                    event_type="dataset_listed",
                    title=name,
                    region=_region(dept) or _region(name),
                    event_date=it.get("modify_date"),
                    announced_date=it.get("modify_date"),
                    url=None,  # 详情页路由未确认，不猜
                    payload={
                        "resourceId": rid,
                        "领域": it.get("area"),
                        "提供部门": dept,
                        "资源类型": it.get("resourceType"),
                        "访问量": it.get("visitsCount"),
                        "下载调用量": it.get("downInvok"),
                    },
                    fetched_at=ts.isoformat(),
                    snapshot=rel(snap.snapshot_path),
                    sha256=snap.sha256,
                    confidence="official",
                )
            )
        return out

    # -- 平台规模 --

    def _situation(self, ts) -> list[Observation]:
        url = BASE + SITUATION_EP
        text, _st = fetch(url, timeout=30)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []

        snap = save_raw(SOURCE_ID, "platform_situation", url, text, ts)
        period = ts.strftime("%Y-%m-%d")
        out: list[Observation] = []
        for key, name, unit in SITUATION_FIELDS:
            val = data.get(key)
            if val is None:
                continue
            out.append(
                Observation(
                    source_id=SOURCE_ID,
                    dataset="platform_situation",
                    region="杭州",
                    indicator=name,
                    value=float(val),
                    unit=unit,
                    period=period,
                    period_type="day",
                    fetched_at=ts.isoformat(),
                    source_url=url,
                    snapshot=rel(snap.snapshot_path),
                    sha256=snap.sha256,
                    confidence="official",
                )
            )
        return out
