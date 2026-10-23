"""Просмотр захваченных запросов: python show.py [подстрока-url] [--headers]"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

FLOWS = str(Path(__file__).parent / "flows" / "flows.jsonl")

SKIP_HEADERS = {
    "content-length", "accept-encoding", "user-agent", "connection",
    "accept", "accept-language", "x-requested-with", "pragma", "cache-control",
    "host", "cookie", "sec-ch-ua", "sec-fetch-mode", "sec-fetch-site",
    "sec-fetch-dest", "sec-fetch-user", "upgrade-insecure-requests",
}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_headers = "--headers" in sys.argv
    needle = args[0] if args else ""

    with open(FLOWS, encoding="utf-8") as f:
        flows = [json.loads(line) for line in f if line.strip()]

    for rec in flows:
        if needle and needle not in rec["url"]:
            continue
        u = urlsplit(rec["url"])
        print("=" * 100)
        print(f'{rec["method"]} {rec["status"]} {u.path}' + (f'?{u.query}' if u.query else ''))
        print(f'HOST: {u.netloc}')
        if show_headers:
            print("-- request headers --")
            for k, v in rec["req_headers"].items():
                if k.lower() in SKIP_HEADERS:
                    continue
                print(f"  {k}: {v}")
        if rec["req_body"]:
            print("-- request body --")
            print(rec["req_body"])
        print("-- response body --")
        print(rec["resp_body"])


if __name__ == "__main__":
    main()
