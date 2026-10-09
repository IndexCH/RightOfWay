"""类型化命令 → Blender 脚本（design_v0.5.md 第 9 节的小工具；MCP 代理的 set_property、create_object、delete_object）。

命令和 FakeBridge.compile_commands 的一样：
    {"action": "modify", "target": "Leaf_3", "set": {"location": [0, 0, 2], "parent": "Trunk"}}
    {"action": "create", "name": "Bird", "kind": "MESH", "parent": "Trunk", "set": {"location": [0, 0, 3]}}
    {"action": "delete", "target": "Rock_2"}

面就是 RNA 属性的名字（和观察时一样，blender_side.object_faces），所以不需要为任何属性手写代码（P4）：
- 普通属性直接 setattr；
- 引用另一个数据块的属性（parent、data……）按名字找，类型来自 RNA 的 fixed_type；
- 列表（material_slots、modifiers……）不能这样设置，报错，让 AI 改用执行代码的工具。
target 先按对象名找（只在当前场景里），找不到再按数据块名找（材质、网格……），所以也能直接改共用材质。

编出来的脚本交给 run_agent 原子地执行（先按许可单预检查，SharedSession.run_commands），
每条命令单独执行，失败了不影响别的（和 Unity MCP 的 batch_execute 一样不是事务）。
结果打印在标准输出里：COMMANDS_MARK + [[序号, "ok" 或出错原因], ...]。
"""
from __future__ import annotations

import json
from typing import Optional

COMMANDS_MARK = "<<<RIGHTOFWAY_COMMANDS>>>"

_RUNTIME = r'''
import json as _json
_sc = bpy.data.scenes.get(_SCENE) if _SCENE else bpy.context.scene
_out = []


def _collections():
    for p in bpy.types.BlendData.bl_rna.properties:
        if p.type == "COLLECTION":
            c = getattr(bpy.data, p.identifier, None)
            if isinstance(c, bpy.types.bpy_prop_collection):
                yield c


def _target(name):
    for o in _sc.objects:
        if o.name == name:
            return o
    for c in _collections():
        x = c.get(name)
        if isinstance(x, bpy.types.ID) and not isinstance(x, bpy.types.Object) and x.library is None:
            return x
    raise KeyError("找不到：" + name)


def _resolve(p, value):
    """引用另一个数据块的属性：按名字找，类型来自 RNA。"""
    if value is None:
        return None
    t = getattr(bpy.types, p.fixed_type.identifier, None)
    if t is not None and issubclass(t, bpy.types.Object):
        for o in _sc.objects:
            if o.name == value:
                return o
    for c in _collections():
        x = c.get(value)
        if x is not None and (t is None or isinstance(x, t)):
            return x
    raise KeyError("找不到：" + str(value))


def _set(x, face, value):
    p = x.bl_rna.properties.get(face)
    if p is None:
        raise KeyError(x.name + " 没有属性 " + face)
    if p.type == "COLLECTION":
        raise ValueError(face + " 是列表，不能直接设置（请用执行代码的工具）")
    if p.type == "POINTER":
        setattr(x, face, _resolve(p, value))
        return
    if p.is_readonly:
        raise ValueError(face + " 是只读的")
    setattr(x, face, tuple(value) if isinstance(value, list) else value)


def _create(name, kind, parent, values):
    kind = (kind or "EMPTY").upper()
    makers = {"EMPTY": lambda: None, "MESH": lambda: bpy.data.meshes.new(name),
              "LIGHT": lambda: bpy.data.lights.new(name, "POINT"), "CAMERA": lambda: bpy.data.cameras.new(name),
              "CURVE": lambda: bpy.data.curves.new(name, "CURVE")}
    if kind not in makers:
        raise ValueError("不认识的类型：" + kind + "（可以用 " + "、".join(makers) + "）")
    o = bpy.data.objects.new(name, makers[kind]())
    _sc.collection.objects.link(o)
    if parent:
        o.parent = _target(parent)
    for f, v in (values or {}).items():
        _set(o, f, v)
'''


def compile_commands(commands: list[dict], scene: Optional[str] = None) -> str:
    lines = [f"_SCENE = {scene!r}", _RUNTIME]
    for i, c in enumerate(commands):
        a = c.get("action")
        if a == "modify":
            body = ("_x = _target(" + repr(c["target"]) + ")\n    "
                    + "\n    ".join(f"_set(_x, {f!r}, _json.loads({json.dumps(json.dumps(v))}))"
                                    for f, v in c.get("set", {}).items()) or "pass")
        elif a == "create":
            body = (f"_create({c['name']!r}, {c.get('kind')!r}, {c.get('parent')!r}, "
                    f"_json.loads({json.dumps(json.dumps(c.get('set', {})))}))")
        elif a == "delete":
            body = ("_x = _target(" + repr(c["target"]) + ")\n    "
                    "(bpy.data.objects if isinstance(_x, bpy.types.Object) else "
                    "next(_c for _c in _collections() if _c.get(_x.name) == _x)).remove(_x)")
        else:
            body = f"raise ValueError({('不认识的命令：' + str(a))!r})"
        lines.append(f"try:\n    {body}\n    _out.append([{i}, 'ok'])\n"
                     f"except Exception as _e:\n    _out.append([{i}, repr(_e)])")
    lines.append(f"print({COMMANDS_MARK!r} + _json.dumps(_out, ensure_ascii=False))")
    return "\n".join(lines) + "\n"


def command_results(stdout: str) -> dict[int, str]:
    for line in reversed(stdout.splitlines()):
        if line.startswith(COMMANDS_MARK):
            return {int(i): r for i, r in json.loads(line[len(COMMANDS_MARK):])}
    return {}
