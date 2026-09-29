"""实验 C：同一个任务放到 Unity 上跑（方式一，同一份，实时），不为 Unity 写任何"面"的定义。

面完全来自 Unity 自己的序列化（每个组件的每个顶层序列化属性）。运行时（SharedSession）和 Blender 用的是同一份代码。
AI 通过现成的 Unity MCP（Unity AI Assistant 的 Unity_RunCommand）执行 C#，Unity 和它的 MCP 都不用改。

准备：Unity 6 + AI Assistant 包（com.unity.ai.assistant），打开任意项目；pip install mcp

运行方式（在项目根目录）：
    python -m experiments.exp_c_unity --human sim        模拟人的操作
    python -m experiments.exp_c_unity                    真人在 Unity 里操作
    python -m experiments.exp_c_unity --no-protocol      对照组：不做保护
    python -m experiments.exp_c_unity --relay "路径"      Unity MCP 中继的位置（默认 %USERPROFILE%\\.unity\\relay\\relay_win.exe）

实验会在当前打开的 Unity 里"追加"一个新的空场景，不影响你已经打开的场景，也不会保存任何东西。
下次运行时会先关掉上次的实验场景。
"""
from __future__ import annotations

import argparse
import threading
import time

from rightofway.blender.evaluate import evaluate
from rightofway.blender.records import label
from rightofway.blender.shared_session import SharedSession
from rightofway.runtime import POLICY_DISCARD, Runtime
from rightofway.unity.bridge import NeedResponse, RelayTransport, ReplayTransport, UnityBridge, UnityError
from experiments import scenario_unity as su
from experiments.common import banner, say, use_utf8_console, wait_for_human, write_row


