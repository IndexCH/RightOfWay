"""两组实验共用的任务：AI 布置一个小场景，人中途删、改、加物体，AI 再按自己之前的计划整体调整。

这里的"AI"是写好的脚本，不是大模型，用来检查规则和实现（证据阶梯第 1 级）。
AI 第二步的脚本是按它第一步之后看到的场景写的，它不知道人中途做了什么，
这正是 CLEO 研究一里"Agent 把人的修改改回去"的情况。
"""
from __future__ import annotations

from typing import Optional

_HELPERS = r'''
import bpy, bmesh, math, re
scene = bpy.data.scenes.get(SCENE_NAME) if SCENE_NAME else bpy.context.scene

def base(name):
    return re.sub(r"\.\d{3}$", "", name)

def obj(name):
    """只在当前场景里按名字找对象（Blender 的对象名是全局的，别的场景里可能有同名对象）。"""
    for o in scene.objects:
        if o.name == name:
            return o
    for o in scene.objects:
        if base(o.name) == name:
            return o
    return None

def material(name, rgba):
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
        try:
            m.use_nodes = True        # Blender 5.x 起材质总是用节点，这个属性已弃用
        except Exception:
            pass
    nt = m.node_tree
    if nt is not None:
        bsdf = nt.nodes.get("Principled BSDF")
        if bsdf is None:
            bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
            out = nt.nodes.get("Material Output") or nt.nodes.new("ShaderNodeOutputMaterial")
            nt.links.new(bsdf.outputs[0], out.inputs[0])
        bsdf.inputs["Base Color"].default_value = rgba
    m.diffuse_color = rgba
    return m

def mesh_object(name, kind, size, location, mat=None, parent=None):
    me = bpy.data.meshes.new(name + "_mesh")
    bm = bmesh.new()
    if kind == "cube":
        bmesh.ops.create_cube(bm, size=size)
    elif kind == "sphere":
        bmesh.ops.create_uvsphere(bm, u_segments=12, v_segments=8, radius=size)
    elif kind == "cylinder":
        bmesh.ops.create_cone(bm, cap_ends=True, segments=12, radius1=size, radius2=size * 0.8, depth=size * 8)
    else:
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=size)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    scene.collection.objects.link(ob)
    ob.location = location
    if mat is not None:
        ob.data.materials.append(mat)
    if parent is not None:
        ob.parent = parent
    return ob
'''

_AI_BUILD = r'''
# AI 第一步：布置场景
# 材质名加 RightOfWay_ 前缀，避免改到你自己项目里同名的材质
bark = material("RightOfWay_Bark", (0.35, 0.2, 0.1, 1))
leaf = material("RightOfWay_Leaf", (0.2, 0.6, 0.2, 1))
stone = material("RightOfWay_Stone", (0.5, 0.5, 0.5, 1))
mesh_object("Ground", "plane", 10, (0, 0, 0))
trunk = mesh_object("Trunk", "cylinder", 0.3, (0, 0, 1.2), bark)
for i in range(5):
    a = i * 2 * math.pi / 5
    mesh_object(f"Leaf_{i + 1}", "sphere", 0.6, (math.cos(a) * 0.8, math.sin(a) * 0.8, 1.4), leaf, parent=trunk)
for i in range(3):
    mesh_object(f"Rock_{i + 1}", "cube", 0.5, (3 + i * 1.2, -2, 0.25), stone)
sun_data = bpy.data.lights.new("Sun", "SUN")
sun_data.energy = 3
sun = bpy.data.objects.new("Sun", sun_data)
scene.collection.objects.link(sun)
sun.location = (4, 4, 8)
print("布置完成：地面、树干、5 片树叶、3 块石头、太阳光")
'''

