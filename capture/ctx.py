"""Контекст вокруг совпадений в бандле: python ctx.py <паттерн> [окно]"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent / "bundle" / "index-BZ-oGyT-.js"


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python ctx.py <pattern> [window]")
        return
    pat = sys.argv[1]
    window = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    text = BUNDLE.read_text(encoding="utf-8", errors="replace")
    hits = list(re.finditer(pat, text, re.I))
    print(f"hits: {len(hits)}")
    for m in hits[:int(sys.argv[3]) if len(sys.argv) > 3 else 25]:
        start = max(0, m.start() - window)
        end = min(len(text), m.end() + window)
        print("=" * 90)
        print(text[start:end])


if __name__ == "__main__":
    main()
