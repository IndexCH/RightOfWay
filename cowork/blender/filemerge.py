"""方式二：人和 AI 各改一份，存盘后按对象合并。

适用于 AI 只能用 computer use 操作、应用没有任何接口的情况：一块屏幕只有一个鼠标，
AI 必须在自己的屏幕上打开自己的一份。只要文件能被解析成对象，就能按对象合并。

文件布局（以 scene.blend 为例）：
    scene.blend                       人那一份（人在 Blender 里打开它）
    .cowork/ai/scene.blend            AI 那一份（AI 在自己的屏幕上打开它）
    .cowork/history/v0000.blend ...   每次合并的结果

合并规则（和运行时规则一致），以"单元"为单位——按对象（granularity="object"），或按对象的面（"aspect"，默认）：
    - 人的修改总是保留（R5）。人改过的单元，AI 的版本不采用；运行时策略为"留作候选"时把 AI 的整个对象放进候选集合（R6）。
    - 只有 AI 改了的单元，用 AI 的版本。按面合并时，人挪了叶子、AI 改了叶子的材质，两边都保留。
    - 两边都没改的单元，用上一版合并结果。
    - 删除按整个对象算：人删了的对象，AI 对它的修改都不采用；AI 删了的对象，只要人改过它的任何部分就不删。
    - 谁的文件是"旧的"（没有重新打开就存盘）由 infer_base() 推断，旧文件里没改的部分不会覆盖新内容。
      这就是 WeaveBench 里"旧副本覆盖新文件"的问题（R21）。

规划部分（infer_base、plan_merge）是纯 Python，不依赖 Blender；真正改文件的部分交给 blender_side.merge_file。
"""
from __future__ import annotations

import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..model import ActorKind, CapabilityMeta, Operation, OpStatus, Target
from ..runtime import POLICY_CANDIDATE, Runtime
from .records import (ASPECT, DELETED, OBJECT, Diff, Records, diff_records, is_deleted, label, split_unit,
                      units)

OBJECT_TYPE = "blender.object"
CAPABILITY = "blender.computer_use"
BASELINE_ACTOR = "baseline"


# ---------------------------------------------------------------------------
# 纯 Python：推断基准、规划合并
# ---------------------------------------------------------------------------
@dataclass
class MergeVersion:
    index: int
    path: str
    records: Records
    from_human: set[str] = field(default_factory=set)   # 这一版里来自人的修改（单元编号）
    from_ai: set[str] = field(default_factory=set)      # 这一版里来自 AI 的修改（单元编号）


def infer_base(history: list[MergeVersion], recs: Records, other: str, granularity: str = OBJECT) -> int:
    """推断一份文件是从第几版合并结果打开的。

    判断依据：第 k 版里"对方"带来的修改，这份文件里有没有。有，说明打开过第 k 版。
    第 k 版里没有对方的修改时，它和上一版对这一方来说没区别，基准可以顺延。
    other = "ai"  用于推断人那一份；other = "human" 用于推断 AI 那一份。
    """
    mine = units(recs, granularity)
    best = 0
    for v in history[1:]:
        ids = v.from_ai if other == "ai" else v.from_human
        now_u, old_u = units(v.records, granularity), units(history[v.index - 1].records, granularity)
        evidence = False
        for uid in ids:
            now, have, old = now_u.get(uid), mine.get(uid), old_u.get(uid)
            if now is None:
                evidence = evidence or (have is None and old is not None)
            elif have is not None and have["cfp"] == now["cfp"] and (old is None or old["cfp"] != now["cfp"]):
                evidence = True
        if evidence:
            best = v.index
        elif not ids and best == v.index - 1:
            best = v.index
    return best


@dataclass
class MergePlan:
    result: dict[str, str] = field(default_factory=dict)      # 单元 → human | ai | current | delete
    take: list[dict] = field(default_factory=list)            # {id, source(ai|current), name, collections, candidate, aspects, record}
    delete: list[str] = field(default_factory=list)           # 要从人那一份里删掉的对象
    rejected: list[str] = field(default_factory=list)         # AI 改了、但没采用的单元
    candidates: list[str] = field(default_factory=list)       # 留作候选的对象
    expected: dict[str, Optional[dict]] = field(default_factory=dict)  # 合并后每个单元应该是什么样


