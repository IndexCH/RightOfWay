"""把两次观察之间的变化写成文字，带上具体的值："人 修改了 Leaf_3：位置 (0.8, 0, 1.4) → (0.8, 0, 2.2)"。

纯 Python，不认识任何面的名字：面的显示名和值的文字都由观察那一层（在应用里运行的代码）提供，
那边按应用自己的类型信息（数字、向量、枚举、引用、列表……）自动生成。
取不到值的面（嵌套结构等）只说"内容有变化"。
"""
from __future__ import annotations

from typing import Callable, Optional

from .records import Records, display, label

Values = dict[str, dict[str, str]]          # 编号 → 面 → 值的文字
Who = Callable[[str, Optional[str]], str]    # (编号, 面) → 谁改的（显示用）


def face_change(face: str, cid: str, old_values: Values, new_values: Values, labels) -> str:
    lab = label(face, labels)
    ov = old_values.get(cid, {}).get(face)
    nv = new_values.get(cid, {}).get(face)
    if ov is not None and nv is not None and ov != nv:
        return f"{lab} {ov} → {nv}"
    if nv is not None and ov is None:
        return f"{lab} 变成 {nv}"
    return f"{lab}（内容有变化）"


def describe_changes(old: Records, new: Records, old_values: Values, new_values: Values, labels=None,
                     who: Optional[Who] = None, exclude: set[tuple[str, str]] | None = None,
                     max_objects: int = 30, max_faces: int = 6) -> list[str]:
    """old → new 之间的变化，一个对象一行。exclude 里的 (编号, 面) 不写（例如已经在别处说明过的）。"""
    who = who or (lambda cid, face: "")
    exclude = exclude or set()
    lines: list[str] = []
    for cid in sorted(set(new) - set(old), key=lambda c: new[c]["name"]):
        lines.append(f"{who(cid, None)}新建了 {display(new[cid])}".strip())
    for cid in sorted(set(old) - set(new), key=lambda c: old[c]["name"]):
        lines.append(f"{who(cid, None)}删除了 {display(old[cid])}".strip())
    for cid in sorted(set(old) & set(new), key=lambda c: new[c]["name"]):
        a, b = old[cid], new[cid]
        if a["fp"] == b["fp"]:
            continue
        faces = [f for f in sorted(set(a["aspects"]) | set(b["aspects"]), key=lambda f: label(f, labels))
                 if a["aspects"].get(f) != b["aspects"].get(f) and (cid, f) not in exclude]
        if not faces:
            continue
        by_who: dict[str, list[str]] = {}
        for f in faces:
            by_who.setdefault(who(cid, f), []).append(f)
        for w, fs in by_who.items():
            parts = [face_change(f, cid, old_values, new_values, labels) for f in fs[:max_faces]]
            if len(fs) > max_faces:
                parts.append(f"等 {len(fs)} 项")
            lines.append(f"{w}修改了 {display(b)}：".lstrip() + "；".join(parts))
    if len(lines) > max_objects:
        lines = lines[:max_objects] + [f"……另外还有 {len(lines) - max_objects} 处变化"]
    return lines
