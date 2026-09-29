"""方式一：人和 AI 改同一份（同一个打开着的 Blender），实时。

AI 通过现成的 Blender MCP 执行脚本。这个类站在 AI 和 Blender 之间（也就是 harness 的位置）：
AI 的每段脚本都交给 run_agent()，由它原子地完成"列对象 → 保护 → 执行 → 比较 → 恢复"，
再把结果记进运行时。人在 Blender 界面里的修改由 poll() 定时发现。

两种"上次看到的场景"分开记：
- 运行时上次看到的（self.view）：每次轮询都更新，用来判断哪些变化是人做的。
- 每个 AI 上次看到的（self.agent_views）：只在这个 AI 执行完、或者它读场景（observe）时更新。
  它错过的变化（人做的、别的 AI 做的）会连同具体的值一起告诉它；
  它按旧印象改到了别人后来改过的面，这部分恢复成别人的版本（后到的让先到的，人永远优先）。

多个 AI：用 add_agent() 登记，run_agent(..., agent=名字)。某个 AI 因为别的 AI 刚改过而被拒时，
这些面给它预留一小段时间（reserve_seconds），期间别的 AI 不能改，避免两个 AI 来回互相作废。

选项 occupy_selection：人选中的对象也算"正在改"，AI 不能动，取消选中就解除。

粒度（granularity）：
- "aspect"（默认）：每个 Blender 对象拆成几个面（位置、材质、网格……），运行时按面记录谁改过。
- "object"：整个 Blender 对象算一个。

应用和它的 MCP 插件都不用改：用到的只有插件已有的"执行代码"命令。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from ..model import ActorKind, CapabilityMeta, Operation, OpStatus, Target
from ..runtime import POLICY_CANDIDATE, Runtime
from .changes import Values, describe_changes
from .records import (ASPECT, DELETED, OBJECT, Diff, Records, base_name, diff_records, is_deleted, label,
                      split_unit, units)

OBJECT_TYPE = "blender.object"
CAPABILITY = "blender.execute_code"
BASELINE_ACTOR = "baseline"     # 开始协作前场景里已有的内容，记在这个名下（不算"人碰过"）

# AI 的修改没生效的原因
R_HUMAN = "human"               # 人改过，或者人正在改（占用）
R_SELECTED = "selected"         # 人正选中着（"选中即占用"选项）
R_OTHER_AI = "other_agent"      # 别的 AI 在这个 AI 上次看之后改过（后到的让先到的）
R_RESERVED = "reserved"         # 别的 AI 刚被拒、正在重试，暂时留给它
R_OTHER = "other"


@dataclass
class AgentView:
    """某个 AI 上次看到的场景。"""
    recs: Records
    values: Values
    versions: dict[str, int]
    time: float


@dataclass
class ObserveResult:
    lines: list[str]            # 它错过的变化（带具体的值）
    diff: Diff                  # 同样的内容，结构化的
    text: str


@dataclass
class AgentReport:
    status: str                                   # ok | deferred
    agent: str = ""
    step_id: Optional[str] = None
    committed: list[str] = field(default_factory=list)          # 生效的单元（对象，或对象的某个面）
    skipped: list[dict] = field(default_factory=list)            # 没生效的单元：[{id, name, reason, by?}]
    partial: dict[str, dict] = field(default_factory=dict)       # 部分生效的对象：名字 → {kept: [面], restored: [面]}
    missed: list[str] = field(default_factory=list)              # 这个 AI 上次看之后、这次执行之前别人做的修改
    missed_diff: Diff = field(default_factory=Diff)
    human_since: Diff = field(default_factory=Diff)              # 运行时上次轮询之后、执行之前人做的修改
    reserved: list[str] = field(default_factory=list)            # 这次给这个 AI 预留的单元
    resurrections: list[str] = field(default_factory=list)       # AI 新建的对象和人删掉的对象同名
    outside: dict = field(default_factory=dict)                  # AI 改到了追踪范围以外的对象
    inexact: list[str] = field(default_factory=list)             # 恢复后内容和人的版本不一致
    renamed: list[str] = field(default_factory=list)
    fallback: list = field(default_factory=list)                 # 按面恢复失败、退回整个换回的对象
    dangling: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    error: Optional[str] = None
    stdout: str = ""
    seconds: float = 0.0
    undo_pushed: bool = False
    protected: bool = True                        # False 表示对照组：没有做保护
    text: str = ""                                # 回给 AI 的说明


class SharedSession:
    def __init__(self, runtime: Runtime, bridge, human: str = "yuan", agent: str = "agent",
                 scene: Optional[str] = None, initial_owner: str = "open", granularity: str = ASPECT,
                 occupy_selection: bool = False, reserve_seconds: float = 30.0, prefix: str = "h-") -> None:
        """initial_owner：开始时场景里已有的对象算谁的。
        'open'（默认）表示 AI 可以改；'human' 表示全部视为人碰过，AI 不能改，直到人交还。"""
        assert granularity in (ASPECT, OBJECT)
        self.rt, self.bridge = runtime, bridge
        self.human, self.agent, self.scene = human, agent, scene
        self.initial_owner, self.granularity = initial_owner, granularity
        self.occupy_selection, self.reserve_seconds, self.prefix = occupy_selection, reserve_seconds, prefix
        for actor, kind in ((human, ActorKind.HUMAN), (BASELINE_ACTOR, ActorKind.AGENT)):
            if actor not in runtime.actors:
                runtime.register_actor(actor, kind)
        if CAPABILITY not in runtime.capabilities:
            # 执行脚本属于可回退的修改（等级 1）；Blender 里的脚本执行不能中途取消
            runtime.register_capability(CapabilityMeta(CAPABILITY, effect_level=1, cancellable=False))
        self.view: Records = {}                    # 运行时上次看到的场景
        self.values: Values = {}                   # 每个面现在的值（文字），增量更新
        self.selected: set[str] = set()            # 人现在选中的对象
        self.agent_views: dict[str, AgentView] = {}
        self.agent_names: dict[str, str] = {}
        self.reservations: dict[str, tuple[str, float]] = {}   # 单元 → (预留给哪个 AI, 到期时间)
        self.human_deleted: dict[str, str] = {}    # 人删掉的对象：编号 → 名字
        self.detections: list[dict] = []           # 每次发现人的修改：时间、内容
        self.labels: dict[str, str] = {}           # 面的显示名（来自应用）
        self._n_op = 0
        self._n_step = 0
        self._started = False
        self.add_agent(agent)

    # ------------------------------------------------------------------
    def add_agent(self, agent: str, display: Optional[str] = None) -> None:
        """登记一个 AI。它从登记时的场景开始"看"。"""
        if agent not in self.rt.actors:
            self.rt.register_actor(agent, ActorKind.AGENT)
        self.agent_names[agent] = display or agent
        if self._started and agent not in self.agent_views:
            self.agent_views[agent] = self._snapshot()

    def _args(self, **kw) -> dict:
        if self.scene:
            kw["scene"] = self.scene
        kw.setdefault("prefix", self.prefix)
        return kw

    def _units(self, recs: Records) -> dict[str, dict]:
        return units(recs, self.granularity)

    @staticmethod
    def _known(recs: Records) -> dict[str, dict]:
        return {cid: r["aspects"] for cid, r in recs.items()}

    def _learn(self, recs: Records) -> None:
        for cid, r in recs.items():
            if r.get("values"):
                self.values.setdefault(cid, {}).update(r["values"])

    def _snapshot(self) -> AgentView:
        return AgentView(recs=dict(self.view),
                         values={cid: dict(v) for cid, v in self.values.items() if cid in self.view},
                         versions={uid: o.version for uid, o in self.rt.objects.items()}, time=time.time())

    def start(self) -> Records:
        res = self.bridge.call("poll", self._args())
        self.labels = res.get("labels", {})
        owner = BASELINE_ACTOR if self.initial_owner == "open" else self.human
        for uid, content in self._units(res["records"]).items():
            if uid not in self.rt.objects:
                self.rt.create_object(uid, OBJECT_TYPE, content, owner)
        self.view = res["records"]
        self._learn(self.view)
        self.selected = {cid for cid, r in self.view.items() if r.get("selected")}
        self._started = True
        for a in self.agent_names:
            self.agent_views[a] = self._snapshot()
        return self.view

    # ------------------------------------------------------------------
    # 观察
    # ------------------------------------------------------------------
    def poll(self) -> Diff:
        """列一次对象，和上次比。执行 AI 脚本之外发生的变化，都是人做的。"""
        res = self.bridge.call("poll", self._args(known=self._known(self.view)))
        recs = res["records"]
        self._learn(recs)
        d = self._absorb_human(self.view, recs)
        self.view = recs
        self.selected = {cid for cid, r in recs.items() if r.get("selected")}
        return d

    def observe(self, agent: Optional[str] = None) -> ObserveResult:
        """AI 读场景时调用（例如 MCP 代理收到它的读取请求）：告诉它上次看之后发生了什么，并更新它的视图。"""
        agent = agent or self.agent
        self.poll()
        av = self.agent_views[agent]
        lines = describe_changes(av.recs, self.view, av.values, self.values, self.labels, who=self._who_for(agent))
        diff = diff_records(av.recs, self.view)
        self.agent_views[agent] = self._snapshot()
        text = ("你上次看场景之后，发生了这些变化：\n" + "\n".join("- " + x for x in lines)) if lines else "你上次看场景之后没有变化。"
        return ObserveResult(lines, diff, text)

    def _absorb_human(self, old: Records, new: Records) -> Diff:
        d = diff_records(old, new)
        if not d:
            return d
        self.detections.append({"time": time.time(), "changes": d.describe()})
        ud = diff_records(self._units(old), self._units(new))
        for uid, content in ud.created.items():
            if uid in self.rt.objects:
                self.rt.human_edit(self.human, uid, content)
            else:
                self.rt.create_object(uid, OBJECT_TYPE, content, self.human)
        for uid, (_, content) in ud.modified.items():
            self.rt.human_edit(self.human, uid, content)
        for uid, content in ud.deleted.items():
            self.rt.human_edit(self.human, uid, {**DELETED, "name": content["name"]})
        for cid, rec in d.deleted.items():
            self.human_deleted[cid] = rec["name"]
        return d

    # ------------------------------------------------------------------
    # 谁改的、为什么没生效
    # ------------------------------------------------------------------
    def _display(self, actor: str, recipient: Optional[str]) -> str:
        if actor == self.human:
            return "人 "
        if actor == BASELINE_ACTOR:
            return ""
        if recipient is not None and actor == recipient:
            return "你 "
        if len(self.agent_names) <= 1:
            return "AI "
        return f"AI「{self.agent_names.get(actor, actor)}」 "

    def _last_author(self, cid: str, face: Optional[str]) -> Optional[str]:
        if face is not None and self.granularity == ASPECT:
            obj = self.rt.objects.get(f"{cid}#{face}")
        else:
            obj = self.rt.objects.get(cid)
            if obj is None:
                obj = next((o for u, o in self.rt.objects.items() if split_unit(u)[0] == cid), None)
        return obj.versions[-1].author_id if obj is not None and obj.versions else None

    def _who_for(self, recipient: Optional[str]):
        def who(cid: str, face: Optional[str]) -> str:
            a = self._last_author(cid, face)
            return self._display(a, recipient) if a else ""
        return who

    def _reason(self, uid: str, agent: str) -> dict:
        obj = self.rt.objects[uid]
        if obj.human_touched is not None or (obj.occupancy is not None and obj.occupancy.holder == self.human):
            return {"reason": R_HUMAN}
        last = obj.versions[-1].author_id if obj.versions else None
        if last == self.human:
            return {"reason": R_HUMAN}
        if last and last != agent and last in self.agent_names:
            return {"reason": R_OTHER_AI, "by": last}
        return {"reason": R_OTHER}

    def _expire_reservations(self) -> None:
        now = time.time()
        self.reservations = {u: v for u, v in self.reservations.items() if v[1] > now}

    def protected(self, for_agent: Optional[str] = None) -> dict[str, list[str]]:
        """人碰过或正被占用、并且还在场景里的部分：{对象编号: [面]}，"*" 表示整个对象。
        for_agent：再加上预留给别的 AI 的部分。"""
        out: dict[str, set[str]] = {}
        for uid, o in self.rt.objects.items():
            if (o.human_touched is None and o.occupancy is None) or is_deleted(o.content) or o.retracted:
                continue
            cid, aspect = split_unit(uid)
            out.setdefault(cid, set()).add(aspect or "*")
        if for_agent is not None:
            self._expire_reservations()
            for uid, (holder, _) in self.reservations.items():
                if holder != for_agent:
                    cid, aspect = split_unit(uid)
                    out.setdefault(cid, set()).add(aspect or "*")
        return {cid: sorted(a) for cid, a in out.items()}

    def protected_ids(self) -> list[str]:
        return sorted(self.protected())

    # ------------------------------------------------------------------
    # 执行 AI 的脚本
    # ------------------------------------------------------------------
    def run_agent(self, code: str, label: str = "", protect: bool = True, agent: Optional[str] = None) -> AgentReport:
        """protect=False 用于对照组：不做保护，直接执行（相当于现在的 Blender MCP）。"""
        agent = agent or self.agent
        if agent not in self.agent_names or agent not in self.agent_views:
            self.add_agent(agent)
            self.agent_views.setdefault(agent, self._snapshot())
        av = self.agent_views[agent]
        keep = self.rt.human_touched_policy == POLICY_CANDIDATE
        res = self.bridge.call("run_agent", self._args(
            code=code, protected=self.protected(for_agent=agent), protect=protect, keep_candidates=keep,
            granularity=self.granularity, protect_selected=bool(self.occupy_selection and protect),
            known={cid: {"fp": r["fp"], "aspects": r["aspects"]} for cid, r in av.recs.items()}))
        if res["status"] == "deferred":
            names = [res["before"][c]["name"] for c in res["editing"]]
            return AgentReport(status="deferred", agent=agent, text=(
                "这次没有执行：人正在编辑模式里修改 " + "、".join(names) + "。等人退出编辑模式后再试。"))

        before, after, raw = res["before"], res["after"], res["after_raw"]
        self.labels = res.get("labels", self.labels)
        self._learn(before)
        before_values = {cid: dict(self.values.get(cid, {})) for cid in before}
        # 1. 运行时上次轮询之后、AI 执行之前，人做的修改
        human_since = self._absorb_human(self.view, before)
        if "selected" in res:
            self.selected = set(res["selected"])
        # 2. 这个 AI 上次看之后错过的变化（人做的、别的 AI 做的），带具体的值
        missed = describe_changes(av.recs, before, av.values, before_values, self.labels, who=self._who_for(agent))
        missed_diff = diff_records(av.recs, before)
        raw_values = {cid: r.get("values", {}) for cid, r in raw.items()}
        self._learn(after)

        # 3. AI 动过的单元，每个单元记一个操作（违反规则的只作废那一个）
        self._n_step += 1
        step_id = f"ai-{self._n_step}" + (f"-{label}" if label else "")
        ub, ua, ur = self._units(before), self._units(after), self._units(raw)
        attempted = diff_records(ub, ur)
        self.rt.add_step(step_id, inputs=[], outputs=sorted(diff_records(ub, ua).ids))
        rep = AgentReport(status="ok", agent=agent, step_id=step_id, missed=missed, missed_diff=missed_diff,
                          human_since=human_since, outside=res.get("outside", {}),
                          inexact=res["inexact"], renamed=res["renamed"], fallback=res.get("fallback", []),
                          dangling=res["dangling"], candidates=res.get("candidates", []), error=res["error"],
                          stdout=res["stdout"], seconds=res["seconds"], undo_pushed=res["undo_pushed"],
                          protected=protect)
        deleted_names = {base_name(n) for n in self.human_deleted.values()}
        selected = set(self.selected) if (self.occupy_selection and protect) else set()
        reserved_by_others = ({u: h for u, (h, _) in self.reservations.items() if h != agent} if protect else {})
        new_objects = set()
        for uid in sorted(attempted.ids):
            cid, _ = split_unit(uid)
            name = (before.get(cid) or raw.get(cid) or {}).get("name", cid)
            if uid in attempted.created:
                if uid not in ua:
                    continue          # AI 新建后又自己删掉
                self._n_op += 1
                op = Operation(f"op-{self._n_op}", agent, CAPABILITY, produces=[uid], step_id=step_id)
                self.rt.submit(op)
                self.rt.complete(op.op_id, produces={uid: ua[uid]}, produce_types={uid: OBJECT_TYPE})
                rep.committed.append(uid)
                if cid not in new_objects and base_name(after[cid]["name"]) in deleted_names:
                    rep.resurrections.append(after[cid]["name"])
                new_objects.add(cid)
                continue
            if uid not in self.rt.objects:
                continue
            if cid in selected:
                rep.skipped.append({"id": uid, "name": name, "reason": R_SELECTED})
                continue
            if uid in reserved_by_others:
                rep.skipped.append({"id": uid, "name": name, "reason": R_RESERVED, "by": reserved_by_others[uid]})
                continue
            obj = self.rt.objects[uid]
            self._n_op += 1
            # 基准版本 = 这个 AI 上次看到的版本：别人之后改过的，就是按旧印象做的修改（R4）
            op = Operation(f"op-{self._n_op}", agent, CAPABILITY,
                           targets=[Target(uid, av.versions.get(uid, -1))], step_id=step_id)
            r = self.rt.submit(op)
            if r.status.is_rejection:
                rep.skipped.append({"id": uid, "name": name, **self._reason(uid, agent)})
                continue
            content = ua.get(uid) or {**DELETED, "name": name}
            r = self.rt.complete(op.op_id, writes={uid: content})
            if r.status in (OpStatus.COMMITTED, OpStatus.MERGED):
                rep.committed.append(uid)
            else:
                rep.skipped.append({"id": uid, "name": name, **self._reason(uid, agent)})
        if not protect:
            # 对照组：场景里 AI 的修改已经全部生效，运行时的"未采用"只是账面上的，报告里按实际情况算
            rep.committed += [s["id"] for s in rep.skipped]
            rep.skipped = []
        if self.granularity == ASPECT:
            for cid in {split_unit(u)[0] for u in rep.committed}:
                skipped = [split_unit(s["id"])[1] for s in rep.skipped if split_unit(s["id"])[0] == cid]
                if skipped:
                    kept = [split_unit(u)[1] for u in rep.committed if split_unit(u)[0] == cid]
                    rep.partial[after.get(cid, before.get(cid, {})).get("name", cid)] = {
                        "kept": sorted(kept), "restored": sorted(skipped)}
        # 4. 预留：这个 AI 上次的预留用完了；这次因为别的 AI 被拒的部分，给它留一段时间重试
        self.reservations = {u: v for u, v in self.reservations.items() if v[0] != agent}
        if protect and self.reserve_seconds > 0:
            until = time.time() + self.reserve_seconds
            for s in rep.skipped:
                if s["reason"] == R_OTHER_AI:
                    self.reservations[s["id"]] = (agent, until)
                    rep.reserved.append(s["id"])
        self.view = after
        self.agent_views[agent] = self._snapshot()
        rep.text = self._describe(rep, before, after, before_values, raw_values)
        return rep

    # ------------------------------------------------------------------
    def _why(self, s: dict) -> str:
        r = s["reason"]
        if r == R_HUMAN:
            return "人改过，保留人的"
        if r == R_SELECTED:
            return "人正选中着，没有改"
        if r == R_OTHER_AI:
            return f"{self._display(s['by'], None).strip()} 在你上次看之后改过，保留它的"
        if r == R_RESERVED:
            return f"{self._display(s['by'], None).strip()} 正在重试，暂时留给它"
        return "没有采用"

    def _describe(self, rep: AgentReport, before: Records, after: Records,
                  before_values: Values, raw_values: Values) -> str:
        """回给 AI 的说明。这段文字会出现在 AI 的上下文里。"""
        by_obj: dict[str, dict[str, list]] = {}
        for uid in rep.committed:
            by_obj.setdefault(split_unit(uid)[0], {"ok": [], "no": []})["ok"].append(uid)
        for s in rep.skipped:
            by_obj.setdefault(split_unit(s["id"])[0], {"ok": [], "no": []})["no"].append(s)
        name = lambda cid: (after.get(cid) or before.get(cid) or {}).get("name", cid)  # noqa: E731
        created = sorted(name(c) for c, v in by_obj.items() if v["ok"] and not v["no"] and c not in before and c in after)
        modified = sorted(name(c) for c, v in by_obj.items() if v["ok"] and not v["no"] and c in before and c in after)
        deleted = sorted(name(c) for c, v in by_obj.items() if v["ok"] and not v["no"] and c in before and c not in after)
        lines = [f"执行完成，用时 {rep.seconds:.2f} 秒。"]
        if rep.missed:
            lines.append("你上次看场景之后（这段脚本执行之前），发生了这些变化：")
            lines += ["- " + x for x in rep.missed]
        parts = []
        if created:
            parts.append("新建 " + "、".join(created))
        if modified:
            parts.append("修改 " + "、".join(modified))
        if deleted:
            parts.append("删除 " + "、".join(deleted))
        lines.append("已生效：" + ("；".join(parts) if parts else "没有"))

        def values_note(cid: str, face: Optional[str]) -> str:
            if face is None:
                return ""
            now, want = before_values.get(cid, {}).get(face), raw_values.get(cid, {}).get(face)
            if now is not None and want is not None and now != want:
                return f"现在是 {now}，你改成的 {want} 没有采用"
            return ""

        def face_note(cid: str, s: dict) -> str:
            face = split_unit(s["id"])[1]
            note = values_note(cid, face)
            return f"{label(face, self.labels)} {self._why(s)}" + (f"（{note}）" if note else "")

        def refused_note(cid: str, items: list[dict]) -> str:
            notes = []
            for s in items[:3]:
                face = split_unit(s["id"])[1]
                note = values_note(cid, face)
                if note:
                    notes.append(f"{label(face, self.labels)} {note}")
            return name(cid) + (f"（{'；'.join(notes)}）" if notes else "")

        partial_objs = [c for c, v in by_obj.items() if v["ok"] and v["no"]]
        if partial_objs and rep.protected:
            items = []
            for cid in sorted(partial_objs, key=name):
                kept = [label(split_unit(u)[1], self.labels) for u in by_obj[cid]["ok"]]
                items.append(f"{name(cid)}（{'、'.join(kept)} 已生效；" + "；".join(face_note(cid, s) for s in by_obj[cid]["no"]) + "）")
            lines.append("部分生效：" + "；".join(items))
        elif rep.partial and not rep.protected:
            items = [f"{n} 的{'、'.join(label(a, self.labels) for a in p['restored'])}" for n, p in sorted(rep.partial.items())]
            lines.append("【对照组，没有保护】这些部分人改过，按规则不该改，但已经被改掉了：" + "；".join(items))
        refused: dict[str, list[str]] = {}
        for cid in sorted(by_obj, key=name):
            v = by_obj[cid]
            if v["no"] and not v["ok"]:
                refused.setdefault(self._why(v["no"][0]), []).append(
                    refused_note(cid, v["no"]) if rep.protected else name(cid))
        for why, names in sorted(refused.items()):
            if not rep.protected:
                lines.append("【对照组，没有保护】这些对象人改过，按规则不该改，但已经被改掉了：" + "、".join(names))
            elif why.startswith("人改过"):
                lines.append("没有生效（人改过，已恢复成人的版本，请不要再改）：" + "、".join(names))
            elif why.startswith("人正选中"):
                lines.append("没有生效（人正选中着，可能马上要改；人取消选中后可以再改）：" + "、".join(names))
            else:
                lines.append(f"没有生效（{why}）：" + "、".join(names))
        if rep.reserved:
            lines.append(f"被别的 AI 抢先改过的部分，给你预留了 {self.reserve_seconds:.0f} 秒：请看上面的最新情况后重新决定要不要改。")
        if not rep.protected and rep.human_since:
            lines.append("【对照组，没有保护】执行前人刚改过：" + "；".join(rep.human_since.describe()))
        if rep.resurrections:
            lines.append("注意：你新建的 " + "、".join(rep.resurrections) + " 和人删掉的对象同名，人可能不想要它。")
        out = [x for k in ("created", "modified", "deleted") for x in rep.outside.get(k, [])]
        if out:
            lines.append("注意：你的脚本还改到了当前场景以外的对象（运行时不追踪，也没有保护）：" + "、".join(out[:10])
                         + (" 等" if len(out) > 10 else ""))
        if rep.error:
            lines.append("脚本出错（出错前的修改仍按上面的规则处理）：\n" + rep.error.strip().splitlines()[-1])
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 人的操作
    # ------------------------------------------------------------------
    def hand_back(self, cid: str) -> None:
        """人把整个对象交还给 AI：同时结束占用。"""
        for uid in [u for u in self.rt.objects if split_unit(u)[0] == cid]:
            obj = self.rt.objects.get(uid)
            if obj is None:
                continue
            if obj.occupancy is not None and obj.occupancy.holder == self.human:
                self.rt.release(self.human, uid)
            if obj.human_touched is not None:
                self.rt.hand_back(self.human, uid)

    def find(self, name: str) -> Optional[str]:
        for cid, rec in self.view.items():
            if rec["name"] == name:
                return cid
        return None