def plan_merge(current: Records, human: Records, ai: Records, h_diff: Diff, a_diff: Diff,
               protected: set[str], keep_candidates: bool, granularity: str = OBJECT) -> MergePlan:
    """current：上一版合并结果；human / ai：两份文件现在的对象记录；
    h_diff / a_diff：两边各自相对自己基准的修改（单元级）；
    protected：运行时里人碰过的单元（已经把这次人的修改算进去了）。"""
    cu, hu, au = units(current, granularity), units(human, granularity), units(ai, granularity)
    obj = lambda uid: split_unit(uid)[0]  # noqa: E731
    p = MergePlan()
    human_deleted = {obj(u) for u in h_diff.deleted if obj(u) not in human}
    ai_deleted = {obj(u) for u in a_diff.deleted if obj(u) not in ai}
    touched_objs = {obj(u) for u in protected | h_diff.ids}

    for uid in h_diff.ids:
        p.result[uid] = "human" if uid in hu else "delete"
    for uid in a_diff.ids:
        cid = obj(uid)
        conflict = (uid in h_diff.ids or uid in protected or cid in human_deleted
                    or (cid in ai_deleted and cid in touched_objs))
        if conflict:
            p.rejected.append(uid)
            if keep_candidates and cid in ai and cid not in p.candidates:
                p.candidates.append(cid)
                p.take.append({"id": cid, "source": "ai", "name": ai[cid]["name"],
                               "collections": ai[cid]["collections"], "candidate": True, "aspects": None,
                               "record": ai[cid]})
            continue
        p.result[uid] = "ai" if uid in au else "delete"
    for uid in set(cu) | set(hu):
        p.result.setdefault(uid, "current" if uid in cu else "delete")

    # 删除按整个对象算：一个对象只要还有单元要保留（人改过它），其余"删除"的单元也按人那一份保留
    for cid in {obj(u) for u in p.result}:
        us = [u for u in p.result if obj(u) == cid]
        if any(p.result[u] != "delete" for u in us) and cid in human:
            for u in us:
                if p.result[u] == "delete" and u in hu:
                    p.result[u] = "human"

    source_units = {"human": hu, "ai": au, "current": cu}
    source_recs = {"ai": ai, "current": current}
    by_obj: dict[str, list[str]] = {}
    for uid in p.result:
        by_obj.setdefault(obj(uid), []).append(uid)
    for cid, uids in sorted(by_obj.items()):
        if all(p.result[u] == "delete" for u in uids):
            for u in uids:
                p.expected[u] = None
            if cid in human:
                p.delete.append(cid)
            continue
        for u in uids:
            r = p.result[u]
            p.expected[u] = source_units[r].get(u) if r != "delete" else None

        def entry(src: str, aspects):
            rec = source_recs[src][cid]
            return {"id": cid, "source": src, "name": rec["name"], "collections": rec["collections"],
                    "candidate": False, "aspects": aspects, "record": rec}

        def whole_ok(src: str) -> bool:
            su = source_units[src]
            return all(p.expected[u] is not None and su.get(u) is not None and su[u]["cfp"] == p.expected[u]["cfp"]
                       for u in uids)

        if cid not in human:
            # 人那一份里没有：AI 新建的，或者人的文件太旧、还没有上一版里的这个对象
            base = "current" if (cid in current and any(p.result[u] == "current" for u in uids)) else "ai"
            p.take.append(entry(base, None))
            other = "ai" if base == "current" else None
            if other:
                asp = [split_unit(u)[1] for u in uids if p.result[u] == other
                       and au.get(u) and cu.get(u) and au[u]["cfp"] != cu[u]["cfp"]]
                if asp:
                    p.take.append(entry(other, asp))
            continue
        for src in ("current", "ai"):
            su = source_units[src]
            need = [u for u in uids if p.result[u] == src and su.get(u) is not None
                    and (hu.get(u) is None or su[u]["cfp"] != hu[u]["cfp"])]
            if not need:
                continue
            if granularity == OBJECT or whole_ok(src):
                p.take.append(entry(src, None))
            else:
                p.take.append(entry(src, [split_unit(u)[1] for u in need]))
    return p


