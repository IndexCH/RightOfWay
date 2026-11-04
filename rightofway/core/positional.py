"""按位置的容器（spec/core.md 第 2.2 节）：文本的行，以后的字符。单元没有自己的编号，按位置排。

一个容器在账本里是：每个位置上的标记（owners：最后改出这个单元的那次修改），以及墓碑（points：有人删掉了几个单元，
落在两个位置之间）。适配侧交来新旧内容的对应关系（和 difflib 的 get_opcodes 一样的五元组，坐标从 0 开始、左闭右开），
核心据此移动标记：没变的单元带着原来的标记，新写出来的带上这次修改的标记，删掉的地方留墓碑。
整个容器被删掉，是位置 0 的一个墓碑（"whole"）。

核心不知道单元是什么：比较两个单元是否相同、单元的文字是什么，都由适配侧给（对应关系、describe）。
能不能改用 rules.judge（和按编号的容器同一张表）。"""
from __future__ import annotations

import time
from typing import Callable, Iterable, Optional

from . import rules as R

Opcode = tuple[str, int, int, int, int]
Describe = Callable[[int, int], list[str]]        # 旧内容里 [i1, i2) 这几个单元的文字（墓碑里记前几个，告诉 AI 用）


# ---------------------------------------------------------------------------
# 标记跟着位置走
# ---------------------------------------------------------------------------
def changed(ops: list[Opcode]) -> bool:
    return any(op[0] != "equal" for op in ops)


def map_point(pos: int, ops: list[Opcode]) -> Optional[int]:
    """墓碑（两个单元之间的位置）在新内容里的位置。落在没变的部分（含边界）就跟着移动；落在被改写的部分里面，
    说明那一块被这次修改重写了，墓碑没了（返回 None）。"""
    for tag, i1, i2, j1, _j2 in ops:
        if tag == "equal" and i1 <= pos <= i2:
            return j1 + (pos - i1)
    if not ops and pos == 0:
        return 0
    return None


def apply(owners: list[Optional[str]], points: list[dict], ops: list[Opcode], mark: str,
          describe: Optional[Describe] = None) -> tuple[list[Optional[str]], list[dict]]:
    """把一次修改（标记 mark）记到位置上：没变的单元带着原来的标记，新写出来的带上 mark，删掉的地方留墓碑。
    墓碑记下删掉了几个、删掉的前几个的文字（describe 给）。"""
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
    for tag, i1, i2, j1, _j2 in ops:
        if tag == "delete":
            new_points.append({"id": mark, "pos": j1, "count": i2 - i1,
                               "text": describe(i1, min(i2, i1 + 3)) if describe else []})
    seen, uniq = set(), []
    for p in sorted(new_points, key=lambda p: (p["pos"], p["id"])):
        if (p["id"], p["pos"]) not in seen:
            seen.add((p["id"], p["pos"]))
            uniq.append(p)
    return new_owners, uniq


def hits(owners: list[Optional[str]], points: list[dict], ops: list[Opcode],
         protected: Callable[[str], bool]) -> dict[str, dict]:
    """一次修改会碰到哪些受保护的标记：{标记: {"units": [位置...], "points": [墓碑...]}}（现在的坐标）。
    改写、删掉了受保护的单元算碰到；受保护的墓碑落在改写的范围里（含两端，例如正好在人删掉的地方插入）也算。
    紧挨着受保护的单元插入、修改别的单元，不算。"""
    out: dict[str, dict] = {}
    for tag, i1, i2, _j1, _j2 in ops:
        if tag == "equal":
            continue
        for k in range(i1, i2):
            m = owners[k] if k < len(owners) else None
            if m is not None and protected(m):
                out.setdefault(m, {"units": [], "points": []})["units"].append(k)
        for p in points:
            if i1 <= p["pos"] <= i2 and protected(p["id"]):
                d = out.setdefault(p["id"], {"units": [], "points": []})
                if p not in d["points"]:
                    d["points"].append(p)
    return out


