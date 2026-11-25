"""L1 落盘（按天一个 JSONL，幂等）+ 源健康度。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from . import DATA, HEALTH, now
from .schema import row_key


def write_day(
    records: Sequence[Any],
    *,
    source_id: str,
    dataset: str,
    day: str,
) -> tuple[Path, int]:
    """写入某天的 JSONL。同键覆盖，其余保留 → 可重复运行。

    返回 (文件路径, 该文件总行数)。
    """
    path = DATA / source_id / dataset / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    merged: dict[tuple, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            merged[row_key(row)] = row

    for rec in records:
        row = rec.model_dump()
        merged[rec.dedupe_key()] = row

    rows = sorted(
        merged.values(),
        key=lambda r: (
            r.get("event_date") or r.get("period") or "",
            r.get("region") or "",
            r.get("indicator") or r.get("title") or "",
        ),
    )
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return path, len(rows)


def read_day(*, source_id: str, dataset: str, day: str) -> list[dict]:
    path = DATA / source_id / dataset / f"{day}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def available_days(*, source_id: str, dataset: str) -> list[str]:
    d = DATA / source_id / dataset
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.jsonl"))


def log_health(
    *,
    source_id: str,
    dataset: str,
    ok: bool,
    rows: int = 0,
    error: str | None = None,
) -> None:
    HEALTH.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": now().isoformat(),
        "source_id": source_id,
        "dataset": dataset,
        "ok": ok,
        "rows": rows,
        "error": error,
    }
    with (HEALTH / "sources.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def health_summary() -> list[dict]:
    """按 (source_id, dataset) 汇总健康度。"""
    path = HEALTH / "sources.jsonl"
    if not path.exists():
        return []

    agg: dict[tuple[str, str], dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        k = (r["source_id"], r["dataset"])
        a = agg.setdefault(
            k,
            {
                "source_id": r["source_id"],
                "dataset": r["dataset"],
                "runs": 0,
                "fails": 0,
                "last_ok": None,
                "last_ts": None,
                "consecutive_fails": 0,
                "last_error": None,
            },
        )
        a["runs"] += 1
        a["last_ts"] = r["ts"]
        if r["ok"]:
            a["last_ok"] = r["ts"]
            a["consecutive_fails"] = 0
            a["last_error"] = None  # 成功了就不该再显示旧错误
        else:
            a["fails"] += 1
            a["consecutive_fails"] += 1
            a["last_error"] = r.get("error")

    out = []
    for a in agg.values():
        a["fail_rate"] = round(a["fails"] / a["runs"], 3) if a["runs"] else None
        out.append(a)
    return sorted(out, key=lambda x: (x["source_id"], x["dataset"]))


def all_events(*, source_id: str, dataset: str) -> dict[str, dict]:
    """把所有天的同一数据集事件合并成 {event_id: row}，用于判断"是否已归档"。"""
    out: dict[str, dict] = {}
    for d in available_days(source_id=source_id, dataset=dataset):
        for r in read_day(source_id=source_id, dataset=dataset, day=d):
            eid = r.get("event_id")
            if eid:
                out[eid] = r
    return out


def all_observations(*, source_id: str, dataset: str) -> list[dict]:
    """把所有天的同一数据集观测记录拉平成一条列表。"""
    out: list[dict] = []
    for d in available_days(source_id=source_id, dataset=dataset):
        out.extend(read_day(source_id=source_id, dataset=dataset, day=d))
    return out


def last_ok_ts(source_id: str) -> "datetime | None":
    """该源最近一次成功的时间。用于频率守卫，避免高频任务被打爆配额。"""
    from datetime import datetime

    path = HEALTH / "sources.jsonl"
    if not path.exists():
        return None
    latest = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("source_id") != source_id or not r.get("ok"):
            continue
        try:
            ts = datetime.fromisoformat(r["ts"])
        except (KeyError, ValueError):
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest
