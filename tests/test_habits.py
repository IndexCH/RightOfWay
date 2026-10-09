"""真实 AI 的写法（实验 H，design_v0.5.md 第 12 节）：类型化命令的预检查、清空重建、认回同一个对象。

用内存里的假应用（rightofway/fakeapp.py）；最后几条在真的 Blender 里再跑一遍（需要 bpy）。
"""
import pytest

from rightofway.blender.shared_session import R_HUMAN, SharedSession
from rightofway.fakeapp import FakeApp, FakeBridge
from rightofway.invariants import check_session, summarize
from rightofway.model import KEEP_HUMAN, Permit
from rightofway.runtime import Runtime
from experiments import scenario_fake as sf


def _tree(restore_deleted=True, reidentify=False, granularity="aspect"):
    """树干 + 三片叶子（叶子的父对象是树干）。人把 Leaf_3 往上挪。"""
    app = FakeApp(restore_deleted=restore_deleted)
    app.add("Trunk", {"location": [0, 0, 1.2], "scale": 1.0, "color": "树皮"}, kind="MESH")
    for i in range(1, 4):
        app.add(f"Leaf_{i}", {"location": [0, i, 1.4], "scale": 1.0, "color": "绿"}, kind="MESH", parent="Trunk")
    s = SharedSession(Runtime(reidentify=reidentify), FakeBridge(app), granularity=granularity)
    s.start()
    app.set("Leaf_3", "location", [0, 3, 2.2])
    s.poll()
    return app, s


def _faces(app, name):
    return [o["faces"] for o in app.objects.values() if o["name"] == name]


REBUILD = """
for n in list(scene.names()):
    scene.delete(n)
scene.create("Trunk", {"location": [0, 0, 1.2], "scale": 1.3, "color": "树皮"}, kind="MESH")
for i in range(1, 4):
    scene.create(f"Leaf_{i}", {"location": [0, i, 1.6], "scale": 1.0, "color": "橙"}, parent="Trunk", kind="MESH")
"""


# ---------------------------------------------------------------------------
# 假应用的父对象
# ---------------------------------------------------------------------------
def test_deleting_a_parent_clears_the_childs_parent_face():
    app, s = _tree()
    trunk = app.find("Trunk")
    assert all(f["parent"] == trunk for n in ("Leaf_1", "Leaf_2") for f in _faces(app, n))
    app.delete("Trunk")
    assert all(f["parent"] is None for n in ("Leaf_1", "Leaf_2") for f in _faces(app, n))


# ---------------------------------------------------------------------------
# 许可单的预检查（Permit.screen）
# ---------------------------------------------------------------------------
def test_permit_screen():
    p = Permit("p", "ai", keep={"x#location": {"reason": KEEP_HUMAN}, "y": {"reason": KEEP_HUMAN}})
    assert p.screen("x", ["location", "color"]) == (["color"], {"location": {"reason": KEEP_HUMAN}})
    assert p.screen("x", delete=True) == ([], {"*": {"reason": KEEP_HUMAN}})
    assert p.screen("y", ["color"]) == ([], {"*": {"reason": KEEP_HUMAN}})          # 整个对象都要保持
    assert p.screen("z", ["color"]) == (["color"], {})
    assert p.screen("z", delete=True) == (["*"], {})


# ---------------------------------------------------------------------------
# 类型化命令
# ---------------------------------------------------------------------------
CMDS = [{"action": "delete", "target": "Leaf_3"},
        {"action": "modify", "target": "Leaf_3", "set": {"location": [0, 0, 1.6], "color": "橙"}},
        {"action": "modify", "target": "Leaf_1", "set": {"scale": 1.2, "color": "橙"}},
        {"action": "modify", "target": "Nope", "set": {"scale": 2}},
        {"action": "create", "name": "Bird", "set": {"location": [0, 0, 3]}, "kind": "MESH"}]


@pytest.mark.parametrize("restore_deleted", [True, False])
def test_typed_delete_of_human_edited_object_is_refused_before_execution(restore_deleted):
    """应用恢复不了删除（现在的 Unity）也没关系：删除在执行前就被拒了，不会违规。"""
    app, s = _tree(restore_deleted=restore_deleted)
    reps = s.run_commands(CMDS, "批量")
    assert len(reps) == 1 and reps[0].outcome == "committed" and not reps[0].breach
    assert [e["status"] for e in reps[0].commands] == ["refused", "partial", "ok", "failed", "ok"]
    leaf3 = _faces(app, "Leaf_3")
    assert len(leaf3) == 1 and leaf3[0]["location"] == [0, 3, 2.2]           # 人的位置保住了
    assert leaf3[0]["color"] == "橙"                                         # 删除被拒，不妨碍别的命令改它的颜色
    assert _faces(app, "Leaf_1")[0]["scale"] == 1.2 and _faces(app, "Bird")
    assert check_session(s, reps[0]) == []
    assert "没有执行" in reps[0].text and "执行失败" in reps[0].text


def test_typed_batches_are_split_and_not_transactional():
    app, s = _tree()
    reps = s.run_commands(CMDS, "批量", max_batch=2)
    assert len(reps) == 3                                                     # 每批单独开许可单、单独核对
    assert [e["index"] for r in reps for e in r.commands] == [0, 1, 2, 3, 4]
    assert reps[1].commands[1]["status"] == "failed" and _faces(app, "Bird")  # 一条失败，后面的照样执行
    for r in reps:
        assert check_session(s, r) == []


