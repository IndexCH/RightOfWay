"""认回同一个对象（design_v0.5.md 12.4–12.6，按 prior_art_solutions.md 第 2 项加固）。

- 配对规则（rightofway/identity.py）：同一个父对象下从上往下，先比完整名字、再比去掉重名后缀的名字，类型相同，
  一对一，有歧义就不配；重名后缀由连接时的自测学出；运行时自己重算一遍核对接入代码的配对。
- 墓碑：人删掉、Agent 还没看到的对象，Agent 新建同一个的，删掉，告诉它。
- 重新绑定：Agent 之前想删、因为人改过而保留下来的对象，后来又新建了同一个的（清空和重建分两次执行），认成一个。
- 祖先不许删：要保持的对象的父对象被删了就放回去，面照样可以改。
- 编号不能转交的应用（Unity）：新对象保留自己的编号，会话记一条"应用编号 → 运行时编号"。

用内存里的假应用；最后几条在真的 Blender 里再跑一遍（需要 bpy）。
"""
import pytest

from rightofway.blender.shared_session import SharedSession
from rightofway.fakeapp import FakeApp, FakeBridge
from rightofway.identity import match_identities, strip_suffix, suffix_pattern, verify
from rightofway.invariants import check_session
from rightofway.runtime import Runtime
from experiments import scenario_fake as sf


def _row(name, type_="MESH", parent=None):
    return {"name": name, "type": type_, "parent": parent}


# ---------------------------------------------------------------------------
# 规则本身
# ---------------------------------------------------------------------------
def test_suffix_is_learned_from_two_names():
    blender = suffix_pattern("Probe", "Probe.001")
    assert strip_suffix("Leaf_3.004", blender) == "Leaf_3" and strip_suffix("Leaf_3", blender) == "Leaf_3"
    assert strip_suffix("Version 2.5", blender) == "Version 2"                 # 只去掉学出来的那种后缀
    editor = suffix_pattern("Probe", "Probe (1)")                                # 另一种应用的写法，同样学得出来
    assert strip_suffix("Leaf (12)", editor) == "Leaf" and strip_suffix("Leaf.001", editor) == "Leaf.001"
    assert suffix_pattern("Probe", "Probe") is None                              # 允许重名（Unity）：没有后缀


def test_matching_goes_top_down_inside_the_parent():
    p = suffix_pattern("X", "X.001")
    gone = {"t": _row("Trunk"), "l1": _row("Leaf", parent="t"), "o": _row("Leaf", parent="other")}
    fresh = {"T": _row("Trunk"), "L1": _row("Leaf.001", parent="T"), "O": _row("Leaf", parent="other")}
    got = match_identities(gone, fresh, p)
    assert got == {"t": ["T", "exact"], "l1": ["L1", "suffix"], "o": ["O", "exact"]}
    assert "l1" not in match_identities(gone, fresh, None)                     # 不知道后缀：名字不同，配不上


def test_ambiguity_and_type_change_are_not_matched():
    two = {"a": _row("Leaf", parent="t"), "b": _row("Leaf", parent="t")}
    assert match_identities(two, {"X": _row("Leaf", parent="t")}) == {}       # 两个旧的、一个新的：不猜
    assert match_identities({"a": _row("Leaf")}, {"X": _row("Leaf"), "Y": _row("Leaf")}) == {}
    assert match_identities({"a": _row("Sun", "LIGHT")}, {"X": _row("Sun", "MESH")}) == {}


def test_verify_recomputes_and_catches_a_wrong_pair():
    before = {"t": {**_row("Trunk")}, "l": {**_row("Leaf", parent="t")}}
    raw = {"t": {**_row("Trunk")}, "l": {**_row("Leaf", parent="t")}}
    fresh = {"T": _row("Trunk"), "L": _row("Leaf", parent="T")}
    good = {"t": ["T", "exact"], "l": ["L", "exact"]}
    assert verify(before, raw, good, fresh)[1] == []
    _, errors = verify(before, raw, {"t": ["T", "exact"], "l": ["L", "global"]}, fresh)
    assert errors and "l" in errors[0]


