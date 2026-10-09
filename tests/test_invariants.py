"""不变量检查（design_v0.5.md 第 3 节；实施顺序第 1 步），以及第 3 步之后应有的行为。

第 1 步时，"删掉人改过的对象"几条测试标了 xfail(strict=True)：在 v0.4 上必须失败，证明不变量抓得到问题。
第 3 步（按实际状态核对、提交，加上"违规"结局）之后它们通过了，标记已去掉。
"""
import pytest

from rightofway.blender.records import split_unit
from rightofway.blender.shared_session import R_HUMAN, SharedSession
from rightofway.fakeapp import FakeApp, FakeBridge
from rightofway.invariants import I1, I3, check_ledger, check_session, summarize
from rightofway.runtime import Runtime

DELETE_LEAF3 = "scene.delete('Leaf_3')\nscene.set('Leaf_1', 'scale', 1.2)"


def _setup(restore_deleted=True, granularity="aspect"):
    app = FakeApp(restore_deleted=restore_deleted)
    for i in range(1, 4):
        app.add(f"Leaf_{i}", {"location": [0, i, 1.4], "scale": 1.0, "color": "green"})
    s = SharedSession(Runtime(), FakeBridge(app), granularity=granularity)
    s.start()
    return app, s


def _human_moves_leaf3(app, s):
    app.set("Leaf_3", "location", [0, 3, 2.2])
    s.poll()


# ---------------------------------------------------------------------------
# 检查本身：正常的冲突里三条都成立，人为制造的不一致能抓到
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("granularity", ["aspect", "object"])
def test_invariants_hold_for_an_ordinary_conflict(granularity):
    app, s = _setup(granularity=granularity)
    _human_moves_leaf3(app, s)
    rep = s.run_agent("for n in scene.names():\n    scene.set(n, 'location', [0, 0, 1.6])\n"
                      "    scene.set(n, 'color', 'orange')", "秋天")
    assert check_session(s, rep) == []
    leaf3 = app.objects[app.find("Leaf_3")]["faces"]
    assert leaf3["location"] == [0, 3, 2.2]                       # 人的保留
    assert leaf3["color"] == ("orange" if granularity == "aspect" else "green")
    assert any(x["reason"] == R_HUMAN for x in rep.skipped)


def test_check_ledger_catches_a_change_the_ledger_never_saw():
    app, s = _setup()
    app.delete("Leaf_2")                                         # 绕过运行时直接删掉（不轮询）
    vs = check_ledger(s.rt, FakeBridge(app).call("poll")["records"], s.granularity)
    assert vs and {v.rule for v in vs} == {I1}
    assert all("Leaf_2" in v.where for v in vs)
    s.poll()                                                     # 记进账本之后就一致了
    assert check_session(s) == []


def test_check_run_catches_a_false_claim():
    """把报告里的一项"保留了"改成"已生效"：说明和场景对不上，I3 抓到。"""
    app, s = _setup()
    _human_moves_leaf3(app, s)
    rep = s.run_agent("scene.set('Leaf_3', 'location', [0, 0, 9])", "改 Leaf_3")
    assert check_session(s, rep, poll=False) == []
    lie = rep.skipped.pop()
    rep.committed.append(lie["id"])
    vs = check_session(s, rep, poll=False)
    assert [v.rule for v in vs] == [I3] and "已生效" in vs[0].detail


# ---------------------------------------------------------------------------
# 复现：Agent 删掉人刚改过的对象（你画的那张时序图）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("granularity", ["aspect", "object"])
def test_unrecoverable_delete_keeps_scene_ledger_and_feedback_consistent(granularity):
    app, s = _setup(restore_deleted=False, granularity=granularity)
    _human_moves_leaf3(app, s)
    rep = s.run_agent(DELETE_LEAF3, "删掉 Leaf_3")
    vs = check_session(s, rep)
    assert vs == [], summarize(vs) + "\n告诉 Agent 的：\n" + rep.text


