"""比较两次"列出对象"的结果。纯 Python，不依赖 Blender。

一条记录长这样（由 blender_side.records() 产生）：
    {"id": "h-3f2a...", "name": "Leaf_3", "type": "MESH",
     "cfp": "内容指纹", "fp": "名字+内容指纹", "aspects": {"location": "...", "material_slots": "...", ...},
     "parent": ..., "collections": [...], "editing": False}

"aspects" 里有哪些面，由观察那一层（在应用里运行的代码）决定，这里不认识、也不需要认识任何面的名字。
Blender 那边是从它自己的数据描述（RNA）自动得到的：对象的每个顶层属性一个面。

粒度（granularity）决定运行时里的"对象"是什么：
    "object"：应用里的一个对象就是运行时里的一个对象。人改了它的任何地方，AI 对它的任何修改都不生效。
    "aspect"：应用里的一个对象拆成它的各个面，每个面是运行时里的一个对象。
              人改了位置，AI 改材质照样生效；只有 AI 改了人改过的那个面才不生效。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

Records = dict[str, dict]

OBJECT, ASPECT = "object", "aspect"
Labels = dict[str, str]     # 面的显示名，由观察那一层提供

DELETED = {"deleted": True}


def base_name(name: str) -> str:
    return re.sub(r"\.\d{3}$", "", name)


def is_deleted(content) -> bool:
    return isinstance(content, dict) and content.get("deleted") is True


@dataclass
class Diff:
    created: dict[str, dict] = field(default_factory=dict)
    deleted: dict[str, dict] = field(default_factory=dict)        # 编号 → 删除前的记录
    modified: dict[str, tuple[dict, dict]] = field(default_factory=dict)

    @property
    def ids(self) -> set[str]:
        return set(self.created) | set(self.deleted) | set(self.modified)

    def __bool__(self) -> bool:
        return bool(self.ids)

    def describe(self, names: dict[str, str] | None = None) -> list[str]:
        out = [f"新建 {r['name']}" for r in self.created.values()]
        out += [f"删除 {r['name']}" for r in self.deleted.values()]
        out += [f"修改 {new['name']}" for _, new in self.modified.values()]
        return out


def diff_records(old: Records, new: Records) -> Diff:
    d = Diff()
    for cid in sorted(set(old) | set(new)):
        a, b = old.get(cid), new.get(cid)
        if a is None:
            d.created[cid] = b
        elif b is None:
            d.deleted[cid] = a
        elif a["fp"] != b["fp"]:
            d.modified[cid] = (a, b)
    return d


# ---------------------------------------------------------------------------
# 粒度：Blender 对象 → 运行时里的对象（"单元"）
# ---------------------------------------------------------------------------
def unit_id(cid: str, aspect: str | None = None) -> str:
    return cid if aspect is None else f"{cid}#{aspect}"


def split_unit(uid: str) -> tuple[str, str | None]:
    cid, _, aspect = uid.partition("#")
    return cid, (aspect or None)


def unit_ids(cid: str, granularity: str, faces) -> list[str]:
    return [cid] if granularity == OBJECT else [unit_id(cid, a) for a in faces]


def units(recs: Records, granularity: str) -> dict[str, dict]:
    """把对象记录展开成单元。按面展开时，每个单元的指纹就是那个面的指纹。"""
    if granularity == OBJECT:
        return dict(recs)
    out = {}
    for cid, r in recs.items():
        for a, fp in r["aspects"].items():
            out[unit_id(cid, a)] = {"id": cid, "aspect": a, "name": r["name"], "fp": fp, "cfp": fp}
    return out


def label(face: str, labels: Labels | None = None) -> str:
    return (labels or {}).get(face, face)


def describe_unit(uid: str, name: str, labels: Labels | None = None) -> str:
    _, aspect = split_unit(uid)
    return name if aspect is None else f"{name} 的{label(aspect, labels)}"
