"""Скачивает бандл игры collect-the-bag и ищет API-эндпоинты/логику подарков."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import requests

BASE = "https://collect-the-bag.prisma-games.ru"
OUT = Path(__file__).resolve().parent / "bundle"
OUT.mkdir(exist_ok=True)

PATTERNS = [
    r"/api/v1/[a-zA-Z0-9_\-/{}]+",
    r"gift\w*",
    r"reward\w*",
    r"coupon\w*",
    r"promo\w*",
    r"threshold\w*",
    r"min_\w+",
    r"reward_coins",
    r"подарок",
    r"купон",
]


def main() -> None:
    html = requests.get(f"{BASE}/game/", timeout=30).text
    (OUT / "index.html").write_text(html, encoding="utf-8")

    assets = sorted(set(re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)))
    if not assets:
        assets = sorted(set(re.findall(r'(?:src|href)="([^"]+\.(?:js|css))"', html)))
    print("assets:", assets)

    found: dict[str, set[str]] = {}
    for url in assets:
        name = url.rstrip("/").split("/")[-1]
        r = requests.get(BASE + url, timeout=60)
        text = r.text
        (OUT / name).write_text(text, encoding="utf-8", errors="replace")
        print(f"{name}: {len(text)} bytes")
        for pat in PATTERNS:
            for m in re.findall(pat, text, re.I):
                found.setdefault(m if len(m) < 120 else pat, set()).add(name)

    print("\n=== matches ===")
    for key in sorted(found):
        print(f"{key}   <- {sorted(found[key])}")


if __name__ == "__main__":
    main()