_AI_ADJUST = r'''
# AI 第二步：按自己之前看到的场景整体调整（它不知道人中途改了什么）
material("RightOfWay_Leaf", (0.8, 0.45, 0.1, 1))     # 秋天：叶子变橙色（所有叶子共用这个材质）
stone = material("RightOfWay_Stone", (0.5, 0.5, 0.5, 1))
obj("Trunk").scale = (1.3, 1.3, 1.3)               # 树干加粗
for i in range(5):                                 # 叶子统一抬高到同一高度
    o = obj(f"Leaf_{i + 1}")
    if o is not None:
        o.location.z = 1.6
for i in range(3):                                 # 三块石头排成一圈；缺了就补上（人明确删掉的不补）
    name = f"Rock_{i + 1}"
    o = obj(name)
    if o is None and name in SKIP:
        continue
    if o is None:
        o = mesh_object(name, "cube", 0.5, (0, 0, 0), stone)
    a = i * 2 * math.pi / 3
    o.location = (math.cos(a) * 3, math.sin(a) * 3, 0.25)
for o in list(scene.objects):                      # 其他散落的物体放到地面上
    if (o.type == "MESH" and o.parent is None and base(o.name) not in ("Ground", "Trunk")
            and not o.name.startswith("Rock_")):
        o.location.z = 0.5
print("调整完成")
'''

_HUMAN_SIM = r'''
# 模拟人的三个修改
bpy.data.objects.remove(obj("Rock_2"), do_unlink=True)               # 1. 删掉一块石头
obj("Leaf_3").location.z += 0.8                                       # 2. 把一片叶子往上挪
me = bpy.data.meshes.new("Cube_mesh")                                 # 3. 新建一个立方体，放在空中
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
bm.to_mesh(me)
bm.free()
cube = bpy.data.objects.new("Cube", me)
scene.collection.objects.link(cube)
cube.location = (-3, 2, 1.5)
vl = scene.view_layers[0]                                             # 4. 最后选中 Leaf_1，准备接着改它
for o in scene.objects:                                               #    （只有打开"选中即占用"时才有影响）
    o.select_set(False, view_layer=vl)
obj("Leaf_1").select_set(True, view_layer=vl)
'''

_HUMAN_STALE_SIM = r'''
# 模拟人没有重新打开文件，在旧的内容上又改了一处，然后保存
obj("Rock_1").location.x -= 1.0
'''

HUMAN_STEPS = [
    "删除 Rock_2（选中后按 X → 删除）",
    "选中 Leaf_3，按 G 再按 Z，把它往上挪一点，点鼠标左键确认",
    "按 Shift+A → 网格 → 立方体，新建一个立方体（Cube），按 G 把它挪到空中任意位置",
]
HUMAN_SELECT_STEP = "最后点一下 Leaf_1 选中它（假装你准备接着改它），保持选中状态"
HUMAN_STALE_STEP = ("不要重新打开文件，直接选中 Rock_1，按 G 把它挪一点，然后 Ctrl+S 保存"
                    "（如果 Blender 提示文件已在外部被修改，选择仍然保存/覆盖）")


def _with_helpers(body: str, scene: Optional[str], skip=(), drop=(), params: Optional[dict] = None) -> str:
    return (f"SCENE_NAME = {scene!r}\nSKIP = {sorted(skip)!r}\nDROP = {sorted(drop)!r}\nP = {dict(params or {})!r}\n"
            + _HELPERS + body)


def ai_build(scene: Optional[str] = None) -> str:
    return _with_helpers(_AI_BUILD, scene)


def ai_adjust(scene: Optional[str] = None, skip=()) -> str:
    """skip：AI 从运行时的提示里知道"人删掉了"的对象名，这些缺了也不补（模拟 AI 读了提示再动手）。"""
    return _with_helpers(_AI_ADJUST, scene, skip)


def human_sim(scene: Optional[str] = None) -> str:
    return _with_helpers(_HUMAN_SIM, scene)


def human_stale_sim(scene: Optional[str] = None) -> str:
    return _with_helpers(_HUMAN_STALE_SIM, scene)


