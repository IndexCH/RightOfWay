"""说明（spec/core.md 第 6 节）：给 AI 和给人的话只从账本生成（P9）。

这里产出结构化的事实：哪些单元、谁改的、为什么不能改、现在的值和 AI 想改成的值、要不要提醒人、给人什么按钮。
写成"第 2 行""Leaf_3 的位置"这样的媒介说法，由各媒介自己的说明模块做（rightofway/code/messages.py、
rightofway/blender/messages.py），用的是应用侧提供的名字和值。"""
from __future__ import annotations

from typing import Iterable, Optional

from . import rules as R
from .positional import PositionalLedger, ranges


# ---------------------------------------------------------------------------
# 按位置的容器
# ---------------------------------------------------------------------------
def hit_items(led: PositionalLedger, hits: dict[str, dict]) -> list[dict]:
    """一次修改碰到的不能改的部分，按位置排好：[{"mark", "reason": locked|human|unattributed|stale, "author",
    "positions": [位置...], "units": [(起, 止)], "points": [墓碑...]}]。"""
    out = []
    for m, d in sorted(hits.items(), key=lambda kv: (kv[1]["units"] or [p["pos"] for p in kv[1]["points"]])[0]):
        out.append({"mark": m, "reason": led.reason(m), "author": (led.marks.get(m) or {}).get("author"),
                    "positions": list(d["units"]), "units": ranges(d["units"]), "points": list(d["points"])})
    return out


def handback_span(items: Iterable[dict]) -> Optional[tuple[int, int]]:
    """要人交还的范围（从 1 开始、两端都含）：碰到的人的、来源不明的单元的最小、最大位置。没有就是 None。"""
    rs = [r for it in items if it["reason"] in ("human", "unattributed", "locked") for r in it["units"]]
    if not rs:
        return None
    return min(r[0] for r in rs), max(r[1] for r in rs)


def author_category(author: Optional[str]) -> str:
    """说明里怎么称呼作者：baseline | human | external | git | agent（措辞由媒介定）。"""
    return R.category(author)
