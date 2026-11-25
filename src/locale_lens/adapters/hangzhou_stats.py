"""杭州市统计局 · 发布监控 + 附件归档。

列表页是 JS 渲染的，但底层有 jpaas 接口：
  GET /api-gateway/jpaas-publish-server/front/page/build/unit
      ?parseType=bulidstatic&webId=3148&tplSetId=...&pageType=column&tagId=列表&pageId=<栏目ID>
返回 JSON，`data.html` 里就是文章列表。

文章正文基本是 PDF 附件，通过
  /api-gateway/jpaas-web-server/front/document/download?fileUrl=...&fileName=...
下载。实测可直接拿到 application/pdf。

v1 策略：**记录发布事件 + 归档原始 PDF**，不做 PDF 表格解析。
理由：月报/年鉴的表格结构每期都可能变，解析脆弱；先保证原件在，
后续可用新解析器从 raw/ 回填（这正是 L0 存在的意义）。
"""

from __future__ import annotations

import json
import re
import time
from html import unescape
from urllib.parse import unquote

from lxml import html as LH

from .. import load_sources, now, rel
from ..fetch import fetch, fetch_bytes, save_raw, save_raw_bytes
from ..schema import Event
from ..store import all_events

SOURCE_ID = "hangzhou.stats"
DEFAULT_BASE = "http://tjj.hangzhou.gov.cn"
DEFAULT_TPL = "rFyaTxeaOqPZC5g5yj16X"
DEFAULT_WEB_ID = "3148"
LIST_EP = "/api-gateway/jpaas-publish-server/front/page/build/unit"


class HangzhouStats:
    source_id = SOURCE_ID

    def _cfg(self) -> dict:
        for s in load_sources().get("sources", []):
            if s.get("id") == SOURCE_ID:
                return s
        return {}

    # -- 列表 --

    def _listing(self, base: str, tpl: str, web_id: str, page_id: int) -> list[tuple[str, str, str | None]]:
        params = {
            "parseType": "bulidstatic",
            "webId": web_id,
            "tplSetId": tpl,
            "pageType": "column",
            "tagId": "列表",
            "editType": "null",
            "pageId": str(page_id),
        }
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        text, _status = fetch(f"{base}{LIST_EP}?{qs}", timeout=30)

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not payload.get("success"):
            return []

        fragment = payload.get("data", {}).get("html", "") or ""
        if not fragment.strip():
            return []

        doc = LH.fromstring(f"<div>{fragment}</div>")
        items: list[tuple[str, str, str | None]] = []
        for a in doc.xpath("//a[@href]"):
            title = " ".join(a.text_content().split())
            href = a.get("href") or ""
            if not title or not href.startswith("/"):
                continue
            parent_text = " ".join(a.getparent().text_content().split())
            m = re.search(r"(\d{4}-\d{2}-\d{2})", parent_text)
            items.append((title, href, m.group(1) if m else None))
        return items

    # -- 附件 --

    def _find_attachment(self, base: str, href: str) -> tuple[str, str] | None:
        text, _status = fetch(f"{base}{href}", timeout=30)
        doc = LH.fromstring(text)

        for a in doc.xpath("//a[@href]"):
            h = unescape(a.get("href") or "")
            if not h:
                continue

            # 新式：走网关下载
            if "document/download" in h:
                fname = a.get("download") or ""
                if not fname:
                    m = re.search(r"fileName=([^&]+)", h)
                    fname = unquote(m.group(1)) if m else "attachment.pdf"
                return base + h, fname

            # 旧式：直接链到 /cms_files/... .pdf
            m = re.search(r"\.(pdf|xlsx?|docx?)(?:\?|$)", h, re.I)
            if m:
                fname = a.get("download") or ""
                if not fname:
                    fname = unquote(h.split("?")[0].rstrip("/").rsplit("/", 1)[-1])
                if not fname:
                    fname = "attachment" + "." + m.group(1).lower()
                return (base + h if h.startswith("/") else h), fname

        return None

    # -- 主流程 --

    def run(self, backfill: bool = False) -> list[Event]:
        cfg = self._cfg()
        base = str(cfg.get("base_url", DEFAULT_BASE)).rstrip("/")
        tpl = str(cfg.get("tpl_set_id", DEFAULT_TPL))
        web_id = str(cfg.get("web_id", DEFAULT_WEB_ID))
        gap = float(cfg.get("politeness", {}).get("min_interval_seconds", 3))

        ts = now()
        out: list[Event] = []

        for col in cfg.get("columns", []):
            key = col["key"]
            page_id = int(col["page_id"])
            title_cn = col.get("title", key)
            want_pdf = bool(col.get("download_pdf", False))
            # 回填时不受单次限额约束（否则要跑很多轮才能补齐历史）
            max_dl = 100 if backfill else int(col.get("max_downloads_per_run", 5))

            items = self._listing(base, tpl, web_id, page_id)
            if not items:
                continue

            existing = all_events(source_id=SOURCE_ID, dataset=key)
            downloaded = 0

            for title, href, date in items:
                event_id = href
                payload: dict = {
                    "栏目": title_cn,
                    "标题": title,
                    "发布日期": date,
                }
                prev = existing.get(event_id)
                # 关键：把上一轮已归档的附件信息带过来。
                # 否则本次跳过下载 → payload 没有 pdf_path → 覆盖旧记录时把归档路径抹掉，
                # 下一轮又会重新下载，产生 _1 重复文件。
                if prev:
                    for k in ("pdf_path", "pdf_sha256", "pdf_bytes", "pdf_error"):
                        v = (prev.get("payload") or {}).get(k)
                        if v is not None:
                            payload[k] = v

                has_pdf = bool(payload.get("pdf_path"))

                if want_pdf and not has_pdf and downloaded < max_dl:
                    att = self._find_attachment(base, href)
                    if att:
                        att_url, fname = att
                        data: bytes | None = None
                        try:
                            # 连续下载会被限流，返回一个 HTML 提示页而不是 PDF。
                            # 遇到非 PDF 就退避重试，最多 3 次。
                            for attempt in range(3):
                                blob, _st = fetch_bytes(att_url, timeout=180)
                                if blob[:4] == b"%PDF":
                                    data = blob
                                    break
                                time.sleep(12 * (attempt + 1))

                            if data is not None:
                                path, sha = save_raw_bytes(SOURCE_ID, key, fname, data, ts)
                                payload["pdf_path"] = rel(path)
                                payload["pdf_sha256"] = sha
                                payload["pdf_bytes"] = len(data)
                                downloaded += 1
                            else:
                                payload["pdf_error"] = "多次重试仍未取到 PDF（疑似限流）"
                        except Exception as exc:  # noqa: BLE001
                            payload["pdf_error"] = str(exc)[:200]
                        time.sleep(gap)
                    else:
                        payload["pdf_error"] = "正文未找到附件"

                out.append(
                    Event(
                        source_id=SOURCE_ID,
                        dataset=key,
                        event_id=event_id,
                        event_type=str(col.get("event_type", "stats_publication")),
                        title=title,
                        region="杭州",
                        event_date=date,
                        announced_date=date,
                        url=base + href,
                        payload=payload,
                        fetched_at=ts.isoformat(),
                        confidence="official",
                    )
                )

            time.sleep(gap)

        return out
