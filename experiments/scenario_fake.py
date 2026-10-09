"""和 scenario_tree.py 同一个故事，在内存里的假应用（rightofway/fakeapp.py）上跑，不需要 Blender 或 Unity。

对象有 location、rotation、scale 几个面，颜色在共用的材质上（LeafMat、BarkMat、StoneMat 是数据块，
对象的 material 面只记引用了哪个），和 Blender 一样：改一次材质，用它的对象都变。
树叶还有 parent（指向树干的编号：树干删掉再新建，就是另一个编号）；太阳有 energy。
FakeApp(restore_deleted=False) 和现在的 Unity 接入一样，恢复不了被删掉的对象。

AI 的写法（scenario_tree.py 里的说明）：
    targeted  精确修改            代码
    broad     整体调整            代码
    rebuild   清空重建            代码
    batch     类型化批量命令      像 Unity MCP（CoplayDev）的 batch_execute：AI 按自己看到的场景
                                  写出一条条 modify / create / delete 命令，每批最多 25 条
三种代码写法和批量命令表达的是同一个计划（scenario_tree.PLAN_*）。
"""
from __future__ import annotations

import math
import re
from typing import Optional

from experiments.scenario_tree import PLAN_ADJUST, PLAN_LAYOUT, PLAN_LOOK

HABITS = ("targeted", "broad", "rebuild", "batch")
LEAF_COLOR = {(0.2, 0.6, 0.2, 1): "绿", (0.8, 0.45, 0.1, 1): "橙"}


def _plan(plan: dict) -> dict:
    """把 Blender 的计划换成假应用里的值（颜色用名字）。"""
    p = dict(plan)
    p["leaf_color"] = LEAF_COLOR[tuple(plan["leaf_rgba"])]
    p.pop("leaf_rgba", None)
    if p.get("rock1") is not None:
        p["rock1"] = list(p["rock1"])
    return p


def circle(i: int, n: int, r: float, z: float) -> list:
    a = i * 2 * math.pi / n
    return [round(math.cos(a) * r, 5), round(math.sin(a) * r, 5), z]


def base(name: str) -> str:
    return re.sub(r"\.\d{3}$", "", name)


_HELPERS = r'''
import math, re

def circle(i, n, r, z):
    a = i * 2 * math.pi / n
    return [round(math.cos(a) * r, 5), round(math.sin(a) * r, 5), z]

def base(name):
    return re.sub(r"\.\d{3}$", "", name)

def has(name):
    return name in scene.names()

def mat(name, color):
    """材质：有就改颜色，没有就新建（和 Blender 脚本里的 material() 一样，按名字找）。"""
    if name in scene.data_names():
        scene.set(name, "color", color)
    else:
        scene.create(name, {"color": color}, kind="MATERIAL", data=True)
    return name

ROT0 = [0, 0, 0]
'''


def _code(body: str, skip=(), drop=(), plan: Optional[dict] = None) -> str:
    params = _plan(plan) if plan else {}
    return f"SKIP = {sorted(skip)!r}\nDROP = {sorted(drop)!r}\nP = {params!r}\n" + _HELPERS + body


# ---------------------------------------------------------------------------
# 布置场景、人的操作
# ---------------------------------------------------------------------------
_BUILD = r'''
# AI 第一步：布置场景
mat("BarkMat", "树皮")
mat("LeafMat", "绿")
mat("StoneMat", "石头")
scene.create("Ground", {"location": [0, 0, 0], "rotation": ROT0, "scale": 10.0}, kind="MESH")
scene.create("Trunk", {"location": [0, 0, 1.2], "rotation": ROT0, "scale": 1.0}, kind="MESH",
             refs={"material": "BarkMat"})
for i in range(5):
    scene.create(f"Leaf_{i + 1}", {"location": circle(i, 5, 0.8, 1.4), "rotation": ROT0, "scale": 1.0},
                 parent="Trunk", kind="MESH", refs={"material": "LeafMat"})
for i in range(3):
    scene.create(f"Rock_{i + 1}", {"location": [3 + i * 1.2, -2, 0.25], "rotation": ROT0, "scale": 1.0},
                 kind="MESH", refs={"material": "StoneMat"})
scene.create("Sun", {"location": [4, 4, 8], "rotation": ROT0, "scale": 1.0, "energy": 3}, kind="LIGHT")
print("布置完成：地面、树干、5 片树叶、3 块石头、太阳光")
'''


