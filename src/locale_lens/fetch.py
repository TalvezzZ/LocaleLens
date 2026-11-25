"""礼貌抓取 + L0 原始快照留档。

为什么用 curl 而不是纯 Python HTTP 客户端：
透明售房网的 WAF 会对非浏览器 TLS 指纹直接返回 405（实测 httpx 全部 405，
curl 正常 200）。因此默认走 curl 子进程；curl 不可用时回退到 httpx。
礼貌抓取靠的是低频、节流和标注来源，而不是伪装成爬虫。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import RAW, now, rel

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}

_STATUS_MARK = "\n__LOCALELENS_STATUS__"


@dataclass
class FetchResult:
    url: str
    text: str
    fetched_at: datetime
    status: int
    sha256: str
    snapshot_path: Path

    @property
    def snapshot_rel(self) -> str:
        return rel(self.snapshot_path)


# ---------- 传输层 ----------


def _fetch_curl(
    url: str, timeout: float, post_data: dict[str, str] | None = None
) -> tuple[str, int]:
    cmd = [
        "curl",
        "-sS",
        "-L",
        "--compressed",
        "--max-time",
        str(int(timeout)),
        "-A",
        USER_AGENT,
    ]
    for k, v in DEFAULT_HEADERS.items():
        cmd += ["-H", f"{k}: {v}"]
    if post_data:
        for k, v in post_data.items():
            cmd += ["--data-urlencode", f"{k}={v}"]
    cmd += ["-w", _STATUS_MARK + "%{http_code}", url]

    proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
    if proc.returncode != 0:
        raise RuntimeError(
            f"curl exit {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[:300]}"
        )

    raw = proc.stdout
    idx = raw.rfind(_STATUS_MARK.encode())
    if idx < 0:
        body, status = raw, 0
    else:
        body = raw[:idx]
        try:
            status = int(raw[idx + len(_STATUS_MARK) :].decode().strip() or 0)
        except ValueError:
            status = 0
    return body.decode("utf-8", "replace"), status


def _fetch_httpx(
    url: str, timeout: float, post_data: dict[str, str] | None = None
) -> tuple[str, int]:
    import httpx

    headers = {"User-Agent": USER_AGENT, **DEFAULT_HEADERS}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
        r = client.post(url, data=post_data) if post_data else client.get(url)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = "utf-8"
        return r.text, r.status_code


def fetch(
    url: str,
    *,
    timeout: float = 30.0,
    retries: int = 2,
    backoff: float = 2.0,
    post_data: dict[str, str] | None = None,
) -> tuple[str, int]:
    """抓一个页面。失败重试后抛出。post_data 存在时用 POST。"""
    use_curl = shutil.which("curl") is not None
    last: Exception | None = None

    for attempt in range(retries + 1):
        try:
            text, status = (_fetch_curl if use_curl else _fetch_httpx)(
                url, timeout, post_data
            )
            if status and status >= 400:
                raise RuntimeError(f"HTTP {status}")
            if not text.strip():
                raise RuntimeError("空响应体")
            return text, status
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))

    raise RuntimeError(f"fetch failed after {retries + 1} attempts: {url}: {last}") from last


# ---------- L0 快照 ----------
def save_raw(source_id: str, dataset: str, url: str, text: str, ts: datetime | None = None) -> FetchResult:
    """把原始页面压缩留档。只增不改。"""
    ts = ts or now()
    d = RAW / source_id / dataset / ts.strftime("%Y") / ts.strftime("%m") / ts.strftime("%d")
    d.mkdir(parents=True, exist_ok=True)

    stem = ts.strftime("%H%M%S")
    path = d / f"{stem}.html.gz"
    n = 1
    while path.exists():  # 同一秒内重复抓取不覆盖
        path = d / f"{stem}_{n}.html.gz"
        n += 1

    data = text.encode("utf-8")
    with gzip.open(path, "wb") as f:
        f.write(data)

    digest = hashlib.sha256(data).hexdigest()
    meta = path.with_name(path.name.replace(".html.gz", ".meta.json"))
    meta.write_text(
        json.dumps(
            {
                "source_id": source_id,
                "dataset": dataset,
                "url": url,
                "fetched_at": ts.isoformat(),
                "sha256": digest,
                "bytes_raw": len(data),
                "bytes_gz": path.stat().st_size,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    return FetchResult(
        url=url, text=text, fetched_at=ts, status=200, sha256=digest, snapshot_path=path
    )


# ---------- 二进制下载（PDF / Excel 等附件） ----------


def fetch_bytes(url: str, *, timeout: float = 120.0, retries: int = 1) -> tuple[bytes, int]:
    """下载二进制文件。返回 (bytes, status)。"""
    import os
    import tempfile

    last: Exception | None = None
    for attempt in range(retries + 1):
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".bin")
            os.close(fd)
            cmd = [
                "curl", "-sS", "-L", "--compressed",
                "--max-time", str(int(timeout)),
                "-A", USER_AGENT,
                "-o", tmp,
                "-w", "%{http_code}",
                url,
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 30)
            status = 0
            try:
                status = int(proc.stdout.decode().strip() or 0)
            except ValueError:
                status = 0
            data = Path(tmp).read_bytes()
            if status and status >= 400:
                raise RuntimeError(f"HTTP {status}")
            if not data:
                raise RuntimeError("空响应体")
            return data, status
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(2.0)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
    raise RuntimeError(f"fetch_bytes failed: {url}: {last}") from last


def save_raw_bytes(
    source_id: str,
    dataset: str,
    filename: str,
    data: bytes,
    ts: datetime | None = None,
) -> tuple[Path, str]:
    """归档二进制原始文件。只增不改。返回 (路径, sha256)。"""
    ts = ts or now()
    d = RAW / source_id / dataset / ts.strftime("%Y") / ts.strftime("%m") / ts.strftime("%d")
    d.mkdir(parents=True, exist_ok=True)

    safe = "".join(c if c not in '/\\:*?"<>|' else "_" for c in filename).strip() or "file"
    path = d / safe
    n = 1
    while path.exists():
        stem, dot, ext = safe.rpartition(".")
        path = d / (f"{stem}_{n}.{ext}" if dot else f"{safe}_{n}")
        n += 1

    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    meta = path.with_name(path.name + ".meta.json")
    meta.write_text(
        json.dumps(
            {
                "source_id": source_id,
                "dataset": dataset,
                "filename": filename,
                "fetched_at": ts.isoformat(),
                "sha256": digest,
                "bytes": len(data),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path, digest