# ---------------------------------------------------------------------------
# 实验 E：AI 删掉人刚改过的对象（v0.5 第 1 步，用不变量检查复现问题）
# ---------------------------------------------------------------------------
_HUMAN_MOVE_LEAF3 = r'''
obj("Leaf_3").location.z += 0.8                                       # 人把 Leaf_3 往上挪
'''

_AI_DELETE_LEAF3 = r'''
# AI 按旧印象：觉得 Leaf_3 多余，删掉；顺手把 Leaf_1 放大一点
o = obj("Leaf_3")
if o is not None:
    bpy.data.objects.remove(o, do_unlink=True)
l1 = obj("Leaf_1")
if l1 is not None:
    l1.scale = (1.2, 1.2, 1.2)
print("删掉了 Leaf_3，放大了 Leaf_1")
'''

HUMAN_STEPS_E = ["选中 Leaf_3，按 G 再按 Z，把它往上挪一点，点鼠标左键确认"]


def human_move_leaf3(scene: Optional[str] = None) -> str:
    return _with_helpers(_HUMAN_MOVE_LEAF3, scene)


def ai_delete_leaf3(scene: Optional[str] = None) -> str:
    return _with_helpers(_AI_DELETE_LEAF3, scene)


# ---------------------------------------------------------------------------
# 实验 D：两个 AI + 一个人（同一份）
#   AI「布局」负责位置和大小，AI「材质」负责颜色和灯光。两个 AI 都会按自己上次看到的场景写死具体数值，
#   这正是真实的大模型写脚本的方式（数值来自它记忆里的场景，而不是执行时现读）。
# ---------------------------------------------------------------------------
_AI_LAYOUT = r'''
# AI「布局」：叶子统一抬高、树干加粗、石头排成一圈（人明确删掉的不补）、散落物体落地
obj("Trunk").scale = (1.3, 1.3, 1.3)
for i in range(5):
    o = obj(f"Leaf_{i + 1}")
    if o is not None:
        o.location.z = 1.6
stone = material("RightOfWay_Stone", (0.5, 0.5, 0.5, 1))
for i in range(3):
    name = f"Rock_{i + 1}"
    o = obj(name)
    if o is None and name in SKIP:
        continue
    if o is None:
        o = mesh_object(name, "cube", 0.5, (0, 0, 0), stone)
    a = i * 2 * math.pi / 3
    o.location = (math.cos(a) * 3, math.sin(a) * 3, 0.25)
for o in list(scene.objects):
    if (o.type == "MESH" and o.parent is None and base(o.name) not in ("Ground", "Trunk")
            and not o.name.startswith("Rock_")):
        o.location.z = 0.5
'''

_AI_LOOK = r'''
# AI「材质」：按它第一次看到的场景做"秋天"的效果（它不知道布局 AI 和人后来改了什么）
material("RightOfWay_Leaf", (0.8, 0.45, 0.1, 1))       # 叶子变橙色
obj("Trunk").scale = (1.1, 1.1, 1.1)                # 顺手把树干调细一点——但布局 AI 已经改过树干了
r1 = obj("Rock_1")
if r1 is not None:
    r1.location = (3.5, -2, 0.25)                  # 它记得 Rock_1 在 (3, -2)，往右挪一点——但布局 AI 已经把石头排成圈了
sun = obj("Sun")
if sun is not None:
    sun.data.energy = 5                            # 灯光调亮
'''

_AI_LAYOUT_FOLLOWUP = r'''
# AI「布局」紧接着又想把树干再加粗一点
obj("Trunk").scale = (1.5, 1.5, 1.5)
'''

_AI_LOOK_RETRY = r'''
# AI「材质」看了最新情况（树干已经是布局 AI 定的 1.3）后重新决定：在它的基础上稍微收一点
obj("Trunk").scale = (1.2, 1.2, 1.2)
'''


