"""最简单的三方合并：按段落（空行分隔）逐段比较。

只用于声明为可合并的文本对象（规范 R4 例外、R6 的 merge 策略）。
真实产品里可以换成更好的算法，这里够用来验证规则。
"""
from __future__ import annotations

from typing import Optional


def _split(text: str) -> list[str]:
    return text.split("\n\n")


def three_way_merge(base: str, ours: str, theirs: str) -> Optional[str]:
    """base：共同祖先；ours：当前版本；theirs：要合进来的版本。

    返回合并结果；同一段落两边都改了、或段落数量不一致时，返回 None 表示合并失败。
    """
    b, o, t = _split(base), _split(ours), _split(theirs)
    if not (len(b) == len(o) == len(t)):
        return None
    merged: list[str] = []
    for pb, po, pt in zip(b, o, t):
        if po == pt:
            merged.append(po)
        elif po == pb:          # 只有 theirs 改了这一段
            merged.append(pt)
        elif pt == pb:          # 只有 ours 改了这一段
            merged.append(po)
        else:                   # 两边都改了同一段：冲突
            return None
    return "\n\n".join(merged)