def test_typed_command_on_whole_kept_object_is_refused():
    app, s = _tree(granularity="object")
    reps = s.run_commands([{"action": "modify", "target": "Leaf_3", "set": {"color": "橙"}}], "改色")
    assert reps[0].commands[0]["status"] == "refused" and _faces(app, "Leaf_3")[0]["color"] == "绿"
    assert any(x["reason"] == R_HUMAN and x.get("precheck") for x in reps[0].skipped)
    assert check_session(s, reps[0]) == []


# ---------------------------------------------------------------------------
# 清空重建：不认回同一个对象时，出现两份、父对象断开；认回之后只有一份
# ---------------------------------------------------------------------------
def test_rebuild_without_reidentify_duplicates():
    """不认回（对照）：AI 新建的都是新对象。人改过的 Leaf_3 和它的祖先 Trunk（不许删，12.6）被恢复，
    和 AI 新建的同名对象并存，各有一份。"""
    app, s = _tree(restore_deleted=True)
    rep = s.run_agent(REBUILD, "清空重建")
    assert len(_faces(app, "Leaf_3")) == 2 and len(_faces(app, "Trunk")) == 2
    human = next(f for f in _faces(app, "Leaf_3") if f["location"] == [0, 3, 2.2])
    assert app.objects[human["parent"]]["faces"]["scale"] == 1.0              # 还挂在原来的树干上
    assert rep.outcome == "committed" and rep.reidentified == []
    assert check_session(s, rep) == []


@pytest.mark.parametrize("restore_deleted", [True, False])
def test_rebuild_with_reidentify_keeps_one_object_with_the_humans_face(restore_deleted):
    """认回同一个对象：删掉又新建的同名同类型对象接过旧编号。恢复不了删除的应用也一样，因为没有东西要恢复。"""
    app, s = _tree(restore_deleted=restore_deleted, reidentify=True)
    ids = {o["name"]: cid for cid, o in app.objects.items()}
    rep = s.run_agent(REBUILD, "清空重建")
    assert rep.outcome == "committed" and not rep.breach
    assert {o["name"]: cid for cid, o in app.objects.items()} == ids          # 编号都没变
    leaf3 = _faces(app, "Leaf_3")
    assert len(leaf3) == 1 and leaf3[0]["location"] == [0, 3, 2.2]           # 人的位置
    assert leaf3[0]["color"] == "橙" and leaf3[0]["parent"] == ids["Trunk"]   # AI 的颜色；父对象还是树干
    assert _faces(app, "Trunk")[0]["scale"] == 1.3
    assert set(rep.reidentified) == {"Trunk", "Leaf_1", "Leaf_2", "Leaf_3"}
    assert "按同一个对象处理" in rep.text and "人改过" in rep.text
    assert check_session(s, rep) == []


def test_reidentify_needs_same_type_and_does_not_bring_back_what_was_not_recreated():
    app, s = _tree(restore_deleted=False, reidentify=True)
    rep = s.run_agent("scene.delete('Leaf_3')\nscene.delete('Leaf_2')\n"
                      "scene.create('Leaf_2', {'location': [0, 0, 0]}, kind='LIGHT')", "换类型")
    assert rep.reidentified == []                                             # 类型不同：不是同一个对象
    assert rep.outcome == "breach"                                            # Leaf_3 删了没新建，又恢复不了：照实报告
    assert check_session(s, rep) == []


@pytest.mark.parametrize("habit", sf.HABITS)
def test_every_habit_keeps_the_human_edit_with_reidentify(habit):
    """实验 H 的场景 A，四种写法都开着认回同一个对象：人的修改都保住，三条不变量都成立。"""
    import argparse
    from experiments.exp_h_habits import Run
    args = argparse.Namespace(backend="fake", granularity="aspect", max_batch=25, verbose=False,
                              host="", port=0, reidentify=True)
    run = Run(args, "A", habit)
    run.go()
    m = run.metrics()
    assert m["ev"].human_overwritten == [] and m["breach"] == 0 and not m["dups"]
    assert run.inv.violations == [], summarize(run.inv.violations)


# ---------------------------------------------------------------------------
# 真的 Blender（需要 bpy）
# ---------------------------------------------------------------------------
def _blender_rebuild(reidentify):
    pytest.importorskip("bpy")
    import argparse
    from experiments.exp_h_habits import Run
    args = argparse.Namespace(backend="inprocess", granularity="aspect", max_batch=25, verbose=False,
                              host="", port=0, reidentify=reidentify)
    run = Run(args, "A", "rebuild")
    run.go()
    return run, run.metrics()


def test_blender_rebuild_without_reidentify_is_a_truthful_breach():
    """不认回（对照）：恢复出来的 Leaf_3 和它的祖先 Trunk 名字被 AI 新建的占了，只能叫 .001：名字变了，如实记为违规。"""
    run, m = _blender_rebuild(False)
    assert m["dups"] == {"Leaf_3": 2, "Trunk": 2} and m["breach"] == 2
    assert m["data_dups"] == {"Leaf_3_mesh（Mesh）": 2, "Trunk_mesh（Mesh）": 2}   # 各用自己的网格；材质没有分叉
    assert run.inv.violations == [], summarize(run.inv.violations)


def test_blender_rebuild_with_reidentify_keeps_one_leaf_on_the_trunk():
    bpy = pytest.importorskip("bpy")
    run, m = _blender_rebuild(True)
    assert not m["dups"] and not m["data_dups"] and m["breach"] == 0 and m["ev"].human_overwritten == []
    assert run.inv.violations == [], summarize(run.inv.violations)
    leaf3 = bpy.context.scene.objects["Leaf_3"]
    assert leaf3.parent is not None and leaf3.parent.name == "Trunk"
    assert abs(leaf3.location.z - 2.2) < 1e-4                                 # 人挪到的高度
    assert leaf3.material_slots[0].material.diffuse_color[0] > 0.7            # AI 的秋天颜色
