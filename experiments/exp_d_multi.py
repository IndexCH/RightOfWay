"""实验 D：两个 AI + 一个人，改同一份（方式一，实时）。

AI「布局」负责位置和大小，AI「材质」负责颜色和灯光。两个 AI 都按自己上次看到的场景写死具体数值。
规则：人优先于所有 AI；AI 之间先执行的赢，后到的那个按旧印象改到了别人刚改过的面，这部分恢复成别人的版本，
并把最新情况告诉它；同时给它预留一小段时间重试，期间别的 AI 不能改这些面，避免两个 AI 来回互相作废。

运行方式（在项目根目录）：
    python -m experiments.exp_d_multi --human sim        模拟人的操作，连接 Blender MCP 插件（端口 9876）
    python -m experiments.exp_d_multi                    真人在 Blender 里操作
    python -m experiments.exp_d_multi --no-protocol      对照组：不做保护，也不告诉 AI 任何事（相当于现在的 Blender MCP）
    python -m experiments.exp_d_multi --backend inprocess --human sim
                                                         不开 Blender，用 pip 装的 bpy 在本进程模拟（需要 Python 3.11）
流程：
    1. 布局 AI 布置场景；材质 AI 看了一眼场景
    2. 人删掉 Rock_2、把 Leaf_3 往上挪
    3. 布局 AI 先看提示（知道人删了 Rock_2），再调整：叶子抬高、树干 1.3、石头排圈
    4. 材质 AI 没有再看，按第 1 步的印象：叶子变橙色、树干 1.1、Rock_1 挪到 (3.5, -2)、灯光调亮
       → 树干和 Rock_1 是布局 AI 刚改过的，这两处不生效，给材质 AI 预留重试
    5. 布局 AI 紧接着想把树干改成 1.5 → 材质 AI 正在重试，这次不生效
    6. 材质 AI 看了最新情况后重试：树干 1.2 → 生效
"""
from __future__ import annotations

import argparse
import threading
import time

from cowork.blender.bridge import BridgeError, InProcessBridge, SocketBridge
from cowork.blender.evaluate import evaluate
from cowork.blender.shared_session import R_OTHER_AI, R_RESERVED, SharedSession
from cowork.runtime import POLICY_DISCARD, Runtime
from experiments import scenario_tree as sc
from experiments.common import banner, say, use_utf8_console, wait_for_human, write_row
from experiments.exp_a_shared import _SETUP

LAYOUT, LOOK = "layout", "look"


