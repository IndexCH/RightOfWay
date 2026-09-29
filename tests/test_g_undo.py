"""G 类：撤回与版本（R14、R15）。"""
from rightofway import OpStatus, StepState
from helpers import AGENT, HUMAN, make_runtime, run_agent_step, submit


def _setup(rt):
    rt.create_object("X", "png", "x1", AGENT)
    rt.create_object("Y", "png", "y1", AGENT)
    rt.create_object("Z", "png", "z1", AGENT)
    rt.add_step("S2", inputs=[], outputs=["X", "Y"])
    rt.add_step("S3", inputs=["X"], outputs=["W"])


def test_S_G1_undo_last_agent_step():
    """S-G1 撤回 AI 最近一步：X、Y 恢复，Z 不受影响；下游过期；AI 不自动重跑。"""
    rt = make_runtime()
    _setup(rt)
    run_agent_step(rt, "S2", "op_s2", {"X": "x2", "Y": "y2"})
    run_agent_step(rt, "S3", "op_s3", {"W": "w1"}, reads=["X"])
    rt.human_edit(HUMAN, "Z", "z-by-human")
    rt.release(HUMAN, "Z")

    rt.undo_step(HUMAN, "S3")                         # 先撤最近的 S3
    out = rt.undo_last_agent_step(HUMAN)              # 再往前撤 S2
    assert set(out["restored"]) == {"X", "Y"}
    assert rt.objects["X"].content == "x1"
    assert rt.objects["Y"].content == "y1"
    assert rt.objects["Z"].content == "z-by-human"
    assert rt.steps["S2"].state == StepState.UNDONE

    # AI 不能自动重跑被撤回的步骤，要人重新发起
    assert submit(rt, "rerun", targets=["X"], step="S2").status == OpStatus.REJECTED_STEP_ASSIGNMENT
    rt.reopen_step(HUMAN, "S2")
    assert submit(rt, "rerun2", targets=["X"], step="S2").status == OpStatus.EXECUTING


def test_S_G1_undo_marks_downstream_stale():
    """撤回 S2 后，读取 X 的下游步骤 S3 标为过期。"""
    rt = make_runtime()
    _setup(rt)
    run_agent_step(rt, "S2", "op_s2", {"X": "x2", "Y": "y2"})
    run_agent_step(rt, "S3", "op_s3", {"W": "w1"}, reads=["X"])
    rt.undo_step(HUMAN, "S2")                         # 直接指定撤回 S2
    assert rt.steps["S3"].state == StepState.STALE


def test_S_G2_undo_skips_object_edited_by_human_afterwards():
    """S-G2 撤回时，之后被人改过的对象保持人的版本，其他对象正常撤回。"""
    rt = make_runtime()
    _setup(rt)
    run_agent_step(rt, "S2", "op_s2", {"X": "x2", "Y": "y2"})
    rt.human_edit(HUMAN, "X", "x-by-human")
    rt.release(HUMAN, "X")

    out = rt.undo_step(HUMAN, "S2")
    assert out["skipped"] == [{"objectId": "X", "reason": "human_touched"}]
    assert rt.objects["X"].content == "x-by-human"
    assert rt.objects["Y"].content == "y1"


def test_S_G3_replan_can_be_undone_and_old_versions_kept():
    """S-G3 重新规划后，旧计划仍在历史里，撤回这次重新规划就回到旧计划。"""
    rt = make_runtime()
    rt.create_object("comp", "composition", {"steps": ["frames", "cutout", "sheet"]}, AGENT)
    rt.add_step("replan", inputs=[], outputs=["comp"])
    run_agent_step(rt, "replan", "op_replan", {"comp": {"steps": ["frames", "cloud_cutout", "sheet"]}})
    assert rt.objects["comp"].get_version(1).content["steps"][1] == "cutout"   # 旧版本保留

    rt.undo_step(HUMAN, "replan")
    assert rt.objects["comp"].content["steps"][1] == "cutout"


def test_undo_restores_human_touched_state_of_restored_version():
    """撤回产生的版本，'人碰过'状态继承自被恢复的那个版本。"""
    rt = make_runtime()
    rt.create_object("X", "png", "by-human", HUMAN)   # 最初由人创建
    rt.hand_back(HUMAN, "X")                          # 交还给 AI
    rt.add_step("S", inputs=[], outputs=["X"])
    run_agent_step(rt, "S", "op", {"X": "by-ai"})
    assert rt.objects["X"].human_touched is None

    rt.undo_step(HUMAN, "S")
    assert rt.objects["X"].content == "by-human"
    assert rt.objects["X"].human_touched is not None
