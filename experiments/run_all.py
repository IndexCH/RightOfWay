"""一次重跑全部测试和实验，最后汇总成一张表。

在项目根目录运行：
    python -m experiments.run_all                  全部实验，模拟人的操作（不用动手）
    python -m experiments.run_all --human real     全部实验，真人操作（每一项开始前会提示，输入 s 跳过）
    python -m experiments.run_all --only A,D       只跑某几组：T（测试）、A、B、C、D、F
    python -m experiments.run_all --quick          每组只跑主要的那一种

各组要准备的（没准备好的会自动跳过，并说明原因）：
    A  同一份，实时                Blender 开着，N 面板 → BlenderMCP → Start MCP Server（端口 9876）
    D  两个 AI + 一个人，同一份    同 A
    B  各自一个窗口，实时同步      再开一个 Blender，BlenderMCP 面板里把 Port 改成 9877 再 Start MCP Server
    F  文件兜底（存盘后合并）      装了 Blender 就行，脚本会在后台自己启动无界面的 Blender
    C  Unity，同一份               Unity 6 开着并装了 AI Assistant；pip install -e ".[unity]"；连不上就先关掉 Claude Desktop

开始前会把旧的 experiments/results/results.csv 改名备份，这次的结果写进新的 results.csv。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import socket
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from experiments.common import RESULTS, banner, use_utf8_console

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Item:
    group: str                 # T / A / B / C
    title: str
    cmd: list[str]
    main: bool = True          # --quick 时只跑 main
    real_capable: bool = True  # 这一项有没有"真人"版本
    status: str = ""           # 通过 / 失败 / 跳过：原因
    rows: list[dict] = field(default_factory=list)


def _module(name: str, *args: str) -> list[str]:
    return [sys.executable, "-m", name, *args]


def plan(args) -> list[Item]:
    human = ["--human", args.human]
    a_base = ["--backend", args.a_backend, *human]
    b_base = ["--backend", args.b_backend, *human]
    c_base = [*human] + (["--relay", args.relay] if args.relay else [])
    exp = lambda name, *a: _module(f"experiments.{name}", *a)       # noqa: E731
    items = [
        Item("T", "单元测试（pytest）", _module("pytest", "-q", "-p", "no:cacheprovider"), real_capable=False),
        Item("A", "实验 A 同一份：有保护（按面）", exp("exp_a_shared", *a_base)),
        Item("A", "实验 A 同一份：有保护（按对象）", exp("exp_a_shared", *a_base, "--granularity", "object"), main=False),
        Item("A", "实验 A 同一份：对照组（没有保护）", exp("exp_a_shared", *a_base, "--no-protocol"), main=False),
        Item("A", "实验 A 同一份：AI 动手前不先看提示", exp("exp_a_shared", *a_base, "--ai-no-look"), main=False),
        Item("A", "实验 A 同一份：选中即占用", exp("exp_a_shared", *a_base, "--occupy-selection"), main=False),
        Item("D", "实验 D 两个 AI + 人：有保护", exp("exp_d_multi", *a_base)),
        Item("D", "实验 D 两个 AI + 人：对照组（没有保护）", exp("exp_d_multi", *a_base, "--no-protocol"), main=False),
        Item("B", "实验 B 各自一个窗口：实时同步（按面）", exp("exp_b_live", *b_base)),
        Item("B", "实验 B 各自一个窗口：实时同步（按对象）", exp("exp_b_live", *b_base, "--granularity", "object"), main=False),
        Item("B", "实验 B 各自一个窗口：对照组（谁后同步谁生效）", exp("exp_b_live", *b_base, "--no-protocol"), main=False),
        Item("F", "实验 B 文件兜底：存盘合并（按面）", exp("exp_b_files", "--blender", args.blender, *human)),
        Item("F", "实验 B 文件兜底：存盘合并（按对象）", exp("exp_b_files", "--blender", args.blender, *human, "--granularity", "object"), main=False),
        Item("C", "实验 C Unity 同一份：有保护（按面）", exp("exp_c_unity", *c_base)),
        Item("C", "实验 C Unity 同一份：有保护（按对象）", exp("exp_c_unity", *c_base, "--granularity", "object"), main=False),
        Item("C", "实验 C Unity 同一份：对照组（没有保护）", exp("exp_c_unity", *c_base, "--no-protocol"), main=False),
    ]
    only = {g.strip().upper() for g in args.only.split(",")} if args.only else None
    return [it for it in items if (only is None or it.group in only) and (it.main or not args.quick)]


# ---------------------------------------------------------------- 准备情况检查

def _can_import(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _port_open(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1.5).close()
        return True
    except OSError:
        return False


def check(group: str, args) -> str:
    """返回空字符串表示可以跑，否则是跳过的原因。"""
    if group == "T":
        return "" if _can_import("pytest") else '没有装 pytest：pip install -e ".[dev]"'
    if group in ("A", "D"):
        if args.a_backend == "inprocess":
            return "" if _can_import("bpy") else "当前 Python 不能 import bpy（需要 Python 3.11 + pip install bpy）"
        return "" if _port_open(9876) else "连不上 Blender MCP 插件：打开 Blender，N 面板 → BlenderMCP → Start MCP Server"
    if group == "B":
        if args.b_backend == "local":
            return "" if _can_import("bpy") else "当前 Python 不能 import bpy（需要 Python 3.11 + pip install bpy）"
        missing = [str(p) for p in (9876, 9877) if not _port_open(p)]
        return "" if not missing else ("连不上端口 " + "、".join(missing) + " 上的 Blender MCP 插件：需要开两个 Blender，"
                                       "你的用 9876，另一个在 BlenderMCP 面板里把 Port 改成 9877 再 Start MCP Server")
    if group == "F":
        if args.blender == "inprocess":
            return "" if _can_import("bpy") else "当前 Python 不能 import bpy"
        if args.blender != "auto":
            return "" if Path(args.blender).exists() else f"找不到 {args.blender}"
        if _can_import("bpy"):
            return ""
        from rightofway.blender.bridge import find_blender
        return "" if find_blender() else "找不到 Blender：用 --blender 指定 blender.exe，或设置环境变量 BLENDER_EXE"
    if group == "C":
        if not _can_import("mcp"):
            return '没有装 MCP 客户端：pip install -e ".[unity]"'
        from rightofway.unity.bridge import default_relay_path
        relay = args.relay or default_relay_path()
        return "" if Path(relay).exists() else f"找不到 Unity MCP 中继：{relay}（Unity 6 + AI Assistant，并在 Unity 里启用过 MCP）"
    return ""


# ---------------------------------------------------------------- 结果表

def read_rows(path: Path = RESULTS) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def verdict(row: dict) -> str:
    over, lost = _int(row.get("人的修改被覆盖")), _int(row.get("AI的修改丢失"))
    err, resur = _int(row.get("恢复或合并出错")), _int(row.get("被删对象被AI重建"))
    if row.get("做法", "").startswith("对照"):
        return "如预期：出了问题" if over + lost > 0 else "意外：对照组没出问题"
    ok = over == 0 and lost == 0 and err == 0
    text = "通过" if ok else "未通过"
    if resur:
        text += "（AI 重建了人删掉的同名对象，只能提示）"
    return text


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, w: int) -> str:
    return s + " " * (w - _width(s))


def print_table(rows: list[list[str]]) -> None:
    widths = [max(_width(r[i]) for r in rows) for i in range(len(rows[0]))]
    for k, r in enumerate(rows):
        print("  " + " │ ".join(_pad(c, widths[i]) for i, c in enumerate(r)))
        if k == 0:
            print("  " + "─┼─".join("─" * w for w in widths))


# ---------------------------------------------------------------- 主流程

def main(argv=None) -> int:
    use_utf8_console()
    ap = argparse.ArgumentParser(description="重跑全部测试和实验")
    ap.add_argument("--human", choices=["sim", "real"], default="sim")
    ap.add_argument("--only", default="", help="只跑某几组，逗号分隔：T,A,B,C,D,F")
    ap.add_argument("--quick", action="store_true", help="每组只跑主要的一种")
    ap.add_argument("--a-backend", choices=["socket", "inprocess"], default="socket",
                    help="实验 A、D：socket 连接打开着的 Blender（默认）；inprocess 用 pip 装的 bpy")
    ap.add_argument("--b-backend", choices=["socket", "local"], default="socket",
                    help="实验 B：socket 连接两个开着的 Blender（默认）；local 起两个用 bpy 的本地进程")
    ap.add_argument("--blender", default="auto", help="文件兜底：auto、inprocess，或 blender.exe 的路径")
    ap.add_argument("--relay", default=None, help="实验 C：Unity MCP 中继的路径")
    ap.add_argument("--keep-results", action="store_true", help="不备份旧结果，接着往 results.csv 里追加")
    args = ap.parse_args(argv)
    os.chdir(ROOT)

    items = plan(args)
    if not items:
        print("没有要跑的项目（检查 --only）")
        return 1

    if RESULTS.exists() and not args.keep_results:
        backup = RESULTS.with_name(f"results_{dt.datetime.now():%Y%m%d_%H%M%S}.csv")
        RESULTS.rename(backup)
        print(f"旧结果已备份为 {backup.name}，这次的结果写进新的 {RESULTS.name}。")

    banner(f"重跑全部实验（人：{'真人' if args.human == 'real' else '模拟'}，共 {len(items)} 项）")
    ready: dict[str, str] = {}
    for it in items:
        if it.group not in ready:
            ready[it.group] = check(it.group, args)
        print(f"  {'✓' if not ready[it.group] else '✗'} {it.title}" + (f"   → 跳过：{ready[it.group]}" if ready[it.group] else ""))

    t_all = time.perf_counter()
    for n, it in enumerate(items, 1):
        if ready[it.group]:
            it.status = "跳过：" + ready[it.group]
            continue
        banner(f"[{n}/{len(items)}] {it.title}")
        if args.human == "real" and it.real_capable:
            ans = input("准备好了按回车开始，输入 s 跳过这一项 ▶ ").strip().lower()
            if ans == "s":
                it.status = "跳过：手动跳过"
                continue
        before = len(read_rows())
        t = time.perf_counter()
        proc = subprocess.run(it.cmd, cwd=ROOT)
        secs = time.perf_counter() - t
        it.rows = read_rows()[before:]
        if it.group == "T":
            it.status = f"{'通过' if proc.returncode == 0 else '失败'}（{secs:.0f} 秒）"
        elif proc.returncode != 0 or not it.rows:
            it.status = f"没跑完（退出码 {proc.returncode}），原因看上面的输出"
        else:
            it.status = f"跑完（{secs:.0f} 秒）"

    # 汇总
    banner(f"汇总（总用时 {time.perf_counter() - t_all:.0f} 秒）")
    for it in items:
        print(f"  {it.title}：{it.status}")
    table = [["实验", "做法", "人", "人的修改被覆盖", "AI的修改丢失", "AI重建被删对象", "出错", "判定"]]
    for it in items:
        for r in it.rows:
            table.append([r.get("实验", ""), r.get("做法", ""), r.get("人", ""),
                          str(r.get("人的修改被覆盖", "")), str(r.get("AI的修改丢失", "")),
                          str(r.get("被删对象被AI重建", "")), str(r.get("恢复或合并出错", "")), verdict(r)])
    if len(table) > 1:
        print()
        print_table(table)
        print(f"\n完整结果（含备注、用时）：{RESULTS}")
        print("判定规则：有保护/合并的行，人的修改被覆盖、AI 的修改丢失、出错都应该是 0；"
              "对照组的行应该至少有一个不是 0（说明不做保护确实会出问题）。")
    failed = [it for it in items if it.status.startswith(("失败", "没跑完"))]
    bad = [r for it in items for r in it.rows if verdict(r).startswith(("未通过", "意外"))]
    return 1 if failed or bad else 0


if __name__ == "__main__":
    sys.exit(main())