_HUMAN_SIM_D = r'''
# 模拟人的两个修改
bpy.data.objects.remove(obj("Rock_2"), do_unlink=True)               # 1. 删掉一块石头
obj("Leaf_3").location.z += 0.8                                       # 2. 把一片叶子往上挪
'''


def human_sim_d(scene: Optional[str] = None) -> str:
    return _with_helpers(_HUMAN_SIM_D, scene)


def ai_layout(scene: Optional[str] = None, skip=()) -> str:
    return _with_helpers(_AI_LAYOUT, scene, skip)


def ai_look(scene: Optional[str] = None) -> str:
    return _with_helpers(_AI_LOOK, scene)


def ai_layout_followup(scene: Optional[str] = None) -> str:
    return _with_helpers(_AI_LAYOUT_FOLLOWUP, scene)


def ai_look_retry(scene: Optional[str] = None) -> str:
    return _with_helpers(_AI_LOOK_RETRY, scene)


HUMAN_STEPS_D = [
    "删除 Rock_2（选中后按 X → 删除）",
    "选中 Leaf_3，按 G 再按 Z，把它往上挪一点，点鼠标左键确认",
]

HUMAN_MOVE_ROCK1 = "选中 Rock_1，按 G 把它挪一点，点鼠标左键确认"


# ---------------------------------------------------------------------------
# 真实 AI 的写法（related_work.md 2c）：上面的脚本只精确地改几处，真实的 AI 不这样写。
#   精确修改（targeted）：上面那些脚本，只改这一步想改的几处。
#   整体调整（broad）：遍历执行时场景里的所有物体，按类型统一设置位置、旋转、缩放、材质；
#   清空重建（rebuild）：先把场景里的东西全删掉，再按自己的计划（带上这次的调整）重新搭一遍。
# 三种写法表达的是同一个计划（P）。数值来自 AI 上次看到的场景。
# SKIP：AI 从运行时的提示里知道"人删掉了"、不再补回的对象；DROP：AI 自己决定不要的对象（实验 E）。
# ---------------------------------------------------------------------------
GREEN = (0.2, 0.6, 0.2, 1)
ORANGE = (0.8, 0.45, 0.1, 1)

# 每个 AI 这一步的计划
PLAN_ADJUST = {"layout": True, "style": True, "leaf_rgba": ORANGE, "trunk_scale": 1.3,           # 实验 A、E
               "leaf_z": 1.6, "rocks": "circle", "sun_energy": 3}
PLAN_LAYOUT = {"layout": True, "style": False, "leaf_rgba": GREEN, "trunk_scale": 1.3,           # 实验 D「布局」
               "leaf_z": 1.6, "rocks": "circle", "sun_energy": 3}
PLAN_LOOK = {"layout": False, "style": True, "leaf_rgba": ORANGE, "trunk_scale": 1.1,            # 实验 D「材质」，按第 1 步的印象
             "leaf_z": 1.4, "rocks": "row", "rock1": (3.5, -2, 0.25), "sun_energy": 5}