def ai_build() -> str:
    return _code(_BUILD)


def _move_leaf3(app) -> None:
    x, y, z = app.objects[app.find("Leaf_3")]["faces"]["location"]
    app.set("Leaf_3", "location", [x, y, round(z + 0.8, 5)])


def human_a(app) -> None:
    """实验 A：删掉 Rock_2、把 Leaf_3 往上挪、新建一个立方体、最后选中 Leaf_1。"""
    app.delete("Rock_2")
    _move_leaf3(app)
    app.add("Cube", {"location": [-3, 2, 1.5], "rotation": [0, 0, 0], "scale": 1.0}, kind="MESH")
    app.select("Leaf_1")


def human_d(app) -> None:
    """实验 D：删掉 Rock_2、把 Leaf_3 往上挪。"""
    app.delete("Rock_2")
    _move_leaf3(app)


def human_e(app) -> None:
    """实验 E：把 Leaf_3 往上挪。"""
    _move_leaf3(app)


HUMAN = {"A": human_a, "D": human_d, "E": human_e}

# ---------------------------------------------------------------------------
# 精确修改（和 scenario_tree.py 里的脚本一一对应）
# ---------------------------------------------------------------------------
_ADJUST = r'''
# AI 第二步：按自己之前看到的场景整体调整（精确修改：只改想改的几处）
mat("LeafMat", "橙")                                     # 秋天：叶子变橙色（所有叶子共用这个材质）
scene.set("Trunk", "scale", 1.3)                         # 树干加粗
for i in range(5):                                       # 叶子统一抬高
    n = f"Leaf_{i + 1}"
    if has(n):
        x, y, _ = scene.get(n, "location")
        scene.set(n, "location", [x, y, 1.6])
for i in range(3):                                       # 石头排成一圈；缺了就补（人明确删掉的不补）
    n = f"Rock_{i + 1}"
    if not has(n) and n in SKIP:
        continue
    if not has(n):
        scene.create(n, {"location": [0, 0, 0], "rotation": ROT0, "scale": 1.0}, kind="MESH",
                     refs={"material": mat("StoneMat", "石头")})
    scene.set(n, "location", circle(i, 3, 3, 0.25))
for n in scene.names():                                  # 其他散落的物体放到地面上
    if (scene.kind(n) == "MESH" and scene.parent(n) is None and base(n) not in ("Ground", "Trunk")
            and not n.startswith("Rock_")):
        x, y, _ = scene.get(n, "location")
        scene.set(n, "location", [x, y, 0.5])
print("调整完成")
'''

_DELETE_LEAF3 = r'''
# AI 按旧印象：觉得 Leaf_3 多余，删掉；顺手把 Leaf_1 放大一点
if has("Leaf_3"):
    scene.delete("Leaf_3")
if has("Leaf_1"):
    scene.set("Leaf_1", "scale", 1.2)
'''

_LAYOUT = r'''
# AI「布局」：叶子统一抬高、树干加粗、石头排成一圈（人明确删掉的不补）、散落物体落地
scene.set("Trunk", "scale", 1.3)
for i in range(5):
    n = f"Leaf_{i + 1}"
    if has(n):
        x, y, _ = scene.get(n, "location")
        scene.set(n, "location", [x, y, 1.6])
for i in range(3):
    n = f"Rock_{i + 1}"
    if not has(n) and n in SKIP:
        continue
    if not has(n):
        scene.create(n, {"location": [0, 0, 0], "rotation": ROT0, "scale": 1.0}, kind="MESH",
                     refs={"material": mat("StoneMat", "石头")})
    scene.set(n, "location", circle(i, 3, 3, 0.25))
for n in scene.names():
    if (scene.kind(n) == "MESH" and scene.parent(n) is None and base(n) not in ("Ground", "Trunk")
            and not n.startswith("Rock_")):
        x, y, _ = scene.get(n, "location")
        scene.set(n, "location", [x, y, 0.5])
'''

