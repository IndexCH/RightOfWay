"""实验 H：按真实 AI 的写法重跑实验 A、D、E（related_work.md 2c）。

之前的实验里，AI 的脚本只精确地改几处。真实的 AI 不这样写：
    整体调整  遍历场景里的所有物体，按类型统一设置位置、旋转、缩放、材质
    清空重建  先清空场景，再按自己的计划重新搭一遍（每轮重新生成整个程序，SceneCraft、LL3M 一类）
    批量命令  Unity MCP（CoplayDev）的 batch_execute：一批最多 25 条类型化命令，不是事务
三种代码写法和批量命令表达的是同一个计划，只是写法不同（experiments/scenario_tree.py、scenario_fake.py）。

量三件事：
    1. 重叠有多频繁：AI 改到的对象里，有多少碰到了人（或别的 AI）刚改过、要保持的部分；
    2. 重建会不会出现两份：被删掉又恢复的对象和 AI 新建的同名对象并存（重复对象）；
       删掉再新建的同名对象换了编号（重建换编号）；
    3. 不变量还成不成立，有没有违规。
这是测量，不判通过与否。结果写进 experiments/results/habits.csv，最后打印一张对比表。

运行方式（在项目根目录）：
    python -m experiments.exp_h_habits --backend fake-unity     不需要任何软件：模拟 Unity 接入
                                                                （恢复不了被删的对象、允许重名、编号不能转交给新对象）
    python -m experiments.exp_h_habits --backend fake           同上，但和 Blender 接入一样（能恢复删除、重名自动加 .001）
    python -m experiments.exp_h_habits --backend inprocess      pip 装的 bpy（需要 Python 3.11）
    python -m experiments.exp_h_habits                          打开着的 Blender（端口 9876），每种组合新建一个场景
可选：--scenario A,E   --habit broad,rebuild   --granularity object   --max-batch 4   --no-csv
      --no-reidentify：关掉"认回同一个对象"（默认打开，design_v0.5.md 12.4、12.5），作为对照
批量命令只在假应用上跑：Blender 的 AI 通过执行代码操作，没有类型化命令。

--sweep：逐一试人的单个修改。AI 布置好场景之后，人改一处（每个对象的移动、旋转、缩放、删除……逐一试），
AI 不知道，按原计划执行实验 A 的第 2 步。统计撞上的概率和撞上之后的结局，结果写进 habits_sweep.csv。
"""
from __future__ import annotations

import argparse
import time
from collections import Counter
from pathlib import Path

from rightofway.blender.evaluate import evaluate
from rightofway.blender.records import base_name, split_unit, units
from rightofway.blender.shared_session import R_HUMAN, R_SELECTED, SharedSession
from rightofway.fakeapp import FakeApp, FakeBridge
from rightofway.runtime import Runtime
from experiments import scenario_fake as sf
from experiments import scenario_tree as sc
from experiments.common import InvariantLog, banner, say, use_utf8_console, write_row

HABITS_CSV = Path(__file__).resolve().parent / "results" / "habits.csv"
SWEEP_CSV = Path(__file__).resolve().parent / "results" / "habits_sweep.csv"
SWEEP_FIELDS = ["时间", "AI写法", "应用", "粒度", "认回同一个对象", "人的修改", "对象", "撞上同一个面", "撞上同一个对象",
                "结局", "重复对象", "重复数据块", "不变量违反", "备注"]
EDIT_KINDS = {"move": "移动", "rotate": "旋转", "scale": "缩放", "recolor": "改颜色", "unparent": "取消父对象",
              "energy": "调亮度", "delete": "删除"}
FIELDS = ["时间", "场景", "AI写法", "应用", "粒度", "认回同一个对象", "AI执行次数", "改到的对象", "和人重叠的对象", "和别的AI重叠的对象",
          "重叠率", "重复对象", "重复数据块", "重建换编号", "人的修改被覆盖", "AI的修改丢失", "违规", "不变量违反", "AI被暂停后拒绝",
          "命令：执行前拒绝/部分执行/失败", "AI修改生效", "AI修改未采用", "备注"]
BACKENDS = {"fake-unity": "模拟 Unity 接入（恢复不了删除、编号不能转交）", "fake": "模拟 Blender 接入（能恢复删除）",
            "inprocess": "Blender（bpy）", "socket": "Blender"}
