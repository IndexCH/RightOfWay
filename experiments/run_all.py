"""一次重跑全部测试和实验，最后汇总成一张表。

在项目根目录运行：
    python -m experiments.run_all                  全部实验，模拟人的操作（不用动手）
    python -m experiments.run_all --human real     全部实验，真人操作（每一项开始前会提示，输入 s 跳过）
    python -m experiments.run_all --only A,D       只跑某几组：T（测试）、A、B、C、D、E、F、H、P、S
    python -m experiments.run_all --quick          每组只跑主要的那一种

各组要准备的（没准备好的会自动跳过，并说明原因）：
    A  同一份，实时                Blender 开着，N 面板 → BlenderMCP → Start MCP Server（端口 9876）
    D  两个 AI + 一个人，同一份    同 A
    B  各自一个窗口，实时同步      再开一个 Blender，BlenderMCP 面板里把 Port 改成 9877 再 Start MCP Server
    F  文件兜底（存盘后合并）      装了 Blender 就行，脚本会在后台自己启动无界面的 Blender
    C  Unity，同一份               Unity 6 开着并装了 AI Assistant；pip install -e ".[unity]"；连不上就先关掉 Claude Desktop
    E  AI 删掉人改过的对象         模拟 Unity 的那一项什么都不用准备；Blender、Unity 那几项分别同 A、C
                                   （应用恢复不了的删除要如实报告为"违规"）
    H  按真实 AI 的写法重跑 A、D、E  模拟应用的几项什么都不用准备；Blender 那几项同 A。
                                   这是测量（重叠多频繁、重建会不会出现两份），不判通过与否，结果另外列一张表
    P  经过 MCP 代理              pip install -e ".[proxy]"；上游是假的应用 MCP 服务器，不用别的软件
    S  影子执行（试验）            同 B（两个 Blender，或者 --b-backend local 用 bpy）；测量，只看能不能跑完

每一项有保护的实验都会在每次操作后检查三条不变量（场景、账本、告诉 AI 的话是否一致），记在"不变量违反"一列。

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

HABITS = RESULTS.with_name("habits.csv")
SWEEP = RESULTS.with_name("habits_sweep.csv")

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Item:
    group: str                 # T / A / B / C
    title: str
    cmd: list[str]
    main: bool = True          # --quick 时只跑 main
    real_capable: bool = True  # 这一项有没有"真人"版本
    needs: str = ""            # 准备情况按哪一组检查（默认就是 group）
    results: Path | None = RESULTS   # 这一项的结果写在哪个表里（None：只看退出码）
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
        Item("E", "实验 E 删掉人改过的对象：模拟 Unity 接入（恢复不了删除）", exp("exp_e_delete", "--backend", "fake-unity"),
             real_capable=False, needs="-"),
        Item("E", "实验 E 删掉人改过的对象：Blender（按面）", exp("exp_e_delete", *a_base), needs="A"),
        Item("E", "实验 E 删掉人改过的对象：Blender（按对象）", exp("exp_e_delete", *a_base, "--granularity", "object"),
             main=False, needs="A"),
        Item("E", "实验 E 删掉人改过的对象：Unity（按面）", exp("exp_e_delete", "--backend", "unity", *c_base),
             main=False, needs="C"),
        Item("H", "实验 H 真实 AI 写法：模拟 Unity 接入（A、D、E × 4 种写法）", exp("exp_h_habits", "--backend", "fake-unity"),
             real_capable=False, needs="-", results=HABITS),
        Item("H", "实验 H 真实 AI 写法：模拟 Unity 接入，对照：不认回同一个对象",
             exp("exp_h_habits", "--backend", "fake-unity", "--no-reidentify"), main=False, real_capable=False, needs="-",
             results=HABITS),
        Item("H", "实验 H 真实 AI 写法：Blender（A、D、E × 3 种写法）", exp("exp_h_habits", "--backend", args.a_backend),
             real_capable=False, needs="A", results=HABITS),
        Item("H", "实验 H 真实 AI 写法：Blender，对照：不认回同一个对象",
             exp("exp_h_habits", "--backend", args.a_backend, "--no-reidentify"), main=False, real_capable=False,
             needs="A", results=HABITS),
        Item("H", "实验 H 人改一处、撞上 AI 这一步的概率：模拟 Unity 接入", exp("exp_h_habits", "--backend", "fake-unity", "--sweep"),
             main=False, real_capable=False, needs="-", results=None),
        Item("H", "实验 H 人改一处、撞上 AI 这一步的概率：Blender", exp("exp_h_habits", "--backend", args.a_backend, "--sweep"),
             main=False, real_capable=False, needs="A", results=None),
        Item("P", "实验 P 经过 MCP 代理：假的应用 MCP 服务器", exp("exp_p_proxy"), real_capable=False),
        Item("S", "实验 S 影子执行（试验）：直接执行 vs 影子执行", exp("exp_s_shadow", "--backend",
             "local" if args.b_backend == "local" else "socket"), main=False, real_capable=False, needs="B", results=None),
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
    if group == "-":
        return ""
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
    if group == "P":
        return "" if _can_import("mcp") else '没有装 MCP：pip install -e ".[proxy]"'
    if group == "C":
        if not _can_import("mcp"):
            return '没有装 MCP 客户端：pip install -e ".[unity]"'
        from rightofway.unity.bridge import default_relay_path
        relay = args.relay or default_relay_path()
        return "" if Path(relay).exists() else f"找不到 Unity MCP 中继：{relay}（Unity 6 + AI Assistant，并在 Unity 里启用过 MCP）"
    return ""


# ---------------------------------------------------------------- 结果表

def read_rows(path: Path | None = RESULTS) -> list[dict]:
    if path is None or not path.exists():
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
    inv, breach = _int(row.get("不变量违反")), _int(row.get("违规"))
    if row.get("做法", "").startswith("对照"):
        return "如预期：出了问题" if over + lost > 0 else "意外：对照组没出问题"
    ok = over == 0 and lost == 0 and err == 0 and inv == 0 and breach == 0
    if ok:
        text = "通过"
    elif inv == 0 and breach > 0 and over + lost <= breach and err <= breach:
        # 应用恢复不了：人的修改没保住，但如实报告了、人收到了提醒、AI 被暂停（design_v0.5.md 第 6 节）
        text = f"违规 {breach} 处，已如实报告"
        if not row.get("实验", "").startswith("E"):
            text = "未通过：" + text            # 只有实验 E 是专门测"恢复不了"的
    else:
        text = "未通过（不变量）" if over == lost == err == 0 else "未通过"
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
    ap.add_argument("--only", default="", help="只跑某几组，逗号分隔：T,A,B,C,D,E,F,H")
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

    stamp = f"{dt.datetime.now():%Y%m%d_%H%M%S}"
    for path in (RESULTS, HABITS, SWEEP):
        if path.exists() and not args.keep_results:
            backup = path.with_name(f"{path.stem}_{stamp}.csv")
            path.rename(backup)
            print(f"旧结果已备份为 {backup.name}，这次的结果写进新的 {path.name}。")

    banner(f"重跑全部实验（人：{'真人' if args.human == 'real' else '模拟'}，共 {len(items)} 项）")
    ready: dict[str, str] = {}
    for it in items:
        key = it.needs or it.group
        if key not in ready:
            ready[key] = check(key, args)
        print(f"  {'✓' if not ready[key] else '✗'} {it.title}" + (f"   → 跳过：{ready[key]}" if ready[key] else ""))

    t_all = time.perf_counter()
    for n, it in enumerate(items, 1):
        if ready[it.needs or it.group]:
            it.status = "跳过：" + ready[it.needs or it.group]
            continue
        banner(f"[{n}/{len(items)}] {it.title}")
        if args.human == "real" and it.real_capable:
            ans = input("准备好了按回车开始，输入 s 跳过这一项 ▶ ").strip().lower()
            if ans == "s":
                it.status = "跳过：手动跳过"
                continue
        before = len(read_rows(it.results))
        t = time.perf_counter()
        proc = subprocess.run(it.cmd, cwd=ROOT)
        secs = time.perf_counter() - t
        it.rows = read_rows(it.results)[before:]
        if it.group == "T":
            it.status = f"{'通过' if proc.returncode == 0 else '失败'}（{secs:.0f} 秒）"
        elif proc.returncode != 0 or (it.results is not None and not it.rows):
            it.status = f"没跑完（退出码 {proc.returncode}），原因看上面的输出"
        else:
            it.status = f"跑完（{secs:.0f} 秒）"

    # 汇总
    banner(f"汇总（总用时 {time.perf_counter() - t_all:.0f} 秒）")
    for it in items:
        print(f"  {it.title}：{it.status}")
    table = [["实验", "做法", "人", "人的修改被覆盖", "AI的修改丢失", "AI重建被删对象", "出错", "不变量", "违规", "判定"]]
    for it in items:
        if it.results != RESULTS:
            continue
        for r in it.rows:
            table.append([r.get("实验", ""), r.get("做法", ""), r.get("人", ""),
                          str(r.get("人的修改被覆盖", "")), str(r.get("AI的修改丢失", "")),
                          str(r.get("被删对象被AI重建", "")), str(r.get("恢复或合并出错", "")),
                          str(r.get("不变量违反", "") or "—"), str(r.get("违规", "") or "—"), verdict(r)])
    if len(table) > 1:
        print()
        print_table(table)
        print(f"\n完整结果（含备注、用时）：{RESULTS}")
        print("判定规则：有保护/合并的行，人的修改被覆盖、AI 的修改丢失、出错、不变量违反（涉及的对象数）都应该是 0；"
              "对照组的行应该至少有一个不是 0（说明不做保护确实会出问题）。"
              "实验 E 里应用恢复不了的删除（模拟 Unity）应该如实报告为违规：不变量成立、人收到提醒、AI 被暂停。")
    habit_rows = [r for it in items if it.results == HABITS for r in it.rows]
    if habit_rows:
        print("\n实验 H（测量，不判通过与否）：按真实 AI 的写法，重叠多频繁、重建会不会出现两份、不变量还成不成立")
        cols = ["场景", "AI写法", "应用", "认回同一个对象", "改到的对象", "和人重叠的对象", "和别的AI重叠的对象", "重复对象",
                "重建换编号", "人的修改被覆盖", "AI的修改丢失", "违规", "不变量违反"]
        print_table([["场景", "写法", "应用", "认回", "改到", "和人重叠", "和AI重叠", "重复", "换编号", "人被覆盖", "AI丢失",
                      "违规", "不变量"]] + [[str(r.get(c, "")) for c in cols] for r in habit_rows])
        print(f"\n完整结果：{HABITS}；人改一处撞上的概率（--sweep）每一次的结果：{SWEEP}")
    failed = [it for it in items if it.status.startswith(("失败", "没跑完"))]
    bad = [r for it in items if it.results == RESULTS for r in it.rows if verdict(r).startswith(("未通过", "意外"))]
    return 1 if failed or bad else 0


if __name__ == "__main__":
    sys.exit(main())
