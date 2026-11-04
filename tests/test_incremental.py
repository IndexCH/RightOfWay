"""增量观察的差分测试（design_v0.5.md 第 16 节）：随机做人的各种修改，每一步都拿增量扫描的结果（只传变化、
按代号对齐）和全量扫描比，每个对象、每个面的指纹都要一样。还有：撤销、回调丢失时退回全量；增量漏掉的由
全量核对如实报告；会话层面增量和全量记的账一样；AI 执行前后不变量照样成立。

需要 bpy（Python 3.11 + pip install bpy），否则整个文件跳过。
"""
import random
import sys

import pytest

bpy = pytest.importorskip("bpy")
bmesh = pytest.importorskip("bmesh")

from rightofway.blender.bridge import InProcessBridge  # noqa: E402
from rightofway.blender.shared_session import SharedSession  # noqa: E402
from rightofway.invariants import check_ledger, check_run, summarize  # noqa: E402
from rightofway.runtime import Runtime  # noqa: E402

ARGS = {"data_units": True, "prefix": "h-"}


@pytest.fixture
def br():
    InProcessBridge.reset()
    return InProcessBridge()


def _strip(rec):
    return {k: v for k, v in rec.items() if k != "values"}


class Mirror:
    """照着接入代码交回的变化维护一份场景（和会话做的一样），随时可以和全量扫描比。"""

    def __init__(self, br, scene=None):
        self.br = br
        self.args = dict(ARGS, **({"scene": scene} if scene else {}))
        r = br.call("poll", dict(self.args, delta=True, since=None))
        assert r["full"]
        self.view = {k: _strip(v) for k, v in r["records"].items()}
        self.gen = r["gen"]

    def step(self, **kw):
        r = self.br.call("poll", dict(self.args, delta=True, since=self.gen, **kw))
        if r.get("full"):
            self.view = {k: _strip(v) for k, v in r["records"].items()}
        else:
            for k, v in r["changed"].items():
                self.view[k] = _strip(v)
            for k in r["deleted"]:
                self.view.pop(k, None)
        sel = set(r["sel"])
        for k, v in self.view.items():
            if v.get("kind") != "data":
                v["selected"] = k in sel
        self.gen = r["gen"]
        return r

    def check(self, label):
        full = {k: _strip(v) for k, v in self.br.call("poll", self.args)["records"].items()}
        assert set(self.view) == set(full), f"{label}: 编号不一样 {sorted(set(self.view) ^ set(full))}"
        for k, b in full.items():
            a = self.view[k]
            if a != b:
                faces = sorted(f for f in set(a["aspects"]) | set(b["aspects"])
                               if a["aspects"].get(f) != b["aspects"].get(f))
                fields = sorted(x for x in set(a) | set(b) if x != "aspects" and a.get(x) != b.get(x))
                raise AssertionError(f"{label}：{b['name']} 和全量不一样，面 {faces}，字段 {fields}")


