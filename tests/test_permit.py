"""v0.5 第 2 步：许可单由运行时开，接入代码只照单执行（design_v0.5.md 第 2 节）。

- 运行时：admit() 开许可单；它和 submit() 的检查用同一组函数，结论必须一致（P9：规则只写一处）。
- 接入代码：自己不再判断哪些该保护；执行前核对场景，和运行时以为的不一样就不执行（replan）。
"""
import itertools

import pytest

from rightofway import ActorKind, Operation, OpStatus, Target
from rightofway.blender.shared_session import R_HUMAN, R_SELECTED, SharedSession
from rightofway.fakeapp import FakeApp, FakeBridge
from rightofway.invariants import check_session
from rightofway.model import KEEP_HUMAN, KEEP_OTHER_AGENT, KEEP_RESERVED, KEEP_SELECTED
from rightofway.runtime import Runtime


def _rt(**kw):
    rt = Runtime(**kw)
    rt.register_actor("yuan", ActorKind.HUMAN)
    rt.register_actor("a1", ActorKind.AGENT)
    rt.register_actor("a2", ActorKind.AGENT)
    for oid in ("x#location", "x#color", "y#location"):
        rt.create_object(oid, "blob", {"v": 0}, "a1")
    rt.mark_seen("a1")
    rt.mark_seen("a2")
    return rt


def _commit(rt, agent, oid, content):
    op = Operation(f"op-{len(rt.ops) + 1}", agent, None, targets=[Target(oid, rt.objects[oid].version)])
    assert not rt.submit(op).status.is_rejection
    rt.complete(op.op_id, writes={oid: content})


# ---------------------------------------------------------------------------
# 运行时
# ---------------------------------------------------------------------------
def test_permit_keeps_what_the_human_touched():
    rt = _rt()
    rt.human_edit("yuan", "x#location", {"v": 1})
    p = rt.admit("a1")
    assert p.keep == {"x#location": {"reason": KEEP_HUMAN}}
    assert p.keep_by_group() == {"x": ["location"]}


def test_permit_stale_from_other_agent_then_reservation():
    rt = _rt()
    _commit(rt, "a2", "y#location", {"v": 2})                      # a2 先改了，a1 还没看到
    p = rt.admit("a1")
    assert p.keep == {"y#location": {"reason": KEEP_OTHER_AGENT, "by": "a2"}}
    assert rt.close_permit(p, ["y#location"]) == ["y#location"]   # a1 被拒，给它预留
    p2 = rt.admit("a2")
    assert p2.keep == {"y#location": {"reason": KEEP_RESERVED, "by": "a1"}}
    op = Operation("direct", "a2", None, targets=[Target("y#location", rt.objects["y#location"].version)])
    assert rt.submit(op).status == OpStatus.REJECTED_RESERVED     # 直接提交也一样被拦
    rt.mark_seen("a1")
    rt.close_permit(rt.admit("a1"), [])                            # a1 用掉了预留
    assert rt.admit("a2").keep == {}


def test_selection_occupies_only_when_the_option_is_on():
    rt = _rt()
    rt.set_selection("yuan", ["x"])
    assert rt.admit("a1").keep == {}
    rt.selection_occupies = True
    assert rt.admit("a1").keep == {"x#location": {"reason": KEEP_SELECTED}, "x#color": {"reason": KEEP_SELECTED}}
    op = Operation("sel", "a1", None, targets=[Target("x#color", rt.objects["x#color"].version)])
    assert rt.submit(op).status == OpStatus.REJECTED_OCCUPIED
    with pytest.raises(Exception):
        rt.set_selection("a1", ["y"])                              # 只有人能选中（R1）


@pytest.mark.parametrize("touched,occupied,stale,reserved,selected",
                         list(itertools.product([False, True], repeat=5)))
def test_permit_and_submit_agree(touched, occupied, stale, reserved, selected):
    """同一个状态下：许可单要保持的，正好就是直接提交会被拒的（P9）。"""
    rt = _rt(selection_occupies=True)
    oid = "x#location"
    if touched:
        rt.human_edit("yuan", oid, {"v": 1}, occupy=occupied)
        rt.mark_seen("a1")
    elif occupied:
        rt.human_edit("yuan", oid, {"v": 1}, occupy=True)
        rt.hand_back("yuan", oid)
        rt.mark_seen("a1")
    if stale:
        _commit(rt, "a2", oid, {"v": 9}) if not (touched or occupied) else rt.human_edit("yuan", oid, {"v": 9})
    if reserved:
        rt.reservations[oid] = ("a2", 1e12)
    if selected:
        rt.set_selection("yuan", ["x"])
    kept = oid in rt.admit("a1").keep
    op = Operation("probe", "a1", None, targets=[Target(oid, rt.seen["a1"].get(oid, -1))])
    rejected = rt.submit(op).status.is_rejection
    assert kept == rejected