SCENARIOS = {"A": "一个 AI + 人", "D": "两个 AI + 人", "E": "AI 删掉人改过的对象"}
LAYOUT, LOOK, AGENT = "layout", "look", "agent"


# ---------------------------------------------------------------------------
# 两种应用：Blender（执行代码）和假应用（执行代码，或者类型化命令）
# ---------------------------------------------------------------------------
class BlenderTasks:
    habits = sc.BLENDER_HABITS

    def __init__(self, bridge, scene) -> None:
        self.bridge, self.scene = bridge, scene

    def build(self):
        return "code", sc.ai_build(self.scene)

    def human(self, scenario: str) -> None:
        code = {"A": sc.human_sim, "D": sc.human_sim_d, "E": sc.human_move_leaf3}[scenario](self.scene)
        self.bridge.execute(code)

    def step(self, habit: str, role: str, skip=(), view=None, values=None):
        return "code", sc.habit_step(habit, role, self.scene, skip)

    def sweep_edits(self):
        return sc.sweep_edits()

    def apply_edit(self, kind: str, target: str) -> None:
        self.bridge.execute(sc.human_edit(kind, target, self.scene))


class FakeTasks:
    habits = sf.HABITS

    def __init__(self, app: FakeApp) -> None:
        self.app = app

    def build(self):
        return "code", sf.ai_build()

    def human(self, scenario: str) -> None:
        sf.HUMAN[scenario](self.app)

    def step(self, habit: str, role: str, skip=(), view=None, values=None):
        return sf.habit_step(habit, role, skip, view, values)

    def sweep_edits(self):
        return sf.sweep_edits(self.app)

    def apply_edit(self, kind: str, target: str) -> None:
        sf.human_edit(self.app, kind, target)


def duplicates(final: dict) -> tuple[dict, dict]:
    """场景里的重复：(同名的对象, 同名同类型、都有人在用的数据块)。数据块重复就是"分叉"
    （例如共用材质被恢复成两份）；没人用的旧数据块（重建后留下的网格）不算。"""
    objs = Counter(base_name(x["name"]) for x in final.values() if x.get("kind") != "data")
    datas = Counter((x.get("type"), base_name(x["name"])) for x in final.values()
                    if x.get("kind") == "data" and x.get("users", 1) > 0)
    return ({n: c for n, c in objs.items() if c > 1},
            {f"{n}（{str(t).removeprefix('data:')}）": c for (t, n), c in datas.items() if c > 1})


def _setup(args, scenario: str, habit: str):
    if args.backend == "fake":                      # 和 Blender 一样：能恢复删除，重名自动加 .001，编号能转交
        app = FakeApp(unique_names=True)
        return FakeBridge(app), None, FakeTasks(app)
    if args.backend == "fake-unity":                # 和 Unity 一样：恢复不了删除，允许重名，编号由应用分配
        app = FakeApp(restore_deleted=False, transfer_ids=False)
        return FakeBridge(app), None, FakeTasks(app)
    from rightofway.blender.bridge import InProcessBridge, SocketBridge
    if args.backend == "inprocess":
        InProcessBridge.reset()
        bridge = InProcessBridge()
        return bridge, None, BlenderTasks(bridge, None)
    from experiments.exp_a_shared import _SETUP
    bridge = SocketBridge(args.host, args.port)
    scene = f"RightOfWay实验_习惯_{scenario}_{habit}_" + time.strftime("%H%M%S")
    bridge.call("cleanup_scenes", {"prefix": "RightOfWay实验_"})
    bridge.execute(_SETUP.format(name=scene))
    return bridge, scene, BlenderTasks(bridge, scene)


