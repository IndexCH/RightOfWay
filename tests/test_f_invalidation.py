"""F 类：失效范围与重跑（R12、R13）。"""
from rightofway import StepState, apply_collection_plan, plan_collection_update
from helpers import AGENT, HUMAN, make_runtime, run_agent_step


def test_S_F1_only_dependents_become_stale():
    """S-F1 A→B→D、A→C→D：改了 B 的输出，只有 D 过期，A、C 不动。"""
    rt = make_runtime()
    rt.add_step("A", inputs=[], outputs=["a"])
    rt.add_step("B", inputs=["a"], outputs=["b"])
    rt.add_step("C", inputs=["a"], outputs=["c"])
    rt.add_step("D", inputs=["b", "c"], outputs=["d"])
    run_agent_step(rt, "A", "opA", {"a": "a1"})
    run_agent_step(rt, "B", "opB", {"b": "b1"}, reads=["a"])
    run_agent_step(rt, "C", "opC", {"c": "c1"}, reads=["a"])
    run_agent_step(rt, "D", "opD", {"d": "d1"}, reads=["b", "c"])
    assert all(s.state == StepState.DONE for s in rt.steps.values())

    rt.human_edit(HUMAN, "b", "b-edited")
    assert rt.steps["A"].state == StepState.DONE
    assert rt.steps["B"].state == StepState.DONE
    assert rt.steps["C"].state == StepState.DONE
    assert rt.steps["D"].state == StepState.BLOCKED   # 已过期，且输入 b 正被占用
    rt.release(HUMAN, "b")
    assert rt.steps["D"].state == StepState.PENDING   # 释放后等待重跑


def test_S_F2_collection_members_added_and_removed():
    """S-F2 集合增删成员：只为新成员加处理；删除成员时，仍被别的步骤引用的产出不能撤。"""
    rt = make_runtime()
    rt.add_step("cut_all", inputs=["folder"], outputs=["out_v1", "out_v2", "out_v3"])
    rt.add_step("thumbnail", inputs=["out_v2"], outputs=["thumb"])   # 另一个步骤引用了 out_v2
    for name in ("out_v1", "out_v2", "out_v3"):
        rt.create_object(name, "png", name, AGENT)

    plan = plan_collection_update(
        rt, "cut_all", old_members=["v1", "v2", "v3"], new_members=["v1", "v4"],
        outputs_by_member={"v1": ["out_v1"], "v2": ["out_v2"], "v3": ["out_v3"]})
    assert plan.added == ["v4"]
    assert plan.removed == ["v2", "v3"]
    assert plan.retract == ["out_v3"]
    assert plan.keep == ["out_v2"]

    apply_collection_plan(rt, plan)
    assert rt.objects["out_v3"].retracted
    assert not rt.objects["out_v2"].retracted
