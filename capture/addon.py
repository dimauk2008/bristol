"""Аддон mitmproxy: логирует запросы/ответы интересных хостов в JSONL и файлы."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from mitmproxy import http

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "flows")
BODIES = os.path.join(OUT, "bodies")
os.makedirs(BODIES, exist_ok=True)

JSONL = os.path.join(OUT, "flows.jsonl")
INDEX = os.path.join(OUT, "index.txt")

FILTER = os.environ.get("MITM_FILTER", "")
MAX_INLINE = 4000


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else url


def _interesting(url: str) -> bool:
    if not FILTER:
        return True
    return re.search(FILTER, url, re.I) is not None


def _decode(data: bytes | None) -> tuple[str, bytes | None]:
    if not data:
        return "", None
    ct = ""
    try:
        text = data.decode("utf-8")
        return text, data
    except UnicodeDecodeError:
        return f"<binary {len(data)} bytes>", data


def response(flow: http.HTTPFlow) -> None:
    url = flow.request.pretty_url
    if not _interesting(url):
        return

    req = flow.request
    resp = flow.response
    req_body = _decode(req.content)
    resp_body = _decode(resp.content)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", _host(url))[:40]
    base = f"{stamp}_{safe}"

    rec = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "method": req.method,
        "url": url,
        "status": resp.status_code,
        "req_headers": dict(req.headers),
        "req_body": req_body[0][:MAX_INLINE],
        "resp_headers": dict(resp.headers),
        "resp_body": resp_body[0][:MAX_INLINE],
    }
    with open(JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    with open(INDEX, "a", encoding="utf-8") as f:
        f.write(f"{req.method} {resp.status_code} {url}\n")

    if req_body[1] and len(req_body[1]) > MAX_INLINE:
        with open(os.path.join(BODIES, base + ".req"), "wb") as f:
            f.write(req_body[1])
    if resp_body[1] and len(resp_body[1]) > MAX_INLINE:
        with open(os.path.join(BODIES, base + ".resp"), "wb") as f:
            f.write(resp_body[1])

    print(f"[{resp.status_code}] {req.method} {url}")