_AI_BROAD = r"""
# AI（整体调整）：遍历场景里现有的所有物体，按类型统一设置。P 是这一步的计划
BARK, STONE = (0.35, 0.2, 0.1, 1), (0.5, 0.5, 0.5, 1)
leaf_mat = material("RightOfWay_Leaf", P["leaf_rgba"]) if P.get("style") else None
bark = material("RightOfWay_Bark", BARK) if P.get("style") else None
stone = material("RightOfWay_Stone", STONE)

def style(o, mat):
    if P.get("style") and o.type == "MESH" and mat is not None:
        o.data.materials.clear()
        o.data.materials.append(mat)

leaves = sorted([o for o in scene.objects if base(o.name).startswith("Leaf_") and base(o.name) not in DROP],
                key=lambda o: o.name)
for o in [o for o in scene.objects if base(o.name) in DROP]:
    bpy.data.objects.remove(o, do_unlink=True)
for i, o in enumerate(leaves):                                  # 叶子：均匀排开、统一高度、摆正
    if P.get("layout"):
        a = i * 2 * math.pi / max(len(leaves), 1)
        o.location = (math.cos(a) * 0.8, math.sin(a) * 0.8, P["leaf_z"])
        o.rotation_euler = (0, 0, 0)
        o.scale = (1, 1, 1)
    style(o, leaf_mat)
trunk = obj("Trunk")
if trunk is not None:                                          # 树干：放正、调粗细
    if P.get("layout"):
        trunk.location = (0, 0, 1.2)
        trunk.rotation_euler = (0, 0, 0)
    if P.get("trunk_scale"):
        t = P["trunk_scale"]
        trunk.scale = (t, t, t)
    style(trunk, bark)
for i in range(3):                                             # 石头：排成一圈；缺了就补（人明确删掉的不补）
    name = f"Rock_{i + 1}"
    o = obj(name)
    if P.get("layout"):
        if o is None and name in SKIP:
            continue
        if o is None:
            o = mesh_object(name, "cube", 0.5, (0, 0, 0), stone)
        a = i * 2 * math.pi / 3
        o.location = (math.cos(a) * 3, math.sin(a) * 3, 0.25)
        o.rotation_euler = (0, 0, 0)
        o.scale = (1, 1, 1)
    if o is not None:
        style(o, stone)
if P.get("rock1") and obj("Rock_1") is not None:
    obj("Rock_1").location = P["rock1"]
sun = obj("Sun")
if P.get("sun_energy") and sun is not None and sun.type == "LIGHT":
    sun.data.energy = P["sun_energy"]
if P.get("layout"):
    for o in list(scene.objects):                              # 其他物体：摆正、放到地面上
        n = base(o.name)
        if (o.type == "MESH" and o.parent is None and n not in ("Ground", "Trunk")
                and not n.startswith(("Rock_", "Leaf_"))):
            o.location.z = 0.5
            o.rotation_euler = (0, 0, 0)
            o.scale = (1, 1, 1)
print("整体调整完成")
"""

_AI_REBUILD = r"""
# AI（清空重建）：先把场景清空，再按自己的计划重新搭一遍。
# 大模型常写成 bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete()，这里用等价的数据接口
for o in list(scene.objects):
    bpy.data.objects.remove(o, do_unlink=True)
bark = material("RightOfWay_Bark", (0.35, 0.2, 0.1, 1))
leaf = material("RightOfWay_Leaf", P["leaf_rgba"])
stone = material("RightOfWay_Stone", (0.5, 0.5, 0.5, 1))
mesh_object("Ground", "plane", 10, (0, 0, 0))
trunk = mesh_object("Trunk", "cylinder", 0.3, (0, 0, 1.2), bark)
t = P.get("trunk_scale", 1.0)
trunk.scale = (t, t, t)
for i in range(5):
    name = f"Leaf_{i + 1}"
    if name in DROP:
        continue
    a = i * 2 * math.pi / 5
    mesh_object(name, "sphere", 0.6, (math.cos(a) * 0.8, math.sin(a) * 0.8, P["leaf_z"]), leaf, parent=trunk)
for i in range(3):
    name = f"Rock_{i + 1}"
    if name in SKIP:                                           # 读了提示：人删掉的不补
        continue
    if P.get("rocks") == "circle":
        a = i * 2 * math.pi / 3
        loc = (math.cos(a) * 3, math.sin(a) * 3, 0.25)
    else:
        loc = (3 + i * 1.2, -2, 0.25)                          # 第 1 步时的位置
    if name == "Rock_1" and P.get("rock1"):
        loc = P["rock1"]
    mesh_object(name, "cube", 0.5, loc, stone)
sun_data = bpy.data.lights.new("Sun", "SUN")
sun_data.energy = P.get("sun_energy", 3)
sun = bpy.data.objects.new("Sun", sun_data)
scene.collection.objects.link(sun)
sun.location = (4, 4, 8)
print("清空重建完成")
"""