_LOOK = r'''
# AI「材质」：按它第一次看到的场景做"秋天"的效果
mat("LeafMat", "橙")                                     # 叶子变橙色
scene.set("Trunk", "scale", 1.1)                         # 顺手把树干调细一点——但布局 AI 已经改过树干了
if has("Rock_1"):
    scene.set("Rock_1", "location", [3.5, -2, 0.25])     # 它记得 Rock_1 在 (3, -2)，往右挪一点
if has("Sun"):
    scene.set("Sun", "energy", 5)
'''

_FOLLOWUP = 'scene.set("Trunk", "scale", 1.5)\n'
_RETRY = 'scene.set("Trunk", "scale", 1.2)\n'

# ---------------------------------------------------------------------------
# 整体调整、清空重建（和 scenario_tree.py 的 _AI_BROAD、_AI_REBUILD 一一对应）
# ---------------------------------------------------------------------------
_BROAD = r'''
# AI（整体调整）：遍历场景里现有的所有物体，按类型统一设置。P 是这一步的计划
if P.get("style"):
    mat("LeafMat", P["leaf_color"])
    mat("BarkMat", "树皮")
mat("StoneMat", "石头")
leaves = sorted(n for n in scene.names() if base(n).startswith("Leaf_") and base(n) not in DROP)
for n in [n for n in scene.names() if base(n) in DROP]:
    scene.delete(n)
for i, n in enumerate(leaves):
    if P.get("layout"):
        scene.set(n, "location", circle(i, len(leaves), 0.8, P["leaf_z"]))
        scene.set(n, "rotation", ROT0)
        scene.set(n, "scale", 1.0)
    if P.get("style"):
        scene.link(n, "material", "LeafMat")
if has("Trunk"):
    if P.get("layout"):
        scene.set("Trunk", "location", [0, 0, 1.2])
        scene.set("Trunk", "rotation", ROT0)
    if P.get("trunk_scale"):
        scene.set("Trunk", "scale", P["trunk_scale"])
    if P.get("style"):
        scene.link("Trunk", "material", "BarkMat")
for i in range(3):
    n = f"Rock_{i + 1}"
    if P.get("layout"):
        if not has(n) and n in SKIP:
            continue
        if not has(n):
            scene.create(n, {"location": [0, 0, 0], "rotation": ROT0, "scale": 1.0}, kind="MESH",
                         refs={"material": "StoneMat"})
        scene.set(n, "location", circle(i, 3, 3, 0.25))
        scene.set(n, "rotation", ROT0)
        scene.set(n, "scale", 1.0)
    if P.get("style") and has(n):
        scene.link(n, "material", "StoneMat")
if P.get("rock1") and has("Rock_1"):
    scene.set("Rock_1", "location", P["rock1"])
if P.get("sun_energy") and has("Sun") and scene.kind("Sun") == "LIGHT":
    scene.set("Sun", "energy", P["sun_energy"])
if P.get("layout"):
    for n in scene.names():
        if (scene.kind(n) == "MESH" and scene.parent(n) is None and base(n) not in ("Ground", "Trunk")
                and not base(n).startswith(("Rock_", "Leaf_"))):
            x, y, _ = scene.get(n, "location")
            scene.set(n, "location", [x, y, 0.5])
            scene.set(n, "rotation", ROT0)
            scene.set(n, "scale", 1.0)
print("整体调整完成")
'''

