"""两组实验共用的小工具：打印、等待人操作、写结果表。"""
from __future__ import annotations

import csv
import datetime as _dt
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results" / "results.csv"
FIELDS = ["时间", "实验", "做法", "人", "人的修改被覆盖", "AI的修改丢失", "被删对象被AI重建",
          "恢复或合并出错", "不变量违反", "违规", "AI候选", "AI修改生效", "AI修改未采用", "人看到AI结果要等(秒)", "备注"]


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


def write_row(row: dict, path: Path = RESULTS, fields: list[str] = FIELDS) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():                       # 旧版本的表头不一样：改名留着，另起一个新文件
        with path.open(encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
        if header and header != fields:
            path.rename(path.with_name(f"{path.stem}_旧表头_{_dt.datetime.now():%Y%m%d_%H%M%S}{path.suffix}"))
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as f:   # utf-8-sig：Excel 打开不乱码
        w = csv.DictWriter(f, fieldnames=fields)
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


class InvariantLog:
    """在实验里每次操作之后检查三条不变量（rightofway/invariants.py），累计违反的地方。
    对照组（没有保护）不检查：它本来就不保证这些。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.violations: list = []

    def _add(self, vs) -> None:
        for v in vs:
            if v not in self.violations:
                self.violations.append(v)

    def after_run(self, session, report) -> None:
        if self.enabled and getattr(report, "status", "ok") == "ok":
            from rightofway.invariants import check_session
            self._add(check_session(session, report, poll=False))

    def ledger(self, rt, scene, granularity, labels=None) -> None:
        if self.enabled:
            from rightofway.invariants import check_ledger
            self._add(check_ledger(rt, scene, granularity, labels))

    def at_end(self, session) -> None:
        if self.enabled:
            from rightofway.invariants import check_session
            self._add(check_session(session, poll=True))

    def column(self):
        if not self.enabled:
            return "—"
        from rightofway.invariants import objects
        return len(objects(self.violations))

    def summary(self) -> str:
        if not self.enabled:
            return "不变量：对照组不检查"
        from rightofway.invariants import summarize
        return summarize(self.violations)


def breach_objects(reports) -> int:
    """违规涉及的应用对象个数（运行时如实报告了的：要保持的部分没能恢复）。"""
    return len({u.partition("#")[0] for r in reports for u in getattr(r, "breach", [])})


def say_alert(report) -> None:
    if getattr(report, "alert", ""):
        say("rt", "提醒人：" + report.alert)