# ---------------------------------------------------------------------------
# 一种组合：某个场景 × 某种写法
# ---------------------------------------------------------------------------
class Run:
    def __init__(self, args, scenario: str, habit: str) -> None:
        self.args, self.scenario, self.habit = args, scenario, habit
        self.bridge, self.scene, self.tasks = _setup(args, scenario, habit)
        self.rt = Runtime(reidentify=args.reidentify)
        first = LAYOUT if scenario == "D" else AGENT
        self.s = SharedSession(self.rt, self.bridge, agent=first, scene=self.scene, granularity=args.granularity)
        if scenario == "D":
            self.s.add_agent(LAYOUT, "布局")
            self.s.add_agent(LOOK, "材质")
        self.s.start()
        self.inv = InvariantLog(True)
        self.reports: list = []          # 人修改之后 AI 的每一次执行（每一批命令算一次）

    def act(self, agent: str, payload, label: str, measure: bool = True) -> list:
        kind, x = payload
        name = self.s.agent_names.get(agent, agent)
        if kind == "code":
            reps = [self.s.run_agent(x, label, agent=agent)]
        else:
            reps = self.s.run_commands(x, label, agent=agent, max_batch=self.args.max_batch)
        for r in reps:
            self.inv.after_run(self.s, r)
            if self.args.verbose:
                say("rt", f"回给「{name}」的说明（{label}）：\n" + r.text)
            if r.alert:
                say("rt", "提醒人：" + r.alert)
        if measure:
            self.reports += reps
        return reps

    def view(self, agent: str):
        av = self.s.agent_views[agent]
        return av.recs, av.values

    def look(self, agent: str) -> list[str]:
        self.s.observe(agent)
        return sorted(self.s.human_deleted.values())

    def go(self) -> None:
        t, h = self.tasks, self.habit
        if self.scenario == "A":
            self.act(AGENT, t.build(), "build", measure=False)
            t.human("A")
            self.s.poll()
            skip = self.look(AGENT)
            self.act(AGENT, t.step(h, "adjust", skip, *self.view(AGENT)), "adjust")
        elif self.scenario == "E":
            self.act(AGENT, t.build(), "build", measure=False)
            t.human("E")
            self.s.poll()
            self.act(AGENT, t.step(h, "delete", (), *self.view(AGENT)), "delete")
        else:
            self.act(LAYOUT, t.build(), "build", measure=False)
            self.look(LOOK)
            t.human("D")
            self.s.poll()
            skip = self.look(LAYOUT)
            self.act(LAYOUT, t.step(h, "layout", skip, *self.view(LAYOUT)), "layout")
            self.act(LOOK, t.step(h, "look", (), *self.view(LOOK)), "look")          # 按第 1 步的印象，没有再看
            self.act(LAYOUT, t.step(h, "followup"), "followup")
            self.look(LOOK)
            self.act(LOOK, t.step(h, "retry"), "retry")
        self.inv.at_end(self.s)

    # ------------------------------------------------------------------
    def metrics(self) -> dict:
        g = self.args.granularity
        touched = overlap_h = overlap_ai = reid = refused_runs = 0
        cmds = Counter()
        for r in self.reports:
            if r.status == "refused":
                refused_runs += 1
            if r.status != "ok":
                continue
            ub, ur = units(r.before, g), units(r.after_raw, g)
            groups = {split_unit(u)[0] for u in set(ub) | set(ur) if (ub.get(u) or {}).get("fp") != (ur.get(u) or {}).get("fp")}
            groups |= {split_unit(x["id"])[0] for x in r.skipped if x.get("precheck")}
            touched += len(groups)
            st = r.settlement
            hits: dict[str, str] = {}
            for uid, k in list(st.kept.items()) + list(st.breach.items()):
                hits.setdefault(split_unit(uid)[0], k["reason"])
            overlap_h += sum(1 for why in hits.values() if why in (R_HUMAN, R_SELECTED))
            overlap_ai += sum(1 for why in hits.values() if why not in (R_HUMAN, R_SELECTED))
            gone = {base_name(r.before[c]["name"]) for c in set(r.before) - set(r.after_raw)
                    if r.before[c].get("kind") != "data"}
            made = {base_name(r.after_raw[c]["name"]) for c in set(r.after_raw) - set(r.before)
                    if r.after_raw[c].get("kind") != "data"}
            reid += len(gone & made)
            for e in r.commands:
                cmds[e["status"]] += 1
                cmds["total"] += 1
        final = self.s.scene_records()
        dups, data_dups = duplicates(final)
        ev = evaluate(self.rt, final, self.s.human, granularity=g, labels=self.s.labels)
        breach = {split_unit(u)[0] for r in self.reports for u in r.breach}
        return {"runs": sum(1 for r in self.reports if r.status == "ok"), "touched": touched,
                "reidentified": sum(len(r.reidentified) for r in self.reports),
                "rebound": sum(len(r.rebound) for r in self.reports),
                "blocked": sum(len(r.recreate_blocked) for r in self.reports),
                "identity_errors": sum(len(r.identity_errors) for r in self.reports),
                "overlap_h": overlap_h, "overlap_ai": overlap_ai, "dups": dups, "data_dups": data_dups, "reid": reid,
                "refused_runs": refused_runs, "cmds": cmds, "ev": ev, "breach": len(breach),
                "committed": sum(len(r.committed) for r in self.reports),
                "skipped": sum(len(r.skipped) for r in self.reports)}