def main(argv=None) -> None:
    use_utf8_console()
    ap = argparse.ArgumentParser(description="实验 D：两个 AI + 一个人，同一份")
    ap.add_argument("--backend", choices=["socket", "inprocess"], default="socket")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9876)
    ap.add_argument("--human", choices=["real", "sim"], default="real")
    ap.add_argument("--no-protocol", action="store_true")
    ap.add_argument("--granularity", choices=["aspect", "object"], default="aspect")
    ap.add_argument("--reserve", type=float, default=30.0, help="被别的 AI 抢先后，给它预留多少秒重试")
    ap.add_argument("--poll", type=float, default=0.5)
    ap.add_argument("--no-csv", action="store_true")
    args = ap.parse_args(argv)
    protect = not args.no_protocol

    if args.backend == "socket":
        bridge = SocketBridge(args.host, args.port)
        scene = "Cowork实验_多AI_" + time.strftime("%H%M%S")
        try:
            old = bridge.call("cleanup_scenes", {"prefix": "Cowork实验_"})
            if old["scenes"]:
                print(f"已删除之前实验留下的 {len(old['scenes'])} 个场景。")
            bridge.execute(_SETUP.format(name=scene))
        except BridgeError as e:
            print(e)
            return
        print(f"已在 Blender 里新建并切换到场景「{scene}」，你原来的场景不受影响。")
    else:
        InProcessBridge.reset()
        bridge, scene = InProcessBridge(), None

    rt = Runtime(human_touched_policy=POLICY_DISCARD)
    s = SharedSession(rt, bridge, agent=LAYOUT, scene=scene, granularity=args.granularity,
                      reserve_seconds=args.reserve)
    s.add_agent(LAYOUT, "布局")
    s.add_agent(LOOK, "材质")
    s.start()
    variant = (f"有保护（{'按面' if args.granularity == 'aspect' else '按对象'}）" if protect else "对照组：没有保护")
    banner(f"实验 D：两个 AI + 一个人（{variant}，人：{'真人' if args.human == 'real' else '模拟'}）")

    def run(agent: str, code: str, what: str):
        say("ai", f"「{s.agent_names[agent]}」{what}")
        r = s.run_agent(code, what, protect=protect, agent=agent)
        say("rt", f"回给「{s.agent_names[agent]}」的说明：\n" + r.text)
        return r

    def look(agent: str) -> list[str]:
        """AI 读一次场景。有没有运行时它都会读；区别是有运行时才会被告知"谁改了什么"。"""
        obs = s.observe(agent)
        if not protect:
            return []                       # 没有运行时，也就没有人告诉 AI 任何事
        say("rt", f"「{s.agent_names[agent]}」动手前先看场景，运行时告诉它：\n" + obs.text)
        return sorted(s.human_deleted.values())

    reports = []
    reports.append(run(LAYOUT, sc.ai_build(scene), "第 1 步：布置场景"))
    look(LOOK)

    if args.human == "sim":
        bridge.execute(sc.human_sim_d(scene))
        say("human", "删除 Rock_2；把 Leaf_3 往上挪")
        d = s.poll()
        say("rt", "发现人的修改：" + "；".join(d.describe()))
    else:
        stop = threading.Event()

        def watch() -> None:
            while not stop.is_set():
                try:
                    d = s.poll()
                    if d:
                        say("rt", "发现人的修改：" + "；".join(d.describe()))
                except BridgeError as e:
                    say("rt", f"检查失败：{e}")
                stop.wait(args.poll)

        th = threading.Thread(target=watch, daemon=True)
        th.start()
        wait_for_human(sc.HUMAN_STEPS_D)
        stop.set()
        th.join()

    skip = look(LAYOUT)
    reports.append(run(LAYOUT, sc.ai_layout(scene, skip), "第 2 步：调整布局（叶子抬高、树干 1.3、石头排圈）"))
    reports.append(run(LOOK, sc.ai_look(scene), "按第 1 步的印象做秋天效果（叶子变橙、树干 1.1、Rock_1 右移、灯光调亮）"))
    reports.append(run(LAYOUT, sc.ai_layout_followup(scene), "紧接着把树干改成 1.5"))
    look(LOOK)
    reports.append(run(LOOK, sc.ai_look_retry(scene), "看了最新情况后重试：树干 1.2"))

    final = bridge.call("poll", {"scene": scene} if scene else {})["records"]
    ev = evaluate(rt, final, s.human, granularity=args.granularity, labels=s.labels)
    between = sum(1 for r in reports for x in r.skipped if x["reason"] in (R_OTHER_AI, R_RESERVED))
    resur = sum(len(r.resurrections) for r in reports)
    errors = sum(len(r.inexact) + len(r.dangling) for r in reports)
    trunk = next((r for r in final.values() if r["name"] == "Trunk"), None)
    banner("结果")
    say("info", ev.summary())
    say("info", f"AI 之间的冲突被拦下：{between} 处；AI 重建了人删掉的对象：{resur} 个；恢复出错：{errors} 处")
    if trunk is not None:
        say("info", f"树干最后的缩放：{s.values.get(trunk['id'], {}).get('scale', '?')}"
                    "（有保护时应该是材质 AI 重试后的 1.2；没有保护时是最后执行的那个脚本写的值，中间谁被覆盖了没人知道）")
    if not args.no_csv:
        path = write_row({
            "实验": "D 多个AI+人", "做法": variant, "人": "真人" if args.human == "real" else "模拟",
            "人的修改被覆盖": len(ev.human_overwritten), "AI的修改丢失": len(ev.ai_lost),
            "被删对象被AI重建": resur, "恢复或合并出错": errors, "AI候选": 0,
            "AI修改生效": sum(len(r.committed) for r in reports), "AI修改未采用": sum(len(r.skipped) for r in reports),
            "人看到AI结果要等(秒)": round(max(r.seconds for r in reports), 4),
            "备注": f"AI 之间的冲突被拦下 {between} 处"})
        say("info", f"结果已追加到 {path}")


if __name__ == "__main__":
    main()
