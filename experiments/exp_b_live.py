"""实验 B：各自一个窗口，实时同步（方式二，给 computer use 用）。

AI 用 computer use 时要点鼠标，必须有自己的屏幕，所以它在自己的 Blender 里改自己的一份。
两个 Blender 都开着现成的 MCP 插件，运行时直接读写两边：不用存盘，也不用"文件 → 恢复"。
这里的"AI"用脚本在它那个 Blender 里操作来代替（第 1 级），检查的是同步规则；真正的 computer use 放在第 2 级。

准备：开两个 Blender，都装 MCP for Blender 插件。
  - 你的 Blender：N 面板 → BlenderMCP → Start MCP Server（端口 9876）
  - AI 的 Blender：同样的面板里把 Port 改成 9877，再 Start MCP Server

运行方式（在项目根目录）：
    python -m experiments.exp_b_live                     真人在你的 Blender 里操作
    python -m experiments.exp_b_live --human sim         模拟人的操作
    python -m experiments.exp_b_live --no-protocol       对照组：AI 的修改一律覆盖你的（谁后同步谁生效）
    python -m experiments.exp_b_live --backend local --human sim
                                                         不开 Blender：起两个用 bpy 的本地进程代替两个 Blender（需要 Python 3.11）
流程：
    1. AI 在它的 Blender 里布置场景 → 同步：场景出现在你的 Blender 里
    2. 你删 Rock_2、挪 Leaf_3、加 Cube（这期间不同步，AI 看不到）
    3. AI 按它窗口里的旧样子整体调整 → 同步：你的修改都保留，AI 其余的修改出现在你那边，
       AI 的窗口更新成和你一样，并被告知你做了什么
    4. 你挪一下 Rock_1 → 同步：AI 的窗口跟着更新
    5. 检查：你的修改有没有被覆盖、AI 的修改有没有丢、两边是否一致
"""
from __future__ import annotations

import argparse
import threading
import time

from cowork.blender import local_server
from cowork.blender.bridge import BridgeError, SocketBridge
from cowork.blender.evaluate import evaluate
from cowork.blender.live_sync import LiveSyncSession
from cowork.runtime import POLICY_DISCARD, Runtime
from experiments import scenario_tree as sc
from experiments.common import banner, say, use_utf8_console, wait_for_human, write_row
from experiments.exp_a_shared import _SETUP


def main(argv=None) -> None:
    use_utf8_console()
    ap = argparse.ArgumentParser(description="实验 B：各自一个窗口，实时同步")
    ap.add_argument("--backend", choices=["socket", "local"], default="socket",
                    help="socket：连接两个开着的 Blender；local：起两个用 bpy 的本地进程")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--human-port", type=int, default=9876)
    ap.add_argument("--ai-port", type=int, default=9877)
    ap.add_argument("--human", choices=["real", "sim"], default="real")
    ap.add_argument("--no-protocol", action="store_true")
    ap.add_argument("--granularity", choices=["aspect", "object"], default="aspect")
    ap.add_argument("--inline", action="store_true", help="两个 Blender 不在同一台机器上时：对象用 base64 传")
    ap.add_argument("--poll", type=float, default=0.5)
    ap.add_argument("--no-csv", action="store_true")
    args = ap.parse_args(argv)
    protect = not args.no_protocol

    procs = []
    if args.backend == "local":
        args.human_port, args.ai_port = 9886, 9887
        print("正在启动两个本地 Blender 进程（bpy）……")
        procs = [(args.human_port, local_server.spawn(args.human_port)), (args.ai_port, local_server.spawn(args.ai_port))]
    try:
        _run(args, protect)
    except BridgeError as e:
        print(e)
        if args.backend == "socket":
            print(f"\n需要两个开着 MCP 插件的 Blender：你的用端口 {args.human_port}，AI 的用端口 {args.ai_port}"
                  "（在 AI 那个 Blender 的 BlenderMCP 面板里把 Port 改成 9877 再 Start MCP Server）。")
    finally:
        for port, p in procs:
            local_server.shutdown(port, p)


