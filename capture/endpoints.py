"""Список уникальных эндпоинтов из captured flows: python endpoints.py [подстрока]"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

FLOWS = str(Path(__file__).parent / "flows" / "flows.jsonl")


def main() -> None:
    needle = sys.argv[1] if len(sys.argv) > 1 else ""
    recs = [json.loads(line) for line in open(FLOWS, encoding="utf-8") if line.strip()]
    counts: Counter[tuple[str, str, str]] = Counter()
    status: dict[tuple[str, str, str], str] = {}
    for r in recs:
        u = urlsplit(r["url"])
        key = (r["method"], u.netloc, u.path)
        if needle and needle not in r["url"]:
            continue
        counts[key] += 1
        status[key] = str(r["status"])
    for (method, host, path), n in counts.most_common():
        print(f"{method:6} {status[(method, host, path)]:4} x{n:<4} {host}{path}")


if __name__ == "__main__":
    main()
