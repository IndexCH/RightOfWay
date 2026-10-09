"""v0.5 第 3 步：按应用交回的实际状态核对、提交（design_v0.5.md 2.3–2.5、第 6 节）。

运行时自己决定每个单元的结论，账本只记实际看到的；要保持的部分没保住就是"违规"。
"""
import pytest

from rightofway import ActorKind, Operation, OpStatus, Target
from rightofway.model import KEEP_HUMAN
from rightofway.runtime import Runtime


def _u(fp, name="X"):
    return {"fp": fp, "name": name}


def _rt(**kw):
    rt = Runtime(**kw)
    rt.register_actor("yuan", ActorKind.HUMAN)
    rt.register_actor("ai", ActorKind.AGENT)
    for oid in ("x#loc", "x#color", "y#loc", "z#loc"):
        rt.create_object(oid, "blob", _u("0", oid[0]), "ai")
    rt.mark_seen("ai")
    return rt


def _scene(rt):
    return {oid: o.content for oid, o in rt.objects.items() if not o.content.get("deleted")}


def test_each_unit_is_classified_from_the_actual_result():
    rt = _rt()
    rt.human_edit("yuan", "x#loc", _u("h", "x"))
    p = rt.admit("ai")
    before = _scene(rt)
    raw = {**before, "x#loc": _u("ai", "x"), "x#color": _u("ai", "x"), "y#loc": _u("ai", "y"),
           "n#loc": _u("new", "n")}
    final = {**raw, "x#loc": _u("h", "x"),                       # 人改过的面：恢复了
             "y#loc": _u("0", "y"),                              # 不用保持，却被改回去了
             "z#loc": _u("side", "z")}                           # AI 没直接改，却变了
    rt.add_step("s1", [], [])
    st = rt.settle(p, before, raw, final, step_id="s1")
    assert st.outcome == "committed" and not st.breach
    assert st.applied == ["x#color"] and st.created == ["n#loc"]
    assert st.kept == {"x#loc": {"reason": KEEP_HUMAN}}
    assert st.reverted == ["y#loc"] and st.side_effects == ["z#loc"]
    assert {oid: o.content["fp"] for oid, o in rt.objects.items()} == {k: v["fp"] for k, v in final.items()}


def test_breach_is_recorded_as_it_actually_is_and_pauses_the_agent():
    rt = _rt()
    rt.human_edit("yuan", "x#loc", _u("h", "x"))
    p = rt.admit("ai")
    before = _scene(rt)
    raw = {**before, "x#loc": _u("ai", "x")}
    st = rt.settle(p, before, raw, raw)                          # 接入代码没能恢复
    assert st.outcome == "breach" and st.suspended and list(st.breach) == ["x#loc"]
    obj = rt.objects["x#loc"]
    assert obj.content["fp"] == "ai" and obj.versions[-1].breach and obj.versions[-1].author_id == "ai"
    assert obj.human_touched is not None                          # 人碰过的标记保留：之后照样不能动
    assert rt.events_of("alert/human") and rt.events_of("agent/suspended")
    assert rt.admit("ai").refused["reason"] == "suspended"
    op = Operation("again", "ai", None, targets=[Target("y#loc", rt.objects["y#loc"].version)])
    assert rt.submit(op).status == OpStatus.REJECTED_SUSPENDED
    with pytest.raises(Exception):
        rt.resume_agent("ai", "ai")                               # 只有人能让它继续
    rt.resume_agent("yuan", "ai")
    assert rt.admit("ai").refused is None


def test_breach_without_pausing_when_configured():
    rt = _rt(pause_on_breach=False)
    rt.human_edit("yuan", "x#loc", _u("h", "x"))
    p = rt.admit("ai")
    before = _scene(rt)
    st = rt.settle(p, before, {**before, "x#loc": _u("ai", "x")}, {**before, "x#loc": _u("ai", "x")})
    assert st.outcome == "breach" and not st.suspended and rt.admit("ai").refused is None


@pytest.mark.parametrize("restored", [True, False])
def test_deleting_an_object_the_human_touched_is_judged_as_a_whole(restored):
    """R6：人碰过的对象整个不许删。删除被恢复 → 整个对象都算保持；没恢复 → 整个对象都是违规。"""
    rt = _rt()
    rt.human_edit("yuan", "x#loc", _u("h", "x"))
    p = rt.admit("ai")
    assert p.no_delete == {"x"}
    before = _scene(rt)
    raw = {k: v for k, v in before.items() if not k.startswith("x#")}
    final = before if restored else raw
    st = rt.settle(p, before, raw, final)
    if restored:
        assert set(st.kept) == {"x#loc", "x#color"} and st.kept["x#color"]["whole_object"] and not st.applied
    else:
        assert set(st.breach) == {"x#loc", "x#color"} and all(b["deleted"] for b in st.breach.values())
        assert all(rt.objects[u].content.get("deleted") for u in ("x#loc", "x#color"))


def test_deleting_an_untouched_object_is_applied():
    rt = _rt()
    p = rt.admit("ai")
    before = _scene(rt)
    after = {k: v for k, v in before.items() if not k.startswith("y#")}
    st = rt.settle(p, before, after, after)
    assert st.applied == ["y#loc"] and rt.objects["y#loc"].content == {"deleted": True, "name": "y"}


def test_reservation_is_granted_from_the_settlement():
    rt = _rt()
    rt.register_actor("ai2", ActorKind.AGENT)
    rt.mark_seen("ai2")
    op = Operation("first", "ai2", None, targets=[Target("y#loc", rt.objects["y#loc"].version)])
    rt.submit(op)
    rt.complete("first", writes={"y#loc": _u("ai2", "y")})        # ai2 先改了，ai 还没看到
    p = rt.admit("ai")
    before = _scene(rt)
    st = rt.settle(p, before, {**before, "y#loc": _u("ai", "y")}, before)
    assert st.reserved == ["y#loc"] and rt.reservations["y#loc"][0] == "ai"