def check_result(expected: dict[str, Optional[dict]], final_units: dict[str, dict],
                 labels: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """对照计划检查合并结果。返回（错误，只是名字变了的对象）。"""
    errors, renamed = [], []
    for uid, exp in expected.items():
        got = final_units.get(uid)
        aspect = split_unit(uid)[1]
        label_ = exp["name"] if exp else (got or {}).get("name", uid)
        if aspect:
            label_ = f"{label_} 的{label(aspect, labels)}"
        if exp is None:
            if got is not None:
                errors.append(f"{label_} 应该被删除，但还在")
        elif got is None:
            errors.append(f"{label_} 应该存在，但没有了")
        elif got["cfp"] != exp["cfp"]:
            if aspect == "name":
                renamed.append(f"{exp['name']} → {got['name']}")
            else:
                errors.append(f"{label_} 和预期不同")
        elif aspect is None and got["name"] != exp["name"]:
            renamed.append(f"{exp['name']} → {got['name']}")
    extra = sorted({got["name"] for uid, got in final_units.items() if uid not in expected})
    errors += [f"多出了计划外的对象 {n}" for n in extra]
    return errors, renamed


# ---------------------------------------------------------------------------
# 会话：管理文件、调用 Blender、把结果记进运行时
# ---------------------------------------------------------------------------
@dataclass
class MergeReport:
    version: int
    human_base: int
    ai_base: int
    human_changes: Diff
    ai_changes: Diff
    human_base_from: str = "推断"                              # 起点从哪来：文件里的编号，或者推断
    ai_base_from: str = "推断"
    applied_ai: list[str] = field(default_factory=list)       # 采用了 AI 版本的对象名
    rejected: list[str] = field(default_factory=list)         # AI 改了但没采用的对象名
    candidates: list[str] = field(default_factory=list)
    partial: dict[str, dict] = field(default_factory=dict)    # 部分采用的对象：名字 → {kept: [面], restored: [面]}
    overridden_ai: list[str] = field(default_factory=list)    # 人在旧版本上改的部分，AI 之后也改过：按人的版本（R5）
    applied_units: int = 0
    rejected_units: int = 0
    errors: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    human_written: bool = False
    ai_written: bool = False
    seconds: float = 0.0
    text_for_ai: str = ""
    text_for_human: str = ""
    raw_human: Records = field(default_factory=dict, repr=False)   # 合并前人那一份的内容
    raw_ai: Records = field(default_factory=dict, repr=False)      # 合并前 AI 那一份的内容
    final: Records = field(default_factory=dict, repr=False)       # 合并结果

    @property
    def human_stale(self) -> bool:
        return self.human_base < self.version - 1

    @property
    def ai_stale(self) -> bool:
        return self.ai_base < self.version - 1


def _mtime(path: Path) -> float:
    return path.stat().st_mtime_ns if path.exists() else 0


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".cowork-tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


class FileMergeSession:
    def __init__(self, runtime: Runtime, runner, human_path: str | Path, work_dir: str | Path | None = None,
                 human: str = "yuan", agent: str = "agent", scene: Optional[str] = None,
                 granularity: str = ASPECT) -> None:
        assert granularity in (ASPECT, OBJECT)
        self.rt, self.runner = runtime, runner
        self.granularity = granularity
        self.human_path = Path(human_path).resolve()
        self.work = Path(work_dir).resolve() if work_dir else self.human_path.parent / ".cowork"
        self.ai_path = self.work / "ai" / self.human_path.name
        self.human, self.agent, self.scene = human, agent, scene
        self.history: list[MergeVersion] = []
        self.human_deleted: dict[str, str] = {}
        self._n_op = 0
        self._mtimes: dict[str, float] = {}
        self.labels: dict[str, str] = {}
        self.session_id = uuid.uuid4().hex[:8]         # 写进文件的起点编号带上它，别的会话留下的编号不会被误用
        for actor, kind in ((human, ActorKind.HUMAN), (agent, ActorKind.AGENT), (BASELINE_ACTOR, ActorKind.AGENT)):
            if actor not in runtime.actors:
                runtime.register_actor(actor, kind)
        if CAPABILITY not in runtime.capabilities:
            runtime.register_capability(CapabilityMeta(CAPABILITY, effect_level=1, cancellable=False))

    def _args(self, **kw) -> dict:
        if self.scene:
            kw["scene"] = self.scene
        return kw

    def _stamp(self, index: int) -> str:
        return f"{self.session_id}:{index}"

    def _base(self, stamp: Optional[str], recs: Records, other: str) -> tuple[int, str]:
        """起点：文件里有这个会话写的编号就用编号；没有（格式放不了、被别的程序去掉了）再推断。"""
        if isinstance(stamp, str) and stamp.startswith(self.session_id + ":"):
            try:
                k = int(stamp.split(":", 1)[1])
                if 0 <= k < len(self.history):
                    return k, "编号"
            except ValueError:
                pass
        return infer_base(self.history, recs, other=other, granularity=self.granularity), "推断"

    def _history_path(self, index: int) -> Path:
        return self.work / "history" / f"v{index:04d}.blend"

    # ------------------------------------------------------------------
    def start(self) -> Records:
        """给人那一份里的所有对象盖章，存为第 0 版，并复制一份给 AI。"""
        (self.work / "history").mkdir(parents=True, exist_ok=True)
        if not self.human_path.exists():
            self.runner.call("new_file", {"path": str(self.human_path)})
        v0 = self._history_path(0)
        res = self.runner.call("merge_file", self._args(base=str(self.human_path), out=str(v0),
                                                        take=[], delete=[], prefix="h-", stamp=self._stamp(0)))
        self.labels = res.get("labels", {})
        _atomic_copy(v0, self.human_path)
        _atomic_copy(v0, self.ai_path)
        self.history = [MergeVersion(0, str(v0), res["records"])]
        for uid, content in units(res["records"], self.granularity).items():
            if uid not in self.rt.objects:
                self.rt.create_object(uid, OBJECT_TYPE, content, BASELINE_ACTOR)
        self._remember_mtimes()
        return res["records"]

    def _remember_mtimes(self) -> None:
        self._mtimes = {"human": _mtime(self.human_path), "ai": _mtime(self.ai_path)}

    def changed_since_last_sync(self) -> bool:
        return (_mtime(self.human_path) != self._mtimes.get("human")
                or _mtime(self.ai_path) != self._mtimes.get("ai"))

    # ------------------------------------------------------------------
    def sync(self) -> MergeReport:
        """合并一次。任意一方存盘后调用（或者由 watch() 在检测到存盘时自动调用）。"""
        t0 = time.perf_counter()
        snap_dir = self.work / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        h_mtime, a_mtime = _mtime(self.human_path), _mtime(self.ai_path)
        h_snap, a_snap = snap_dir / "human.blend", snap_dir / "ai.blend"
        shutil.copyfile(self.human_path, h_snap)          # 先拷一份，避免合并期间对方又存盘
        shutil.copyfile(self.ai_path, a_snap)
        h_res = self.runner.call("dump_file", self._args(path=str(h_snap), prefix="h-"))
        a_res = self.runner.call("dump_file", self._args(path=str(a_snap), prefix="a-"))
        h_recs, a_recs = h_res["records"], a_res["records"]

        g = self.granularity
        cur = self.history[-1]
        hb, hb_from = self._base(h_res.get("stamp"), h_recs, other="ai")
        ab, ab_from = self._base(a_res.get("stamp"), a_recs, other="human")
        h_diff = diff_records(self.history[hb].records, h_recs)                       # 对象级，用于说明
        a_diff = diff_records(self.history[ab].records, a_recs)
        h_udiff = diff_records(units(self.history[hb].records, g), units(h_recs, g))  # 单元级，用于规则
        a_udiff = diff_records(units(self.history[ab].records, g), units(a_recs, g))

        # 1. 人的修改先记进运行时（R5：总能提交）
        self._absorb_human(h_udiff, units(h_recs, g), h_diff)
        protected = {uid for uid, o in self.rt.objects.items() if o.human_touched is not None}
        keep = self.rt.human_touched_policy == POLICY_CANDIDATE

        # 2. 规划并执行合并
        plan = plan_merge(cur.records, h_recs, a_recs, h_udiff, a_udiff, protected, keep, g)
        sources = {"ai": str(a_snap), "current": cur.path}
        take = [{**t, "source": sources[t["source"]]} for t in plan.take]
        index = cur.index + 1
        out = self._history_path(index)
        res = self.runner.call("merge_file", self._args(base=str(h_snap), out=str(out), take=take,
                                                        delete=plan.delete, prefix="h-", stamp=self._stamp(index)))
        self.labels = res.get("labels", self.labels)
        errors, renamed = check_result(plan.expected, units(res["records"], g), self.labels)
        errors += [f"{e['id']} 的这些面没能按计划合并：{'、'.join(label(f, self.labels) for f in e['faces'])}"
                   for e in res.get("face_errors", [])]
        errors += [f"悬空引用：{n}" for n in res.get("dangling", [])]
        errors += [f"源文件里找不到：{n}" for n in res.get("missing", [])]

        # 3. AI 的修改记进运行时：采用的提交，没采用的按策略作废或留作候选
        applied_units = self._absorb_ai(a_udiff, units(a_recs, g), plan)

        ai_after_base = set().union(*[v.from_ai for v in self.history[hb + 1:]])
        cur_u = units(cur.records, g)
        overridden = sorted({self._label(u, cur_u[u]["name"], self.labels) for u in h_udiff.ids & ai_after_base if u in cur_u})
        from_ai = {u for u, r in plan.result.items() if r == "ai"}
        from_human = {u for u, r in plan.result.items() if r == "human"}
        self.history.append(MergeVersion(index, str(out), res["records"], from_human, from_ai))

        # 4. 写回两份文件；如果合并期间某一方又存了盘，这一方先不写，下次同步再合并
        human_written = _mtime(self.human_path) == h_mtime
        ai_written = _mtime(self.ai_path) == a_mtime
        if human_written:
            _atomic_copy(out, self.human_path)
        if ai_written:
            _atomic_copy(out, self.ai_path)
        self._remember_mtimes()

        obj_name = lambda cid: (a_recs.get(cid) or h_recs.get(cid) or cur.records.get(cid) or {}).get("name", cid)  # noqa: E731
        applied_objs = {split_unit(u)[0] for u in applied_units}
        rejected_objs = {split_unit(u)[0] for u in plan.rejected}
        partial = {}
        if g == ASPECT:
            for cid in applied_objs & rejected_objs:
                partial[obj_name(cid)] = {
                    "kept": sorted(split_unit(u)[1] for u in applied_units if split_unit(u)[0] == cid),
                    "restored": sorted(split_unit(u)[1] for u in plan.rejected if split_unit(u)[0] == cid)}
        rep = MergeReport(
            version=index, human_base=hb, ai_base=ab, human_changes=h_diff, ai_changes=a_diff,
            human_base_from=hb_from, ai_base_from=ab_from,
            applied_ai=sorted(obj_name(c) for c in applied_objs - rejected_objs),
            rejected=sorted(obj_name(c) for c in rejected_objs - applied_objs), partial=partial,
            candidates=sorted(obj_name(c) for c in plan.candidates), overridden_ai=overridden,
            errors=errors, renamed=renamed, human_written=human_written, ai_written=ai_written,
            seconds=round(time.perf_counter() - t0, 3), applied_units=len(applied_units),
            rejected_units=len(plan.rejected), raw_human=h_recs, raw_ai=a_recs, final=res["records"])
        rep.text_for_ai, rep.text_for_human = self._describe(rep)
        return rep

    @staticmethod
    def _label(uid: str, name: str, labels=None) -> str:
        aspect = split_unit(uid)[1]
        return name if aspect is None else f"{name} 的{label(aspect, labels)}"

    def _absorb_human(self, d: Diff, recs: dict[str, dict], obj_diff: Diff) -> None:
        for uid in sorted(d.ids):
            new = recs.get(uid)
            obj = self.rt.objects.get(uid)
            if obj is None:
                if new is not None:
                    self.rt.create_object(uid, OBJECT_TYPE, new, self.human)
                continue
            cur = obj.content
            if new is None:
                if not is_deleted(cur):
                    name = d.deleted[uid]["name"] if uid in d.deleted else uid
                    self.rt.human_edit(self.human, uid, {**DELETED, "name": name}, occupy=False)
            elif is_deleted(cur) or cur.get("fp") != new["fp"]:
                # 存盘时才发现的修改：记为人碰过，但不产生占用（无法知道人是否还在改）
                self.rt.human_edit(self.human, uid, new, occupy=False)
        for cid, rec in obj_diff.deleted.items():
            self.human_deleted[cid] = rec["name"]

    def _absorb_ai(self, d: Diff, recs: dict[str, dict], plan: MergePlan) -> list[str]:
        applied = []
        rejected = set(plan.rejected)
        for uid in sorted(d.ids):
            new = recs.get(uid)
            name = (new or d.deleted.get(uid) or {}).get("name", uid)
            self._n_op += 1
            op_id = f"merge-op-{self._n_op}"
            if uid not in self.rt.objects:
                if new is None or uid in rejected:
                    continue
                op = Operation(op_id, self.agent, CAPABILITY, produces=[uid])
                self.rt.submit(op)
                self.rt.complete(op_id, produces={uid: new}, produce_types={uid: OBJECT_TYPE})
                applied.append(uid)
                continue
            obj = self.rt.objects[uid]
            op = Operation(op_id, self.agent, CAPABILITY, targets=[Target(uid, obj.version)])
            r = self.rt.submit(op)
            if r.status.is_rejection:
                continue
            if uid in rejected:
                # 规划认为有冲突（例如人删了这个对象、或 AI 删了人改过的对象），运行时也要作废
                r = self.rt.complete(op_id, failed=True)
                continue
            r = self.rt.complete(op_id, writes={uid: new if new is not None else {**DELETED, "name": name}})
            if r.status in (OpStatus.COMMITTED, OpStatus.MERGED):
                applied.append(uid)
        return applied

    def _describe(self, rep: MergeReport) -> tuple[str, str]:
        ai = [f"合并完成（第 {rep.version} 版，用时 {rep.seconds:.1f} 秒）。",
              "你的修改已生效：" + ("、".join(rep.applied_ai) if rep.applied_ai else "没有")]
        if rep.partial:
            ai.append("部分采用：" + "；".join(
                f"{n}（{'、'.join(label(a, self.labels) for a in p['kept'])} 已生效；{'、'.join(label(a, self.labels) for a in p['restored'])} 人改过，保留人的）"
                for n, p in sorted(rep.partial.items())))
        if rep.rejected:
            ai.append("没有采用（人改过这些对象，请不要再改）：" + "、".join(rep.rejected))
        if rep.human_changes:
            ai.append("人的修改：" + "；".join(rep.human_changes.describe()))
        if rep.ai_written:
            ai.append("你那一份文件已更新为合并结果，请在你的 Blender 里 文件 → 恢复（Revert）后再继续。")
        human = [f"已合并（第 {rep.version} 版）。"]
        if rep.human_changes:
            human.append("你的修改：" + "；".join(rep.human_changes.describe()))
        if rep.applied_ai:
            human.append("AI 的修改：" + "、".join(rep.applied_ai))
        if rep.partial:
            human.append("两边都改了、但改的是不同部分，都保留：" + "；".join(
                f"{n}（AI 的{'、'.join(label(a, self.labels) for a in p['kept'])}，你的{'、'.join(label(a, self.labels) for a in p['restored'])}）"
                for n, p in sorted(rep.partial.items())))
        if rep.rejected:
            human.append("你改过、所以没采用 AI 版本的：" + "、".join(rep.rejected)
                         + ("（AI 的版本放在 Cowork_AI候选 集合里）" if rep.candidates else ""))
        if rep.human_stale:
            human.append("你存盘的是较早的版本，里面没改过的部分没有覆盖 AI 的新内容。")
        if rep.overridden_ai:
            human.append("你在旧版本上改的 " + "、".join(rep.overridden_ai)
                         + " 之前也被 AI 改过，现在按你的版本（AI 的那次修改被替换掉了）。")
        if rep.human_written:
            human.append("在 Blender 里 文件 → 恢复（Revert）就能看到合并结果。")
        else:
            human.append("合并期间你又存了盘，这次先不写回，下次同步再合并。")
        if rep.errors:
            human.append("合并检查发现问题：" + "；".join(rep.errors))
        return "\n".join(ai), "\n".join(human)

    # ------------------------------------------------------------------
    def watch(self, interval: float = 1.0, stop=lambda: False, on_report=print) -> None:
        """一直运行，发现任意一方存盘就合并。stop() 返回 True 时退出。"""
        while not stop():
            if self.changed_since_last_sync():
                time.sleep(0.5)          # 等存盘写完
                on_report(self.sync())
            time.sleep(interval)