HABITS = {"targeted": "精确修改", "broad": "整体调整", "rebuild": "清空重建", "batch": "类型化批量命令"}
BLENDER_HABITS = ("targeted", "broad", "rebuild")


def ai_broad(scene: Optional[str] = None, skip=(), drop=(), plan: Optional[dict] = None) -> str:
    return _with_helpers(_AI_BROAD, scene, skip, drop, plan or PLAN_ADJUST)


def ai_rebuild(scene: Optional[str] = None, skip=(), drop=(), plan: Optional[dict] = None) -> str:
    return _with_helpers(_AI_REBUILD, scene, skip, drop, plan or PLAN_ADJUST)


def habit_step(habit: str, role: str, scene: Optional[str] = None, skip=()) -> str:
    """真实 AI 的某一步，按某种写法。role：
        adjust    实验 A 的第 2 步（整体调整成秋天）
        delete    实验 E 的第 2 步（不要 Leaf_3，顺手放大 Leaf_1）
        layout    实验 D「布局」的第 2 步
        look      实验 D「材质」按第 1 步的印象做秋天效果
        followup  实验 D「布局」紧接着把树干改成 1.5（小改动，三种写法都只改这一处）
        retry     实验 D「材质」看了最新情况后重试：树干 1.2（同上）"""
    if habit not in BLENDER_HABITS:
        raise ValueError(f"Blender 上没有 {habit} 这种写法")
    if role == "followup":
        return ai_layout_followup(scene)
    if role == "retry":
        return ai_look_retry(scene)
    targeted = {"adjust": lambda: ai_adjust(scene, skip), "delete": lambda: ai_delete_leaf3(scene),
                "layout": lambda: ai_layout(scene, skip), "look": lambda: ai_look(scene)}
    plans = {"adjust": PLAN_ADJUST, "delete": PLAN_ADJUST, "layout": PLAN_LAYOUT, "look": PLAN_LOOK}
    drop = ["Leaf_3"] if role == "delete" else []
    if habit == "targeted":
        return targeted[role]()
    if habit == "broad":
        return ai_broad(scene, skip, drop, plans[role])
    return ai_rebuild(scene, skip, drop, plans[role])


# ---------------------------------------------------------------------------
# 逐一试人的单个修改（实验 H 的 --sweep）：人在 AI 上次看过之后改了一处，AI 按原计划执行这一步，
# 看撞上的概率和结局。人的修改都是界面里最常见的：移动、旋转、缩放、删除，以及改共用材质的颜色。
# ---------------------------------------------------------------------------
SWEEP_OBJECTS = ["Ground", "Trunk", "Leaf_1", "Leaf_2", "Leaf_3", "Leaf_4", "Leaf_5", "Rock_1", "Rock_2", "Rock_3", "Sun"]
SWEEP_MATERIALS = ["RightOfWay_Leaf", "RightOfWay_Bark", "RightOfWay_Stone"]


def sweep_edits() -> list[tuple[str, str]]:
    return ([(k, o) for o in SWEEP_OBJECTS for k in ("move", "rotate", "scale", "delete")]
            + [("recolor", m) for m in SWEEP_MATERIALS])


def human_edit(kind: str, target: str, scene: Optional[str] = None) -> str:
    body = {
        "move": f"obj({target!r}).location.z += 0.5",
        "rotate": f"obj({target!r}).rotation_euler.z += math.radians(30)",
        "scale": f"o = obj({target!r})\no.scale = tuple(x * 1.5 for x in o.scale)",
        "delete": f"bpy.data.objects.remove(obj({target!r}), do_unlink=True)",
        "recolor": f"material({target!r}, (0.5, 0.2, 0.6, 1))",          # 共用材质：用它的对象都变
    }[kind]
    return _with_helpers(body + "\n", scene)