# ---------------------------------------------------------------------------
# 连接时的自测
# ---------------------------------------------------------------------------
def test_probe_learns_the_suffix_and_what_the_integration_can_do():
    rt = Runtime()
    s = SharedSession(rt, FakeBridge(FakeApp(unique_names=True)))
    s.start()
    assert rt.app["suffix"] and rt.identity_rule()["suffix"] == rt.app["suffix"]
    rt2 = Runtime()
    SharedSession(rt2, FakeBridge(FakeApp(transfer_ids=False))).start()
    assert rt2.app["suffix"] is None and rt2.app["transfer_ids"] is False and rt2.identity_rule() is not None
    rt3 = Runtime(reidentify=False)
    SharedSession(rt3, FakeBridge(FakeApp())).start()
    assert rt3.identity_rule() is None                                          # 关掉认回：许可单里没有规则
    # 能不能恢复是测出来的：恢复不了删除的应用，自测里删掉的对象回不来
    assert rt.app["restore_deleted"] is True and rt2.app["restore_deleted"] is True
    assert FakeApp(restore_deleted=False).probe({})["restore_deleted"] is False


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------
def _scene(**kw):
    """AI 布置好树、石头、太阳；人随后改了点东西。"""
    app = FakeApp(**kw)
    s = SharedSession(Runtime(), FakeBridge(app))
    s.start()
    s.run_agent(sf.ai_build(), "build")
    return app, s


def _named(app, name):
    return [(cid, o) for cid, o in app.objects.items() if o["name"] == name]


REBUILD = sf._code(sf._REBUILD, plan=sf.PLAN_ADJUST)
CLEAR = "for n in list(scene.names()):\n    scene.delete(n)\n"
BUILD_ONLY = sf._code(sf._REBUILD.replace(CLEAR, ""), plan=sf.PLAN_ADJUST)


@pytest.mark.parametrize("kw", [dict(unique_names=True), dict(restore_deleted=False, transfer_ids=False)])
def test_recreating_what_the_human_deleted_is_undone_until_the_agent_has_seen_it(kw):
    app, s = _scene(**kw)
    app.delete("Rock_2")                                                        # Agent 没看到
    s.poll()
    rep = s.run_agent(REBUILD, "清空重建")
    assert rep.recreate_blocked == ["Rock_2"] and not _named(app, "Rock_2")
    assert "没有补回来" in rep.text and rep.outcome == "committed"
    assert check_session(s, rep) == []
    again = s.run_agent(REBUILD, "又一次清空重建")                               # 已经被告知：再新建算有意的
    assert again.recreate_blocked == [] and len(_named(app, "Rock_2")) == 1
    assert check_session(s, again) == []


def test_no_tombstone_for_an_object_the_agent_never_saw():
    app, s = _scene(unique_names=True)
    app.add("Bird", {"location": [0, 0, 3]}, kind="MESH")
    s.poll()
    app.delete("Bird")                                                          # 人新建又删掉，Agent 从没见过
    s.poll()
    rep = s.run_agent("scene.create('Bird', {'location': [1, 1, 1]}, kind='MESH')", "新建")
    assert rep.recreate_blocked == [] and len(_named(app, "Bird")) == 1


@pytest.mark.parametrize("kw", [dict(unique_names=True), dict(transfer_ids=False)])
def test_clear_and_rebuild_in_two_runs_is_bound_to_the_kept_object(kw):
    """清空和重建分两次执行：第一次删不掉的（人改过的 Leaf_3、它的祖先 Trunk）保留下来；
    第二次新建的同名对象认成它们，不出现两份，人挪的位置还在，名字也没有 .001。"""
    app, s = _scene(**kw)
    sf.human_e(app)                                                             # 人把 Leaf_3 往上挪
    s.poll()
    r1 = s.run_agent(CLEAR, "清空")
    assert r1.outcome == "committed" and "下面有人改过的对象" in r1.text and "不能删除" in r1.text
    assert {o["name"] for o in app.objects.values() if not o.get("data")} == {"Trunk", "Leaf_3"}
    assert check_session(s, r1) == []
    r2 = s.run_agent(BUILD_ONLY, "重建")
    assert sorted(r2.rebound) == ["Leaf_3", "Trunk"] and r2.outcome == "committed"
    assert [len(_named(app, n)) for n in ("Trunk", "Leaf_3", "Leaf_1")] == [1, 1, 1]
    (_, leaf3), (trunk_id, _) = _named(app, "Leaf_3")[0], _named(app, "Trunk")[0]
    assert leaf3["faces"]["location"][2] == 2.2 and leaf3["faces"]["parent"] == trunk_id
    assert "你想删" in r2.text
    assert check_session(s, r2) == []