_REBUILD = r'''
# AI（清空重建）：先把场景里的物体清空，再按自己的计划重新搭一遍（材质按名字找，有就改、没有就新建）
for n in list(scene.names()):
    scene.delete(n)
mat("BarkMat", "树皮")
mat("LeafMat", P["leaf_color"])
mat("StoneMat", "石头")
scene.create("Ground", {"location": [0, 0, 0], "rotation": ROT0, "scale": 10.0}, kind="MESH")
scene.create("Trunk", {"location": [0, 0, 1.2], "rotation": ROT0, "scale": P.get("trunk_scale", 1.0)},
             kind="MESH", refs={"material": "BarkMat"})
for i in range(5):
    n = f"Leaf_{i + 1}"
    if n in DROP:
        continue
    scene.create(n, {"location": circle(i, 5, 0.8, P["leaf_z"]), "rotation": ROT0, "scale": 1.0},
                 parent="Trunk", kind="MESH", refs={"material": "LeafMat"})
for i in range(3):
    n = f"Rock_{i + 1}"
    if n in SKIP:
        continue
    loc = circle(i, 3, 3, 0.25) if P.get("rocks") == "circle" else [3 + i * 1.2, -2, 0.25]
    if n == "Rock_1" and P.get("rock1"):
        loc = P["rock1"]
    scene.create(n, {"location": loc, "rotation": ROT0, "scale": 1.0}, kind="MESH", refs={"material": "StoneMat"})
scene.create("Sun", {"location": [4, 4, 8], "rotation": ROT0, "scale": 1.0, "energy": P.get("sun_energy", 3)},
             kind="LIGHT")
print("清空重建完成")
'''

_PLANS = {"adjust": PLAN_ADJUST, "delete": PLAN_ADJUST, "layout": PLAN_LAYOUT, "look": PLAN_LOOK}
_TARGETED = {"adjust": _ADJUST, "delete": _DELETE_LEAF3, "layout": _LAYOUT, "look": _LOOK,
             "followup": _FOLLOWUP, "retry": _RETRY}


def habit_step(habit: str, role: str, skip=(), view: Optional[dict] = None, values: Optional[dict] = None):
    """真实 AI 的某一步（role 和 scenario_tree.habit_step 一样）。
    返回 ("code", 代码) 或 ("commands", 命令列表)。
    批量命令由 AI 按它上次看到的场景写出：view 是它看到的对象记录，values 是它看到的值（文字）。"""
    drop = ["Leaf_3"] if role == "delete" else []
    if role in ("followup", "retry"):
        if habit == "batch":
            return "commands", [{"action": "modify", "target": "Trunk",
                                 "set": {"scale": 1.5 if role == "followup" else 1.2}}]
        return "code", _code(_TARGETED[role])
    if habit == "targeted":
        return "code", _code(_TARGETED[role], skip, drop)
    if habit == "broad":
        return "code", _code(_BROAD, skip, drop, _PLANS[role])
    if habit == "rebuild":
        return "code", _code(_REBUILD, skip, drop, _PLANS[role])
    if habit == "batch":
        return "commands", batch_commands(view or {}, values or {}, _PLANS[role], skip, drop)
    raise ValueError(f"没有 {habit} 这种写法")


# ---------------------------------------------------------------------------
# 类型化批量命令：和整体调整同一个计划，但由 AI 按它上次看到的场景写成一条条命令
# ---------------------------------------------------------------------------
def _vec(text: Optional[str]) -> Optional[list]:
    """从看到的值（"(0, 3, 2.2)"）里取出数字。"""
    try:
        return [float(x) for x in text.strip("()").split(",")]
    except (AttributeError, ValueError):
        return None


