"""按行记账的基本操作（spec/design_code.md 第 2 节）。只用标准库，不认识任何语言：文件就是一行一行的字节。

一个文件在账本里是：每一行的"标记"（owners，最后改出这一行的那次修改），以及删除点（points：
有人删掉了几行，删掉的位置在两行之间）。一次修改 = 旧内容到新内容的差异（difflib），没变的行带着原来的标记，
新写出来的行带上这次修改的标记，删掉的地方留一个删除点。

比较两行时不看行尾的换行符（CRLF 和 LF 算一样），保存时原样保存字节。"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Callable, Optional

Opcode = tuple[str, int, int, int, int]


def split_lines(data: Optional[bytes]) -> list[bytes]:
    """按行切开，保留每行的换行符；None（文件不存在）和空文件都是 []。"""
    return data.splitlines(keepends=True) if data else []


def _key(line: bytes) -> bytes:
    return line.rstrip(b"\r\n")


def diff(old: list[bytes], new: list[bytes]) -> list[Opcode]:
    """旧行到新行的差异，和 SequenceMatcher.get_opcodes 一样的五元组（坐标是行号，从 0 开始，左闭右开）。
    先去掉相同的开头和结尾（多数修改只动中间几行，大文件也快），中间部分再交给 difflib。"""
    a, b = [_key(x) for x in old], [_key(x) for x in new]
    lo = 0
    while lo < len(a) and lo < len(b) and a[lo] == b[lo]:
        lo += 1
    hi_a, hi_b = len(a), len(b)
    while hi_a > lo and hi_b > lo and a[hi_a - 1] == b[hi_b - 1]:
        hi_a, hi_b = hi_a - 1, hi_b - 1
    ops: list[Opcode] = []
    if lo:
        ops.append(("equal", 0, lo, 0, lo))
    if hi_a > lo or hi_b > lo:
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, a[lo:hi_a], b[lo:hi_b], autojunk=False).get_opcodes():
            ops.append((tag, i1 + lo, i2 + lo, j1 + lo, j2 + lo))
    if hi_a < len(a):
        ops.append(("equal", hi_a, len(a), hi_b, len(b)))
    return _merge(ops)


def _merge(ops: list[Opcode]) -> list[Opcode]:
    out: list[Opcode] = []
    for op in ops:
        if op[1] == op[2] and op[3] == op[4]:
            continue
        if out and out[-1][0] == op[0] == "equal" and out[-1][2] == op[1]:
            out[-1] = ("equal", out[-1][1], op[2], out[-1][3], op[4])
        else:
            out.append(op)
    return out


def changed(ops: list[Opcode]) -> bool:
    return any(op[0] != "equal" for op in ops)


def map_point(pos: int, ops: list[Opcode]) -> Optional[int]:
    """删除点（两行之间的位置）在新内容里的位置。落在没变的部分（含边界）就跟着移动；落在被改写的部分里面，
    说明那一块被这次修改重写了，删除点没了（返回 None）。"""
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal" and i1 <= pos <= i2:
            return j1 + (pos - i1)
    if not ops and pos == 0:
        return 0
    return None


def apply(owners: list[Optional[str]], points: list[dict], ops: list[Opcode], mark: str,
          old: list[bytes]) -> tuple[list[Optional[str]], list[dict]]:
    """把一次修改（标记 mark）记到行上：没变的行带着原来的标记，新写出来的行带上 mark，删掉的地方留删除点。
    删除点记下删掉了几行、删掉的前几行（告诉 Agent 用）。"""
    new_owners: list[Optional[str]] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            new_owners.extend(owners[i1:i2])
        else:
            new_owners.extend([mark] * (j2 - j1))
    new_points = []
    for p in points:
        q = map_point(p["pos"], ops)
        if q is not None:
            new_points.append({**p, "pos": q})
    for tag, i1, i2, j1, j2 in ops:
        if tag == "delete":
            new_points.append({"id": mark, "pos": j1, "count": i2 - i1,
                               "text": [x.decode("utf-8", "replace").rstrip("\r\n") for x in old[i1:min(i2, i1 + 3)]]})
    seen, uniq = set(), []
    for p in sorted(new_points, key=lambda p: (p["pos"], p["id"])):
        if (p["id"], p["pos"]) not in seen:
            seen.add((p["id"], p["pos"]))
            uniq.append(p)
    return new_owners, uniq


def hits(owners: list[Optional[str]], points: list[dict], ops: list[Opcode],
         protected: Callable[[str], bool]) -> dict[str, dict]:
    """一次修改会碰到哪些受保护的标记：{标记: {"lines": [行号...], "points": [删除点...]}}（现在的坐标）。
    改写、删掉了受保护的行算碰到；受保护的删除点落在改写的范围里（含两端，例如正好在人删掉的地方插入）也算。
    紧挨着受保护的行插入、修改别的行，不算。"""
    out: dict[str, dict] = {}
    for tag, i1, i2, _j1, _j2 in ops:
        if tag == "equal":
            continue
        for k in range(i1, i2):
            m = owners[k] if k < len(owners) else None
            if m is not None and protected(m):
                out.setdefault(m, {"lines": [], "points": []})["lines"].append(k)
        for p in points:
            if i1 <= p["pos"] <= i2 and protected(p["id"]):
                d = out.setdefault(p["id"], {"lines": [], "points": []})
                if p not in d["points"]:
                    d["points"].append(p)
    return out


def ranges(lines: list[int]) -> list[tuple[int, int]]:
    """行号（从 0 开始）合并成连续的段，返回从 1 开始、两端都含的 (起, 止)。"""
    out: list[tuple[int, int]] = []
    for k in sorted(set(lines)):
        if out and out[-1][1] == k:
            out[-1] = (out[-1][0], k + 1)
        else:
            out.append((k + 1, k + 1))
    return out


def rle(owners: list[Optional[str]]) -> list[list]:
    """存盘用：[[标记, 连续几行], ...]。"""
    out: list[list] = []
    for m in owners:
        if out and out[-1][0] == m:
            out[-1][1] += 1
        else:
            out.append([m, 1])
    return out


def unrle(runs: list[list]) -> list[Optional[str]]:
    out: list[Optional[str]] = []
    for m, n in runs or ():
        out.extend([m] * int(n))
    return out
