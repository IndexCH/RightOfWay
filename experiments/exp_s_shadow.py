"""实验 S：影子执行（试验，prior_art_solutions.md 第 6 项）。

影子执行：AI 的脚本先在另一个 Blender（影子）里执行，按面取差异，再经方式二（实时同步）的写入路径，
只把允许的改动写进人的场景。人的场景从来不会出现不允许的改动，不需要事后恢复——这是对任意代码真正做到
"事先阻止"的唯一路线。代价是失去原子性（靠写入前核对补上）和多一份场景。这个实验量一下代价和结局。

每种 AI 写法（精确修改、整体调整、清空重建）跑实验 A 的故事两遍：
    直接执行（方式一）：SharedSession.run_agent，在人的 Blender 里原子地执行、恢复、核对；
    影子执行：         同步（人 → 影子）→ 在影子里执行（带认回，不用恢复）→ 同步（影子 → 人，只写允许的）。
比较：AI 这一步的用时、人的修改被覆盖、AI 的修改丢失、重复对象、数据块分叉、账本 = 人的场景（不变量 I1）。

    python -m experiments.exp_s_shadow                   起两个 bpy 本地进程（需要 Python 3.11 + bpy）
    python -m experiments.exp_s_shadow --backend socket  两个开着的 Blender：人的 9876、影子 9877
"""
from __future__ import annotations

import argparse
import time
from collections import Counter

from rightofway.blender import local_server
from rightofway.blender.bridge import BridgeError, SocketBridge
from rightofway.blender.evaluate import evaluate
from rightofway.blender.live_sync import LiveSyncSession
from rightofway.blender.records import base_name
from rightofway.blender.shared_session import SharedSession
from rightofway.invariants import check_ledger
from rightofway.runtime import Runtime
from experiments import scenario_tree as sc
from experiments.common import banner, say, use_utf8_console
from experiments.exp_a_shared import _SETUP

HABITS = ("targeted", "broad", "rebuild")


def _fresh(*bridges) -> str:
    scene = "RightOfWay实验_影子_" + time.strftime("%H%M%S") + f"_{time.perf_counter_ns() % 10000}"
    for br in bridges:
        br.call("cleanup_scenes", {"prefix": "RightOfWay实验_"})
        br.execute(_SETUP.format(name=scene))
    return scene


def _dups(recs: dict) -> tuple[int, int]:
    objs = Counter(base_name(r["name"]) for r in recs.values() if r.get("kind") != "data")
    return sum(c - 1 for c in objs.values() if c > 1), 0


def _data_forks(bridge, scene: str) -> int:
    """人的场景里同类型、去掉 .001 后同名、都有人用的数据块（材质、网格……）多出来的份数。"""
    code = f'''
import bpy, json, re
sc = bpy.data.scenes[{scene!r}]
seen = {{}}
for o in sc.objects:
    ids = [o.data] if o.data is not None else []
    ids += [s.material for s in o.material_slots if s.material is not None]
    for x in ids:
        seen.setdefault((type(x).__name__, re.sub(r"\\.\\d{{3}}$", "", x.name)), set()).add(x.name)
print("FORKS=" + str(sum(len(v) - 1 for v in seen.values())))
'''
    out = bridge.execute(code)
    return int(out.split("FORKS=")[-1].strip().splitlines()[0])


def direct(hb, habit: str) -> dict:
    scene = _fresh(hb)
    rt = Runtime()
    s = SharedSession(rt, hb, scene=scene)
    s.start()
    s.run_agent(sc.ai_build(scene), "build")
    hb.execute(sc.human_sim(scene))
    s.poll()
    t0 = time.perf_counter()
    rep = s.run_agent(sc.habit_step(habit, "adjust", scene), habit)
    seconds = time.perf_counter() - t0
    final = s.scene_records()
    return _outcome(rt, s.human, final, seconds, hb, scene, check_ledger(rt, s.view, s.granularity), rep.outcome)