def _row(run: Run, m: dict) -> dict:
    ev, cmds = m["ev"], m["cmds"]
    overlap = m["overlap_h"] + m["overlap_ai"]
    notes = []
    if m["dups"] or m["data_dups"]:
        notes.append("重复：" + "、".join(f"{n}×{c}" for n, c in sorted({**m["dups"], **m["data_dups"]}.items())))
    if ev.human_overwritten:
        notes.append("人的修改被覆盖：" + "、".join(ev.human_overwritten[:4]) + ("…" if len(ev.human_overwritten) > 4 else ""))
    if ev.ai_lost:
        notes.append("AI 的修改丢失：" + "、".join(ev.ai_lost[:4]) + ("…" if len(ev.ai_lost) > 4 else ""))
    if run.inv.violations:
        notes.append("不变量：" + "；".join(sorted({f"{v.rule} {v.where}" for v in run.inv.violations})[:4]))
    if m["identity_errors"]:
        notes.append(f"认回核对不一致 {m['identity_errors']} 处")
    return {"场景": f"{run.scenario} {SCENARIOS[run.scenario]}", "AI写法": sc.HABITS[run.habit],
            "应用": BACKENDS[run.args.backend], "粒度": "按面" if run.args.granularity == "aspect" else "按对象",
            "认回同一个对象": (f"开（认回 {m['reidentified']}，重新绑定 {m['rebound']}，拦下补回 {m['blocked']}）"
                         if run.args.reidentify else "关"),
            "AI执行次数": m["runs"], "改到的对象": m["touched"], "和人重叠的对象": m["overlap_h"],
            "和别的AI重叠的对象": m["overlap_ai"],
            "重叠率": f"{overlap / m['touched']:.0%}" if m["touched"] else "—",
            "重复对象": sum(c - 1 for c in m["dups"].values()),
            "重复数据块": sum(c - 1 for c in m["data_dups"].values()), "重建换编号": m["reid"],
            "人的修改被覆盖": len(ev.human_overwritten), "AI的修改丢失": len(ev.ai_lost), "违规": m["breach"],
            "不变量违反": run.inv.column(), "AI被暂停后拒绝": m["refused_runs"],
            "命令：执行前拒绝/部分执行/失败": (f"{cmds['refused']}/{cmds['partial']}/{cmds['failed']}（共 {cmds['total']} 条）"
                                         if cmds["total"] else "—"),
            "AI修改生效": m["committed"], "AI修改未采用": m["skipped"], "备注": "；".join(notes)}


# ---------------------------------------------------------------------------
# --sweep：逐一试人的单个修改
# ---------------------------------------------------------------------------
def _fp(u):
    return (u or {}).get("fp")


