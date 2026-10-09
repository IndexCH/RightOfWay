"""v0.4 新增的部分：
- 两种"上次看到的"分开：AI 错过的变化带具体的值告诉它
- 多个 AI：后到的让先到的，被拒后预留重试
- 选中即占用；AI 的脚本不改人的选中状态
- 方式二改成实时同步（两个独立的 Blender 进程）
- 文件兜底：起点编号写进文件
"""
import shutil
import socket

import pytest

from rightofway.blender.changes import describe_changes
from rightofway.blender.records import unit_id


# ---------------------------------------------------------------------------
# 不需要 Blender 的部分
# ---------------------------------------------------------------------------
def _rec(cid, name, faces):
    return {"id": cid, "name": name, "fp": "|".join(f"{k}={v}" for k, v in sorted(faces.items())),
            "cfp": "", "aspects": faces}


def test_describe_changes_with_values():
    old = {"a": _rec("a", "Leaf_3", {"loc": "1", "mat": "x"}), "b": _rec("b", "Rock_2", {"loc": "1"})}
    new = {"a": _rec("a", "Leaf_3", {"loc": "2", "mat": "y"}), "c": _rec("c", "Cube", {"loc": "1"})}
    ov = {"a": {"loc": "(0, 0, 1.4)", "mat": "1 项：Leaf"}}
    nv = {"a": {"loc": "(0, 0, 2.2)", "mat": "1 项：Leaf"}}
    who = lambda cid, face: "人 " if face in (None, "loc") else "AI "      # noqa: E731
    lines = describe_changes(old, new, ov, nv, {"loc": "位置", "mat": "材质"}, who=who)
    assert "人 新建了 Cube" in lines and "人 删除了 Rock_2" in lines
    assert "人 修改了 Leaf_3：位置 (0, 0, 1.4) → (0, 0, 2.2)" in lines
    assert "AI 修改了 Leaf_3：材质（内容有变化）" in lines          # 文字一样但指纹变了：内容里面变了
    assert describe_changes(old, new, ov, nv, exclude={("a", "loc"), ("a", "mat")}) == ["新建了 Cube", "删除了 Rock_2"]


# ---------------------------------------------------------------------------
# 需要 bpy 的部分
# ---------------------------------------------------------------------------
bpy = pytest.importorskip("bpy")

from rightofway.blender import local_server  # noqa: E402
from rightofway.blender.bridge import InProcessBridge, SocketBridge  # noqa: E402
from rightofway.blender.evaluate import evaluate  # noqa: E402
from rightofway.blender.filemerge import FileMergeSession  # noqa: E402
from rightofway.blender.live_sync import LiveSyncSession  # noqa: E402
from rightofway.blender.shared_session import R_OTHER_AI, R_RESERVED, R_SELECTED, SharedSession  # noqa: E402
from rightofway.runtime import Runtime  # noqa: E402
from experiments import scenario_tree as sc  # noqa: E402


@pytest.fixture
def br():
    InProcessBridge.reset()
    return InProcessBridge()


def by_name(recs, name):
    return next(c for c, r in recs.items() if r["name"] == name)


def test_agent_is_told_what_it_missed_with_values(br):
    rt = Runtime()
    s = SharedSession(rt, br)
    s.start()
    s.run_agent(sc.ai_build(), "build")
    br.execute(sc.human_sim())
    s.poll()                                              # 运行时看到了，但 AI 还没看
    rep = s.run_agent(sc.ai_adjust(), "adjust")
    missed = "\n".join(rep.missed)
    assert "人 删除了 Rock_2" in missed and "人 新建了 Cube" in missed
    assert "人 修改了 Leaf_3：Location (-0.647, 0.47, 1.4) → (-0.647, 0.47, 2.2)" in missed
    assert "你上次看场景之后" in rep.text
    assert "现在是 (-0.647, 0.47, 2.2)，你改成的 (-0.647, 0.47, 1.6) 没有采用" in rep.text