# ---------------------------------------------------------------------------
# 接入代码 + 会话
# ---------------------------------------------------------------------------
def _scene(**kw):
    app = FakeApp()
    for i in range(1, 4):
        app.add(f"Leaf_{i}", {"location": [0, i, 1.4], "color": "green"})
    br = CountingBridge(app)
    s = SharedSession(Runtime(), br, **kw)
    s.start()
    return app, br, s


class CountingBridge(FakeBridge):
    def __init__(self, app):
        super().__init__(app)
        self.runs = []

    def call(self, fn, args=None):
        res = super().call(fn, args)
        if fn == "run_agent":
            self.runs.append(res["status"])
        return res


def test_integration_no_longer_protects_on_its_own():
    """接入代码只照许可单恢复：许可单里没有的，哪怕和 known 不一样，也不再自己保护。"""
    app = FakeApp()
    cid = app.add("Leaf_1", {"location": [0, 0, 1]})
    old = app.records()
    app.set("Leaf_1", "location", [0, 0, 5])                     # known 是旧的
    res = FakeBridge(app).call("run_agent", {"code": "scene.set('Leaf_1', 'location', [0, 0, 7])",
                                             "protected": {}, "known": old})
    assert res["status"] == "ok" and res["restored"] == [] and app.objects[cid]["faces"]["location"] == [0, 0, 7]


def test_human_change_between_poll_and_run_triggers_replan():
    app, br, s = _scene()
    app.set("Leaf_3", "location", [0, 3, 2.2])                   # 人刚改过，运行时还没轮询到
    rep = s.run_agent("for n in scene.names():\n    scene.set(n, 'location', [0, 0, 1.6])", "抬高")
    assert br.runs == ["replan", "ok"]                           # 第一次不执行，记下人的修改后重开许可单
    assert app.objects[app.find("Leaf_3")]["faces"]["location"] == [0, 3, 2.2]
    assert [x["reason"] for x in rep.skipped] == [R_HUMAN]
    assert s.rt.objects[f"{app.find('Leaf_3')}#location"].versions[-1].author_id == s.human
    assert check_session(s, rep) == []


def test_selection_change_without_poll_triggers_replan():
    app, br, s = _scene(occupy_selection=True)
    app.select("Leaf_1")                                         # 选中不改变指纹，但改变许可单
    rep = s.run_agent("scene.set('Leaf_1', 'color', 'orange')\nscene.set('Leaf_2', 'color', 'orange')", "秋天")
    assert br.runs == ["replan", "ok"]
    assert {x["name"] for x in rep.skipped if x["reason"] == R_SELECTED} == {"Leaf_1"}
    assert app.objects[app.find("Leaf_1")]["faces"]["color"] == "green"
    assert app.objects[app.find("Leaf_2")]["faces"]["color"] == "orange"


def test_gives_up_when_the_scene_keeps_changing():
    app, br, s = _scene()
    real = app.run_agent

    def busy(args):                                              # 人一直在改
        app.set("Leaf_2", "location", [0, 0, len(br.runs)])
        return real(args)
    app.run_agent = busy
    rep = s.run_agent("scene.set('Leaf_1', 'color', 'orange')", "秋天", max_replan=2)
    assert rep.status == "deferred" and br.runs == ["replan"] * 3
    assert app.objects[app.find("Leaf_1")]["faces"]["color"] == "green"


def test_control_group_is_not_held_back_by_the_check():
    """对照组模拟现在的 Blender MCP：不核对、不保护。"""
    app, br, s = _scene()
    app.set("Leaf_3", "location", [0, 3, 2.2])
    s.run_agent("scene.set('Leaf_3', 'location', [0, 0, 1.6])", "抬高", protect=False)
    assert br.runs == ["ok"] and app.objects[app.find("Leaf_3")]["faces"]["location"] == [0, 0, 1.6]