def main(argv=None) -> None:
    use_utf8_console()
    ap = argparse.ArgumentParser(description="实验 C：Unity，同一份，实时")
    ap.add_argument("--relay", default=None, help="Unity MCP 中继的路径")
    ap.add_argument("--replay-dir", default=None, help="不直接连接 Unity：把每一步的 C# 写到这个文件夹，由别人执行后放回结果")
    ap.add_argument("--human", choices=["real", "sim"], default="real")
    ap.add_argument("--no-protocol", action="store_true")
    ap.add_argument("--granularity", choices=["aspect", "object"], default="aspect")
    ap.add_argument("--poll", type=float, default=2.0, help="检查人的修改的间隔（秒）。每次检查都要编译一段 C#，别设太小")
    ap.add_argument("--ai-no-look", action="store_true", help="AI 第 2 步动手前不先看运行时的提示")
    ap.add_argument("--occupy-selection", action="store_true", help="选中即占用：人选中的对象 AI 不能动")
    ap.add_argument("--no-csv", action="store_true")
    args = ap.parse_args(argv)
    protect = not args.no_protocol

    try:
        transport = ReplayTransport(args.replay_dir) if args.replay_dir else RelayTransport(args.relay)
        bridge = UnityBridge(transport)
        res = bridge.setup()
        if res.get("closed"):
            print(f"已关闭上次实验留下的 {res['closed']} 个场景。")
        print("已在 Unity 里追加一个新的空场景作为实验场景（Hierarchy 里名字是 Untitled），你原来的场景不受影响。")

        rt = Runtime(human_touched_policy=POLICY_DISCARD)
        session = SharedSession(rt, bridge, granularity=args.granularity, occupy_selection=args.occupy_selection)
        session.start()
        gran = "按面" if args.granularity == "aspect" else "按对象"
        variant = (f"有保护（{gran}）" if protect else "对照组：没有保护") + \
            ("，AI 不先看提示" if protect and args.ai_no_look else "") + ("，选中即占用" if args.occupy_selection else "")
        banner(f"实验 C：Unity，同一份，实时（{variant}，人：{'真人' if args.human == 'real' else '模拟'}）")

        say("ai", "第 1 步：布置场景")
        r1 = session.run_agent(su.AI_BUILD, "build", protect=protect)
        say("rt", r1.text)

        detect = None
        if args.human == "sim":
            t = time.perf_counter()
            bridge.execute(su.HUMAN_SIM)
            say("human", "删除 Rock_2；把 Leaf_3 往上挪；新建一个立方体 Cube；最后选中 Leaf_1")
            d = session.poll()
            detect = round(time.perf_counter() - t, 2)
            say("rt", f"发现人的修改（{detect} 秒）：" + "；".join(d.describe()))
        else:
            stop = threading.Event()

            def watch() -> None:
                while not stop.is_set():
                    try:
                        d = session.poll()
                        if d:
                            say("rt", "发现人的修改：" + "；".join(d.describe()))
                    except UnityError as e:
                        say("rt", f"检查失败：{e}")
                    stop.wait(args.poll)

            th = threading.Thread(target=watch, daemon=True)
            th.start()
            wait_for_human(su.HUMAN_STEPS + ([su.HUMAN_SELECT_STEP] if args.occupy_selection else []), extra=(
                "（先确认 Hierarchy 里的 Untitled 场景是粗体，也就是活动场景，这样新建的立方体会放进实验场景。"
                f"运行时每 {args.poll} 秒检查一次）"))
            stop.set()
            th.join()

        skip = []
        if protect and not args.ai_no_look:
            obs = session.observe()
            say("rt", "AI 动手前先看场景，运行时告诉它：\n" + obs.text)
            skip = sorted({n for n in session.human_deleted.values() if "/" not in n and not n.startswith("Material:")})
            if skip:
                say("ai", "人删掉了 " + "、".join(skip) + "，这些我不补回来。其余按原计划做。")
        say("ai", "第 2 步：整体调整（叶子变秋色、统一高度、石头排成一圈、散落物体落地）")
        r2 = session.run_agent(su.ai_adjust(skip), "adjust", protect=protect)
        say("rt", "回给 AI 的说明：\n" + r2.text)

        final = bridge.call("poll", {})["records"]
        ev = evaluate(rt, final, session.human, granularity=args.granularity, labels=session.labels)
        banner("结果")
        say("info", ev.summary())
        say("info", f"AI 新建的对象和人删掉的对象同名：{len(r2.resurrections)} 个 {r2.resurrections or ''}")
        if r2.partial and protect:
            say("info", "部分生效的对象：" + "；".join(
                f"{n}（AI 改的{'、'.join(label(a, session.labels) for a in p['kept'])}生效；"
                f"人改的{'、'.join(label(a, session.labels) for a in p['restored'])}保留）"
                for n, p in sorted(r2.partial.items())))
        outside = sum(len(v) for v in r2.outside.values())
        if outside:
            say("info", f"AI 改到了实验场景以外的对象 {outside} 个：{r2.outside}")
        say("info", f"恢复出错：{len(r2.inexact)} 处；AI 第 2 步在 Unity 里执行用时 {r2.seconds:.3f} 秒")
        if not args.no_csv:
            path = write_row({
                "实验": "C Unity 同一份", "做法": variant, "人": "真人" if args.human == "real" else "模拟",
                "人的修改被覆盖": len(ev.human_overwritten), "AI的修改丢失": len(ev.ai_lost),
                "被删对象被AI重建": len(r2.resurrections), "恢复或合并出错": len(r2.inexact),
                "AI候选": 0, "AI修改生效": len(r2.committed), "AI修改未采用": len(r2.skipped),
                "人看到AI结果要等(秒)": round(r2.seconds, 4),
                "备注": (f"发现人的修改用时 {detect} 秒" if detect is not None else f"轮询间隔 {args.poll} 秒")
                + (f"；改到场景外 {outside} 个对象" if outside else "")})
            say("info", f"结果已追加到 {path}")
    except NeedResponse as e:
        print(f"\n{e}\n在 Unity 里执行它，把 Unity_RunCommand 的完整返回存为 {e.code_path.with_suffix('').with_suffix('.out.json')}，再运行一次。")
    except UnityError as e:
        print(f"\n出错了：{e}")


if __name__ == "__main__":
    main()