def test_observe_updates_the_agent_view(br):
    rt = Runtime()
    s = SharedSession(rt, br)
    s.start()
    s.run_agent(sc.ai_build(), "build")
    br.execute(sc.human_sim())
    first = s.observe()
    assert "删除了 Rock_2" in first.text and "Rock_2" in {r["name"] for r in first.diff.deleted.values()}
    assert s.observe().lines == []                        # 看过了，就没有新变化
    rep = s.run_agent(sc.ai_adjust(scene=None, skip=["Rock_2"]), "adjust")
    assert rep.missed == [] and rep.resurrections == []   # 读了提示的 AI 不再补回人删掉的石头
    ev = evaluate(rt, s.scene_records(), s.human, granularity="aspect", labels=s.labels)
    assert ev.human_overwritten == [] and ev.ai_lost == []


def test_two_agents_later_one_yields_and_gets_reservation(br):
    rt = Runtime()
    s = SharedSession(rt, br, agent="layout", reserve_seconds=30)
    s.add_agent("look")
    s.start()
    s.run_agent(sc.ai_build(), "build", agent="layout")
    s.observe("look")
    r_layout = s.run_agent(sc.ai_layout(), "layout", agent="layout")
    assert r_layout.skipped == []
    r_look = s.run_agent(sc.ai_look(), "look", agent="look")         # 按旧印象改了树干和 Rock_1
    stale = {x["name"] for x in r_look.skipped if x["reason"] == R_OTHER_AI}
    assert stale == {"Trunk", "Rock_1"} and r_look.reserved
    trunk = by_name(s.view, "Trunk")
    assert s.values[trunk]["scale"] == "(1.3, 1.3, 1.3)"             # 布局 AI 先到，保留它的
    r_follow = s.run_agent(sc.ai_layout_followup(), "follow", agent="layout")
    assert [x["reason"] for x in r_follow.skipped] == [R_RESERVED]    # 材质 AI 正在重试，留给它
    s.observe("look")
    r_retry = s.run_agent(sc.ai_look_retry(), "retry", agent="look")
    assert r_retry.skipped == [] and s.values[trunk]["scale"] == "(1.2, 1.2, 1.2)"
    assert s.reservations == {}
    ev = evaluate(rt, s.scene_records(), s.human, granularity="aspect", labels=s.labels)
    assert ev.human_overwritten == [] and ev.ai_lost == []


def test_occupy_selection_and_selection_is_restored(br):
    rt = Runtime()
    s = SharedSession(rt, br, occupy_selection=True)
    s.start()
    s.run_agent(sc.ai_build(), "build")
    leaf1 = bpy.data.objects["Leaf_1"]
    leaf1.select_set(True)
    rep = s.run_agent(sc._with_helpers(
        'obj("Leaf_1").location.z = 9\nobj("Leaf_2").location.z = 9\n'
        'for o in scene.objects: o.select_set(False)\nobj("Rock_1").select_set(True)', None), "move")
    assert {x["name"] for x in rep.skipped if x["reason"] == R_SELECTED} == {"Leaf_1"}
    leaf1 = bpy.data.objects["Leaf_1"]                                            # 被整个换回过，重新取
    assert leaf1.location.z != 9 and bpy.data.objects["Leaf_2"].location.z == 9
    assert leaf1.select_get() and not bpy.data.objects["Rock_1"].select_get()     # AI 改的选中状态被还原
    leaf1.select_set(False)
    rep = s.run_agent(sc._with_helpers('obj("Leaf_1").location.z = 9', None), "again")
    assert rep.skipped == [] and bpy.data.objects["Leaf_1"].location.z == 9     # 取消选中就解除


# ---------------------------------------------------------------------------
# 方式二：两个独立的 Blender 进程，实时同步
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def two_blenders():
    ports = [_free_port(), _free_port()]
    procs = [local_server.spawn(p) for p in ports]
    yield [SocketBridge("127.0.0.1", p) for p in ports]
    for p, proc in zip(ports, procs):
        local_server.shutdown(p, proc)


