"""两组实验共用的小工具：打印、等待人操作、写结果表。"""
from __future__ import annotations

import csv
import datetime as _dt
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results" / "results.csv"
FIELDS = ["时间", "实验", "做法", "人", "人的修改被覆盖", "AI的修改丢失", "被删对象被AI重建",
          "恢复或合并出错", "AI候选", "AI修改生效", "AI修改未采用", "人看到AI结果要等(秒)", "备注"]


def banner(text: str) -> None:
    print("\n" + "=" * 70 + f"\n{text}\n" + "=" * 70)


def say(who: str, text: str) -> None:
    prefix = {"ai": "【AI】", "human": "【人】", "rt": "【运行时】", "info": "  "}[who]
    for i, line in enumerate(str(text).splitlines() or [""]):
        print((prefix if i == 0 else " " * len(prefix) * 2) + line)


def wait_for_human(steps: list[str], extra: str = "") -> None:
    print("\n请在 Blender 里做下面这些操作：")
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {s}")
    if extra:
        print(extra)
    input("\n做完后回到这里按回车 ▶ ")


def write_row(row: dict, path: Path = RESULTS) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as f:   # utf-8-sig：Excel 打开不乱码
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({"时间": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **row})
    return path


def use_utf8_console() -> None:
    """Windows 终端默认不是 UTF-8，中文会乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