def shadow(hb, ab, habit: str) -> dict:
    scene = _fresh(hb, ab)
    rt = Runtime()
    ls = LiveSyncSession(rt, hb, ab, human_scene=scene, ai_scene=scene)
    ls.start()
    agent = ls.agent

    def run_in_shadow(code: str, label: str):
        t0 = time.perf_counter()
        ls.sync(label_=label + "-前")                    # 人的最新状态 → 影子（AI 这边没有改动）
        permit = rt.admit(agent)                          # 只取认回的规则、墓碑；记账走同步的路径
        rt.close_permit(permit, [])
        args = ls._ai_args(code=code, protected={}, protect=True, known={}, watch_outside=False,
                           undo_push=False, restore_selection=False, defer_if_editing=False)
        if permit.reidentify:
            args.update(reidentify=permit.reidentify, rebind=permit.rebind, no_recreate=permit.no_recreate)
        t1 = time.perf_counter()
        res = ab.call("run_agent", args)
        t2 = time.perf_counter()
        r = ls.sync(label_=label)                         # 影子 → 人：只写允许的
        rt.mark_seen(agent)                               # AI 的窗口（影子）已经和人的一样了
        t3 = time.perf_counter()
        return r, res, {"前同步": t1 - t0, "影子执行": t2 - t1, "后同步": t3 - t2}

    run_in_shadow(sc.ai_build(scene), "build")
    hb.execute(sc.human_sim(scene))
    r, res, parts = run_in_shadow(sc.habit_step(habit, "adjust", scene), habit)
    final = ls.hs.bridge.call("poll", ls.hs._args())["records"]
    out = _outcome(rt, ls.human, final, sum(parts.values()), hb, scene, check_ledger(rt, ls.hs.view, ls.g),
                   "committed")
    out["parts"] = parts
    out["errors"] = r.errors
    out["reidentified"] = len((res.get("identity") or {}).get("pairs", {}))
    return out


def _outcome(rt, human, final, seconds, hb, scene, ledger, outcome) -> dict:
    ev = evaluate(rt, final, human, granularity="aspect")
    dups, _ = _dups(final)
    return {"seconds": seconds, "human_overwritten": len(ev.human_overwritten), "ai_lost": len(ev.ai_lost),
            "dups": dups, "forks": _data_forks(hb, scene), "I1": len(ledger), "outcome": outcome}


def main(argv=None) -> int:
    use_utf8_console()
    ap = argparse.ArgumentParser(description="实验 S：影子执行（试验）")
    ap.add_argument("--backend", choices=["local", "socket"], default="local")
    ap.add_argument("--habit", default=",".join(HABITS))
    args = ap.parse_args(argv)
    procs = []
    if args.backend == "local":
        hp, ap_ = 9888, 9889
        print("正在启动两个本地 Blender 进程（bpy）……")
        procs = [(hp, local_server.spawn(hp)), (ap_, local_server.spawn(ap_))]
    else:
        hp, ap_ = 9876, 9877
    rows = []
    try:
        hb, ab = SocketBridge(port=hp), SocketBridge(port=ap_)
        for habit in [h.strip() for h in args.habit.split(",") if h.strip()]:
            say("info", f"—— {sc.HABITS[habit]}")
            d = direct(hb, habit)
            s = shadow(hb, ab, habit)
            rows.append((habit, d, s))
            say("info", "   影子执行用时分解：" + "，".join(f"{k} {v:.2f} 秒" for k, v in s["parts"].items())
                + (f"；同步检查：{'；'.join(s['errors'])}" if s["errors"] else ""))
    except BridgeError as e:
        print(e)
        return 1
    finally:
        for port, p in procs:
            local_server.shutdown(port, p)
    from experiments.run_all import print_table
    banner("直接执行 vs 影子执行（实验 A 的第 2 步）")
    table = [["写法", "做法", "用时(秒)", "人的修改被覆盖", "AI的修改丢失", "重复对象", "数据块分叉", "账本≠场景(I1)"]]
    for habit, d, s in rows:
        for name, x in (("直接执行", d), ("影子执行", s)):
            table.append([sc.HABITS[habit], name, f"{x['seconds']:.2f}", str(x["human_overwritten"]), str(x["ai_lost"]),
                          str(x["dups"]), str(x["forks"]), str(x["I1"])])
    print_table(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
