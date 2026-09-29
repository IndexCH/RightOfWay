"""实验 A：人和 AI 改同一份（方式一，实时）。

AI 通过现成的 Blender MCP 插件执行脚本，我们的程序站在中间做保护。Blender 和插件都不用改。

运行方式（在项目根目录）：
    python -m experiments.exp_a_shared                  真人操作，连接 Blender MCP 插件（默认端口 9876）
    python -m experiments.exp_a_shared --human sim      模拟人的操作，也连接 Blender
    python -m experiments.exp_a_shared --no-protocol    对照组：不做保护，相当于直接用现在的 Blender MCP
    python -m experiments.exp_a_shared --granularity object
                                                        按整个对象保护（默认按面：人改位置、AI 改材质，两边都生效）
    python -m experiments.exp_a_shared --backend inprocess --human sim
                                                        不开 Blender，用 pip 装的 bpy 在本进程模拟（需要 Python 3.11）
    python -m experiments.exp_a_shared --ai-no-look     AI 第 2 步动手前不先看运行时的提示（对比用）
    python -m experiments.exp_a_shared --occupy-selection
                                                        "选中即占用"：人选中的对象 AI 也不能动

流程：
    1. AI 布置场景（地面、树干、5 片叶子、3 块石头、太阳光）
    2. 人删掉 Rock_2、挪动 Leaf_3、新建一个立方体
    3. AI 按它第 1 步之后看到的场景整体调整。动手前它先"看一眼"：运行时告诉它上次之后人做了什么
       （带具体的值）。这里的 AI 是写好的脚本，它能利用的只有一条：人明确删掉的石头不再补回来。
    4. 对照运行时的记录检查最终场景，结果追加到 experiments/results/results.csv
"""
from __future__ import annotations

import argparse
import threading
import time

from cowork.blender.bridge import BridgeError, InProcessBridge, SocketBridge
from cowork.blender.evaluate import evaluate
from cowork.blender.records import label
from cowork.blender.shared_session import SharedSession
from cowork.runtime import POLICY_CANDIDATE, POLICY_DISCARD, Runtime
from experiments import scenario_tree as sc
from experiments.common import banner, say, use_utf8_console, wait_for_human, write_row

_SETUP = '''
import bpy
sc = bpy.data.scenes.get({name!r}) or bpy.data.scenes.new({name!r})
for w in bpy.context.window_manager.windows:
    w.scene = sc
'''


