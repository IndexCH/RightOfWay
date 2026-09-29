"""实验 B（文件兜底）：人和 AI 各改一份，存盘后按对象合并。

给没有 MCP、也没有脚本接口的应用用；有 MCP 的应用用 exp_b_live.py（实时同步，不用存盘和恢复）。

AI 用 computer use 时必须在自己的屏幕上打开自己的一份文件。这里的"AI"用脚本在无界面 Blender 里
改它那一份来代替（第 1 级），检查的是合并规则；真正的 computer use 放在第 2 级。

运行方式（在项目根目录）：
    python -m experiments.exp_b_files                      真人在 Blender 里改文件并存盘
    python -m experiments.exp_b_files --human sim          模拟人的操作
    python -m experiments.exp_b_files --blender "C:\\Program Files\\Blender Foundation\\Blender 4.5\\blender.exe"
                                                           指定 Blender（默认自动查找，或设置环境变量 BLENDER_EXE）
    python -m experiments.exp_b_files --blender inprocess --human sim
                                                           用 pip 装的 bpy 在本进程模拟（需要 Python 3.11）

流程：
    1. AI 在自己那一份里布置场景，存盘 → 合并（第 1 版）
    2. 人打开 scene.blend，删 Rock_2、挪 Leaf_3、新建立方体，存盘 → 合并（第 2 版）
    3. AI 没有重新打开文件，按内存里的旧场景整体调整，存盘 → 合并（第 3 版）
    4. 人也没有重新打开，在旧内容上挪一下 Rock_1，存盘 → 合并（第 4 版）
    5. 对照：如果不合并、谁最后存盘谁生效，会丢掉什么
"""
from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

from cowork.blender.bridge import BridgeError, make_file_runner
from cowork.blender.evaluate import evaluate
from cowork.blender.filemerge import FileMergeSession
from cowork.blender.records import label
from cowork.runtime import POLICY_CANDIDATE, POLICY_DISCARD, Runtime
from experiments import scenario_tree as sc
from experiments.common import banner, say, use_utf8_console, wait_for_human, write_row

WORK = Path(__file__).resolve().parent / "work_b"