def test_typed_delete_and_create_in_one_batch_become_one_object():
    """Unity 那样允许重名：删除被拒（人改过）、紧接着新建同名的，认成同一个，不出现两个 Leaf_3。"""
    app, s = _scene(restore_deleted=False, transfer_ids=False)
    sf.human_e(app)
    s.poll()
    cmds = [{"action": "delete", "target": "Leaf_3"},
            {"action": "create", "name": "Leaf_3", "kind": "MESH", "parent": "Trunk", "refs": {"material": "LeafMat"},
             "set": {"location": [0, 0, 1.6], "rotation": [0, 0, 0], "scale": 2.0}}]
    rep = s.run_commands(cmds, "删了重建")[0]
    assert rep.rebound == ["Leaf_3"] and rep.outcome == "committed"
    (_, leaf3), = _named(app, "Leaf_3")
    assert leaf3["faces"]["location"][2] == 2.2 and leaf3["faces"]["scale"] == 2.0   # 人的位置，AI 的缩放
    assert s.aliases                                                            # 新对象保留自己的编号，会话记下映射
    assert check_session(s, rep) == []


def test_suffix_match_gets_its_name_back():
    """Blender 那样不许重名：AI 先新建 Leaf_3（得到 Leaf_3.001），再删旧的。去掉后缀后同名，认回，名字还给它。"""
    app, s = _scene(unique_names=True)
    sf.human_e(app)
    s.poll()
    code = ("scene.create('Leaf_3', {'location': [0, 0, 1.6], 'rotation': [0, 0, 0], 'scale': 1.5}, parent='Trunk',"
            " kind='MESH', refs={'material': 'LeafMat'})\nscene.delete('Leaf_3')\n")
    rep = s.run_agent(code, "先建后删")
    assert rep.reidentified == ["Leaf_3"] and rep.identity_levels["Leaf_3"] == "去掉重名后缀后同名"
    (_, leaf3), = _named(app, "Leaf_3")
    assert leaf3["faces"]["location"][2] == 2.2 and leaf3["faces"]["scale"] == 1.5
    assert check_session(s, rep) == []


def test_ambiguous_siblings_are_not_guessed():
    """两个同名同类型的兄弟（Unity 允许），AI 都删了又新建两个：配不上就不配，人改过的那个照常保护。"""
    app = FakeApp()
    app.add("Trunk", {"location": [0, 0, 1]}, kind="MESH")
    for z in (1.0, 2.0):
        app.add("Leaf", {"location": [0, 0, z]}, kind="MESH", parent="Trunk")
    s = SharedSession(Runtime(), FakeBridge(app))
    s.start()
    leaf = next(cid for cid, o in app.objects.items() if o["faces"].get("location") == [0, 0, 2.0])
    app.objects[leaf]["faces"]["location"] = [0, 0, 5.0]                     # 人改了第二片
    s.poll()
    rep = s.run_agent("for n in [n for n in scene.names() if n == 'Leaf']:\n    scene.delete(n)\n"
                      "for z in (1.5, 2.5):\n    scene.create('Leaf', {'location': [0, 0, z]}, parent='Trunk', kind='MESH')\n",
                      "重建两片")
    assert rep.reidentified == [] and rep.identity_errors == []
    assert app.objects[leaf]["faces"]["location"] == [0, 0, 5.0]             # 人改过的那片被恢复
    assert check_session(s, rep) == []


def test_typed_delete_of_an_ancestor_is_refused():
    app, s = _scene(restore_deleted=False, transfer_ids=False)
    sf.human_e(app)
    s.poll()
    rep = s.run_commands([{"action": "delete", "target": "Trunk"}], "删树干")[0]
    assert rep.commands[0]["status"] == "refused" and "下面有人改过的对象" in rep.text
    assert _named(app, "Trunk") and check_session(s, rep) == []


def test_aliases_keep_later_human_edits_on_the_same_ledger_unit():
    """编号不能转交的应用：重建之后人再改 Leaf_3，记在原来那个单元上（人碰过的标记没断）。"""
    app, s = _scene(restore_deleted=False, transfer_ids=False)
    sf.human_e(app)
    s.poll()
    ledger_id = s.find("Leaf_3")
    s.run_agent(REBUILD, "清空重建")
    assert s.find("Leaf_3") == ledger_id and any(v == ledger_id for v in s.aliases.values())
    app.set("Leaf_3", "scale", 3.0)
    s.poll()
    assert s.rt.objects[f"{ledger_id}#scale"].versions[-1].author_id == s.human
    rep = s.run_agent(REBUILD, "再重建一次")
    (_, leaf3), = _named(app, "Leaf_3")
    assert leaf3["faces"]["scale"] == 3.0 and leaf3["faces"]["location"][2] == 2.2
    assert check_session(s, rep) == []


