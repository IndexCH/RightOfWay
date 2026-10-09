"""实验 E：AI 删掉人刚改过的对象（v0.5 实施顺序第 1 步：用不变量检查复现问题）。

就是那张时序图里的情况：人把 Leaf_3 往上挪了；AI 还按旧印象，觉得 Leaf_3 多余，把它删掉，
顺手把 Leaf_1 放大。规则（R6）：人碰过的对象 AI 不能删。

检查三条不变量（rightofway/invariants.py）：
    I1 账本 = 场景
    I2 要保持的部分要么没变，要么记为违规
    I3 告诉 AI 的和实际一致

v0.5 第 3 步之后应该是：
    - 应用能恢复被删的对象（Blender）：Leaf_3 回来，告诉 AI"人改过的对象不能删除，已恢复"；
    - 应用恢复不了（现在的 Unity）：如实报告为违规，账本照实记下，提醒人，暂停 AI。
两种情况下三条不变量都成立。

运行方式（在项目根目录）：
    python -m experiments.exp_e_delete --backend fake-unity    不需要任何软件：内存里的假应用，
                                                               和现在的 Unity 接入一样恢复不了被删的对象
    python -m experiments.exp_e_delete --backend fake          同上，但能恢复被删的对象（和 Blender 接入一样）
    python -m experiments.exp_e_delete --backend inprocess     pip 装的 bpy（需要 Python 3.11）
    python -m experiments.exp_e_delete                         打开着的 Blender（端口 9876），默认模拟人的操作
    python -m experiments.exp_e_delete --backend unity         Unity 6 + AI Assistant
加 --human real 由你在 Blender / Unity 里挪 Leaf_3；加 --granularity object 按整个对象判断。
"""
from __future__ import annotations

import argparse
import time

from rightofway.blender.evaluate import evaluate
from rightofway.blender.shared_session import SharedSession
from rightofway.fakeapp import FakeApp, FakeBridge
from rightofway.runtime import POLICY_DISCARD, Runtime
from experiments.common import (InvariantLog, banner, breach_objects, say, say_alert, use_utf8_console,
                                wait_for_human, write_row)

BACKENDS = {
    "fake-unity": "模拟 Unity 接入（恢复不了被删的对象）",
    "fake": "模拟 Blender 接入（能恢复被删的对象）",
    "inprocess": "Blender（bpy）",
    "socket": "Blender",
    "unity": "Unity",
}

_FAKE_BUILD = """
for i in range(1, 6):
    scene.create(f"Leaf_{i}", {"location": [0, i, 1.4], "scale": 1.0, "color": "green"})
"""
_FAKE_DELETE = "scene.delete('Leaf_3')\nscene.set('Leaf_1', 'scale', 1.2)"


def _setup(args):
    """返回 (bridge, scene, 人的操作, AI 的三段代码, 人要做的步骤, 怎么看场景里的 Leaf_3)。"""
    if args.backend.startswith("fake"):
        app = FakeApp(restore_deleted=args.backend == "fake")
        bridge = FakeBridge(app)

        def human_move():
            app.set("Leaf_3", "location", [0, 3, 2.2])
        return bridge, None, human_move, (_FAKE_BUILD, _FAKE_DELETE), None
    if args.backend == "unity":
        from rightofway.unity.bridge import RelayTransport, UnityBridge
        from experiments import scenario_unity as su
        bridge = UnityBridge(RelayTransport(args.relay))
        bridge.setup()
        return bridge, None, lambda: bridge.execute(su.HUMAN_MOVE_LEAF3), (su.AI_BUILD, su.AI_DELETE_LEAF3), su.HUMAN_STEPS_E
    from rightofway.blender.bridge import InProcessBridge, SocketBridge
    from experiments import scenario_tree as sc
    from experiments.exp_a_shared import _SETUP
    if args.backend == "inprocess":
        InProcessBridge.reset()
        bridge, scene = InProcessBridge(), None
    else:
        bridge = SocketBridge(args.host, args.port)
        scene = "RightOfWay实验_删除_" + time.strftime("%H%M%S")
        bridge.call("cleanup_scenes", {"prefix": "RightOfWay实验_"})
        bridge.execute(_SETUP.format(name=scene))
        print(f"已在 Blender 里新建并切换到场景「{scene}」，你原来的场景不受影响。")
    return (bridge, scene, lambda: bridge.execute(sc.human_move_leaf3(scene)),
            (sc.ai_build(scene), sc.ai_delete_leaf3(scene)), sc.HUMAN_STEPS_E)