def ranges(positions: Iterable[int]) -> list[tuple[int, int]]:
    """位置（从 0 开始）合并成连续的段，返回从 1 开始、两端都含的 (起, 止)。"""
    out: list[tuple[int, int]] = []
    for k in sorted(set(positions)):
        if out and out[-1][1] == k:
            out[-1] = (out[-1][0], k + 1)
        else:
            out.append((k + 1, k + 1))
    return out


def rle(owners: list[Optional[str]]) -> list[list]:
    """存盘用：[[标记, 连续几个], ...]。"""
    out: list[list] = []
    for m in owners:
        if out and out[-1][0] == m:
            out[-1][1] += 1
        else:
            out.append([m, 1])
    return out


def unrle(runs) -> list[Optional[str]]:
    out: list[Optional[str]] = []
    for m, n in runs or ():
        out.extend([m] * int(n))
    return out


def equal_spans(ops: list[Opcode]) -> list[tuple[int, int]]:
    """对应关系里没变的部分，在新内容里的坐标 [(j1, j2)]。"""
    return [(j1, j2) for tag, _i1, _i2, j1, j2 in ops if tag == "equal"]


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------
SECTIONS = ("containers", "marks", "agents", "permits", "notes")
LOG_KEEP = 200
FORGET_AFTER = 86400.0                     # 一天没动静的 AI、一天前的未结算执行：不再留


def empty_state() -> dict:
    return {"n": 0, "containers": {}, "marks": {}, "agents": {}, "permits": {}, "notes": {}, "log": []}


