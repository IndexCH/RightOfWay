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
# 材质名加 Cowork_ 前缀，避免改到你自己项目里同名的材质
bark = material("Cowork_Bark", (0.35, 0.2, 0.1, 1))
leaf = material("Cowork_Leaf", (0.2, 0.6, 0.2, 1))
stone = material("Cowork_Stone", (0.5, 0.5, 0.5, 1))
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
material("Cowork_Leaf", (0.8, 0.45, 0.1, 1))     # 秋天：叶子变橙色（所有叶子共用这个材质）
stone = material("Cowork_Stone", (0.5, 0.5, 0.5, 1))
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


def _with_helpers(body: str, scene: Optional[str], skip=()) -> str:
    return f"SCENE_NAME = {scene!r}\nSKIP = {sorted(skip)!r}\n" + _HELPERS + body


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
stone = material("Cowork_Stone", (0.5, 0.5, 0.5, 1))
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
material("Cowork_Leaf", (0.8, 0.45, 0.1, 1))       # 叶子变橙色
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