def _sphere(name, segments=8):
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=max(3, segments // 2), radius=0.5)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    return me


def _material(name, rgba):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = rgba
    return m


def _build(n=8):
    sc = bpy.context.scene
    mats = [_material(f"Mat_{i}", (i / 3, 0.5, 0.2, 1)) for i in range(3)]
    for i in range(n):
        o = bpy.data.objects.new(f"Obj_{i}", _sphere(f"Mesh_{i}"))
        o.data.materials.append(mats[i % 3])
        o.location = (i, 0, 0)
        sc.collection.objects.link(o)
    bpy.data.objects["Obj_1"].modifiers.new("Sub", "SUBSURF")
    bpy.data.objects["Obj_2"].parent = bpy.data.objects["Obj_1"]
    e = bpy.data.objects.new("Empty", None)
    sc.collection.objects.link(e)
    lamp = bpy.data.objects.new("Lamp", bpy.data.lights.new("Light", "POINT"))
    sc.collection.objects.link(lamp)
    c = bpy.data.collections.new("Group")
    sc.collection.children.link(c)
    return mats


def _meshes():
    return [o for o in bpy.context.scene.objects if o.type == "MESH"]


# 人的各种修改（随机挑）。每个都只用 Blender 的普通接口，和人在界面里、或者跑一段脚本一样
def e_transform(r):
    o = r.choice(list(bpy.context.scene.objects))
    getattr(o, r.choice(["location", "rotation_euler", "scale"]))[r.randrange(3)] += r.choice([0.25, -0.5, 1.0])


def e_material(r):
    m = r.choice([m for m in bpy.data.materials if m.users])
    kind = r.randrange(3)
    if kind == 0:
        m.diffuse_color = (r.random(), r.random(), r.random(), 1)
    elif kind == 1:
        m.roughness = r.random()
    else:
        m.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = r.random()


def e_mesh(r):
    me = r.choice(_meshes()).data
    kind = r.randrange(4)
    if kind == 0:
        me.vertices[r.randrange(len(me.vertices))].co.z += 0.1
    elif kind == 1:
        co = [0.0] * (3 * len(me.vertices))
        me.vertices.foreach_get("co", co)
        me.vertices.foreach_set("co", [x * 1.01 for x in co])
        me.update()
    elif kind == 2:                                   # 直接写边的顶点：depsgraph 不通知（自测测得出来）
        e = me.edges[r.randrange(len(me.edges))]
        e.vertices = (e.vertices[1], e.vertices[0])
    else:
        bm = bmesh.new()
        bm.from_mesh(me)
        bmesh.ops.translate(bm, verts=bm.verts[:3], vec=(0, 0, 0.2))
        bm.to_mesh(me)
        bm.free()


def e_modifier(r):
    o = r.choice(_meshes())
    if o.modifiers and r.random() < 0.4:
        mod = o.modifiers[0]
        if mod.type == "SUBSURF":
            mod.levels = (mod.levels + 1) % 3
        else:
            o.modifiers.remove(mod)
    elif r.random() < 0.5:
        o.modifiers.new("Bevel", "BEVEL").width = r.random() * 0.1
    elif o.modifiers:
        o.modifiers[0].show_in_editmode = not o.modifiers[0].show_in_editmode


def e_custom(r):
    """自定义属性：数据块上的、材质节点上的（嵌套的）。写自定义属性 depsgraph 不通知，自测测得出来。"""
    m = r.choice(list(bpy.data.materials))
    target = r.choice([r.choice(_meshes()).data, m, m.node_tree.nodes["Principled BSDF"]])
    if r.random() < 0.7:
        target["note"] = r.randrange(1000)
    elif "note" in target:
        del target["note"]


def e_rename(r):
    kind = r.randrange(4)
    if kind == 0:
        o = r.choice(list(bpy.context.scene.objects))
        o.name = o.name.split("~")[0] + f"~{r.randrange(100)}"
    elif kind == 1:
        me = r.choice(_meshes()).data
        me.name = me.name.split("~")[0] + f"~{r.randrange(100)}"
    elif kind == 2:
        m = r.choice(list(bpy.data.materials))
        m.name = m.name.split("~")[0] + f"~{r.randrange(100)}"
    else:
        c = bpy.data.collections.get("Group") or next(iter(bpy.data.collections), None)
        if c is not None:
            c.name = "Group~" + str(r.randrange(100))


def e_collection(r):
    o = r.choice(list(bpy.context.scene.objects))
    c = next(iter(bpy.data.collections), None)
    if c is None:
        return
    if o.name in c.objects:
        if len(o.users_collection) > 1:
            c.objects.unlink(o)
    else:
        c.objects.link(o)


def e_delete(r):
    objs = [o for o in bpy.context.scene.objects if o.name.startswith(("Obj_", "New_"))]
    if len(objs) > 4:
        bpy.data.objects.remove(r.choice(objs))


def e_create(r):
    sc = bpy.context.scene
    if r.random() < 0.5:
        o = bpy.data.objects.new(f"New_{r.randrange(1000)}", _sphere(f"NewMesh_{r.randrange(1000)}", 6))
        o.data.materials.append(r.choice(list(bpy.data.materials)))
        sc.collection.objects.link(o)
    else:                                             # 复制（Shift+D）：编号跟着复制过去，要认出它是新的
        src = r.choice(_meshes())
        dup = src.copy()
        dup.data = src.data.copy()
        sc.collection.objects.link(dup)


def e_hide(r):
    o = r.choice(_meshes())
    if r.random() < 0.5:
        o.hide_viewport = not o.hide_viewport         # 隐藏的对象不在 depsgraph 里：每次重算
    else:
        o.lock_rotation_w = not o.lock_rotation_w     # 没有更新回调的属性：自测测得出来


def e_select(r):
    o = r.choice(list(bpy.context.scene.objects))
    o.select_set(not o.select_get())


EDITS = [e_transform, e_material, e_mesh, e_modifier, e_custom, e_rename, e_collection, e_delete, e_create,
         e_hide, e_select]


def _handler():
    st = sys.modules["_rightofway_state"]
    return st.obs_handlers[0]


def _undo_behind_the_back(r):
    """撤销：Blender 把数据整个换回去，depsgraph 的回调不一定报。这里模拟最坏的情况——改动完全没报，
    只有 undo_post 回调——缓存应该作废、全量重算。"""
    h = _handler()
    bpy.app.handlers.depsgraph_update_post.remove(h)
    try:
        e_transform(r)
        e_material(r)
        bpy.context.view_layer.update()               # 更新照常发生，但回调不在，没人记下
    finally:
        bpy.app.handlers.depsgraph_update_post.append(h)
    for f in list(bpy.app.handlers.undo_post):
        f(bpy.context.scene, None)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_incremental_equals_full_under_random_human_edits(br, seed):
    _build()
    br.call("probe", dict(ARGS, delta=True))         # 连接时的自测（测出哪些改动 depsgraph 不通知）
    m = Mirror(br)
    m.check("开始")
    r = random.Random(seed)
    kinds = set()
    for step in range(60):
        for _ in range(r.randint(1, 3)):
            edit = r.choice(EDITS)
            kinds.add(edit.__name__)
            edit(r)
        if step % 15 == 7:
            _undo_behind_the_back(r)
            kinds.add("undo")
        out = m.step()
        assert not out.get("missed"), out.get("missed")
        m.check(f"第 {step} 步")
    assert len(kinds) == len(EDITS) + 1


def test_after_exec_only_dirty_objects_recomputed(br):
    """AI 改了很多对象：每个只算一次；改一个对象时，别的对象不重算。"""
    _build(12)
    br.call("probe", dict(ARGS, delta=True))
    m = Mirror(br)
    ns = sys.modules["_rightofway_code"].ns
    calls = []
    orig = ns["_rec_obj"]
    ns["_rec_obj"] = lambda o, cache, *a: calls.append(o.name) or orig(o, cache, *a)
    try:
        bpy.data.objects["Obj_3"].location.x += 1
        m.step()
        assert calls == ["Obj_3"]
        calls.clear()
        for o in _meshes():
            o.location.z += 0.1
        m.step()
        assert sorted(calls) == sorted(o.name for o in _meshes())
    finally:
        ns["_rec_obj"] = orig
    m.check("改很多个之后")


def test_missed_change_is_found_by_full_check(br):
    """增量漏掉的（这里人为去掉自测测出的静默路径来模拟）：全量核对如实报告，会话记为来源不明、照样保护。"""
    _build(4)
    rt = Runtime()
    s = SharedSession(rt, br)
    s.start()
    st = sys.modules["_rightofway_state"]
    spec = st.watch["Object"]
    saved = spec["paths"], spec.get("tree"), spec.get("fn")
    spec["paths"] = [q for q in spec["paths"] if q != ("lock_rotation_w",)]    # 假装自测没测出它是静默的
    spec["tree"] = spec["fn"] = None
    try:
        assert not s.poll()                           # 签名按改过的路径重新记一遍
        o = bpy.data.objects["Obj_0"]
        o.lock_rotation_w = not o.lock_rotation_w
        assert not s.poll()                           # 增量看不到
        res = br.call("poll", s._dargs(verify=True))  # 全量核对
        assert res["missed"] == [o["rightofway_id"]]
        d = s._observed(res)
        assert set(d.modified) == {o["rightofway_id"]}
    finally:
        spec["paths"], spec["tree"], spec["fn"] = saved
    authors = {rt.objects[u].versions[-1].author_id for u in rt.objects if u.startswith(o["rightofway_id"] + "#")}
    assert authors == {"external:missed"}
    assert any(u.startswith(o["rightofway_id"] + "#") for u in rt.guarded("agent"))
    assert check_ledger(rt, s.scene_records(), s.granularity, s.labels) == []


def test_handlers_gone_falls_back_to_full(br):
    """回调不在了（例如打开了别的文件）：缓存丢掉、全量重算，会话整份对齐。"""
    _build(4)
    rt = Runtime()
    s = SharedSession(rt, br)
    s.start()
    gen = s.obs_gen
    bpy.app.handlers.depsgraph_update_post.remove(_handler())
    bpy.data.objects["Obj_1"].location.x += 3
    d = s.poll()
    assert set(d.modified) == {bpy.data.objects["Obj_1"]["rightofway_id"]}
    assert s.obs_gen[0] != gen[0]                     # 新的缓存
    assert check_ledger(rt, s.scene_records(), s.granularity, s.labels) == []


def test_session_incremental_and_full_keep_the_same_ledger(br):
    """会话层面：同一个场景，一个会话只传变化，一个整份观察，人随机改，两边记的账一样。"""
    _build(6)
    a = SharedSession(Runtime(), br)
    b = SharedSession(Runtime(), br, incremental=False)
    a.start()
    b.start()
    assert a._incr and not b._incr
    r = random.Random(7)
    for step in range(30):
        r.choice(EDITS)(r)
        da, db = a.poll(), b.poll()
        assert (set(da.created), set(da.deleted), set(da.modified)) == (set(db.created), set(db.deleted),
                                                                       set(db.modified)), step
        assert {k: _strip(v) for k, v in a.view.items()} == {k: _strip(v) for k, v in b.view.items()}
        assert a.rt.guarded("agent") == b.rt.guarded("agent")
        for uid in a.rt.guarded("agent"):
            assert a.rt.content_of(uid) == b.rt.content_of(uid)


def test_agent_runs_keep_invariants_incremental(br):
    """只传变化时执行 AI 的代码：许可单、恢复、记账照旧，三条不变量每一步都成立（对照全量扫描）。"""
    _build(6)
    rt = Runtime()
    s = SharedSession(rt, br)
    s.start()
    r = random.Random(11)
    codes = [
        "import bpy\nfor o in bpy.context.scene.objects:\n    o.location.z += 0.1\n",
        "import bpy\nbpy.data.materials[0].diffuse_color = (0.1, 0.9, 0.1, 1)\n",
        "import bpy\no = bpy.data.objects.get('Obj_2')\nif o: bpy.data.objects.remove(o)\n",
        "import bpy\nme = bpy.data.meshes.new('ai_mesh')\no = bpy.data.objects.new('AI_Rock', me)\n"
        "bpy.context.scene.collection.objects.link(o)\n",
        "import bpy\nfor o in bpy.context.scene.objects:\n    if o.type == 'MESH':\n"
        "        e = o.data.edges[0]\n        e.vertices = (e.vertices[1], e.vertices[0])\n",
    ]
    for step in range(12):
        for _ in range(r.randint(0, 2)):
            r.choice(EDITS)(r)
        if r.random() < 0.5:
            s.poll()
        rep = s.run_agent(r.choice(codes), label=f"s{step}")
        if rep.status != "ok":
            continue
        scene = s.scene_records()                     # 独立的全量扫描
        vs = check_run(rt, rep, s.granularity, scene=scene, labels=s.labels)
        assert vs == [], summarize(vs)
        assert {k: _strip(v)["fp"] for k, v in s.view.items()} == {k: v["fp"] for k, v in scene.items()}, step