class PositionalLedger:
    """按位置的容器的账本：每个容器的标记和墓碑、每个 AI 看过的版本、还没结算的许可单、要告诉 AI 的话。

    容器的记录 {"version", "owners", "points", ...}（其余字段是适配侧的，例如内容的指纹）；
    标记 {"author", "container", "version", "time", "released"}；released=True 是人交还时打的标记（已经交还）。
    状态是一个普通的 dict（state），能整份存成 JSON；改了哪些记录记在 _dirty 里，存盘时只追加这些（store.AppendLog）。"""

    def __init__(self, state: Optional[dict] = None) -> None:
        self.state = state if state is not None else empty_state()
        for k, v in empty_state().items():
            self.state.setdefault(k, v)
        self._dirty: dict[str, set] = {s: set() for s in SECTIONS}
        self._log_saved = len(self.state["log"])

    # ---- 记录
    @property
    def containers(self) -> dict:
        return self.state["containers"]

    @property
    def marks(self) -> dict:
        return self.state["marks"]

    @property
    def agents(self) -> dict:
        return self.state["agents"]

    @property
    def permits(self) -> dict:
        return self.state["permits"]

    def touch(self, section: str, key: str) -> None:
        self._dirty[section].add(key)

    def put_container(self, cid: str, entry: dict) -> None:
        self.containers[cid] = entry
        self.touch("containers", cid)

    def update_container(self, cid: str, **fields) -> None:
        self.containers[cid].update(fields)
        self.touch("containers", cid)

    def log(self, kind: str, **kw) -> None:
        self.state["log"].append({"time": round(time.time(), 3), "kind": kind, **kw})
        if len(self.state["log"]) > LOG_KEEP:
            drop = len(self.state["log"]) - LOG_KEEP
            del self.state["log"][:drop]
            self._log_saved = max(0, self._log_saved - drop)

    def new_mark(self, author: str, container: str, version: int, **extra) -> str:
        self.state["n"] += 1
        mid = f"m{self.state['n']}"
        self.marks[mid] = {"author": author, "path": container, "version": version, "time": round(time.time(), 3),
                           "released": False, **extra}
        self.touch("marks", mid)
        return mid

    def owners(self, cid: str, n: int) -> list[Optional[str]]:
        """每个位置的标记，长度正好是 n（没记过的是基线：None）。"""
        owners = unrle((self.containers.get(cid) or {}).get("owners"))
        return (owners + [None] * n)[:n]

    # ---- 判断（rules.judge，和按编号的容器同一张表）
    def last(self, mid: str) -> R.Last:
        meta = self.marks.get(mid) or {}
        author = meta.get("author")
        cat = R.category(author)
        held = author if cat in (R.HUMAN, R.EXTERNAL) and not meta.get("released") else None
        return R.Last(author=author, kind=None, stamp=meta.get("version", 0), held=held)

    def seen(self, agent: str, cid: str) -> int:
        return self.agents.get(agent, {}).get("seen", {}).get(cid, -1)

    def verdict(self, mid: Optional[str], agent: str, cid: str) -> Optional[dict]:
        """这个标记的单元（或墓碑），agent 能不能改：返回要保持的原因，None 表示能。"""
        if mid is None or mid not in self.marks:
            return None
        return R.judge(self.last(mid), agent, self.seen(agent, cid))

    def held(self, mid: Optional[str]) -> bool:
        """对所有 AI 都不能改（人或来源不明改的、还没交还）。"""
        return mid is not None and mid in self.marks and self.last(mid).held is not None

    def protected(self, mid: str, agent: Optional[str], cid: str) -> bool:
        if agent is None:
            return self.held(mid)
        return self.verdict(mid, agent, cid) is not None

    def hits(self, cid: str, ops: list[Opcode], agent: str, n_old: int, human_only: bool = False) -> dict[str, dict]:
        """agent 按对应关系 ops 改这个容器，会碰到哪些不能改的标记：{标记: {"units", "points"}}（现在的坐标）。
        human_only：只有人能改的容器（rules.locked）：任何改动都算碰到（调用方已经确认内容确实变了）。"""
        if human_only:
            units = [k for tag, i1, i2, _j1, _j2 in ops if tag != "equal" for k in range(i1, i2)]
            return {R.KEEP_LOCKED: {"units": units, "points": [] if units else [{"id": R.KEEP_LOCKED, "pos": 0, "count": 0}]}}
        if not changed(ops):
            return {}
        e = self.containers.get(cid) or {}
        return hits(self.owners(cid, n_old), e.get("points") or [], ops, lambda m: self.protected(m, agent, cid))

    def all_protected(self, cid: str, agent: str) -> dict[str, dict]:
        """这个容器里对这个 AI 不能改的全部（事先算不出结果的执行只能这样粗查）。"""
        e = self.containers.get(cid) or {}
        out: dict[str, dict] = {}
        for k, m in enumerate(unrle(e.get("owners"))):
            if m is not None and self.protected(m, agent, cid):
                out.setdefault(m, {"units": [], "points": []})["units"].append(k)
        for p in e.get("points") or ():
            if self.protected(p["id"], agent, cid):
                out.setdefault(p["id"], {"units": [], "points": []})["points"].append(p)
        return out

    def reason(self, mid: str) -> str:
        """说明用的原因类别：locked | human | unattributed | stale（只从账本来）。"""
        if mid == R.KEEP_LOCKED:
            return "locked"
        cat = R.category((self.marks.get(mid) or {}).get("author"))
        if cat == R.HUMAN:
            return "human"
        if cat == R.EXTERNAL:
            return "unattributed"
        return "stale"

    # ---- 记一次修改
    def record(self, cid: str, ops: list[Opcode], author: str, n_old: int, *, whole_deleted: bool = False,
               describe: Optional[Describe] = None, entry: Optional[dict] = None, **extra) -> str:
        """把容器 cid 的一次修改（对应关系 ops，作者 author）记进账本，返回这次的标记。
        whole_deleted：整个容器被删掉了，留一个容器的墓碑。entry：适配侧要一起记的字段（例如内容的指纹）。"""
        e = self.containers.get(cid) or {}
        version = e.get("version", 0) + 1
        mark = self.new_mark(author, cid, version, **extra)
        owners, points = apply(self.owners(cid, n_old), e.get("points") or [], ops, mark, describe)
        if whole_deleted:
            owners, points = [], [p for p in points if p["id"] != mark] + [
                {"id": mark, "pos": 0, "count": n_old, "file": True, "text": describe(0, min(3, n_old)) if describe else []}]
        self.put_container(cid, {**(entry or {}), "version": version, "owners": rle(owners), "points": points})
        self.log("change", path=cid, author=author, mark=mark)
        return mark

    def baseline(self, cid: str, entry: dict) -> None:
        """第一次看到时的样子：没有作者（基线，不保护）。"""
        self.put_container(cid, {**entry, "version": 0, "owners": [], "points": []})

    # ---- 交还（人的决定）
    def hand_back(self, cid: Optional[str] = None, first: Optional[int] = None,
                  last: Optional[int] = None) -> list[dict]:
        """人交还：cid 为 None 时全部；只给 cid 时整个容器；给了位置（从 1 开始、两端都含）时只交还这几个。
        交还的单元打上一个"人交还了"的标记（released）：之后任何 AI 都能改（判断表第一行），说明里仍知道是人改的。
        返回 [{"path", "units": [(起, 止)], "points": n}]。"""
        out = []
        for p, e in sorted(self.containers.items()):
            if cid is not None and p != cid:
                continue
            owners = unrle(e.get("owners"))
            done, pts = [], 0
            lo = (first - 1) if first else 0
            hi = last if last else len(owners)
            new_mark = None
            for k in range(lo, min(hi, len(owners))):
                m = owners[k]
                if m is not None and self.held(m):
                    if new_mark is None:
                        new_mark = self.new_mark(R.HUMAN, p, e.get("version", 0), released=True)
                    owners[k] = new_mark
                    done.append(k)
            keep = []
            for pt in e.get("points") or []:
                if self.held(pt["id"]) and (first is None or lo <= pt["pos"] <= hi):
                    pts += 1
                    continue
                keep.append(pt)
            if done or pts:
                self.update_container(p, owners=rle(owners), points=keep)
                out.append({"path": p, "units": ranges(done), "points": pts})
        if out:
            self.log("release", items=[o["path"] for o in out])
        return out

    def settle_to_baseline(self, cid: str, n: int, positions: Iterable[int],
                           points_gone: Callable[[dict], bool]) -> bool:
        """人提交（commit）之后：提交进去的单元（和提交的版本一样的位置）、已经提交的删除，不再保护，变成基线。
        n：容器现在有几个单元；positions：和提交的版本一样的位置；points_gone(墓碑)：这个删除已经提交了。返回有没有变化。"""
        e = self.containers.get(cid) or {}
        owners = self.owners(cid, n)
        hit = False
        for k in positions:
            if k < len(owners) and owners[k] is not None and self.held(owners[k]):
                owners[k] = None
                hit = True
        keep = []
        for p in e.get("points") or []:
            if self.held(p["id"]) and points_gone(p):
                hit = True
                continue
            keep.append(p)
        if hit:
            self.update_container(cid, owners=rle(owners), points=keep)
        return hit

    def drop_points(self, cid: str) -> None:
        self.update_container(cid, points=[])

    def has_held(self, cid: str) -> bool:
        e = self.containers.get(cid) or {}
        return any(m is not None and self.held(m) for m in unrle(e.get("owners"))) or \
            any(self.held(p["id"]) for p in e.get("points") or ())

    # ---- 现在受保护的、AI 没看过的
    def protected_regions(self, agent: Optional[str] = None) -> dict[str, list[dict]]:
        """现在受保护的部分：{容器: [{"mark", "author", "units": [(起, 止)], "points": [...]}]}。
        agent 为 None 时只列对所有 AI 都受保护的（人和来源不明的）。"""
        out: dict[str, list[dict]] = {}
        for cid, e in sorted(self.containers.items()):
            owners = unrle(e.get("owners"))
            by: dict[str, dict] = {}
            for k, m in enumerate(owners):
                if m is not None and self.protected(m, agent, cid):
                    by.setdefault(m, {"units": [], "points": []})["units"].append(k)
            for p in e.get("points") or ():
                if self.protected(p["id"], agent, cid):
                    by.setdefault(p["id"], {"units": [], "points": []})["points"].append(p)
            for m, d in sorted(by.items(), key=lambda kv: (kv[1]["units"] or [p["pos"] for p in kv[1]["points"]])[0]):
                out.setdefault(cid, []).append({"mark": m, "author": self.marks[m]["author"],
                                                "units": ranges(d["units"]), "points": d["points"]})
        return out

    def unseen(self, agent: str, cid: Optional[str] = None, untold: bool = False) -> dict[str, list[dict]]:
        """别人（人、别的 AI、git）在 agent 上次看之后改的、现在还在的部分：{容器: [区域]}。untold=True 时只要还没告诉过它的。"""
        a = self.agent(agent)
        told = set(a.get("told") or ())
        out: dict[str, list[dict]] = {}
        for p, e in sorted(self.containers.items()):
            if cid is not None and p != cid:
                continue
            seen = a["seen"].get(p, -1)
            owners = unrle(e.get("owners"))
            by: dict[str, dict] = {}

            def fresh(m: str) -> bool:
                meta = self.marks.get(m) or {}
                return (meta.get("author") not in (None, agent) and meta.get("version", 0) > seen
                        and not (untold and m in told))
            for k, m in enumerate(owners):
                if m is not None and fresh(m):
                    by.setdefault(m, {"units": [], "points": []})["units"].append(k)
            for pt in e.get("points") or ():
                if fresh(pt["id"]):
                    by.setdefault(pt["id"], {"units": [], "points": []})["points"].append(pt)
            for m, d in by.items():
                out.setdefault(p, []).append({"mark": m, "author": self.marks[m]["author"],
                                              "units": ranges(d["units"]), "points": d["points"],
                                              "protected": self.protected(m, agent, p)})
        return out

    # ---- AI
    def agent(self, agent: str, name: Optional[str] = None) -> dict:
        a = self.agents.setdefault(agent, {"name": name or agent, "seen": {}, "told": [], "refusals": {},
                                           "since": round(time.time(), 3)})
        if name:
            a["name"] = name
        a["last"] = round(time.time(), 3)
        self.touch("agents", agent)
        return a

    def see(self, agent: str, cid: str) -> None:
        """agent 看了这个容器：之前别人改的，它都看到了。"""
        e = self.containers.get(cid)
        if e is not None:
            self.agent(agent)["seen"][cid] = e.get("version", 0)

    def tell(self, agent: str, regions: dict[str, list[dict]]) -> None:
        a = self.agent(agent)
        a["told"] = sorted(set(a.get("told") or ()) | {r["mark"] for rs in regions.values() for r in rs})

    def note(self, agent: str, text: str) -> None:
        self.state["notes"].setdefault(agent, []).append(text)
        self.touch("notes", agent)

    def take_notes(self, agent: str) -> list[str]:
        if agent in self.state["notes"]:
            self.touch("notes", agent)
        return self.state["notes"].pop(agent, [])

    # ---- 许可单（lifecycle 里定义状态；这里只存还没结算的）
    def open_permit(self, pid: str, agent: str, kind: str, **extra) -> dict:
        rec = {"kind": kind, "agent": agent, "state": "dispatched", "time": round(time.time(), 3), **extra}
        self.permits[pid] = rec
        self.touch("permits", pid)
        return rec

    def end_permit(self, pid: str) -> Optional[dict]:
        """结束一张许可单：正好一次。已经结束（或者没有）的返回 None。"""
        rec = self.permits.pop(pid, None)
        if rec is not None:
            self.touch("permits", pid)
        return rec

    def open_scope(self, now: Optional[float] = None) -> tuple[bool, set[str]]:
        """现在还在执行、不能原子执行的：(有没有一条命令在执行（整个都说不准）, 正在被编辑的容器)。
        这期间看到的变化先不记在谁名下，等它结算（结果没收到的，变化一律说不准是谁的）。"""
        now = time.time() if now is None else now
        running = any(op.get("kind") == "command" and op.get("until", 0) > now for op in self.permits.values())
        busy = {op["path"] for op in self.permits.values() if op.get("kind") == "edit"}
        return running, busy

    def expired(self, now: Optional[float] = None, edit_after: float = 300.0) -> list[tuple[str, str]]:
        """没等到结束通知的执行：[(编号, "settle" | "lost")]。编辑过了一会儿还没结算的，照实际结果结算；
        命令过了它的时限还没结束通知的，这期间的变化说不准是谁的。"""
        now = time.time() if now is None else now
        out = []
        for pid, op in sorted(self.permits.items()):
            if op.get("kind") == "edit" and now - op.get("time", now) > edit_after:
                out.append((pid, "settle"))
            elif op.get("kind") == "command" and op.get("until", now) <= now:
                out.append((pid, "lost"))
        return out

    # ---- 整理（存盘前）
    def forget(self, now: Optional[float] = None) -> None:
        """别的 AI、git 留下的标记，所有（一天内活动过的）AI 都看过了，就不用再留（变成基线）；
        不再被任何单元、墓碑引用的标记删掉；一天没动静的 AI、一天前的未结算执行也删掉。"""
        now = time.time() if now is None else now
        for a in [a for a, v in self.agents.items() if now - v.get("last", now) > FORGET_AFTER]:
            del self.agents[a]
            self.touch("agents", a)
        for pid in [t for t, op in self.permits.items() if now - op.get("time", now) > FORGET_AFTER]:
            self.end_permit(pid)

        def settled(m: Optional[str], cid: str) -> bool:
            meta = self.marks.get(m or "") or {}
            if not m or R.category(meta.get("author")) not in (R.AGENT, R.GIT):
                return False
            last = self.last(m)
            return all(R.judge(last, name, v.get("seen", {}).get(cid, -1)) is None
                       for name, v in self.agents.items() if name != meta.get("author"))
        used = set()
        for cid, e in self.containers.items():
            owners = unrle(e.get("owners"))
            pts = e.get("points") or ()
            if any(settled(m, cid) for m in set(owners)) or any(settled(p["id"], cid) for p in pts):
                owners = [None if settled(m, cid) else m for m in owners]
                self.update_container(cid, owners=rle(owners), points=[p for p in pts if not settled(p["id"], cid)])
                pts = e["points"]
            used |= {m for m in owners if m is not None}
            used |= {p["id"] for p in pts}
        for m in [m for m in self.marks if m not in used]:
            del self.marks[m]
            self.touch("marks", m)
        for name, v in self.agents.items():
            told = [m for m in v.get("told") or () if m in self.marks]
            if told != list(v.get("told") or ()):
                v["told"] = told
                self.touch("agents", name)

    # ---- 存盘（store.AppendLog）：整份，或者这一步的变化
    def snapshot(self) -> dict:
        self.mark_saved()
        return self.state

    def mark_saved(self) -> None:
        for s in SECTIONS:
            self._dirty[s].clear()
        self._log_saved = len(self.state["log"])

    def delta(self, top: Iterable[str] = ()) -> dict:
        """上次存盘之后的变化：每个分区变了的记录（删掉的记 None）、新的日志、top 里列的顶层字段。"""
        d: dict = {}
        for s in SECTIONS:
            keys = self._dirty[s]
            if keys:
                sec = self.state[s]
                d[s] = {k: sec.get(k) for k in sorted(keys)}
        if self.state.get("n") is not None:
            d["n"] = self.state["n"]
        for k in top:
            d[k] = self.state.get(k)
        if len(self.state["log"]) > self._log_saved:
            d["log_add"] = self.state["log"][self._log_saved:]
        self.mark_saved()
        return d

    def apply_delta(self, d: dict) -> None:
        for s in SECTIONS:
            for k, v in (d.get(s) or {}).items():
                if v is None:
                    self.state[s].pop(k, None)
                else:
                    self.state[s][k] = v
        for k, v in d.items():
            if k not in SECTIONS and k not in ("t", "log_add"):
                self.state[k] = v
        if d.get("log_add"):
            self.state["log"].extend(d["log_add"])
            del self.state["log"][:-LOG_KEEP]
        self.mark_saved()

    def dirty(self) -> bool:
        return any(self._dirty[s] for s in SECTIONS) or len(self.state["log"]) > self._log_saved
