"""说明（spec/core.md 第 6 节）：给 AI 和给人的话只从账本生成（P9）。

这里产出结构化的事实：哪些单元、谁改的、为什么不能改、现在的值和 AI 想改成的值、要不要提醒人、给人什么按钮。
写成"第 2 行""Leaf_3 的位置"这样的媒介说法，由各媒介自己的说明模块做（rightofway/code/messages.py、
rightofway/blender/messages.py），用的是应用侧提供的名字和值。"""
from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# 按编号的容器（单元编号写成"对象编号#面"）
# ---------------------------------------------------------------------------
def split(uid: str) -> tuple[str, Optional[str]]:
    g, _, face = uid.partition("#")
    return g, face or None


def conflicts(skipped: list[dict], commands: list[dict], before: dict, raw: dict, after: dict,
              before_values: dict, raw_values: dict, name_of, label_of) -> list[dict]:
    """每个没生效的单元一条（参照 K8s Server-Side Apply 的 409：每个冲突字段一条，写明所有者）。
    skipped：结算里保持了的单元 [{id, reason, by?, whole_object?, precheck?, ancestor?}]（不含"改回去了"）；
    commands：类型化命令的结果（执行前就被拒的，AI 想改成的值来自命令本身）；
    name_of(记录, 编号)、label_of(面或 None)：应用侧给的名字、面的说法。"""
    out = []
    asked: dict = {}
    deleting = set()
    for e in commands:
        c = e.get("command") or {}
        for f, v in (c.get("set") or {}).items():
            asked[(c.get("target"), f)] = json.dumps(v, ensure_ascii=False)
        if c.get("action") == "delete" and e.get("status") == "refused":
            deleting.add(c.get("target"))
    for s_ in skipped:
        cid, face = split(s_["id"])
        wanted = raw_values.get(cid, {}).get(face) if face and cid in raw else None
        if s_.get("precheck") and face:
            wanted = asked.get(((before.get(cid) or {}).get("name"), face))
        if s_["reason"] in (R.KEEP_HUMAN, R.KEEP_SELECTED):
            owner = "human"
        elif s_.get("by"):
            owner = f"agent:{s_['by']}"
        else:
            owner = s_["reason"]
        out.append({"id": cid, "object": name_of(before.get(cid) or raw.get(cid) or after.get(cid), cid),
                    "face": face, "label": label_of(face), "reason": s_["reason"], "owner": owner,
                    "now": before_values.get(cid, {}).get(face) if face else None, "wanted": wanted,
                    # 删除没有生效：代码里删掉了（执行后没有它）、或者删除命令在执行前就被拒了
                    "delete_refused": (bool(s_.get("whole_object")) or (cid in before and cid not in raw)
                                       or (before.get(cid) or {}).get("name") in deleting),
                    "precheck": bool(s_.get("precheck")), "ancestor": bool(s_.get("ancestor"))})
    return out


def facts(conflict_items: list[dict], recreate_blocked: Iterable[str]) -> list[dict]:
    """失效的事实（RIR）和新的目标（Flux 的 Adopt）：AI 以为自己改成了、其实以现在的值为准的部分，
    以及删不掉的、被人删掉的对象。只列人和先到的 AI 定下的（"人正选中着""预留"是暂时的，不算）。"""
    out, exists = [], set()
    for c in conflict_items:
        if c["reason"] not in (R.KEEP_HUMAN, R.KEEP_OTHER_AGENT):
            continue
        if c["delete_refused"]:
            if c["id"] not in exists:
                exists.add(c["id"])
                out.append({"id": c["id"], "object": c["object"], "fact": "exists", "owner": c["owner"],
                            "ancestor": c["ancestor"]})
            continue
        if c["face"] is None:
            out.append({"id": c["id"], "object": c["object"], "fact": "unchanged", "owner": c["owner"]})
        elif c["now"] is not None:
            out.append({"id": c["id"], "object": c["object"], "fact": "value", "face": c["face"],
                        "label": c["label"], "value": c["now"], "owner": c["owner"]})
    for n in recreate_blocked:
        out.append({"object": n, "fact": "deleted", "owner": "human"})
    return out


def partial(committed: Iterable[str], skipped: list[dict]) -> dict[str, dict]:
    """部分生效的对象：{对象编号: {"kept": [生效的面], "restored": [保持原样的面]}}。"""
    out = {}
    for cid in {split(u)[0] for u in committed}:
        restored = [split(s["id"])[1] for s in skipped if split(s["id"])[0] == cid]
        if restored:
            out[cid] = {"kept": sorted(split(u)[1] for u in committed if split(u)[0] == cid), "restored": sorted(restored)}
    return out


def resurrections(committed: Iterable[str], before: dict, after: dict, deleted_names: Iterable[str],
                  strip) -> list[str]:
    """AI 新建的对象和人删掉的对象同名（去掉重名后缀后）：人可能不想要它（只告知）。"""
    gone = {strip(n) for n in deleted_names}
    return [after[cid]["name"] for cid in sorted({split(u)[0] for u in committed})
            if cid not in before and cid in after and strip(after[cid]["name"]) in gone]


def derived(before: dict, after: dict, changed: Iterable[str], suffix: Optional[str], strip, touched, name_of) -> list[dict]:
    """派生改动，只告知（prior_art_solutions.md 问题三）：
    - copy：AI 新建的数据块是已有数据块的副本（去掉探测出的重名后缀后同名同类型，例如材质 X → X.001）；
    - fewer_users：人改过的数据块（touched），这一步之后用它的对象变少了，人在它上面的修改在这些对象上看不到了。"""
    out: list[dict] = []
    changed = set(changed)
    if suffix:
        names = {(r.get("type"), r["name"]): cid for cid, r in before.items() if r.get("kind") == "data"}
        for cid in sorted(c for c in changed if c in after):
            r = after[cid]
            if r.get("kind") != "data" or cid in before:
                continue
            base = strip(r["name"])
            if base != r["name"] and (r.get("type"), base) in names:
                out.append({"kind": "copy", "object": name_of(r, cid), "of": name_of(before[names[(r.get("type"), base)]], base)})
    for cid in sorted(c for c in changed if c in before):
        r, a = before[cid], after.get(cid)
        if (r.get("kind") == "data" and a is not None and touched(cid, r)
                and (a.get("users") or 0) < (r.get("users") or 0)):
            out.append({"kind": "fewer_users", "object": name_of(r, cid), "before": r.get("users"), "after": a.get("users")})
    return out


def offers(st, permit, agent: str, alerted: set) -> tuple[list[str], list[str]]:
    """给人的按钮（每件事只提醒一次，alerted 原地更新）：(拦下了补回的人删掉的对象 → "允许重建"，
    推断是同一个对象、把人改过的部分带到了新对象上 → "不是同一个")。"""
    held, carried = [], []
    if st is None:
        return held, carried
    for cid in st.holds:
        if ("hold", agent, cid) not in alerted:
            alerted.add(("hold", agent, cid))
            held.append(cid)
    human = {split(u)[0] for u, k in permit.keep.items() if k.get("reason") == R.KEEP_HUMAN}
    for old, v in sorted(st.reidentified.items()):
        if v["kind"] not in ("gone", "rebind") or old not in human or ("unmatch", agent, old) in alerted:
            continue
        alerted.add(("unmatch", agent, old))
        carried.append(old)
    return held, carried
