"""反复改改不动的东西（prior_art_solutions.md 第 3 项）：同一个单元一次次被保持，说明 AI 在反复改改不动的东西，
提醒人一次（这是告知，不是保护手段，不暂停）。只从账本算。

两种数法，同一条规则——"同一个 AI 对同一个单元被拦下了 repeat 次"：
- 不限次数（window=None）：累计次数，正好到 repeat 次时提醒（以后不再提醒）。用在每次拒绝都是一次单独尝试的地方
  （例如一次编辑被整次拒绝）。
- 最近 window 次执行里（window=n）：被保持了 repeat 次就算进入循环；新进入循环的提醒一次，不再反复的下次重新算。
  用在一次执行里同时改很多单元的地方（例如执行一段脚本）。"""
from __future__ import annotations

from typing import Iterable, Optional


def count(counts: dict, keys: Iterable[str], repeat: int = 3) -> list[str]:
    """不限次数：每个键加一次；返回这次正好到 repeat 次的键（要提醒人的）。counts 原地更新。"""
    out = []
    for k in keys:
        counts[k] = counts.get(k, 0) + 1
        if counts[k] == repeat:
            out.append(k)
    return out


def detect(history: list[set], alerted: set, kept_now: set, repeat: int = 3,
           window: Optional[int] = 5) -> tuple[dict[str, int], list[str]]:
    """最近 window 次执行里：记下这一次被保持的单元，返回 (正在反复的 {单元: 次数}, 这次新进入循环、要提醒的单元)。
    history、alerted 原地更新（history 只留最近 window 次）。"""
    history.append(set(kept_now))
    if window is not None:
        del history[:-window]
    counts: dict[str, int] = {}
    for kept in history:
        for u in kept:
            counts[u] = counts.get(u, 0) + 1
    loops = {u: n for u, n in sorted(counts.items()) if n >= repeat and u in history[-1]}
    alerted &= set(loops)                                    # 不再反复的，下次再进入循环时重新提醒
    new = sorted(set(loops) - alerted)
    alerted.update(new)
    return loops, new