class _WrongApp(FakeApp):
    """接入代码出错：把两片叶子配反了。"""

    def _identity(self, before, args):
        out, tombs = super()._identity(before, args)
        p = out["pairs"]
        leaves = sorted(k for k in p if before.get(k, {}).get("name", "").startswith("Leaf_"))
        if len(leaves) >= 2:                            # 只在报告里配反（应用里已经按正确的做了），运行时照样能发现
            a, b = leaves[:2]
            p[a], p[b] = p[b], p[a]
        return out, tombs


def test_runtime_catches_an_integration_that_pairs_wrongly():
    app = _WrongApp(unique_names=True)
    s = SharedSession(Runtime(), FakeBridge(app))
    s.start()
    s.run_agent(sf.ai_build(), "build")
    rep = s.run_agent(REBUILD, "清空重建")
    assert rep.identity_errors and "接入代码认回同一个对象的结果和运行时的规则不一致" in rep.alert
    assert len(rep.reidentified) >= 8                                         # 其余配对核对通过
    assert any(e.type == "alert/human" and e.data.get("kind") == "identity" for e in s.rt.events)


# ---------------------------------------------------------------------------
# 真的 Blender（需要 bpy）
# ---------------------------------------------------------------------------
def _blender_session():
    bpy = pytest.importorskip("bpy")
    from rightofway.blender.bridge import InProcessBridge
    from experiments import scenario_tree as sc
    InProcessBridge.reset()
    br = InProcessBridge()
    s = SharedSession(Runtime(), br)
    s.start()
    s.run_agent(sc.ai_build(), "build")
    return bpy, br, s, sc


def test_blender_probe_learns_the_dot_suffix_and_measures_what_it_can_undo():
    bpy, br, s, sc = _blender_session()
    names = s.capabilities["names"]
    assert names[1] == names[0] + ".001" and strip_suffix("Leaf_3.002", s.capabilities["suffix"]) == "Leaf_3"
    assert {k: s.capabilities[k] for k in ("restore_face", "restore_deleted", "around", "measured")} == {
        "restore_face": True, "restore_deleted": True, "around": True, "measured": True}
    assert not any(x.name.startswith("RightOfWayProbe")                       # 临时场景和里面的东西都删掉了
                   for coll in (bpy.data.objects, bpy.data.meshes, bpy.data.scenes) for x in coll)


def test_blender_rebuild_keeps_mesh_identity_and_leaves_no_orphans():
    """清空重建：对象和网格都认回，网格名字没有 .001，旧网格不留在文件里；人删掉的 Rock_2 没有被补回来。"""
    bpy, br, s, sc = _blender_session()
    br.execute(sc.human_edit("delete", "Rock_2"))
    br.execute(sc.human_edit("move", "Leaf_3"))
    s.poll()
    rep = s.run_agent(sc.ai_rebuild(), "清空重建")
    assert rep.outcome == "committed" and rep.recreate_blocked == ["Rock_2"] and rep.identity_errors == []
    assert "Leaf_3_mesh（Mesh）" in rep.reidentified
    assert "Rock_2" not in bpy.data.objects and "Rock_2_mesh.001" not in bpy.data.meshes
    assert not any("." in m.name for m in bpy.data.meshes)                    # AI 删掉对象后留下的旧网格不在了，新网格叫原来的名字
    assert abs(bpy.data.objects["Leaf_3"].location.z - 1.9) < 1e-4            # 人挪的高度（1.4 + 0.5）
    assert check_session(s, rep) == []


def test_blender_clear_then_build_is_bound_to_the_kept_leaf():
    bpy, br, s, sc = _blender_session()
    br.execute(sc.human_edit("move", "Leaf_3"))
    s.poll()
    r1 = s.run_agent("import bpy\nfor o in list(bpy.context.scene.objects):\n    bpy.data.objects.remove(o)\n", "清空")
    assert sorted(o.name for o in bpy.context.scene.objects) == ["Leaf_3", "Trunk"] and r1.outcome == "committed"
    assert check_session(s, r1) == []
    r2 = s.run_agent(sc.ai_build(), "只建")                                   # 第二次只新建，不先删
    assert sorted(r2.rebound) == ["Leaf_3", "Trunk"] and r2.outcome == "committed"
    names = sorted(o.name for o in bpy.context.scene.objects)
    assert names.count("Leaf_3") == 1 and not any("." in n for n in names)
    assert abs(bpy.data.objects["Leaf_3"].location.z - 1.9) < 1e-4
    assert bpy.data.objects["Leaf_3"].parent == bpy.data.objects["Trunk"]
    assert check_session(s, r2) == []