def batch_commands(view: dict, values: dict, plan: dict, skip=(), drop=()) -> list[dict]:
    p = _plan(plan)
    recs = sorted(view.values(), key=lambda r: r["name"])
    names = [r["name"] for r in recs]
    data = {r["name"] for r in recs if r.get("kind") == "data"}
    names = [n for n in names if n not in data]
    cmds: list[dict] = [{"action": "delete", "target": n} for n in names if base(n) in drop]
    if p.get("style"):                                     # 颜色改在共用材质上，一条命令
        for m, color in (("LeafMat", p["leaf_color"]), ("BarkMat", "树皮")):
            cmds.append({"action": "modify", "target": m, "set": {"color": color}} if m in data else
                        {"action": "create", "name": m, "kind": "MATERIAL", "data": True, "set": {"color": color}})
    leaves = [n for n in names if base(n).startswith("Leaf_") and base(n) not in drop]
    for i, n in enumerate(leaves):
        if p.get("layout"):
            cmds.append({"action": "modify", "target": n,
                         "set": {"location": circle(i, len(leaves), 0.8, p["leaf_z"]), "rotation": [0, 0, 0],
                                 "scale": 1.0}})
    if "Trunk" in names:
        s = {}
        if p.get("layout"):
            s.update(location=[0, 0, 1.2], rotation=[0, 0, 0])
        if p.get("trunk_scale"):
            s["scale"] = p["trunk_scale"]
        if s:
            cmds.append({"action": "modify", "target": "Trunk", "set": s})
    for i in range(3):
        n = f"Rock_{i + 1}"
        s = {}
        if p.get("layout"):
            if n not in names and n in skip:
                continue
            s.update(location=circle(i, 3, 3, 0.25), rotation=[0, 0, 0], scale=1.0)
        if n == "Rock_1" and p.get("rock1"):
            s["location"] = p["rock1"]
        if not s:
            continue
        if n in names:
            cmds.append({"action": "modify", "target": n, "set": s})
        elif p.get("layout"):
            cmds.append({"action": "create", "name": n, "kind": "MESH", "refs": {"material": "StoneMat"},
                         "set": {"location": s["location"], "rotation": [0, 0, 0], "scale": 1.0}})
    if p.get("sun_energy") and "Sun" in names:
        cmds.append({"action": "modify", "target": "Sun", "set": {"energy": p["sun_energy"]}})
    if p.get("layout"):
        for r in recs:
            n = r["name"]
            if (r["type"] == "MESH" and r.get("parent") is None and base(n) not in ("Ground", "Trunk")
                    and not base(n).startswith(("Rock_", "Leaf_"))):
                loc = _vec(values.get(r["id"], {}).get("location")) or [0, 0, 0]
                cmds.append({"action": "modify", "target": n,
                             "set": {"location": [loc[0], loc[1], 0.5], "rotation": [0, 0, 0], "scale": 1.0}})
    return cmds


# ---------------------------------------------------------------------------
# 逐一试人的单个修改（实验 H 的 --sweep）：每个对象的每个面改一次，再加上删除
# ---------------------------------------------------------------------------
_FACE_EDIT = {"location": "move", "rotation": "rotate", "scale": "scale", "color": "recolor",
              "parent": "unparent", "energy": "energy"}


def sweep_edits(app) -> list[tuple[str, str]]:
    """每个对象的每个面改一次、删一次；共用材质改一次颜色（材质不删）。"""
    out = []
    for o in sorted(app.objects.values(), key=lambda o: o["name"]):
        out += [(_FACE_EDIT[f], o["name"]) for f in o["faces"] if f in _FACE_EDIT]
        if not o.get("data"):
            out.append(("delete", o["name"]))
    return out


def human_edit(app, kind: str, target: str) -> None:
    f = app.objects[app.find(target)]["faces"]
    if kind == "move":
        x, y, z = f["location"]
        app.set(target, "location", [x, y, round(z + 0.5, 5)])
    elif kind == "rotate":
        app.set(target, "rotation", [0, 0, 30])
    elif kind == "scale":
        app.set(target, "scale", round(f["scale"] * 1.5, 5))
    elif kind == "recolor":
        app.set(target, "color", "紫")
    elif kind == "unparent":
        app.set(target, "parent", None)
    elif kind == "energy":
        app.set(target, "energy", f["energy"] + 2)
    elif kind == "delete":
        app.delete(target)
    else:
        raise ValueError(kind)