def sweep_one(args, habit: str, kind: str, target: str) -> dict:
    run = Run(args, "A", habit)
    g = args.granularity
    run.act(AGENT, run.tasks.build(), "build", measure=False)
    before = dict(run.s.view)
    run.tasks.apply_edit(kind, target)                 # AI 上次看过之后，人改了一处
    run.s.poll()
    after = dict(run.s.view)
    ub, ua = units(before, g), units(after, g)
    human = {u for u in set(ub) | set(ua) if _fp(ub.get(u)) != _fp(ua.get(u))}
    human_groups = {split_unit(u)[0] for u in human}
    deleted = {base_name(before[c]["name"]) for c in set(before) - set(after)}
    reps = run.act(AGENT, run.tasks.step(habit, "adjust", (), *run.view(AGENT)), "adjust")
    run.inv.at_end(run.s)
    kept, breach, touched, pre = set(), set(), set(), False
    resurrected = error = blocked = False
    failed = 0
    for r in reps:
        if r.status != "ok":
            continue
        kept |= set(r.settlement.kept) & human
        breach |= set(r.settlement.breach)
        pre = pre or any(x.get("precheck") and x["id"] in human for x in r.skipped)
        rb, rr = units(r.before, g), units(r.after_raw, g)
        touched |= {split_unit(u)[0] for u in set(rb) | set(rr) if _fp(rb.get(u)) != _fp(rr.get(u))}
        touched |= {split_unit(x["id"])[0] for x in r.skipped if x.get("precheck")}
        resurrected = resurrected or any(base_name(r.after[c]["name"]) in deleted
                                         for c in set(r.after) - set(r.before))      # 执行完还在的才算补回来了
        blocked = blocked or bool(r.recreate_blocked)
        error = error or bool(r.error)
        failed += sum(1 for e in r.commands if e["status"] == "failed")
    final = run.s.scene_records()
    obj_dups, data_dups = duplicates(final)
    dups = sum(c - 1 for c in obj_dups.values())
    ddups = sum(c - 1 for c in data_dups.values())
    if kind == "delete":
        outcome = ("AI 重建了它" if resurrected else "补回被拦下" if blocked else "AI 脚本出错" if error
                   else "命令失败" if failed else "没影响")
        same_face = same_obj = resurrected or blocked or error or failed > 0
    else:
        same_face, same_obj = bool(kept or (breach & human)), bool(human_groups & touched)
        if breach:
            outcome = "违规"
        elif same_face:
            outcome = "执行前拒绝" if pre else "保住了"
        elif same_obj:
            outcome = "同一对象的别的面"
        else:
            outcome = "没撞上"
        if error:
            outcome += "（AI 脚本出错）"
    return {"AI写法": sc.HABITS[habit], "应用": BACKENDS[args.backend],
            "粒度": "按面" if g == "aspect" else "按对象", "认回同一个对象": "开" if args.reidentify else "关",
            "人的修改": EDIT_KINDS.get(kind, kind), "对象": target,
            "撞上同一个面": "是" if same_face else "否", "撞上同一个对象": "是" if same_obj else "否",
            "结局": outcome, "重复对象": dups, "重复数据块": ddups, "不变量违反": run.inv.column(),
            "备注": "；".join(sorted({f"{v.rule} {v.where}" for v in run.inv.violations})[:3])}


def sweep(args, habits: list[str]) -> list[dict]:
    probe = Run(args, "A", habits[0])
    probe.act(AGENT, probe.tasks.build(), "build", measure=False)
    edits = probe.tasks.sweep_edits()
    rows = []
    for habit in habits:
        say("info", f"—— {sc.HABITS[habit]}：逐一试 {len(edits)} 种人的修改")
        for kind, target in edits:
            row = sweep_one(args, habit, kind, target)
            rows.append(row)
            if not args.no_csv:
                write_row(row, SWEEP_CSV, SWEEP_FIELDS)
    from experiments.run_all import print_table
    banner("人改一处，撞上 AI 这一步的概率（实验 A 的第 2 步）" + ("" if args.reidentify else "，不认回同一个对象（对照）"))
    table = [["写法", "人改一处", "撞上同一个面", "撞上同一个对象", "撞上后保住", "撞上后违规", "重复对象", "重复数据块",
              "人删一个对象", "AI 重建了它", "补回被拦下", "AI 出错/命令失败", "不变量违反"]]
    for habit in habits:
        rs = [r for r in rows if r["AI写法"] == sc.HABITS[habit]]
        mod = [r for r in rs if r["人的修改"] != "删除"]
        dele = [r for r in rs if r["人的修改"] == "删除"]
        hit = [r for r in mod if r["撞上同一个面"] == "是"]
        pct = lambda a, b: f"{a}/{b}（{a / b:.0%}）" if b else "—"      # noqa: E731
        table.append([sc.HABITS[habit], str(len(mod)), pct(len(hit), len(mod)),
                      pct(sum(r["撞上同一个对象"] == "是" for r in mod), len(mod)),
                      str(sum(r["结局"].startswith(("保住了", "执行前拒绝")) for r in hit)),
                      str(sum(r["结局"].startswith("违规") for r in hit)),
                      str(sum(int(r["重复对象"]) for r in rs)), str(sum(int(r["重复数据块"]) for r in rs)),
                      str(len(dele)),
                      str(sum(r["结局"] == "AI 重建了它" for r in dele)),
                      str(sum(r["结局"] == "补回被拦下" for r in dele)),
                      str(sum(r["结局"] in ("AI 脚本出错", "命令失败") for r in dele)),
                      str(sum(int(r["不变量违反"]) for r in rs if str(r["不变量违反"]).isdigit()))])
    print_table(table)
    if not args.no_csv:
        print(f"\n每一次的结果：{SWEEP_CSV}")
    return rows