def _fresh(bridges):
    for b in bridges:
        b.execute("import bpy\nbpy.ops.wm.read_factory_settings(use_empty=True)")


@pytest.mark.parametrize("inline", [False, True])
def test_live_sync_scenario(two_blenders, inline):
    _fresh(two_blenders)
    hb, ab = two_blenders
    rt = Runtime()
    s = LiveSyncSession(rt, hb, ab, inline=inline)
    s.start()
    s.ai_execute(sc.ai_build())
    r1 = s.sync()
    assert r1.consistent and len(r1.created) == 11
    hb.execute(sc.human_sim())
    s.ai_execute(sc.ai_adjust())                          # AI 的窗口里还是旧的样子
    r2 = s.sync()
    assert r2.consistent, r2.errors
    assert r2.partial["Leaf_3"]["kept"] == ["material_slots"] and r2.partial["Leaf_3"]["restored"] == ["location"]
    assert any(x["name"] == "Rock_2" for x in r2.skipped)                       # 人删掉的，AI 改不了
    assert any("人 删除了 Rock_2" in x for x in r2.to_ai) and any("人 新建了 Cube" in x for x in r2.to_ai)
    hb.execute(sc.human_stale_sim())
    r3 = s.sync()
    assert r3.consistent and any("Rock_1" in x for x in r3.to_ai)
    final = hb.call("poll", {})["records"]
    ai = ab.call("poll", {"prefix": "a-"})["records"]
    assert {c: r["cfp"] for c, r in final.items()} == {c: r["cfp"] for c, r in ai.items()}
    ev = evaluate(rt, final, s.human, granularity="aspect", labels=s.labels)
    assert ev.human_overwritten == [] and ev.ai_lost == []


def test_live_sync_without_protection_overwrites_human(two_blenders):
    _fresh(two_blenders)
    hb, ab = two_blenders
    rt = Runtime()
    s = LiveSyncSession(rt, hb, ab)
    s.start()
    s.ai_execute(sc.ai_build())
    s.sync(protect=False)
    hb.execute(sc.human_sim())
    s.ai_execute(sc.ai_adjust())
    s.sync(protect=False)
    ev = evaluate(rt, hb.call("poll", {})["records"], s.human, granularity="aspect", labels=s.labels)
    assert ev.human_overwritten == ["Leaf_3 的Location"]


# ---------------------------------------------------------------------------
# 文件兜底：起点编号
# ---------------------------------------------------------------------------
def test_file_stamp_distinguishes_deliberate_revert(br, tmp_path):
    """未决问题 7：人打开了第 3 版，又故意把 AI 的修改全改回去。只靠推断会以为人没看到第 3 版；有编号就知道是故意的。"""
    rt = Runtime()
    s = FileMergeSession(rt, br, tmp_path / "scene.blend")
    s.start()
    br.call("edit_file", {"path": str(s.ai_path), "code": sc.ai_build()})
    s.sync()                                                          # 第 1 版
    br.call("edit_file", {"path": str(s.ai_path),
                          "code": sc._with_helpers('obj("Trunk").scale = (1.3, 1.3, 1.3)', None)})
    r2 = s.sync()                                                     # 第 2 版：AI 加粗树干
    assert r2.ai_base_from == "编号"
    shutil.copyfile(s.history[1].path, s.human_path)                  # 人的文件内容回到第 1 版的样子……
    br.call("edit_file", {"path": str(s.human_path),
                          "code": f'import bpy\nbpy.context.scene["rightofway_base"] = "{s.session_id}:2"'})   # ……但它是从第 2 版打开的
    r3 = s.sync()
    assert (r3.human_base, r3.human_base_from) == (2, "编号")
    trunk = by_name(r3.final, "Trunk")
    assert r3.final[trunk]["aspects"]["scale"] == s.history[1].records[trunk]["aspects"]["scale"]   # 按人的来
    assert unit_id(trunk, "scale") in {u for u, o in rt.objects.items() if o.human_touched is not None}