@pytest.mark.parametrize("granularity", ["aspect", "object"])
def test_unrecoverable_delete_is_a_breach(granularity):
    """恢复不了的删除：结局是违规。账本照实记为已删除（标违规），提醒人，暂停这个 AI，如实告诉它。"""
    app, s = _setup(restore_deleted=False, granularity=granularity)
    _human_moves_leaf3(app, s)
    leaf3 = app.find("Leaf_3")
    rep = s.run_agent(DELETE_LEAF3, "删掉 Leaf_3")
    assert rep.outcome == "breach" and rep.suspended
    assert {split_unit(u)[0] for u in rep.breach} == {leaf3}
    assert "Leaf_3" not in [o["name"] for o in app.objects.values()]       # 场景：叶子没了
    for uid, obj in s.rt.objects.items():                                  # 账本：也没了，标明违规
        if split_unit(uid)[0] == leaf3:
            assert obj.content.get("deleted") and obj.versions[-1].breach
    assert "违规" in rep.text and "被你删除了" in rep.text and "保留人的" not in rep.text
    assert "删除了 Leaf_3" in rep.alert and "已暂停" in rep.alert
    assert rep.committed == ([f"{app.find('Leaf_1')}#scale"] if granularity == "aspect" else [app.find("Leaf_1")])
    # 被暂停之后不执行，人处理完让它继续
    again = s.run_agent("scene.set('Leaf_2', 'scale', 2.0)", "再改")
    assert again.status == "refused" and "已被暂停" in again.text
    assert app.objects[app.find("Leaf_2")]["faces"]["scale"] == 1.0
    s.resume_agent()
    assert s.run_agent("scene.set('Leaf_2', 'scale', 2.0)", "再改").status == "ok"
    assert app.objects[app.find("Leaf_2")]["faces"]["scale"] == 2.0
    assert check_session(s) == []


def test_restored_delete_is_not_reported_as_partly_applied():
    app, s = _setup(restore_deleted=True, granularity="aspect")
    _human_moves_leaf3(app, s)
    rep = s.run_agent(DELETE_LEAF3, "删掉 Leaf_3")
    assert "Leaf_3" in [o["name"] for o in app.objects.values()]         # 整个对象被恢复了
    vs = check_session(s, rep)
    assert vs == [], summarize(vs) + "\n告诉 Agent 的：\n" + rep.text
    assert rep.outcome == "committed" and not rep.breach
    assert "人改过的对象不能删除，已恢复" in rep.text and "部分生效" not in rep.text
    assert all(x.get("whole_object") or x["id"].endswith("#location") for x in rep.skipped)


def test_restored_delete_holds_at_object_granularity():
    app, s = _setup(restore_deleted=True, granularity="object")
    _human_moves_leaf3(app, s)
    rep = s.run_agent(DELETE_LEAF3, "删掉 Leaf_3")
    assert check_session(s, rep) == []
    assert "Leaf_3" in rep.text and "人改过" in rep.text and rep.outcome == "committed"


# ---------------------------------------------------------------------------
# 同样的情况放到真的 Blender 里（需要 bpy）
# ---------------------------------------------------------------------------
def _blender(granularity):
    pytest.importorskip("bpy")
    from rightofway.blender.bridge import InProcessBridge
    from experiments import scenario_tree as sc
    InProcessBridge.reset()
    br = InProcessBridge()
    s = SharedSession(Runtime(), br, granularity=granularity)
    s.start()
    s.run_agent(sc.ai_build(), "build")
    br.execute(sc.human_move_leaf3())
    s.poll()
    rep = s.run_agent(sc.ai_delete_leaf3(), "删掉 Leaf_3")
    return s, rep


def test_blender_delete_of_human_edited_object_is_reported_truthfully():
    s, rep = _blender("aspect")
    vs = check_session(s, rep)
    assert vs == [], summarize(vs) + "\n告诉 Agent 的：\n" + rep.text


def test_blender_delete_of_human_edited_object_at_object_granularity():
    s, rep = _blender("object")
    assert check_session(s, rep) == [], rep.text
