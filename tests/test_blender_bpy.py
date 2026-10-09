"""在真正的 Blender 代码上跑的测试（无界面）。需要当前 Python 能 import bpy（Python 3.11 + pip install bpy），
否则整个文件跳过。在云端开发环境里用 bpy 4.5 跑过。"""
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

bpy = pytest.importorskip("bpy")

from rightofway.blender.bridge import InProcessBridge, SubprocessBlender  # noqa: E402
from rightofway.blender.evaluate import evaluate  # noqa: E402
from rightofway.blender.filemerge import FileMergeSession  # noqa: E402
from rightofway.blender.shared_session import SharedSession  # noqa: E402
from rightofway.runtime import POLICY_CANDIDATE, Runtime  # noqa: E402
from experiments import exp_a_shared, exp_b_files  # noqa: E402
from experiments import scenario_tree as sc  # noqa: E402


@pytest.fixture
def br():
    InProcessBridge.reset()
    return InProcessBridge()


def poll(br):
    return br.call("poll", {})["records"]


def by_name(records, name):
    return next(r for r in records.values() if r["name"] == name)


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------
def test_fingerprint_stable_and_ignores_selection(br):
    br.execute(sc.ai_build())
    a = poll(br)
    bpy.data.objects["Leaf_1"].select_set(True)
    bpy.data.objects["Rock_1"].hide_set(True)          # 眼睛图标：只影响显示，不算修改
    b = poll(br)
    assert {c: r["fp"] for c, r in b.items()} == {c: r["fp"] for c, r in a.items()}
    assert by_name(b, "Leaf_1")["selected"] and not by_name(a, "Leaf_1")["selected"]   # 选中状态单独报告


def test_fingerprint_sees_transform_mesh_material_modifier(br):
    br.execute(sc.ai_build())
    a = poll(br)
    rock = bpy.data.objects["Rock_1"]
    rock.location.x += 0.1
    b = poll(br)
    assert by_name(b, "Rock_1")["fp"] != by_name(a, "Rock_1")["fp"]
    rock.data.vertices[0].co.z += 0.1
    c = poll(br)
    assert by_name(c, "Rock_1")["fp"] != by_name(b, "Rock_1")["fp"]
    mod = rock.modifiers.new("Bevel", "BEVEL")
    d = poll(br)
    mod.width = 0.3                                     # 修改器参数也算进指纹
    e = poll(br)
    assert len({by_name(x, "Rock_1")["fp"] for x in (c, d, e)}) == 3
    # 共用材质变了：所有用它的石头都变
    bpy.data.materials["RightOfWay_Stone"].node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (1, 0, 0, 1)
    f = poll(br)
    assert by_name(f, "Rock_2")["fp"] != by_name(e, "Rock_2")["fp"]
    assert by_name(f, "Trunk")["fp"] == by_name(e, "Trunk")["fp"]


def test_duplicate_gets_new_id_original_keeps_old(br):
    br.execute(sc.ai_build())
    a = poll(br)
    rock = bpy.data.objects["Rock_1"]
    dup = rock.copy()                                   # 和 Shift+D 一样，会把 rightofway_id 一起复制
    bpy.context.scene.collection.objects.link(dup)
    b = poll(br)
    assert by_name(b, "Rock_1")["id"] == by_name(a, "Rock_1")["id"]
    assert by_name(b, dup.name)["id"] not in a
    assert len(b) == len(a) + 1


# ---------------------------------------------------------------------------
# 方式一：执行时保护
# ---------------------------------------------------------------------------
def test_protected_objects_restored_exactly_and_children_kept(br):
    rt = Runtime()
    s = SharedSession(rt, br, granularity="object")
    s.start()
    s.run_agent(sc.ai_build())
    bpy.data.objects["Trunk"].location.x += 2           # 人挪了树干（叶子是它的子对象）
    s.poll()
    rep = s.run_agent("import bpy\nbpy.data.objects['Trunk'].scale = (3, 3, 3)\n"
                      "bpy.data.objects.remove(bpy.data.objects['Rock_1'])")
    trunk = bpy.data.objects["Trunk"]
    assert tuple(trunk.scale) == (1, 1, 1) and abs(trunk.location.x - 2) < 1e-6
    assert all(bpy.data.objects[f"Leaf_{i}"].parent == trunk for i in range(1, 6))
    assert "Rock_1" not in bpy.data.objects             # Rock_1 没被人碰过，AI 可以删
    assert rep.inexact == [] and [x["name"] for x in rep.skipped] == ["Trunk"]


