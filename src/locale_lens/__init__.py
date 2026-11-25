"""LocaleLens — 地区数据观察站。文件即真相，长期留档。"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

__version__ = "0.1.0"

TZ = ZoneInfo("Asia/Shanghai")

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
RAW = ROOT / "raw"
DATA = ROOT / "data"
DERIVED = ROOT / "derived"
DIGEST = ROOT / "digest"
HEALTH = ROOT / "health"
NOTES = ROOT / "notes"

LOCAL_DIRS = (RAW, DATA, DERIVED, DIGEST, HEALTH, NOTES)


def load_env() -> None:
    """读取项目根目录 .env 写入环境变量。不覆盖已存在的变量。

    自己实现是为了不引入 python-dotenv 依赖——格式足够简单。
    """
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_secret(name: str) -> str | None:
    """取密钥。空值视为未配置，返回 None。"""
    value = os.environ.get(name, "").strip()
    return value or None


load_env()


def now() -> datetime:
    return datetime.now(TZ)


def ensure_dirs() -> None:
    for d in LOCAL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


def rel(p: Path) -> str:
    """把绝对路径转成相对仓库根的路径，用于写入 snapshot 字段。"""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def load_sources() -> dict:
    import yaml

    with (CONFIG / "sources.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)