def main(argv=None) -> int:
    use_utf8_console()
    ap = argparse.ArgumentParser(description="实验 H：按真实 AI 的写法重跑实验 A、D、E")
    ap.add_argument("--backend", choices=list(BACKENDS), default="socket")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9876)
    ap.add_argument("--scenario", default="A,D,E", help="A、D、E，逗号分隔")
    ap.add_argument("--habit", default="all", help="targeted、broad、rebuild、batch，逗号分隔；all 表示这个应用上能跑的全部")
    ap.add_argument("--granularity", choices=["aspect", "object"], default="aspect")
    ap.add_argument("--max-batch", type=int, default=25, help="批量命令每批最多几条（Unity MCP 是 25）")
    ap.add_argument("--verbose", action="store_true", help="打印回给 AI 的每一段说明")
    ap.add_argument("--sweep", action="store_true", help="逐一试人的单个修改，统计撞上的概率和结局")
    ap.add_argument("--no-reidentify", dest="reidentify", action="store_false",
                    help="关掉认回同一个对象（默认打开：删掉又新建的同一个对象按同一个对象处理、拦下补回人删掉的对象，"
                         "design_v0.5.md 12.4、12.5），作为对照")
    ap.add_argument("--reidentify", dest="reidentify", action="store_true", help=argparse.SUPPRESS)  # 旧参数，现在是默认
    ap.add_argument("--no-csv", action="store_true")
    args = ap.parse_args(argv)

    available = FakeTasks.habits if args.backend.startswith("fake") else BlenderTasks.habits
    habits = list(available) if args.habit == "all" else [h.strip() for h in args.habit.split(",")]
    skipped = [h for h in habits if h not in available]
    habits = [h for h in habits if h in available]
    if skipped:
        print(f"{BACKENDS[args.backend]} 上不跑：" + "、".join(sc.HABITS.get(h, h) for h in skipped))
    scenarios = [x.strip().upper() for x in args.scenario.split(",") if x.strip()]

    banner(f"实验 H：真实 AI 的写法（{BACKENDS[args.backend]}，{'按面' if args.granularity == 'aspect' else '按对象'}"
           + ("" if args.reidentify else "，不认回同一个对象（对照）") + "）")
    if args.sweep:
        sweep(args, habits)
        return 0
    rows = []
    for scenario in scenarios:
        for habit in habits:
            say("info", f"—— {scenario} {SCENARIOS[scenario]} × {sc.HABITS[habit]}")
            run = Run(args, scenario, habit)
            run.go()
            row = _row(run, run.metrics())
            rows.append(row)
            say("info", f"   改到 {row['改到的对象']} 个对象，和人重叠 {row['和人重叠的对象']}，和别的 AI 重叠 {row['和别的AI重叠的对象']}，"
                        f"重复对象 {row['重复对象']}，重复数据块 {row['重复数据块']}，重建换编号 {row['重建换编号']}，违规 {row['违规']}，"
                        f"不变量违反 {row['不变量违反']}" + (f"；{row['备注']}" if row["备注"] else ""))
            if not args.no_csv:
                write_row(row, HABITS_CSV, FIELDS)

    from experiments.run_all import print_table
    banner("对比")
    cols = ["场景", "AI写法", "改到的对象", "和人重叠的对象", "和别的AI重叠的对象", "重叠率", "重复对象", "重复数据块",
            "重建换编号", "人的修改被覆盖", "AI的修改丢失", "违规", "不变量违反", "AI被暂停后拒绝"]
    print_table([["场景", "写法", "改到", "和人重叠", "和AI重叠", "重叠率", "重复", "数据块分叉", "换编号", "人被覆盖",
                  "AI丢失", "违规", "不变量", "暂停后拒绝"]] + [[str(r[c]) for c in cols] for r in rows])
    if any(r["命令：执行前拒绝/部分执行/失败"] != "—" for r in rows):
        print("\n  批量命令（执行前拒绝/部分执行/失败）：" + "；".join(
            f"{r['场景'].split()[0]} {r['命令：执行前拒绝/部分执行/失败']}" for r in rows if r["命令：执行前拒绝/部分执行/失败"] != "—"))
    if not args.no_csv:
        print(f"\n结果已追加到 {HABITS_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