def main(argv=None) -> None:
    use_utf8_console()
    ap = argparse.ArgumentParser(description="实验 E：AI 删掉人刚改过的对象")
    ap.add_argument("--backend", choices=list(BACKENDS), default="socket")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9876)
    ap.add_argument("--relay", default=None, help="Unity MCP 中继的路径")
    ap.add_argument("--human", choices=["real", "sim"], default="sim")
    ap.add_argument("--granularity", choices=["aspect", "object"], default="aspect")
    ap.add_argument("--no-csv", action="store_true")
    args = ap.parse_args(argv)
    real = args.human == "real" and not args.backend.startswith("fake")

    bridge, scene, human_move, (build, delete), steps = _setup(args)
    rt = Runtime(human_touched_policy=POLICY_DISCARD)
    s = SharedSession(rt, bridge, scene=scene, granularity=args.granularity)
    s.start()
    inv = InvariantLog(True)
    gran = "按面" if args.granularity == "aspect" else "按对象"
    variant = f"有保护（{gran}），{BACKENDS[args.backend]}"
    banner(f"实验 E：AI 删掉人刚改过的对象（{variant}，人：{'真人' if real else '模拟'}）")

    say("ai", "第 1 步：布置场景")
    r1 = s.run_agent(build, "build")
    inv.after_run(s, r1)

    if real:
        wait_for_human(steps)
    else:
        human_move()
        say("human", "把 Leaf_3 往上挪")
    d = s.poll()
    say("rt", "发现人的修改：" + ("；".join(d.describe()) or "没有"))

    say("ai", "第 2 步：按第 1 步之后看到的样子，觉得 Leaf_3 多余，删掉；顺手把 Leaf_1 放大")
    r2 = s.run_agent(delete, "删掉 Leaf_3")
    say("rt", "回给 AI 的说明：\n" + r2.text)
    say_alert(r2)
    inv.after_run(s, r2)
    if r2.suspended:
        r3 = s.run_agent(delete, "再试一次")
        say("rt", "AI 被暂停后又想执行，运行时的回答：" + r3.text)
    inv.at_end(s)

    final = s.scene_records()
    ev = evaluate(rt, final, s.human, granularity=args.granularity, labels=s.labels)
    leaf3_in_scene = any(r["name"] == "Leaf_3" for r in final.values())
    leaf3_in_ledger = any(isinstance(o.content, dict) and o.content.get("name") == "Leaf_3"
                          and not o.content.get("deleted") for o in rt.objects.values())
    banner("结果")
    say("info", f"场景里还有 Leaf_3 吗：{'有' if leaf3_in_scene else '没有'}；"
                f"账本里还有 Leaf_3 吗：{'有' if leaf3_in_ledger else '没有'}")
    say("info", ev.summary())
    say("info", inv.summary())
    say("info", f"结局：{'违规（已如实报告，人已收到提醒，AI 已暂停）' if r2.outcome == 'breach' else '正常提交'}")
    if not args.no_csv:
        path = write_row({
            "实验": "E 删掉人改过的对象", "做法": variant, "人": "真人" if real else "模拟",
            "人的修改被覆盖": len(ev.human_overwritten), "AI的修改丢失": len(ev.ai_lost),
            "被删对象被AI重建": 0, "恢复或合并出错": len(r2.inexact), "不变量违反": inv.column(), "违规": breach_objects([r1, r2]),
            "AI候选": 0, "AI修改生效": len(r2.committed), "AI修改未采用": len(r2.skipped),
            "人看到AI结果要等(秒)": r2.seconds,
            "备注": f"场景里 Leaf_3 {'在' if leaf3_in_scene else '不在'}，账本里 {'在' if leaf3_in_ledger else '不在'}；"
                    + f"结局 {r2.outcome}" + "；".join(sorted({v.rule for v in inv.violations}))})
        say("info", f"结果已追加到 {path}")


if __name__ == "__main__":
    main()