def test_ai_deleting_protected_object_is_undone(br):
    rt = Runtime()
    s = SharedSession(rt, br)
    s.start()
    s.run_agent(sc.ai_build())
    bpy.data.objects["Rock_3"].location.y = 5
    s.poll()
    rep = s.run_agent("import bpy\nbpy.data.objects.remove(bpy.data.objects['Rock_3'])")
    assert bpy.data.objects["Rock_3"].location.y == 5
    assert rep.inexact == [] and rep.dangling == []


def test_candidate_policy_keeps_ai_version_out_of_scene(br):
    rt = Runtime(human_touched_policy=POLICY_CANDIDATE)
    s = SharedSession(rt, br, granularity="object")
    s.start()
    s.run_agent(sc.ai_build())
    bpy.data.objects["Rock_1"].location.z = 3
    s.poll()
    rep = s.run_agent("import bpy\nbpy.data.objects['Rock_1'].location.z = 0")
    assert bpy.data.objects["Rock_1"].location.z == 3
    cand = bpy.data.collections["RightOfWay_AI候选"]
    assert [o.name for o in cand.objects] == ["Rock_1 [AI候选]"] and cand.hide_viewport
    assert all(r["name"] != "Rock_1 [AI候选]" for r in poll(br).values())
    assert rep.candidates


def test_experiment_a_with_and_without_protection(tmp_path, capsys):
    exp_a_shared.main(["--backend", "inprocess", "--human", "sim", "--no-csv"])
    out = capsys.readouterr().out
    assert "人的修改被覆盖 0 处" in out and "AI 的修改丢失 0 处" in out
    exp_a_shared.main(["--backend", "inprocess", "--human", "sim", "--no-csv", "--no-protocol"])
    out = capsys.readouterr().out
    assert "人的修改被覆盖 2 处（Cube 的Location、Leaf_3 的Location）" in out


# ---------------------------------------------------------------------------
# 方式二：存盘合并
# ---------------------------------------------------------------------------
def test_experiment_b_full(tmp_path, capsys):
    exp_b_files.main(["--blender", "inprocess", "--human", "sim", "--no-csv", "--dir", str(tmp_path / "b")])
    out = capsys.readouterr().out
    assert "合并（按面）：人的修改被覆盖 0 处；AI 的修改丢失 0 处" in out
    assert "Leaf_3（AI 的Material Slots，人的Location）" in out
    assert "合并出错：0 处" in out
    assert ("第 3 次合并时人=第 2 版（编号）、AI=第 1 版（编号）；第 4 次合并时人=第 2 版（编号）、AI=第 3 版（编号）" in out)
    assert "AI 最后存盘：人的修改被覆盖 3 处" in out
    assert "人最后存盘：人的修改被覆盖 0 处；AI 的修改丢失 11 处" in out
    exp_b_files.main(["--blender", "inprocess", "--human", "sim", "--no-csv", "--dir", str(tmp_path / "o"),
                      "--granularity", "object"])
    out = capsys.readouterr().out
    assert "合并（按对象）：人的修改被覆盖 0 处；AI 的修改丢失 0 处" in out and "合并出错：0 处" in out
    assert "人最后存盘：人的修改被覆盖 0 处；AI 的修改丢失 6 处" in out


def test_same_name_created_on_both_sides_keeps_both(br, tmp_path):
    rt = Runtime()
    s = FileMergeSession(rt, br, tmp_path / "scene.blend")
    s.start()
    add_cube = sc._with_helpers('mesh_object("Cube", "cube", 1, (0, 0, 0))', None)
    br.call("edit_file", {"path": str(s.human_path), "code": add_cube})
    br.call("edit_file", {"path": str(s.ai_path), "code": add_cube.replace("(0, 0, 0)", "(5, 0, 0)")})
    rep = s.sync()
    assert rep.errors == []
    names = sorted(r["name"] for r in rep.final.values())
    assert names == ["Cube", "Cube.001"] and len(rep.renamed) == 1


