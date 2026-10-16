"""Мини-обёртка над adb для автоматизации кликов в Android-приложении."""
from __future__ import annotations

import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

ADB = r"C:\adb\adb.exe"


def _run(args: list[str], binary: bool = False, timeout: int = 30):
    proc = subprocess.run(
        [ADB, *args],
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0 and not binary:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"adb {' '.join(args)} -> код {proc.returncode}: {err}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


@dataclass
class Bounds:
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2


class UiNode:
    def __init__(self, attrib: dict[str, str]):
        self.attrib = attrib

    @property
    def text(self) -> str:
        return self.attrib.get("text", "")

    @property
    def resource_id(self) -> str:
        return self.attrib.get("resource-id", "")

    @property
    def desc(self) -> str:
        return self.attrib.get("content-desc", "")

    @property
    def bounds(self) -> Bounds | None:
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", self.attrib.get("bounds", ""))
        if not m:
            return None
        return Bounds(*(int(v) for v in m.groups()))

    def __repr__(self) -> str:
        return f"UiNode(text={self.text!r}, id={self.resource_id!r}, desc={self.desc!r})"


class Adb:
    def __init__(self, serial: str | None = None, adb_path: str = ADB):
        self.serial = serial
        self.adb_path = adb_path

    def _base(self) -> list[str]:
        return ["-s", self.serial] if self.serial else []

    def shell(self, cmd: str, timeout: int = 30) -> str:
        return _run([*self._base(), "shell", cmd], timeout=timeout)

    def exec_out(self, cmd: str, timeout: int = 30) -> bytes:
        return _run([*self._base(), "exec-out", cmd], binary=True, timeout=timeout)

    def devices(self) -> list[str]:
        out = _run(["devices"])
        serials = []
        for line in out.splitlines()[1:]:
            if "\t" in line and line.split("\t")[1].strip() == "device":
                serials.append(line.split("\t")[0].strip())
        return serials

    def ensure_device(self) -> str:
        serials = self.devices()
        if not serials:
            raise RuntimeError("Нет подключённых устройств. Подключите телефон по USB и включите отладку.")
        if self.serial is None:
            self.serial = serials[0]
        elif self.serial not in serials:
            raise RuntimeError(f"Устройство {self.serial} не найдено. Доступны: {serials}")
        return self.serial

    def size(self) -> tuple[int, int]:
        out = self.shell("wm size")
        m = re.search(r"(\d+)x(\d+)", out)
        if not m:
            raise RuntimeError("Не удалось определить размер экрана")
        return int(m.group(1)), int(m.group(2))

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self.swipe(x, y, x, y, duration_ms)

    def key(self, code: str) -> None:
        self.shell(f"input keyevent {code}")

    def back(self) -> None:
        self.key("KEYCODE_BACK")

    def home(self) -> None:
        self.key("KEYCODE_HOME")

    def launch(self, package: str, activity: str | None = None) -> None:
        if activity:
            self.shell(f"am start -n {package}/{activity}")
        else:
            self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

    def force_stop(self, package: str) -> None:
        self.shell(f"am force-stop {package}")

    def screenshot(self, path: str | Path) -> Path:
        data = self.exec_out("screencap -p")
        path = Path(path)
        path.write_bytes(data)
        return path

    def dump_ui(self) -> list[UiNode]:
        self.shell("uiautomator dump /sdcard/ui.xml >/dev/null 2>&1")
        xml = _run([*self._base(), "exec-out", "cat /sdcard/ui.xml"])
        root = ET.fromstring(xml)
        return [UiNode(el.attrib) for el in root.iter("node")]

    def find(self, text: str | None = None, resource_id: str | None = None,
             desc: str | None = None, contains: bool = False) -> UiNode | None:
        def match(value: str, target: str | None) -> bool:
            if target is None:
                return True
            return (target in value) if contains else (value == target)

        for node in self.dump_ui():
            if match(node.text, text) and match(node.resource_id, resource_id) and match(node.desc, desc):
                if node.bounds:
                    return node
        return None

    def tap_text(self, text: str, contains: bool = True, timeout: float = 5.0) -> bool:
        node = self.wait_for(text=text, contains=contains, timeout=timeout)
        if node and node.bounds:
            self.tap(*node.bounds.center)
            return True
        return False

    def wait_for(self, text: str | None = None, resource_id: str | None = None,
                 desc: str | None = None, contains: bool = False,
                 timeout: float = 10.0, interval: float = 0.5) -> UiNode | None:
        deadline = time.time() + timeout
        while True:
            node = self.find(text=text, resource_id=resource_id, desc=desc, contains=contains)
            if node:
                return node
            if time.time() >= deadline:
                return None
            time.sleep(interval)

    def current_package(self) -> str:
        out = self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
        m = re.search(r"([a-zA-Z0-9_.]+)/", out)
        return m.group(1) if m else ""


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="ADB-инструмент")
    parser.add_argument("-s", "--serial", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices")
    sub.add_parser("size")
    sub.add_parser("ui")
    sub.add_parser("current")

    p_shot = sub.add_parser("shot")
    p_shot.add_argument("path", nargs="?", default="screen.png")

    p_tap = sub.add_parser("tap")
    p_tap.add_argument("x", type=int)
    p_tap.add_argument("y", type=int)

    p_taptext = sub.add_parser("tap-text")
    p_taptext.add_argument("text")

    p_launch = sub.add_parser("launch")
    p_launch.add_argument("package")

    args = parser.parse_args()
    adb = Adb(args.serial)

    if args.cmd == "devices":
        print("\n".join(adb.devices()) or "нет устройств")
        return
    adb.ensure_device()
    if args.cmd == "size":
        print(adb.size())
    elif args.cmd == "ui":
        for node in adb.dump_ui():
            if node.text or node.resource_id or node.desc:
                print(node, node.bounds)
    elif args.cmd == "current":
        print(adb.current_package())
    elif args.cmd == "shot":
        print(adb.screenshot(args.path))
    elif args.cmd == "tap":
        adb.tap(args.x, args.y)
    elif args.cmd == "tap-text":
        print(adb.tap_text(args.text))
    elif args.cmd == "launch":
        adb.launch(args.package)


if __name__ == "__main__":
    _cli()
