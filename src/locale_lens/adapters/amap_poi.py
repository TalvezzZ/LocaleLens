"""高德 POI · 商圈门店密度（自适应网格版）。

问题：高德 place/text 的 count 有硬上限 600。
     「上城区便利店」和「淳安县便利店」都返回 600，这不是数据，是接口封顶。

解决办法：用 polygon 查询 + 自适应二分网格。
     - 先按区县 bbox 查一次
     - count < 600 → 可信，直接采用
     - count >= 600 → 把 bbox 切成 4 格分别查
     - 递归，直到全部可信，或切到最小格（此时该格记为未知）

⚠️ 口径边界（很重要）：
    这是**高德数据库里的门店存量**，不是工商注册数、不是实际营业数，
    更不是客流或营收。只能做同一类门店的区县横向对比和自身趋势，
    不能做绝对值推断，不能跨数据源混算。

需要 AMAP_KEY（写在 .env）。没有 key 时抛 NothingToDo，不污染健康度。
"""

from __future__ import annotations

import json
import time

from .. import get_secret, now, rel
from ..fetch import fetch, save_raw
from ..schema import Observation
from . import NothingToDo

SOURCE_ID = "amap.poi"
POLY_EP = "https://restapi.amap.com/v3/place/polygon"
DISTRICT_EP = "https://restapi.amap.com/v3/config/district"

# 高德 polygon 接口的 count 会在结果过多时封顶，实测封顶值为 600 或 1000（不固定）。
# 只要命中这两个值，就说明「真实数量 ≥ 该值」，不是精确数。
# 因此：命中封顶 → 继续拆分；拆到最小格仍封顶 → 记为未知，绝不当真实值入库。
COUNT_CAPS = {600, 1000}
MIN_CELL_DEG = 0.015   # 最小格约 1.5km。再细调用量爆炸，且已低于「商圈」粒度
MAX_QUERIES = 1800     # 单次运行总预算
QUERIES_PER_DISTRICT = 120
POLITENESS_SECONDS = 0.25

DISTRICTS: list[str] = [
    "上城区", "拱墅区", "西湖区", "滨江区", "萧山区", "余杭区", "临平区",
    "富阳区", "临安区", "桐庐县", "淳安县", "建德市", "钱塘区",
]

CATEGORIES: list[tuple[str, str]] = [
    ("餐饮", "050000"),
    ("咖啡厅", "050500"),
    ("便利店", "060200"),
    ("购物中心", "060101"),
]


def _bbox(polyline: str) -> tuple[float, float, float, float] | None:
    pts = [p.split(",") for p in polyline.split(";") if "," in p]
    try:
        lngs = [float(p[0]) for p in pts]
        lats = [float(p[1]) for p in pts]
    except (IndexError, ValueError):
        return None
    if not lngs or not lats:
        return None
    return min(lngs), min(lats), max(lngs), max(lats)


def _rect(x0: float, y0: float, x1: float, y1: float) -> str:
    return f"{x0:.6f},{y0:.6f}|{x1:.6f},{y1:.6f}"


class AmapPoi:
    source_id = SOURCE_ID

    def __init__(self) -> None:
        self._queries = 0

    # -- 单次计数 --

    def _count(self, key: str, rect: tuple[float, float, float, float], types: str) -> int | None:
        """返回 count；失败返回 None。"""
        if self._queries >= MAX_QUERIES:
            return None
        self._queries += 1

        x0, y0, x1, y1 = rect
        url = (
            f"{POLY_EP}?key={key}&polygon={_rect(x0, y0, x1, y1)}"
            f"&types={types}&offset=1&page=1&extensions=base&output=json"
        )
        try:
            text, _status = fetch(url, timeout=30)
            payload = json.loads(text)
        except Exception:  # noqa: BLE001
            return None
        finally:
            time.sleep(POLITENESS_SECONDS)

        if str(payload.get("status")) != "1":
            return None
        try:
            return int(float(payload.get("count") or 0))
        except (TypeError, ValueError):
            return None

    # -- 自适应网格求和 --

    def _count_rect(
        self,
        key: str,
        rect: tuple[float, float, float, float],
        types: str,
        budget: list[int],
        depth: int = 0,
    ) -> tuple[float | None, bool]:
        """返回 (计数, 是否完整可信)。

        完整可信 = 每一格都低于接口上限。
        budget 是单元素列表，用共享可变对象控制单区县预算。
        """
        if budget[0] <= 0:
            return None, False
        budget[0] -= 1

        c = self._count(key, rect, types)
        if c is None:
            return None, False

        if c not in COUNT_CAPS:
            return float(c), True

        x0, y0, x1, y1 = rect
        w, h = x1 - x0, y1 - y0
        # 到最小格还是封顶 → 记为未知，不再细分
        if max(w, h) < MIN_CELL_DEG or depth > 5:
            return None, False

        # 优先切长边：切短边对降低计数几乎没帮助，会浪费预算
        if w >= h:
            m = (x0 + x1) / 2
            cells = [(x0, y0, m, y1), (m, y0, x1, y1)]
        else:
            m = (y0 + y1) / 2
            cells = [(x0, y0, x1, m), (x0, m, x1, y1)]

        total = 0.0
        complete = True
        for cell in cells:
            sub, ok = self._count_rect(key, cell, types, budget, depth + 1)
            if sub is None or not ok:
                complete = False
            if sub:
                total += sub
        return (total if complete else None), complete

    # -- 主流程 --

    def run(self, backfill: bool = False) -> list[Observation]:
        key = get_secret("AMAP_KEY")
        if not key:
            raise NothingToDo("未配置 AMAP_KEY（见 .env），POI 源跳过")

        ts = now()
        period = ts.strftime("%Y-%m-%d")
        out: list[Observation] = []
        log: dict = {}

        for dname in DISTRICTS:
            # 拿区县边界
            url = (
                f"{DISTRICT_EP}?key={key}&keywords={dname}"
                f"&subdistrict=0&extensions=all&output=json"
            )
            try:
                text, _st = fetch(url, timeout=40)
                payload = json.loads(text)
                poly = payload["districts"][0].get("polyline") or ""
            except Exception as exc:  # noqa: BLE001
                log[dname] = {"error": f"边界获取失败: {str(exc)[:80]}"}
                continue
            finally:
                time.sleep(POLITENESS_SECONDS)

            box = _bbox(poly)
            if box is None:
                log[dname] = {"error": "边界解析失败"}
                continue

            log[dname] = {"bbox": [round(v, 4) for v in box]}

            for cname, ctype in CATEGORIES:
                budget = [QUERIES_PER_DISTRICT]
                value, complete = self._count_rect(key, box, ctype, budget)
                out.append(
                    Observation(
                        source_id=SOURCE_ID,
                        dataset="store_density",
                        region=dname.replace("区", "").replace("县", "").replace("市", ""),
                        indicator=f"{cname}门店数",
                        value=value,
                        unit="家",
                        period=period,
                        period_type="day",
                        fetched_at=ts.isoformat(),
                        source_url=POLY_EP,
                        confidence="poi",
                        notes=(
                            "高德POI库内存量，非工商注册数、非实际营业数"
                            if complete
                            else "网格拆分后仍有格点超过接口上限600，真实值未知"
                        ),
                    )
                )
                log[dname][cname] = {"value": value, "complete": complete}

        if not out:
            raise RuntimeError("POI 查询全部失败")

        snap = save_raw(
            SOURCE_ID, "store_density", POLY_EP, json.dumps(log, ensure_ascii=False), ts
        )
        for obs in out:
            obs.snapshot = rel(snap.snapshot_path)
            obs.sha256 = snap.sha256

        return out