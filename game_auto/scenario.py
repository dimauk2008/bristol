"""Сценарий сбора призов. Координаты и точки нажатия задаются в STEPS."""
from __future__ import annotations

import time

from adb import Adb, UiNode

PACKAGE = "PUT.PACKAGE.NAME.HERE"
PRIZE_DELAY = 1.0
LOOP_DELAY = 5.0


def get_prizes(adb: Adb) -> int:
    collected = 0
    node: UiNode | None = adb.find(text="Забрать", contains=True)
    while node and node.bounds:
        adb.tap(*node.bounds.center)
        time.sleep(PRIZE_DELAY)
        collected += 1
        node = adb.find(text="Забрать", contains=True)
    return collected


def run_once(adb: Adb) -> None:
    adb.ensure_device()
    adb.launch(PACKAGE)
    time.sleep(3)


def main() -> None:
    adb = Adb()
    run_once(adb)
    while True:
        count = get_prizes(adb)
        print(f"Собрано призов: {count}")
        time.sleep(LOOP_DELAY)


if __name__ == "__main__":
    main()
