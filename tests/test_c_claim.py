"""C 类：认领与分工（R11）。"""
from cowork import OpStatus, StepState
from helpers import AGENT, HUMAN, make_runtime, submit


def test_S_C1_human_claims_pending_step():
    """S-C1 人顺手做了 AI 还没做的子任务：AI 不再执行，下游用人的产出。"""
    rt = make_runtime()
    rt.add_step("footer", inputs=[], outputs=["footer"])
    rt.add_step("publish", inputs=["footer"], outputs=["site"])

    rt.claim_step(HUMAN, "footer")
    res = submit(rt, "op_footer", produces=["footer"], step="footer")
    assert res.status == OpStatus.REJECTED_STEP_ASSIGNMENT

    rt.complete_human_step(HUMAN, "footer", {"footer": "footer-by-human"})
    assert rt.steps["footer"].state == StepState.DONE_BY_HUMAN
    assert rt.objects["footer"].human_touched is not None
    assert rt.objects["footer"].content == "footer-by-human"


def test_S_C2_step_assigned_to_human():
    """S-C2 分配给人的步骤，AI 不能执行。"""
    rt = make_runtime()
    rt.create_object("draft", "text", "d", AGENT)
    rt.add_step("approve", inputs=["draft"], outputs=["approved"], assignee="human")
    res = submit(rt, "op", reads=["draft"], produces=["approved"], step="approve")
    assert res.status == OpStatus.REJECTED_STEP_ASSIGNMENT
