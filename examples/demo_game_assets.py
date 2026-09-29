"""演示：游戏素材流程里，人和 AI 同时干活时运行时怎么处理。

在 VS Code 里：左侧"运行和调试" → 选"运行演示：游戏素材流程" → 点绿色三角。
也可以在终端里运行：python examples/demo_game_assets.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # 没安装包时也能直接运行

from cowork import ActorKind, CapabilityMeta, Event, Operation, Runtime, Target  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 把运行时发出的事件翻译成中文，方便看懂
DESCRIBE = {
    "op/dispatched": lambda d: f"派发操作 {d['opId']}",
    "op/result": lambda d: f"操作 {d['opId']} 的结果：{d['status']}"
                           + (f"（原因：{d['reason']}）" if d.get("reason") else ""),
    "result/discarded": lambda d: f"  ↳ 丢弃了对 {d['objectId']} 的写入（{d['reason']}）",
    "occupancy/acquired": lambda d: f"{d['holder']} 开始修改 {d['objectId']} → 占用",
    "occupancy/released": lambda d: f"{d['holder']} 释放了 {d['objectId']}",
    "step/state": lambda d: f"步骤「{d['stepId']}」→ {d['state']}",
    "step/undone": lambda d: f"撤回步骤「{d['stepId']}」：恢复 {d['restored']}，撤掉 {d['retracted']}，跳过 {d['skipped']}",
    "confirm/request": lambda d: f"请求人确认：{d['opId']}（副作用等级 {d['effectLevel']}）",
    "object/changed": lambda d: f"{d['objectId']} → 版本 {d['version']}（{d['author']}）",
}


def show(event: Event) -> None:
    if event.type in DESCRIBE:
        print(f"   [{event.seq:03d}] {DESCRIBE[event.type](event.data)}")


def title(text: str) -> None:
    print(f"\n== {text} ==")


def main() -> None:
    rt = Runtime()
    rt.subscribe(show)
    rt.register_actor("你", ActorKind.HUMAN)
    rt.register_actor("AI", ActorKind.AGENT)
    rt.register_capability(CapabilityMeta("remove_background", effect_level=1))
    rt.register_capability(CapabilityMeta("pack_atlas", effect_level=0))
    rt.register_capability(CapabilityMeta("upload_to_store", effect_level=3))

    frames = [f"帧{i}" for i in range(1, 7)]
    for f in frames:
        rt.create_object(f"原图_{f}", "png", f"{f}-原图", "AI")
    rt.add_step("去背景", inputs=[f"原图_{f}" for f in frames], outputs=[f"抠图_{f}" for f in frames])
    rt.add_step("拼图集", inputs=[f"抠图_{f}" for f in frames], outputs=["图集"])
    rt.add_step("上传", inputs=["图集"], outputs=["商店页面"])

    title("1. AI 批量去背景：每帧一个操作，全部派发")
    for f in frames:
        rt.submit(Operation(f"抠_{f}", "AI", "remove_background", step_id="去背景",
                            reads=[Target(f"原图_{f}", 1)], produces=[f"抠图_{f}"]))
    for f in frames[:2]:
        rt.complete(f"抠_{f}", produces={f"抠图_{f}": f"{f}-AI抠图"})

    title("2. 你在 PS 里直接修帧3 的原图（AI 还在处理帧3）")
    rt.human_edit("你", "原图_帧3", "帧3-你修过的原图")

    title("3. AI 基于旧原图算出的帧3 结果到了 → 作废；其余帧照常提交")
    for f in frames[2:]:
        rt.complete(f"抠_{f}", produces={f"抠图_{f}": f"{f}-AI抠图"})

    title("4. 你修完，明确释放")
    rt.release("你", "原图_帧3")

    title("5. 你直接在 PS 里精修了 帧5 的抠图，然后释放")
    rt.human_edit("你", "抠图_帧5", "帧5-你精修的边缘")
    rt.release("你", "抠图_帧5")

    title("6. AI 决定用新参数把所有帧重抠一遍 → 你精修过的帧5 不动")
    done = [f for f in frames if f"抠图_{f}" in rt.objects]
    for f in done:
        rt.submit(Operation(f"重抠_{f}", "AI", "remove_background", step_id="去背景",
                            reads=[Target(f"原图_{f}", rt.objects[f"原图_{f}"].version)],
                            targets=[Target(f"抠图_{f}", rt.objects[f"抠图_{f}"].version)]))
    for f in done:
        if rt.ops[f"重抠_{f}"].status.value == "executing":
            rt.complete(f"重抠_{f}", writes={f"抠图_{f}": f"{f}-AI新参数抠图"})
    if "抠图_帧3" not in rt.objects:                  # 帧3 之前作废了，这次补上
        rt.submit(Operation("补抠_帧3", "AI", "remove_background", step_id="去背景",
                            reads=[Target("原图_帧3", rt.objects["原图_帧3"].version)],
                            produces=["抠图_帧3"]))
        rt.complete("补抠_帧3", produces={"抠图_帧3": "帧3-用你修过的原图抠的"})

    title("7. AI 拼图集")
    rt.submit(Operation("拼", "AI", "pack_atlas", step_id="拼图集",
                        reads=[Target(f"抠图_{f}", rt.objects[f"抠图_{f}"].version) for f in frames],
                        produces=["图集"]))
    rt.complete("拼", produces={"图集": "图集-v1"})

    title("8. 你觉得图集不对，长按撤回最近一步")
    rt.undo_last_agent_step("你")

    title("9. 你重新发起拼图集，AI 重做")
    rt.reopen_step("你", "拼图集")
    rt.submit(Operation("再拼", "AI", "pack_atlas", step_id="拼图集",
                        reads=[Target(f"抠图_{f}", rt.objects[f"抠图_{f}"].version) for f in frames],
                        produces=["图集"]))
    rt.complete("再拼", produces={"图集": "图集-v2"})

    title("10. AI 要上传到商店（撤不回）→ 必须等你确认")
    rt.submit(Operation("上传", "AI", "upload_to_store", step_id="上传",
                        reads=[Target("图集", rt.objects["图集"].version)], produces=["商店页面"]))
    rt.confirm("你", "上传", approved=True)
    rt.complete("上传", produces={"商店页面": "已上架"})

    title("最终状态")
    for oid in [f"抠图_{f}" for f in frames] + ["图集", "商店页面"]:
        obj = rt.objects[oid]
        mark = "（人碰过）" if obj.human_touched else ""
        print(f"   {oid}: {obj.content}  版本 {obj.version}{mark}")
    print(f"\n   一共 {len(rt.events)} 条事件，可以用 rt.log_since(序号) 查任意时间点之后的变化。")


if __name__ == "__main__":
    main()
