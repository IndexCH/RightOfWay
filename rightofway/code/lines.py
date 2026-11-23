"""文本文件的单元：行（spec/design_code.md 第 2 节）。只用标准库，不认识任何语言：文件就是一行一行的字节。

这里只有文本这一侧的事：怎么切成行、新旧两份怎么对应（difflib）。标记怎么跟着行走、墓碑（删除点）怎么留、
一次修改碰到了哪些不能改的标记，都在核心里（rightofway/core/positional.py），这里原样转出，名字照旧。

比较两行时不看行尾的换行符（CRLF 和 LF 算一样），保存时原样保存字节。"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Callable, Optional

from ..core.positional import Opcode, changed, map_point, ranges, rle, unrle   # noqa: F401
from ..core import positional as P

__all__ = ["Opcode", "split_lines", "text_of", "diff", "apply", "hits", "changed", "map_point", "ranges", "rle", "unrle"]


def split_lines(data: Optional[bytes]) -> list[bytes]:
    """按行切开，保留每行的换行符；None（文件不存在）和空文件都是 []。"""
    return data.splitlines(keepends=True) if data else []


def _key(line: bytes) -> bytes:
    return line.rstrip(b"\r\n")


def text_of(lines: list[bytes]) -> Callable[[int, int], list[str]]:
    """墓碑里记下删掉的前几行的文字（告诉 AI 用）。"""
    return lambda i, j: [x.decode("utf-8", "replace").rstrip("\r\n") for x in lines[i:j]]


def diff(old: list[bytes], new: list[bytes]) -> list[Opcode]:
    """旧行到新行的对应关系，和 SequenceMatcher.get_opcodes 一样的五元组（坐标是行号，从 0 开始，左闭右开）。
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


def apply(owners: list[Optional[str]], points: list[dict], ops: list[Opcode], mark: str,
          old: list[bytes]) -> tuple[list[Optional[str]], list[dict]]:
    """把一次修改记到行上（核心的 positional.apply；墓碑里记下删掉的前几行）。"""
    return P.apply(owners, points, ops, mark, text_of(old))


def hits(owners: list[Optional[str]], points: list[dict], ops: list[Opcode],
         protected: Callable[[str], bool]) -> dict[str, dict]:
    """一次修改会碰到哪些受保护的标记（核心的 positional.hits）。"""
    return P.hits(owners, points, ops, protected)