def test_candidate_policy_in_file_merge(br, tmp_path):
    rt = Runtime(human_touched_policy=POLICY_CANDIDATE)
    s = FileMergeSession(rt, br, tmp_path / "scene.blend")
    s.start()
    br.call("edit_file", {"path": str(s.ai_path), "code": sc.ai_build()})
    s.sync()
    br.call("edit_file", {"path": str(s.human_path), "code": "import bpy\nbpy.data.objects['Rock_1'].location.z = 3"})
    br.call("edit_file", {"path": str(s.ai_path), "code": "import bpy\nbpy.data.objects['Rock_1'].location.z = 0"})
    rep = s.sync()
    assert rep.rejected == ["Rock_1"] and rep.candidates == ["Rock_1"] and rep.errors == []
    br.call("dump_file", {"path": str(s.human_path)})
    assert bpy.data.objects["Rock_1"].location.z == 3
    assert "Rock_1 [AI候选]" in bpy.data.objects


def test_subprocess_runner_with_fake_blender(tmp_path):
    """SubprocessBlender 的命令行和输出解析：用一个假的 blender（转给当前 Python 执行）。"""
    fake = tmp_path / "blender"
    fake.write_text(f"#!/bin/sh\nfor last; do :; done\nexec {sys.executable} \"$last\"\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    if os.name == "nt":
        pytest.skip("假 blender 脚本只在类 Unix 系统上可用")
    runner = SubprocessBlender(str(fake), timeout=120)
    path = tmp_path / "x.blend"
    runner.call("new_file", {"path": str(path)})
    runner.call("edit_file", {"path": str(path), "code": sc.ai_build()})
    res = runner.call("dump_file", {"path": str(path)})
    assert len(res["records"]) == 11


# ---------------------------------------------------------------------------
# 按面保护（方式一）
# ---------------------------------------------------------------------------
def _leaf_color(o):
    return tuple(round(x, 2) for x in
                 o.material_slots[0].material.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value)


def test_aspect_human_moves_leaf_ai_recolors_both_kept(br):
    rt = Runtime()
    s = SharedSession(rt, br)
    s.start()
    s.run_agent(sc.ai_build())
    bpy.data.objects["Leaf_3"].location.z = 3.0         # 人挪了叶子
    s.poll()
    rep = s.run_agent(sc.ai_adjust())                   # AI 统一高度 + 改成秋色
    leaf3, leaf1 = bpy.data.objects["Leaf_3"], bpy.data.objects["Leaf_1"]
    assert abs(leaf3.location.z - 3.0) < 1e-6           # 位置保留人的
    assert _leaf_color(leaf3) == (0.8, 0.45, 0.1, 1.0)  # 颜色采用 AI 的
    assert abs(leaf1.location.z - 1.6) < 1e-6
    # 颜色在共用材质上（材质是自己的单元），Leaf_3 这边只改了位置、被整个保留，所以不算"部分生效"
    mat_id = bpy.data.materials["RightOfWay_Leaf"]["rightofway_id"]
    assert rep.partial == {} and any(u.startswith(mat_id + "#") for u in rep.committed)
    assert rep.fallback == [] and rep.inexact == []


def test_object_granularity_reverts_whole_leaf(br):
    """按整个对象：人挪了 Leaf_3，Leaf_3 这个对象整个保留。叶子共用的材质是另一个单元，人没碰过，
    AI 改成秋色照常生效，所有叶子（包括 Leaf_3）一起变，材质不分叉。"""
    rt = Runtime()
    s = SharedSession(rt, br, granularity="object")
    s.start()
    s.run_agent(sc.ai_build())
    bpy.data.objects["Leaf_3"].location.z = 3.0
    s.poll()
    s.run_agent(sc.ai_adjust())
    leaf3 = bpy.data.objects["Leaf_3"]
    assert abs(leaf3.location.z - 3.0) < 1e-6 and _leaf_color(leaf3) == (0.8, 0.45, 0.1, 1.0)
    used = {sl.material.name for o in bpy.data.objects if o.name.startswith("Leaf_") for sl in o.material_slots}
    assert used == {"RightOfWay_Leaf"}


def test_aspect_restore_modifier_and_mesh_keep_ai_parts(br):
    """人加了修改器、改了网格；AI 挪了位置、换了材质。人的两个面恢复，AI 的两个面保留。"""
    rt = Runtime()
    s = SharedSession(rt, br)
    s.start()
    s.run_agent(sc.ai_build())
    rock = bpy.data.objects["Rock_1"]
    m = rock.modifiers.new("Bevel", "BEVEL")
    m.width = 0.2
    rock.data.vertices[0].co.z += 0.3
    s.poll()
    rep = s.run_agent("import bpy\n"
                      "o = bpy.data.objects['Rock_1']\n"
                      "o.location.x = 9\n"
                      "o.modifiers.clear()\n"
                      "o.data.vertices[0].co.z = -5\n"
                      "red = bpy.data.materials.new('Red')\n"
                      "o.material_slots[0].material = red\n")
    rock = bpy.data.objects["Rock_1"]
    assert rock.location.x == 9 and rock.material_slots[0].material.name == "Red"
    assert [(x.type, round(x.width, 3)) for x in rock.modifiers] == [("BEVEL", 0.2)]
    assert rock.data.vertices[0].co.z > 0
    # 网格是自己的单元：人改过的顶点在网格上恢复；Rock_1 这个对象只恢复修改器
    assert rep.partial["Rock_1"]["restored"] == ["modifiers"]
    assert sorted(rep.partial["Rock_1"]["kept"]) == ["location", "material_slots"]
    mesh_id = rock.data["rightofway_id"]
    assert any(x["id"].startswith(mesh_id + "#") for x in rep.skipped)


def test_outside_scene_changes_are_reported(br):
    """AI 的脚本改到了当前场景以外的对象：运行时不追踪，但要报告出来（R18）。"""
    other = bpy.data.scenes.new("Other")
    ob = bpy.data.objects.new("Elsewhere", None)
    other.collection.objects.link(ob)
    rt = Runtime()
    s = SharedSession(rt, br)
    s.start()
    rep = s.run_agent("import bpy\nbpy.data.objects['Elsewhere'].location.x = 5")
    assert rep.outside["modified"] == ["Elsewhere"]
    assert "当前场景以外" in rep.text


def test_old_experiment_scenes_do_not_interfere(br):
    """你遇到的情况：之前实验的场景里有同名对象。先清理旧场景，新实验里名字不再带 .001；
    即使不清理，AI 的脚本也只改当前场景。"""
    for name in ("RightOfWay实验_1", "RightOfWay实验_2"):
        sc_ = bpy.data.scenes.new(name)
        br.execute(sc.ai_build(name))
    res = br.call("cleanup_scenes", {"prefix": "RightOfWay实验_"})
    assert len(res["scenes"]) == 2 and res["objects"] == 22
    assert "Leaf_1" not in bpy.data.objects
    # 不清理的情况
    old = bpy.data.scenes.new("Old")
    br.execute(sc.ai_build("Old"))
    new = bpy.data.scenes.new("New")
    rt = Runtime()
    s = SharedSession(rt, br, scene="New")
    s.start()
    s.run_agent(sc.ai_build("New"))
    rep = s.run_agent(sc.ai_adjust("New"))
    assert tuple(bpy.data.objects["Trunk"].scale) == (1, 1, 1)          # 旧场景的 Trunk 没被动
    # 材质名是全局的，旧场景的叶子和新场景共用 RightOfWay_Leaf。材质是自己的单元，AI 改颜色记在材质上
    # （运行时追踪着它），旧场景的叶子对象本身没变，所以不再算"场景以外的改动"
    mat_id = bpy.data.materials["RightOfWay_Leaf"]["rightofway_id"]
    assert any(u.startswith(mat_id + "#") for u in rep.committed)
    assert rep.outside["modified"] == [] and rep.outside["created"] == [] and rep.outside["deleted"] == []