def _run(args, protect: bool) -> None:
    hb, ab = SocketBridge(args.host, args.human_port), SocketBridge(args.host, args.ai_port)
    scene = "Cowork实验_同步_" + time.strftime("%H%M%S")
    for who, br in (("你", hb), ("AI", ab)):
        old = br.call("cleanup_scenes", {"prefix": "Cowork实验_"})
        if old["scenes"]:
            print(f"{who}的 Blender：已删除之前实验留下的 {len(old['scenes'])} 个场景。")
        br.execute(_SETUP.format(name=scene))
    print(f"两个 Blender 里都新建并切换到了场景「{scene}」，你原来的场景不受影响。")

    rt = Runtime(human_touched_policy=POLICY_DISCARD)
    s = LiveSyncSession(rt, hb, ab, human_scene=scene, ai_scene=scene, granularity=args.granularity, inline=args.inline)
    s.start()
    gran = "按面" if args.granularity == "aspect" else "按对象"
    variant = f"实时同步（{gran}）" if protect else "对照组：谁后同步谁生效"
    banner(f"实验 B：各自一个窗口，实时同步（{variant}，人：{'真人' if args.human == 'real' else '模拟'}）")

    def sync(what: str):
        r = s.sync(protect=protect, label_=what)
        say("rt", "告诉你：\n" + r.text_for_human)
        say("rt", "告诉 AI：\n" + r.text_for_ai)
        return r

    say("ai", "第 1 步：在我自己的 Blender 里布置场景")
    s.ai_execute(sc.ai_build(scene))
    r1 = sync("build")

    if args.human == "sim":
        hb.execute(sc.human_sim(scene))
        say("human", "删除 Rock_2；把 Leaf_3 往上挪；新建一个立方体 Cube")
    else:
        stop = threading.Event()
        seen: dict = {}

        def watch() -> None:                 # 只是为了让你看到运行时发现了你的修改；这里不同步
            while not stop.is_set():
                try:
                    res = hb.call("poll", {"scene": scene, "known": {c: r["aspects"] for c, r in s.hs.view.items()}})
                    names = sorted(r["name"] for c, r in res["records"].items()
                                   if c not in s.hs.view or r["fp"] != s.hs.view[c]["fp"])
                    gone = sorted(r["name"] for c, r in s.hs.view.items() if c not in res["records"])
                    key = (tuple(names), tuple(gone))
                    if (names or gone) and seen.get("k") != key:
                        seen["k"] = key
                        say("rt", "发现你的修改：" + "；".join([f"修改/新建 {n}" for n in names] + [f"删除 {n}" for n in gone]))
                except BridgeError as e:
                    say("rt", f"检查失败：{e}")
                stop.wait(args.poll)

        th = threading.Thread(target=watch, daemon=True)
        th.start()
        wait_for_human(sc.HUMAN_STEPS, extra="（在你的 Blender 里做；这期间不同步，AI 的窗口还是旧的样子）")
        stop.set()
        th.join()

    say("ai", "第 2 步：按我窗口里的样子整体调整（叶子变秋色、统一高度、石头排成一圈、散落物体落地）")
    s.ai_execute(sc.ai_adjust(scene))
    r2 = sync("adjust")

    if args.human == "sim":
        hb.execute(sc.human_stale_sim(scene))
        say("human", "把 Rock_1 挪了一下")
    else:
        wait_for_human([sc.HUMAN_MOVE_ROCK1])
    r3 = sync("human-only")

    final = hb.call("poll", {"scene": scene})["records"]
    ai_final = ab.call("poll", {"scene": scene, "prefix": "a-"})["records"]
    ev = evaluate(rt, final, s.human, granularity=args.granularity, labels=s.labels)
    same = {c: r["cfp"] for c, r in final.items()} == {c: r["cfp"] for c, r in ai_final.items()}
    reports = [r1, r2, r3]
    errors = sum(len(r.errors) for r in reports)
    banner("结果")
    say("info", ev.summary())
    say("info", f"两边是否一致：{'是' if same else '否'}；同步出错：{errors} 处；AI 重建了人删掉的对象：{len(r2.resurrections)} 个")
    say("info", f"AI 做完一步到你看到结果：{r2.seconds:.2f} 秒（不用存盘，也不用「文件 → 恢复」）")
    if args.human == "real":
        say("info", "请在你的 Blender 里看一下：Leaf_3 是不是还在你放的高度、颜色变成了橙色？Cube 还在空中吗？"
                    "再按一次 Ctrl+Z，看撤掉的是不是刚才同步进来的 AI 修改。")
    if not args.no_csv:
        path = write_row({
            "实验": "B 各自一个窗口（实时同步）", "做法": variant, "人": "真人" if args.human == "real" else "模拟",
            "人的修改被覆盖": len(ev.human_overwritten), "AI的修改丢失": len(ev.ai_lost),
            "被删对象被AI重建": len(r2.resurrections), "恢复或合并出错": errors, "AI候选": 0,
            "AI修改生效": sum(len(r.committed) for r in reports), "AI修改未采用": sum(len(r.skipped) for r in reports),
            "人看到AI结果要等(秒)": r2.seconds,
            "备注": f"两边{'一致' if same else '不一致'}；不用存盘和恢复" + ("；对象用 base64 传" if args.inline else "")})
        say("info", f"结果已追加到 {path}")


if __name__ == "__main__":
    main()
