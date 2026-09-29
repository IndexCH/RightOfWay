"""I 类：观察与追溯（R19、R20），以及身份（R1）。"""
import pytest

from cowork import ActorKind, OpStatus, ProtocolError
from helpers import AGENT, HUMAN, agent_op, make_runtime, submit


def test_S_I1_contract_violation_recorded_and_undoable():
    """S-I1 实际改动超出声明：记录违反契约，下调保证等级，超出部分可以撤回。"""
    rt = make_runtime()
    rt.create_object("X", "png", "x1", AGENT)
    rt.create_object("W", "png", "w1", AGENT)
    rt.add_step("S", inputs=[], outputs=["X"])
    submit(rt, "op", targets=["X"], step="S")
    rt.complete("op", writes={"X": "x2"}, extra_changes={"W": "w-changed"})

    violations = rt.events_of("violation/contract")
    assert violations and violations[0].data["unexpectedChanges"] == ["W"]
    assert rt.capabilities["edit"].verified is False

    rt.undo_step(HUMAN, "S")
    assert rt.objects["W"].content == "w1"
    assert rt.objects["X"].content == "x1"


def test_S_I2_exact_change_log_since_a_point():
    """S-I2 离开一段时间后回来，拿到准确的变化记录：谁、改了什么、结果如何。"""
    rt = make_runtime()
    rt.create_object("A", "png", "a1", AGENT)
    mark = rt.events[-1].seq

    submit(rt, "op1", targets=["A"])
    rt.human_edit(HUMAN, "A", "a-by-human")
    rt.complete("op1", writes={"A": "a-ai"})

    log = rt.log_since(mark)
    kinds = [e.type for e in log]
    assert "object/changed" in kinds
    assert "result/discarded" in kinds
    results = [e.data["status"] for e in log if e.type == "op/result"]
    assert results == ["rejected_stale"]


def test_R1_agent_cannot_act_as_human():
    """R1：身份由运行时决定。Agent 不能用人的接口，自报身份也无效。"""
    rt = make_runtime()
    rt.create_object("A", "png", "a1", AGENT)
    rt.human_edit(HUMAN, "A", "human")
    rt.release(HUMAN, "A")

    with pytest.raises(ProtocolError):
        rt.human_edit(AGENT, "A", "pretend")
    with pytest.raises(ProtocolError):
        rt.hand_back(AGENT, "A")

    op = agent_op(rt, "op", targets=["A"])
    op.actor_kind = ActorKind.HUMAN                   # 自报为人
    assert rt.submit(op).status == OpStatus.REJECTED_HUMAN_TOUCHED
    assert rt.ops["op"].actor_kind == ActorKind.AGENT