def main(argv=None) -> None:
    use_utf8_console()
    ap = argparse.ArgumentParser(description="实验 B：各自一份，存盘合并")
    ap.add_argument("--blender", default="auto", help="auto、inprocess，或 Blender 可执行文件路径")
    ap.add_argument("--human", choices=["real", "sim"], default="real")
    ap.add_argument("--policy", choices=[POLICY_DISCARD, POLICY_CANDIDATE], default=POLICY_DISCARD)
    ap.add_argument("--granularity", choices=["aspect", "object"], default="aspect",
                    help="aspect：按面合并（默认）；object：整个对象")
    ap.add_argument("--dir", default=str(WORK), help="实验文件放在哪里（每次运行会清空）")
    ap.add_argument("--no-csv", action="store_true")
    args = ap.parse_args(argv)

    try:
        runner = make_file_runner(args.blender)
    except BridgeError as e:
        print(e)
        return
    work = Path(args.dir)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    rt = Runtime(human_touched_policy=args.policy)
    s = FileMergeSession(rt, runner, work / "scene.blend", granularity=args.granularity)
    gran = "按面" if args.granularity == "aspect" else "按对象"
    s.start()
    ai_memory = work / ".cowork" / "ai_memory.blend"          # AI 的 Blender 内存里的状态
    human_memory = work / ".cowork" / "human_memory.blend"    # 模拟人没有重新打开文件时内存里的状态
    real = args.human == "real"

    banner(f"实验 B：各自一份，存盘合并（{gran}，人：{'真人' if real else '模拟'}）")
    say("info", f"人那一份：{s.human_path}\nAI 那一份：{s.ai_path}")

    # 1. AI 布置场景
    say("ai", "第 1 步：在我自己那一份里布置场景，存盘")
    runner.call("edit_file", {"path": str(s.ai_path), "code": sc.ai_build()})
    r1 = s.sync()
    say("rt", r1.text_for_human)
    shutil.copyfile(s.ai_path, ai_memory)          # AI 之后一直在这个状态上工作，不会自己重新打开

    # 2. 人修改
    if real:
        wait_for_human(sc.HUMAN_STEPS, extra=f"先用 Blender 打开 {s.human_path}；做完按 Ctrl+S 存盘。")
    else:
        runner.call("edit_file", {"path": str(s.human_path), "code": sc.human_sim()})
        say("human", "删除 Rock_2；把 Leaf_3 往上挪；新建一个立方体 Cube；存盘")
    r2 = s.sync()
    say("rt", r2.text_for_human)
    shutil.copyfile(s.human_path, human_memory)    # 人之后不重新打开，内存里是这个状态

    # 3. AI 在旧状态上调整
    say("ai", "第 2 步：按我之前看到的场景整体调整，存盘（我没有重新打开文件）")
    runner.call("edit_file", {"path": str(ai_memory), "code": sc.ai_adjust(), "save_to": str(s.ai_path)})
    r3 = s.sync()
    say("rt", "告诉人：\n" + r3.text_for_human)
    say("rt", "告诉 AI：\n" + r3.text_for_ai)
    baseline_ai_last = evaluate(rt, r3.raw_ai, s.human, granularity=args.granularity, labels=s.labels)      # 对照：AI 最后存盘、整份覆盖

    # 4. 人在旧内容上又改一处
    if real:
        wait_for_human([sc.HUMAN_STALE_STEP])
    else:
        runner.call("edit_file", {"path": str(human_memory), "code": sc.human_stale_sim(),
                                  "save_to": str(s.human_path)})
        say("human", "（没有重新打开文件）把 Rock_1 挪了一下，存盘")
    r4 = s.sync()
    say("rt", r4.text_for_human)
    baseline_human_last = evaluate(rt, r4.raw_human, s.human, granularity=args.granularity, labels=s.labels)  # 对照：人最后存盘、整份覆盖

    # 5. 检查
    ev = evaluate(rt, r4.final, s.human, granularity=args.granularity, labels=s.labels)
    reports = [r1, r2, r3, r4]
    errors = sum(len(r.errors) for r in reports)
    banner("结果")
    say("info", f"合并（{gran}）：" + ev.summary())
    if r3.partial:
        say("info", "两边改了同一个对象的不同部分、都保留：" + "；".join(
            f"{n}（AI 的{'、'.join(label(a, s.labels) for a in p['kept'])}，人的{'、'.join(label(a, s.labels) for a in p['restored'])}）"
            for n, p in sorted(r3.partial.items())))
    say("info", f"合并出错：{errors} 处" + (f"（{'；'.join(e for r in reports for e in r.errors)}）" if errors else ""))
    say("info", f"起点（从第几版开始改的）：第 3 次合并时人=第 {r3.human_base} 版（{r3.human_base_from}）、"
                f"AI=第 {r3.ai_base} 版（{r3.ai_base_from}）；第 4 次合并时人=第 {r4.human_base} 版（{r4.human_base_from}）、"
                f"AI=第 {r4.ai_base} 版（{r4.ai_base_from}）")
    say("info", "对照（不合并，谁最后存盘谁生效）：")
    say("info", f"  第 2 步后 AI 最后存盘：{baseline_ai_last.summary()}")
    say("info", f"  第 3 步后人最后存盘：{baseline_human_last.summary()}")
    wait = r3.seconds
    say("info", f"AI 存盘到合并完成用时 {wait:.2f} 秒；人还要在 Blender 里 文件 → 恢复 才能看到")
    if real:
        say("info", f"现在可以在 Blender 里 文件 → 恢复，看最终结果。历史版本在 {s.work / 'history'}")
    if not args.no_csv:
        common = {"实验": "B 文件兜底", "人": "真人" if real else "模拟"}
        write_row({**common, "做法": f"合并（{gran}）", "人的修改被覆盖": len(ev.human_overwritten),
                   "AI的修改丢失": len(ev.ai_lost), "被删对象被AI重建": 0, "恢复或合并出错": errors,
                   "AI候选": len(r3.candidates), "AI修改生效": r3.applied_units, "AI修改未采用": r3.rejected_units,
                   "人看到AI结果要等(秒)": wait, "备注": "另需人手动 文件→恢复；"
                   + (f"人在旧版本上覆盖了 AI 的修改：{'、'.join(r4.overridden_ai)}" if r4.overridden_ai else "")})
        write_row({**common, "做法": "对照：AI 最后存盘", "人的修改被覆盖": len(baseline_ai_last.human_overwritten),
                   "AI的修改丢失": len(baseline_ai_last.ai_lost), "备注": "整份文件覆盖"})
        path = write_row({**common, "做法": "对照：人最后存盘", "人的修改被覆盖": len(baseline_human_last.human_overwritten),
                          "AI的修改丢失": len(baseline_human_last.ai_lost), "备注": "整份文件覆盖"})
        say("info", f"结果已追加到 {path}")


if __name__ == "__main__":
    main()