def main(argv=None) -> None:
    use_utf8_console()
    ap = argparse.ArgumentParser(description="实验 A：同一份，实时")
    ap.add_argument("--backend", choices=["socket", "inprocess"], default="socket")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9876)
    ap.add_argument("--human", choices=["real", "sim"], default="real")
    ap.add_argument("--no-protocol", action="store_true", help="对照组：AI 的脚本直接执行，不做保护")
    ap.add_argument("--policy", choices=[POLICY_DISCARD, POLICY_CANDIDATE], default=POLICY_DISCARD,
                    help="人改过的对象被 AI 改到时：discard 丢弃 AI 的版本（默认）；candidate 留作候选")
    ap.add_argument("--granularity", choices=["aspect", "object"], default="aspect",
                    help="aspect：按面（位置、材质……）判断人改过什么（默认）；object：整个对象")
    ap.add_argument("--keep-old-scenes", action="store_true", help="不删除之前实验留下的场景")
    ap.add_argument("--poll", type=float, default=0.5, help="检查人的修改的间隔（秒）")
    ap.add_argument("--ai-no-look", action="store_true", help="AI 第 2 步动手前不先看运行时的提示")
    ap.add_argument("--occupy-selection", action="store_true", help="选中即占用：人选中的对象 AI 不能动")
    ap.add_argument("--no-csv", action="store_true")
    args = ap.parse_args(argv)
    protect = not args.no_protocol

    if args.backend == "socket":
        bridge = SocketBridge(args.host, args.port)
        scene = "Cowork实验_" + time.strftime("%H%M%S")
        try:
            if not args.keep_old_scenes:
                old = bridge.call("cleanup_scenes", {"prefix": "Cowork实验_"})
                if old["scenes"]:
                    print(f"已删除之前实验留下的 {len(old['scenes'])} 个场景和 {old['objects']} 个对象"
                          "（避免同名对象变成 .001、.002）。不想删除可以加 --keep-old-scenes。")
            bridge.execute(_SETUP.format(name=scene))
        except BridgeError as e:
            print(e)
            return
        print(f"已在 Blender 里新建并切换到场景「{scene}」，你原来的场景不受影响。")
    else:
        InProcessBridge.reset()
        bridge, scene = InProcessBridge(), None

    rt = Runtime(human_touched_policy=args.policy)
    session = SharedSession(rt, bridge, scene=scene, granularity=args.granularity,
                            occupy_selection=args.occupy_selection)
    session.start()

    gran = "按面" if args.granularity == "aspect" else "按对象"
    variant = (f"有保护（{gran}）" if protect else "对照组：没有保护") + \
        ("，AI 不先看提示" if protect and args.ai_no_look else "") + ("，选中即占用" if args.occupy_selection else "")
    banner(f"实验 A：同一份，实时（{variant}，人：{'真人' if args.human == 'real' else '模拟'}）")

    # 1. AI 布置场景
    say("ai", "第 1 步：布置场景")
    r1 = session.run_agent(sc.ai_build(scene), "build", protect=protect)
    say("rt", r1.text)

    # 2. 人修改
    detect_seconds = None
    if args.human == "sim":
        t_edit = time.perf_counter()
        bridge.execute(sc.human_sim(scene))
        say("human", "删除 Rock_2；把 Leaf_3 往上挪；新建一个立方体 Cube；最后选中 Leaf_1")
        d = session.poll()
        detect_seconds = round(time.perf_counter() - t_edit, 3)
        say("rt", f"发现人的修改（{detect_seconds} 秒）：" + "；".join(d.describe()))
    else:
        stop = threading.Event()

        def watch() -> None:
            while not stop.is_set():
                try:
                    d = session.poll()
                    if d:
                        say("rt", "发现人的修改：" + "；".join(d.describe()))
                except BridgeError as e:
                    say("rt", f"检查失败：{e}")
                stop.wait(args.poll)

        th = threading.Thread(target=watch, daemon=True)
        th.start()
        steps = sc.HUMAN_STEPS + ([sc.HUMAN_SELECT_STEP] if args.occupy_selection else [])
        wait_for_human(steps, extra=f"（运行时每 {args.poll} 秒检查一次，发现你的修改会在这里打印出来）")
        stop.set()
        th.join()

    # 3. AI 按旧计划调整。有运行时时，它动手前先看一眼（相当于 AI 读场景时，运行时把它错过的变化告诉它）
    skip = []
    if protect and not args.ai_no_look:
        obs = session.observe()
        say("rt", "AI 动手前先看场景，运行时告诉它：\n" + obs.text)
        skip = sorted(session.human_deleted.values())
        if skip:
            say("ai", "人删掉了 " + "、".join(skip) + "，这些我不补回来。其余按原计划做。")
    say("ai", "第 2 步：整体调整（叶子变秋色、统一高度、石头排成一圈、散落物体落地）")
    r2 = session.run_agent(sc.ai_adjust(scene, skip), "adjust", protect=protect)
    if r2.status == "deferred":
        say("rt", r2.text)
        return
    say("rt", "回给 AI 的说明：\n" + r2.text)

    # 4. 检查
    final = bridge.call("poll", {"scene": scene} if scene else {})["records"]
    ev = evaluate(rt, final, session.human, granularity=args.granularity, labels=session.labels)
    errors = len(r2.inexact) + len(r2.dangling)
    outside = sum(len(v) for v in r2.outside.values())
    banner("结果")
    say("info", ev.summary())
    say("info", f"AI 新建的对象和人删掉的对象同名：{len(r2.resurrections)} 个 {r2.resurrections or ''}")
    undo = "已推送（能不能单独撤掉这一段，要在 Blender 界面里按 Ctrl+Z 确认）" if r2.undo_pushed else "没有推送（无界面模式或失败）"
    say("info", f"恢复出错：{errors} 处；AI 候选：{len(r2.candidates)} 个；Ctrl+Z 撤销步：{undo}")
    if r2.partial and protect:
        say("info", "部分生效的对象：" + "；".join(f"{n}（AI 改的{'、'.join(label(a, session.labels) for a in p['kept'])}生效；人改的{'、'.join(label(a, session.labels) for a in p['restored'])}保留）"
                                         for n, p in sorted(r2.partial.items())))
    if outside:
        say("info", f"AI 改到了当前场景以外的对象 {outside} 个：{r2.outside}")
    say("info", f"AI 第 2 步执行用时 {r2.seconds:.3f} 秒；人立刻就能看到结果")
    if args.human == "real":
        say("info", "请在 Blender 里看一下：Leaf_3 和 Cube 是否还在你放的位置？再按一次 Ctrl+Z，看撤掉的是什么，记在备注里。")
    if not args.no_csv:
        path = write_row({
            "实验": "A 同一份", "做法": variant,
            "人": "真人" if args.human == "real" else "模拟",
            "人的修改被覆盖": len(ev.human_overwritten), "AI的修改丢失": len(ev.ai_lost),
            "被删对象被AI重建": len(r2.resurrections), "恢复或合并出错": errors,
            "AI候选": len(r2.candidates), "AI修改生效": len(r2.committed), "AI修改未采用": len(r2.skipped),
            "人看到AI结果要等(秒)": r2.seconds,
            "备注": (f"发现人的修改用时 {detect_seconds} 秒" if detect_seconds is not None else f"轮询间隔 {args.poll} 秒")
            + (f"；部分生效 {len(r2.partial)} 个对象" if r2.partial else "")
            + (f"；改到场景外 {outside} 个对象" if outside else ""),
        })
        say("info", f"结果已追加到 {path}")


if __name__ == "__main__":
    main()
