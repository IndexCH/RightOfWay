"""运行时：执行规范第 6 节的全部规则（R1–R21）。

阅读建议：
  1. 先看 submit()  —— Agent 提交操作时做哪些检查
  2. 再看 complete() / _finalize() —— 操作执行完之后怎么决定"提交还是作废"
  3. 再看 human_edit() —— 人修改时发生什么（占用、人碰过、失效传播）
  4. 其余方法对应释放、交还、认领、撤回、暂停、确认、观察、日志

每个关键判断旁边都标了对应的规则编号，可以和 spec/spec_v0.2.md 对照着看。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Optional

from .identity import suffix_pattern
from .identity import verify as verify_identity
from .merge import three_way_merge
from .model import (
    Actor,
    ActorKind,
    Authority,
    AuthorityKind,
    CapabilityMeta,
    Channel,
    Event,
    KEEP_HUMAN,
    KEEP_OTHER,
    KEEP_OTHER_AGENT,
    KEEP_RESERVED,
    KEEP_SELECTED,
    HumanTouched,
    ObjectState,
    Occupancy,
    Operation,
    OpResult,
    OpStatus,
    Permit,
    Settlement,
    Step,
    StepState,
    Target,
    VersionRecord,
    group_of,
)

# 仍在进行中的操作状态（步骤以此判断是否全部完成）
IN_FLIGHT = {OpStatus.QUEUED, OpStatus.PENDING_CONFIRMATION, OpStatus.EXECUTING, OpStatus.HELD}

# "人碰过"对象遇到 AI 写入时的处理策略（R6）
POLICY_DISCARD = "discard"      # 默认：丢弃 AI 的结果
POLICY_CANDIDATE = "candidate"  # 另存为候选，不成为当前版本
POLICY_MERGE = "merge"          # 可合并对象尝试三方合并，失败再丢弃
REIDENTIFY_RULE = "name+type+parent"   # 认回同一个对象：同一个父对象下名字、类型一样（rightofway/identity.py）


class ProtocolError(Exception):
    """调用方违反协议（例如 Agent 冒充人、释放别人占用的对象）。"""


class ObservationError(ProtocolError):
    """R18：适配器报告的变化找不到可归属的对象，不能静默丢弃。"""


class Runtime:
    def __init__(self, human_touched_policy: str = POLICY_DISCARD, selection_occupies: bool = False,
                 reserve_seconds: float = 30.0, pause_on_breach: bool = True, reidentify: bool = True,
                 loop_repeat: int = 3, loop_window: int = 5) -> None:
        """reidentify：认回同一个对象（design_v0.5.md 12.4、12.5，默认打开）。一次执行里 Agent 删掉一个对象、
        又新建了同一个父对象下名字（或去掉应用自动加的重名后缀后的名字）和类型都一样的对象时，按同一个对象处理：
        新对象接过旧对象的编号。同一条规则还用来拦下"补回人删掉的对象"（墓碑）、
        把"之前想删、因为人改过而保留下来的对象"和后来新建的同名对象认成一个。
        只有接入代码在连接时的自测（learn_app）里说明支持，才会写进许可单。"""
        assert human_touched_policy in (POLICY_DISCARD, POLICY_CANDIDATE, POLICY_MERGE)
        self.reidentify = reidentify
        self.app: dict = {}                                   # 连接时探测出的应用能力（learn_app）
        self.wanted_gone: dict[str, dict[str, dict]] = {}     # Agent → {它想删、因为人改过而保留下来的对象: 行}
        self.identity_log: list[dict] = []                    # 认回的记录：哪次执行、哪个对象、置信等级
        # 循环检测（prior_art_solutions.md 第 3 项）：同一个单元在这个 Agent 最近 loop_window 次执行里
        # 被保持了 loop_repeat 次，说明它在反复改改不动的东西，提醒人（不暂停，这是告知，不是安全手段）
        self.loop_repeat, self.loop_window = loop_repeat, loop_window
        self.kept_history: dict[str, list[set[str]]] = {}      # Agent → 最近几次执行里被保持的单元
        self.looping: dict[str, set[str]] = {}                 # Agent → 已经提醒过的、正在反复的单元
        self.human_touched_policy = human_touched_policy
        self.selection_occupies = selection_occupies          # R8 选项："选中即占用"
        self.reserve_seconds = reserve_seconds                # R23：被别的 Agent 抢先后，预留多少秒
        self.seen: dict[str, dict[str, int]] = {}             # R3：每个 Agent 上次看到的每个对象的版本
        self.selection: dict[str, set[str]] = {}              # 人 → 选中的应用对象（group_of 的结果）
        self.reservations: dict[str, tuple[str, float]] = {}  # 对象 → (预留给哪个 Agent, 到期时间)
        self.permits: dict[str, Permit] = {}
        self.pause_on_breach = pause_on_breach                # 违规后暂停这个 Agent，等人处理（第 6 节）
        self.suspended: dict[str, dict] = {}                  # 被暂停的 Agent → 原因
        self.actors: dict[str, Actor] = {}
        self.objects: dict[str, ObjectState] = {}
        self.capabilities: dict[str, CapabilityMeta] = {}
        self.steps: dict[str, Step] = {}
        self.ops: dict[str, Operation] = {}
        self.events: list[Event] = []
        self.paused = False
        self._seq = 0
        self._subscribers: list[Callable[[Event], None]] = []

    # ------------------------------------------------------------------
    # 注册与基础设施
    # ------------------------------------------------------------------
    def register_actor(self, actor_id: str, kind: ActorKind) -> Actor:
        """R1：操作者身份由运行时登记，之后一律以登记的身份为准。"""
        actor = Actor(actor_id, ActorKind(kind))
        self.actors[actor_id] = actor
        return actor

    def register_capability(self, meta: CapabilityMeta) -> None:
        self.capabilities[meta.name] = meta

    def learn_app(self, probe: dict) -> dict:
        """连接时接入代码自测的结果（design_v0.5.md 第 4 节）。不手写任何应用的规则（P4）：
        重名后缀由应用给两个同名东西起的名字学出（例如 "X" 和 "X.001" → 去掉 ".数字"；Unity 允许重名，就没有后缀）。
        probe：{"names": [第一个, 第二个], "identity": 接入代码会不会认回, "transfer_ids": 能不能把编号交给新对象}。"""
        names = list(probe.get("names") or [])
        suffix = suffix_pattern(names[0], names[1]) if len(names) >= 2 else None
        self.app = {**probe, "suffix": suffix}
        self._emit("app/probed", identity=bool(probe.get("identity")), suffix=suffix,
                   transferIds=bool(probe.get("transfer_ids")))
        return self.app

    def identity_rule(self) -> Optional[dict]:
        """许可单里的认回规则；运行时关掉了认回、或者接入代码不支持时为 None。"""
        if not self.reidentify or not self.app.get("identity"):
            return None
        return {"rule": REIDENTIFY_RULE, "suffix": self.app.get("suffix")}

    def add_step(self, step_id: str, inputs: Iterable[str], outputs: Iterable[str],
                 assignee: str = "agent") -> Step:
        step = Step(step_id, list(inputs), list(outputs), assignee=assignee)
        self.steps[step_id] = step
        return step

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        self._subscribers.append(callback)

    def _emit(self, type_: str, **data: Any) -> Event:
        self._seq += 1
        event = Event(self._seq, type_, data)
        self.events.append(event)
        for cb in self._subscribers:
            cb(event)
        return event

    def _actor(self, actor_id: str) -> Actor:
        if actor_id not in self.actors:
            raise ProtocolError(f"未登记的操作者：{actor_id}")
        return self.actors[actor_id]

    def _require_human(self, actor_id: str) -> Actor:
        actor = self._actor(actor_id)
        if actor.kind != ActorKind.HUMAN:
            raise ProtocolError(f"R1：{actor_id} 不是人，不能执行只有人才能做的操作")
        return actor

    # ------------------------------------------------------------------
    # 对象与版本
    # ------------------------------------------------------------------
    def create_object(self, object_id: str, type_: str, content: Any, author_id: str,
                      authority: Optional[Authority] = None, mergeable: bool = False,
                      provenance: Optional[dict] = None) -> ObjectState:
        if object_id in self.objects and not self.objects[object_id].retracted:
            raise ProtocolError(f"对象已存在：{object_id}")
        author = self._actor(author_id)
        obj = ObjectState(object_id, type_, authority=authority or Authority(),
                          provenance=provenance, mergeable=mergeable)
        self.objects[object_id] = obj
        self._commit_version(obj, content, author)
        self._emit("object/created", objectId=object_id, author=author_id)
        return obj

    def copy_object(self, actor_id: str, source_id: str, new_id: str) -> ObjectState:
        """复制产生新对象，并记录来源（S-D1、S-D2）。"""
        src = self.objects[source_id]
        return self.create_object(new_id, src.type, src.content, actor_id,
                                  mergeable=src.mergeable,
                                  provenance={"derivedFrom": source_id, "version": src.version})

    def _commit_version(self, obj: ObjectState, content: Any, author: Actor,
                        op_id: Optional[str] = None, step_id: Optional[str] = None,
                        restored_from: Optional[int] = None) -> VersionRecord:
        self._seq += 1
        rec = VersionRecord(
            version=obj.version + 1, content=content, author_id=author.actor_id,
            author_kind=author.kind, op_id=op_id, step_id=step_id,
            parent_version=obj.version or None, seq=self._seq, restored_from=restored_from,
        )
        obj.versions.append(rec)
        obj.retracted = False
        # "人碰过"标记：人或外部修改时设置，AI 修改时清除（AI 只能改没被人碰过的对象）
        if author.kind in (ActorKind.HUMAN, ActorKind.EXTERNAL):
            obj.human_touched = HumanTouched(by=author.actor_id, since_version=rec.version)
        else:
            obj.human_touched = None
        return rec

    # ------------------------------------------------------------------
    # Agent 提交操作（R1、R3、R4、R6、R9、R11、R16、R17、R21）
    # ------------------------------------------------------------------
    def submit(self, op: Operation) -> OpResult:
        actor = self._actor(op.actor_id)
        op.actor_kind = actor.kind                      # R1：以登记的身份为准，忽略自报
        meta = self.capabilities.get(op.capability) if op.capability else None
        op.effect_level = meta.effect_level if meta else 1   # R16：副作用等级以能力元数据为准
        self.ops[op.op_id] = op
        self._emit("op/submitted", opId=op.op_id, actor=op.actor_id, capability=op.capability)

        if self.paused:                                  # R17：暂停期间不派发
            op.status = OpStatus.QUEUED
            self._emit("op/queued", opId=op.op_id)
            return OpResult(op.op_id, op.status)
        return self._dispatch(op)

    def _dispatch(self, op: Operation) -> OpResult:
        rejection = self._precheck(op)
        if rejection is not None:
            status, reason = rejection
            return self._reject(op, status, reason)

        # R16：副作用等级 3 的 Agent 操作，执行前必须由人确认
        if op.actor_kind == ActorKind.AGENT and op.effect_level >= 3:
            op.status = OpStatus.PENDING_CONFIRMATION
            self._begin_step_if_needed(op)
            self._emit("confirm/request", opId=op.op_id, effectLevel=op.effect_level,
                       capability=op.capability)
            return OpResult(op.op_id, op.status)

        op.status = OpStatus.EXECUTING
        self._begin_step_if_needed(op)
        self._emit("op/dispatched", opId=op.op_id)
        return OpResult(op.op_id, op.status)

    def _precheck(self, op: Operation) -> Optional[tuple[OpStatus, dict]]:
        """派发前的检查。返回 None 表示可以派发。"""
        is_agent = op.actor_kind == ActorKind.AGENT

        # R11 / R14：步骤被人认领、分配给人、或被撤回后，Agent 不能执行
        if is_agent and op.step_id:
            step = self.steps[op.step_id]
            if step.assignee == "human" or step.claimed_by or step.state in (
                    StepState.CLAIMED, StepState.DONE_BY_HUMAN):
                return OpStatus.REJECTED_STEP_ASSIGNMENT, {"stepId": step.step_id, "assignee": "human"}
            if step.state == StepState.UNDONE:
                return OpStatus.REJECTED_STEP_ASSIGNMENT, {"stepId": step.step_id, "stepState": "undone"}

        # R21：权威在会话里时，不能直接写存储副本
        meta = self.capabilities.get(op.capability) if op.capability else None
        if meta and meta.channel == Channel.STORAGE:
            for t in op.targets:
                obj = self.objects[t.object_id]
                if obj.authority.kind == AuthorityKind.SESSION:
                    return OpStatus.REJECTED_AUTHORITY, {
                        "objectId": t.object_id, "authority": obj.authority.adapter_id}

        if not is_agent:
            return None   # R5：人的操作不受以下限制

        if op.actor_id in self.suspended:                 # 违规后被暂停，等人处理
            return OpStatus.REJECTED_SUSPENDED, {"agent": op.actor_id, **self.suspended[op.actor_id]}

        # R8 选项、R23：人选中的、预留给别的 Agent 的
        for t in op.targets:
            hit = self._claim_conflict(op.actor_id, t.object_id)
            if hit is not None:
                return hit

        # R9：被占用的对象，不派发以它为目标或输入的 Agent 操作
        for t in list(op.targets) + list(op.reads):
            obj = self.objects[t.object_id]
            if obj.occupancy is not None:
                return OpStatus.REJECTED_OCCUPIED, {"objectId": t.object_id, "holder": obj.occupancy.holder}

        # R4：读集过时，一律拒绝
        for r in op.reads:
            obj = self.objects[r.object_id]
            if r.base_version != obj.version:
                return OpStatus.REJECTED_STALE, {"objectId": r.object_id, "baseVersion": r.base_version,
                                                 "currentVersion": obj.version, "set": "reads"}

        # 写集：和 _finalize 用同样的判断顺序
        for t in op.targets:
            hit = self._target_conflict(self.objects[t.object_id], t.base_version)
            if hit is not None:
                return hit
        return None

    def _claim_conflict(self, agent_id: str, object_id: str) -> Optional[tuple[OpStatus, dict]]:
        """R8 选项（选中即占用）和 R23（预留）。_precheck 和 admit 共用。"""
        if self.selection_occupies:
            g = group_of(object_id)
            for holder, groups in self.selection.items():
                if g in groups:
                    return OpStatus.REJECTED_OCCUPIED, {"objectId": object_id, "holder": holder, "kind": "selection"}
        r = self.reservations.get(object_id)
        if r is not None and r[0] != agent_id and r[1] > time.time():
            return OpStatus.REJECTED_RESERVED, {"objectId": object_id, "holder": r[0]}
        return None

    def _target_conflict(self, obj: ObjectState, base_version: int) -> Optional[tuple[OpStatus, dict]]:
        """写集里的一个对象：过时（R4）或人碰过（R6）时返回拒绝原因。_precheck 和 admit 共用。"""
        stale = base_version != obj.version
        touched = obj.human_touched is not None
        if touched and self.human_touched_policy == POLICY_CANDIDATE:
            return None                                   # R6 替代策略：执行完再存为候选
        if obj.mergeable and stale and (not touched or self.human_touched_policy == POLICY_MERGE):
            return None                                   # 执行完再尝试合并
        if stale:                                         # R4
            return OpStatus.REJECTED_STALE, {"objectId": obj.object_id, "baseVersion": base_version,
                                             "currentVersion": obj.version, "set": "targets"}
        if touched:                                       # R6
            return OpStatus.REJECTED_HUMAN_TOUCHED, {"objectId": obj.object_id, "by": obj.human_touched.by}
        return None

    def _begin_step_if_needed(self, op: Operation) -> None:
        if not op.step_id:
            return
        step = self.steps[op.step_id]
        if step.state != StepState.RUNNING:
            step.state = StepState.RUNNING
            step.committed_ops = []
            self._emit("step/state", stepId=step.step_id, state=step.state.value)

    # ------------------------------------------------------------------
    # 人确认（R16）
    # ------------------------------------------------------------------
    def confirm(self, actor_id: str, op_id: str, approved: bool) -> OpResult:
        self._require_human(actor_id)
        op = self.ops[op_id]
        if op.status != OpStatus.PENDING_CONFIRMATION:
            raise ProtocolError(f"{op_id} 不在等待确认的状态")
        if not approved:
            return self._reject(op, OpStatus.REJECTED_BY_HUMAN, {"by": actor_id})
        op.status = OpStatus.EXECUTING
        self._emit("op/dispatched", opId=op_id, confirmedBy=actor_id)
        return OpResult(op_id, op.status)

    # ------------------------------------------------------------------
    # 操作执行完成（适配器返回结果）
    # ------------------------------------------------------------------
    def complete(self, op_id: str, writes: Optional[dict[str, Any]] = None,
                 produces: Optional[dict[str, Any]] = None,
                 produce_types: Optional[dict[str, str]] = None,
                 extra_changes: Optional[dict[str, Any]] = None,
                 failed: bool = False) -> OpResult:
        """writes：对 targets 的新内容；produces：新建对象的内容；
        extra_changes：适配器观察到、但能力没有声明的变化（R19）。"""
        op = self.ops[op_id]
        result = {"writes": writes or {}, "produces": produces or {},
                  "produce_types": produce_types or {}, "extra": extra_changes or {}}

        if op.status == OpStatus.CANCELLED:            # 暂停时已取消，迟到的结果只记日志
            self._emit("result/ignored", opId=op_id, reason="cancelled")
            return OpResult(op_id, op.status)
        if op.status != OpStatus.EXECUTING:
            raise ProtocolError(f"{op_id} 当前状态为 {op.status.value}，不能完成")
        if failed:
            op.status = OpStatus.FAILED
            self._emit("op/result", opId=op_id, status=op.status.value)
            self._maybe_finish_step(op)
            return OpResult(op_id, op.status)
        if self.paused:                                # R17：暂停期间完成的结果先暂存
            op.status = OpStatus.HELD
            op.held_result = result
            self._emit("op/held", opId=op_id)
            return OpResult(op_id, op.status)
        return self._finalize(op, result)

    def _finalize(self, op: Operation, result: dict) -> OpResult:
        writes, produces, extra = result["writes"], result["produces"], result["extra"]
        is_agent = op.actor_kind == ActorKind.AGENT
        merged_content: dict[str, Any] = {}

        if is_agent:
            # R4：读集过时 → 整个操作作废
            for r in op.reads:
                obj = self.objects[r.object_id]
                if r.base_version != obj.version:
                    return self._reject(op, OpStatus.REJECTED_STALE, {
                        "objectId": r.object_id, "baseVersion": r.base_version,
                        "currentVersion": obj.version, "set": "reads"})

            # 判断顺序：先看策略是否允许候选或合并，再按 过时(R4) > 人碰过(R6) > 被占用(R9) 报告原因
            candidates_needed = False
            for t in op.targets:
                obj = self.objects[t.object_id]
                new = writes.get(t.object_id)
                stale = t.base_version != obj.version
                touched = obj.human_touched is not None

                if touched and self.human_touched_policy == POLICY_CANDIDATE:   # R6 替代策略
                    candidates_needed = True
                    continue
                # 只有"过时"时才合并：合并只接受人没改过的段落，所以人的改动一定保留。
                # 人碰过、但 AI 的基准就是人的版本时不合并，否则 AI 等于直接改了人写的内容。
                # 人还占用着（正在改）时也不合并：不往人正在编辑的对象里写东西（R9）。
                merge_allowed = obj.mergeable and stale and obj.occupancy is None and (
                    not touched or self.human_touched_policy == POLICY_MERGE)
                if merge_allowed:
                    m = self._try_merge(obj, t.base_version, new)
                    if m is not None:
                        merged_content[t.object_id] = m
                        continue
                if stale:                                                        # R4
                    return self._reject(op, OpStatus.REJECTED_STALE, {
                        "objectId": t.object_id, "baseVersion": t.base_version,
                        "currentVersion": obj.version, "set": "targets"})
                if touched:                                                      # R6
                    return self._reject(op, OpStatus.REJECTED_HUMAN_TOUCHED, {
                        "objectId": t.object_id, "by": obj.human_touched.by})
                if obj.occupancy is not None:                                    # R9
                    return self._reject(op, OpStatus.REJECTED_OCCUPIED, {
                        "objectId": t.object_id, "holder": obj.occupancy.holder})

            if candidates_needed:                                    # R6 "留作候选"
                return self._store_candidates(op, writes)

        # ---- 提交 ----
        actor = self._actor(op.actor_id)
        new_versions: dict[str, int] = {}
        for t in op.targets:
            obj = self.objects[t.object_id]
            content = merged_content.get(t.object_id, writes.get(t.object_id))
            prior_touched = obj.human_touched
            rec = self._commit_version(obj, content, actor, op.op_id, op.step_id)
            if t.object_id in merged_content and prior_touched is not None:
                obj.human_touched = prior_touched      # 合并结果里仍有人写的内容，标记保留
            new_versions[t.object_id] = rec.version
        for oid, content in produces.items():
            if oid in self.objects and not self.objects[oid].retracted:
                raise ProtocolError(f"{oid} 已存在：重新执行时应放在 targets 里并带基准版本")
            if oid in self.objects:
                obj = self.objects[oid]
            else:
                obj = ObjectState(oid, result["produce_types"].get(oid, "blob"),
                                  provenance={"opId": op.op_id, "stepId": op.step_id})
                self.objects[oid] = obj
            rec = self._commit_version(obj, content, actor, op.op_id, op.step_id)
            new_versions[oid] = rec.version

        # R19：实际改动超出声明
        if extra:
            self._record_contract_violation(op, actor, extra, new_versions)

        if op.actor_id in self.seen:                   # R3：Agent 知道自己刚写进去的版本
            self.seen[op.actor_id].update(new_versions)
        status = OpStatus.MERGED if merged_content else OpStatus.COMMITTED
        op.status = status
        self._emit("op/result", opId=op.op_id, status=status.value, newVersions=new_versions)
        for oid in new_versions:
            self._emit("object/changed", objectId=oid, version=new_versions[oid],
                       author=op.actor_id, opId=op.op_id)

        if op.step_id:
            self.steps[op.step_id].committed_ops.append(op.op_id)
        for oid in new_versions:
            self._invalidate_from(oid, exclude_step=op.step_id)      # R12
        self._maybe_finish_step(op)
        return OpResult(op.op_id, status, new_versions=new_versions)

    def _try_merge(self, obj: ObjectState, base_version: int, theirs: Any) -> Optional[str]:
        base = obj.get_version(base_version).content
        if not all(isinstance(x, str) for x in (base, obj.content, theirs)):
            return None
        return three_way_merge(base, obj.content, theirs)

    def _store_candidates(self, op: Operation, writes: dict[str, Any]) -> OpResult:
        for t in op.targets:
            obj = self.objects[t.object_id]
            self._seq += 1
            obj.candidates.append(VersionRecord(
                version=-1, content=writes.get(t.object_id), author_id=op.actor_id,
                author_kind=ActorKind.AGENT, op_id=op.op_id, step_id=op.step_id,
                parent_version=obj.version, seq=self._seq))
        op.status = OpStatus.CANDIDATE
        self._emit("result/candidate", opId=op.op_id, objects=[t.object_id for t in op.targets])
        self._maybe_finish_step(op)
        return OpResult(op.op_id, op.status)

    def _record_contract_violation(self, op: Operation, actor: Actor, extra: dict[str, Any],
                                   new_versions: dict[str, int]) -> None:
        for oid, content in extra.items():
            obj = self.objects[oid]
            rec = self._commit_version(obj, content, actor, op.op_id, op.step_id)
            new_versions[oid] = rec.version
        meta = self.capabilities.get(op.capability) if op.capability else None
        if meta:
            meta.verified = False          # R19：下调保证等级，直到重新验证
        self._emit("violation/contract", opId=op.op_id, capability=op.capability,
                   unexpectedChanges=list(extra.keys()))

    def _reject(self, op: Operation, status: OpStatus, reason: dict) -> OpResult:
        op.status = status
        op.reason = reason
        self._emit("op/result", opId=op.op_id, status=status.value, reason=reason)
        for t in op.targets:                           # R15：被拒绝的结果记入日志
            self._emit("result/discarded", opId=op.op_id, objectId=t.object_id, reason=status.value)
        self._maybe_finish_step(op)
        return OpResult(op.op_id, status, reason)

    def _maybe_finish_step(self, op: Operation) -> None:
        """步骤里没有进行中的操作时，结束这一步。"""
        if not op.step_id:
            return
        step = self.steps[op.step_id]
        if step.state != StepState.RUNNING:
            return
        if any(o.step_id == step.step_id and o.status in IN_FLIGHT for o in self.ops.values()):
            return
        step.state = StepState.DONE if step.committed_ops else StepState.PENDING
        if step.state == StepState.DONE:
            step.done_seq = self._seq
        self._emit("step/state", stepId=step.step_id, state=step.state.value)
        # 如果这一步完成时输入正被占用，立即转为阻塞
        if step.state != StepState.DONE and self._inputs_occupied(step):
            self._set_step(step, StepState.BLOCKED)

    # ------------------------------------------------------------------
    # 许可单（design_v0.5.md 第 2 节）：Agent 执行之前，运行时决定哪些可以改、哪些要保持
    # ------------------------------------------------------------------
    def mark_seen(self, agent_id: str, versions: Optional[dict[str, int]] = None) -> None:
        """R3：记下这个 Agent 看到了哪些版本（它读了场景，或者它的一次执行完成了）。
        之后它按旧印象做的修改，以这些版本为基准判断是否过时（R4）。"""
        self._actor(agent_id)
        self.seen[agent_id] = dict(versions) if versions is not None else {
            oid: o.version for oid, o in self.objects.items()}

    def set_selection(self, actor_id: str, groups: Iterable[str]) -> None:
        """R8 选项：人现在选中的应用对象。只有 selection_occupies 打开时才影响许可。"""
        self._require_human(actor_id)
        new = set(groups)
        if new != self.selection.get(actor_id, set()):
            self.selection[actor_id] = new
            self._emit("selection/changed", actor=actor_id, objects=sorted(new))

    def _expire_reservations(self) -> None:
        now = time.time()
        self.reservations = {u: v for u, v in self.reservations.items() if v[1] > now}

    def reason_for(self, agent_id: str, object_id: str) -> dict:
        """告诉 Agent 某个对象为什么不能改：人改过或正在改 → 人；最后由别的 Agent 改 → 那个 Agent。"""
        obj = self.objects[object_id]
        if obj.human_touched is not None or obj.occupancy is not None:
            return {"reason": KEEP_HUMAN}
        last = obj.versions[-1] if obj.versions else None
        if last is not None and last.author_kind == ActorKind.HUMAN:
            return {"reason": KEEP_HUMAN}
        if last is not None and last.author_kind == ActorKind.AGENT and last.author_id != agent_id:
            return {"reason": KEEP_OTHER_AGENT, "by": last.author_id}
        return {"reason": KEEP_OTHER}

    def admit(self, agent_id: str) -> Permit:
        """为这个 Agent 的一次执行开许可单。判断和 _precheck 用的是同一组函数，规则只写一处（P9）：
        选中、预留（_claim_conflict）→ 占用（R9）→ 过时、人碰过（_target_conflict，R4、R6）。
        人碰过的对象在"留作候选"策略下也要保持（场景里保留人的，Agent 的版本另存为候选）。"""
        actor = self._actor(agent_id)
        if actor.kind != ActorKind.AGENT:
            raise ProtocolError(f"{agent_id} 不是 Agent，不需要许可")
        self._expire_reservations()
        if agent_id in self.suspended:
            permit = Permit(f"permit-{len(self.permits) + 1}", agent_id,
                            refused={"reason": "suspended", **self.suspended[agent_id]})
            self.permits[permit.permit_id] = permit
            self._emit("permit/refused", permitId=permit.permit_id, agent=agent_id, reason="suspended")
            return permit
        seen = self.seen.get(agent_id, {})
        keep: dict[str, dict] = {}
        for oid, obj in self.objects.items():
            if obj.retracted or _is_deleted(obj.content):
                continue
            base = seen.get(oid, -1)
            hit = self._claim_conflict(agent_id, oid)
            if hit is not None:
                status, info = hit
                keep[oid] = ({"reason": KEEP_RESERVED, "by": info["holder"]} if status == OpStatus.REJECTED_RESERVED
                             else {"reason": KEEP_SELECTED})
                continue
            if obj.occupancy is not None:
                keep[oid] = {"reason": KEEP_HUMAN}
                continue
            if obj.human_touched is not None and self.human_touched_policy == POLICY_CANDIDATE:
                keep[oid] = {"reason": KEEP_HUMAN, "candidate": True}
                continue
            if self._target_conflict(obj, base) is not None:
                keep[oid] = self.reason_for(agent_id, oid)
        permit = Permit(f"permit-{len(self.permits) + 1}", agent_id, keep,
                        {oid: seen.get(oid, -1) for oid in self.objects}, reidentify=self.identity_rule())
        permit.keep_alive = self._ancestors(keep)
        if permit.reidentify is not None:
            protected = permit.no_delete
            wanted = self.wanted_gone.setdefault(agent_id, {})
            for g in [g for g in wanted if g not in protected]:
                del wanted[g]                     # 不再受保护了：它想删的那次已经过去，不再认
            permit.rebind = {g: dict(row) for g, row in sorted(wanted.items())}
            permit.no_recreate = self._tombstones(agent_id, seen)
        self.permits[permit.permit_id] = permit
        self._emit("permit/issued", permitId=permit.permit_id, agent=agent_id, keep=sorted(keep),
                   keepAlive=sorted(permit.keep_alive), rebind=sorted(permit.rebind),
                   noRecreate=sorted(permit.no_recreate))
        return permit

    def _ancestors(self, keep: dict[str, dict]) -> dict[str, dict]:
        """要保持的对象的祖先（design_v0.5.md 12.6）：不许删，面照样可以改。
        删掉父对象，子对象的父对象就断了（Blender），或者被连带删掉（Unity），人改过的子对象跟着坏掉。
        父对象来自账本里每个单元记的 "parent"，对所有应用都一样（P4）。"""
        parents: dict[str, Optional[str]] = {}
        for oid, obj in self.objects.items():
            c = obj.content
            if obj.retracted or _is_deleted(c) or not isinstance(c, dict):
                continue
            parents.setdefault(group_of(oid), c.get("parent"))
        kept = {group_of(uid) for uid in keep}
        out: dict[str, dict] = {}
        for g in sorted(kept):
            p, seen = parents.get(g), {g}
            while p and p not in seen and p in parents:
                seen.add(p)
                if p not in kept:
                    out.setdefault(p, {"reason": KEEP_HUMAN, "ancestor": True, "of": g})
                p = parents.get(p)
        return out

    def _tombstones(self, agent_id: str, seen: dict[str, int]) -> dict[str, dict]:
        """墓碑（design_v0.5.md 12.5）：人删掉的对象，这个 Agent 看到过它还在、但还没看到它被删。
        它新建同名同类型的对象，就是按旧印象把人删掉的东西补回来（R4 + R6），接入代码会把新建的删掉。
        看到过删除之后再新建，算有意的，照常新建。数据块不记墓碑（删数据块的多半是清理，不是"不要这个东西"）。"""
        out: dict[str, dict] = {}
        for oid, obj in sorted(self.objects.items()):
            content = obj.content
            if obj.retracted or not _is_deleted(content) or not obj.versions:
                continue
            last = obj.versions[-1]
            if last.author_kind != ActorKind.HUMAN or content.get("kind") == "data":
                continue
            base = seen.get(oid, -1)
            if base < 0 or base >= obj.version:
                continue                          # 没见过这个对象，或者已经看到它被删了
            try:
                if _is_deleted(obj.get_version(base).content):
                    continue
            except KeyError:
                continue
            g = group_of(oid)
            out.setdefault(g, {"name": content.get("name"), "type": content.get("type"),
                               "parent": content.get("parent")})
        return out

    def close_permit(self, permit: Permit, refused: Iterable[str]) -> list[str]:
        """一次执行结束。refused：Agent 改到了、但按许可单保持原样的对象。
        R23：这个 Agent 之前的预留用完了；这次因为别的 Agent 先改而被拒的，给它预留一段时间重试。"""
        self.reservations = {u: v for u, v in self.reservations.items() if v[0] != permit.agent}
        out = []
        if self.reserve_seconds > 0:
            until = time.time() + self.reserve_seconds
            for oid in sorted(set(refused)):
                k = permit.keep.get(oid)
                if k is not None and k["reason"] == KEEP_OTHER_AGENT:
                    self.reservations[oid] = (permit.agent, until)
                    out.append(oid)
        self._emit("permit/closed", permitId=permit.permit_id, reserved=out)
        return out

    def settle(self, permit: Permit, before: dict[str, Any], raw: dict[str, Any], final: dict[str, Any],
               step_id: Optional[str] = None, produce_type: str = "blob",
               refused: Iterable[str] = (), refused_deletes: Iterable[str] = (),
               identity: Optional[dict] = None) -> Settlement:
        """一次执行结束：按应用交回的实际状态核对，再记账（design_v0.5.md 2.3–2.5、第 6 节）。

        before / raw / final：执行前、执行后（恢复前）、最终的单元 {编号: 内容}，内容是带 "fp" 的 dict。
        - 要保持的单元（许可单里的；以及删掉了不许删的对象时，这个对象的全部单元，R6 按整个对象）：
          最终和执行前一样 → 保持（kept）；不一样 → 违规（breach），按实际状态记账并标明违规。
        - 其余单元：最终状态和执行前不一样的，一律按最终状态提交（applied / side_effects）；
          Agent 改了、但最终又被改回去的，记为 reverted。
        refused / refused_deletes：类型化命令执行前就按许可单拒掉的单元、拒掉删除的应用对象（Permit.screen）。
          算作 Agent 想改、按许可单保持，同样要核对场景里确实没变。
        identity：接入代码认回同一个对象的结果（{"pairs", "kinds", "fresh"}），由运行时按同一条规则重算核对。
          配上的旧编号在 raw / final 里已经是新对象的内容，所以照常按面核对：人改过的面要么还是人的，要么是违规。
          补回人删掉的对象（墓碑）被接入代码删掉了：记为保持（kept，"recreate"）。
        账本只记实际看到的（P1）：结束后账本里每个单元都和 final 一样。"""
        agent = permit.agent
        actor = self._actor(agent)
        st = Settlement(permit.permit_id, agent)
        refused, pre_deleted = set(refused), set(refused_deletes)
        before_groups = {group_of(u) for u in before}
        raw_groups = {group_of(u) for u in raw}
        if permit.reidentify is not None:
            self._settle_identity(st, permit, before, raw, identity, pre_deleted)
        tomb_groups = {g for g, v in st.reidentified.items() if v["kind"] == "tomb"}
        refused_delete = {g for g in before_groups - raw_groups if g in permit.no_delete}
        group_reason = {g: dict(k) for g, k in permit.keep_alive.items()}
        for uid, k in sorted(permit.keep.items()):
            group_reason.setdefault(group_of(uid), k)

        def required(uid: str) -> Optional[dict]:
            if uid in permit.keep:
                return permit.keep[uid]
            g = group_of(uid)
            if g in refused_delete:
                return {**group_reason[g], "whole_object": True}
            return None

        n = 0
        for uid in sorted(set(before) | set(raw) | set(final)):
            b, r, f = before.get(uid), raw.get(uid), final.get(uid)
            # 执行前拒掉的：这条命令改的面；或者删除被拒掉的对象里、没被同一批别的命令改过的面
            precheck = uid in refused or (group_of(uid) in pre_deleted and _fp(b) == _fp(r))
            attempted = _fp(b) != _fp(r) or precheck
            n += 1
            op_id = f"{permit.permit_id}-{n}"
            if b is None or (uid not in self.objects and f is not None):   # 执行前没有（或账本里没有）：新建
                if f is None:
                    if r is not None and group_of(uid) in tomb_groups:          # 补回人删掉的对象，已被删掉
                        st.kept[uid] = {"reason": KEEP_HUMAN, "recreate": True}
                    continue
                self._settle_create(op_id, agent, uid, f, step_id, produce_type)
                (st.created if r is not None and _fp(f) == _fp(r) else st.side_effects).append(uid)
                continue
            if uid not in self.objects:                          # 账本里没有、场景里也没了：没什么可记
                continue
            req = required(uid)
            if req is None and precheck and group_of(uid) in pre_deleted and group_of(uid) in group_reason:
                req = {**group_reason[group_of(uid)], "whole_object": True}
            if req is not None:
                if _fp(f) == _fp(b):
                    if attempted:
                        st.kept[uid] = {**req, "precheck": True} if precheck else req
                        if req.get("candidate") and r is not None:
                            self._settle_candidate(op_id, agent, uid, r, step_id)
                    continue
                st.breach[uid] = {**req, "deleted": f is None}
                self._settle_breach(op_id, agent, actor, uid, f, b, step_id)
                continue
            if _fp(f) == _fp(b):
                if attempted:
                    st.reverted.append(uid)
                continue
            if self._settle_write(op_id, agent, uid, f, b, permit.base.get(uid, -1), step_id):
                (st.applied if attempted and _fp(f) == _fp(r) else st.side_effects).append(uid)
            else:   # 执行期间规则状态变了（例如接入代码没核对、人的修改是事后才记的）：如实记为违规
                st.breach[uid] = {**self.reason_for(agent, uid), "deleted": f is None, "late": True}
                self._settle_breach(op_id, agent, actor, uid, f, b, step_id)

        st.reserved = self.close_permit(permit, list(st.kept))
        if permit.reidentify is not None:
            self._track_wanted_gone(permit, st, before, raw, final, pre_deleted)
        self._detect_loops(st)
        self.mark_seen(agent)
        if st.breach:
            st.outcome = "breach"
            groups = sorted({group_of(u) for u in st.breach})
            self._emit("alert/human", kind="breach", agent=agent, permitId=permit.permit_id, objects=groups)
            if self.pause_on_breach:
                self.suspended[agent] = {"permitId": permit.permit_id, "objects": groups}
                st.suspended = True
                self._emit("agent/suspended", agent=agent, permitId=permit.permit_id, objects=groups)
        self._emit("permit/settled", permitId=permit.permit_id, outcome=st.outcome,
                   applied=st.applied, created=st.created, kept=sorted(st.kept), breach=sorted(st.breach))
        return st

    def _detect_loops(self, st: Settlement) -> None:
        """从账本算：同一个单元在这个 Agent 最近 loop_window 次执行里被保持（它想改、没生效）了几次。
        到 loop_repeat 次记进 st.loops；新进入循环的单元提醒人一次。补回人删掉的对象不算（看到之后再建就是有意的）。"""
        agent = st.agent
        history = self.kept_history.setdefault(agent, [])
        history.append({u for u, k in st.kept.items() if not k.get("recreate")})
        del history[:-self.loop_window]
        counts: dict[str, int] = {}
        for kept in history:
            for u in kept:
                counts[u] = counts.get(u, 0) + 1
        st.loops = {u: n for u, n in sorted(counts.items()) if n >= self.loop_repeat and u in history[-1]}
        alerted = self.looping.setdefault(agent, set())
        alerted &= set(st.loops)                                # 不再反复的，下次再进入循环时重新提醒
        new = sorted(set(st.loops) - alerted)
        st.loop_alert = new
        if new:
            alerted.update(new)
            self._emit("alert/human", kind="loop", agent=agent, permitId=st.permit_id, objects=new,
                       times={u: st.loops[u] for u in new}, window=self.loop_window)

    def _settle_identity(self, st: Settlement, permit: Permit, before: dict[str, Any], raw: dict[str, Any],
                         identity: Optional[dict], pre_deleted: set[str]) -> None:
        """核对接入代码认回的结果（design_v0.5.md 12.4）：只用执行前、执行后（恢复前）的记录，按许可单里的同一条规则
        自己重算一遍（rightofway/identity.py 的 verify）。一致的记进 st.reidentified 和认回记录（带置信等级）；
        不一致的照实记下、提醒人。账本仍按场景的实际状态记（P1）；这是接入代码的问题，不暂停 Agent。"""
        identity = identity or {}
        pairs = {o: list(v) for o, v in (identity.get("pairs") or {}).items()}
        kinds = identity.get("kinds") or {}
        wanted = set(permit.rebind) | set(pre_deleted)
        rows_b, rows_r = _group_rows(before), _group_rows(raw)
        expected, errors = verify_identity(rows_b, rows_r, pairs, identity.get("fresh") or {}, wanted,
                                           permit.no_recreate, (permit.reidentify or {}).get("suffix"))
        if not identity and expected:
            errors.insert(0, "接入代码没有交回认回的结果")
        for old, (new, level) in sorted(pairs.items()):
            if expected.get(old) != [new, level]:
                continue
            if old in permit.no_recreate and old not in rows_b:
                kind = "tomb"
            elif kinds.get(old) == "rebind" and old in wanted:
                kind = "rebind"
            else:
                kind = "gone"
            name = (rows_b.get(old) or permit.no_recreate.get(old) or {}).get("name")
            st.reidentified[old] = {"name": name, "level": level, "kind": kind}
            self.identity_log.append({"permitId": permit.permit_id, "agent": permit.agent, "object": old,
                                      "name": name, "new": new, "level": level, "kind": kind})
            self._emit("identity/matched", permitId=permit.permit_id, agent=permit.agent, objectId=old,
                       level=level, kind=kind)
        st.identity_errors = errors
        if errors:
            self._emit("alert/human", kind="identity", agent=permit.agent, permitId=permit.permit_id,
                       errors=list(errors))

    def _track_wanted_gone(self, permit: Permit, st: Settlement, before: dict[str, Any], raw: dict[str, Any],
                           final: dict[str, Any], pre_deleted: set[str]) -> None:
        """记下 Agent 想删、因为人改过而保留下来的对象：之后它新建同名同类型的对象，就认成这一个（清空和重建分两次执行）。
        认上了、被删掉了、或者 Agent 又去改它（说明它知道这个对象还在）时，不再记。"""
        wanted = self.wanted_gone.setdefault(permit.agent, {})
        rows_b = _group_rows(before)
        final_groups = {group_of(u) for u in final}
        raw_groups = {group_of(u) for u in raw}
        for g in list(wanted):
            if g in st.reidentified or g not in final_groups:
                del wanted[g]
        for uid in raw:
            if uid in before and _fp(before[uid]) != _fp(raw[uid]):
                wanted.pop(group_of(uid), None)
        for g in sorted((set(rows_b) - raw_groups) | pre_deleted):
            if (g in final_groups and g in permit.no_delete and g not in st.reidentified
                    and rows_b.get(g, {}).get("kind") != "data"):
                wanted[g] = {k: rows_b[g].get(k) for k in ("name", "type", "parent")}

    def _settle_write(self, op_id: str, agent: str, uid: str, f: Any, b: Any, base: int,
                      step_id: Optional[str]) -> bool:
        """按最终状态提交一个单元，走和 submit 一样的规则检查。"""
        op = Operation(op_id, agent, targets=[Target(uid, base)], step_id=step_id)
        if self.submit(op).status.is_rejection:
            return False
        content = f if f is not None else _deleted_like(b)
        return self.complete(op_id, writes={uid: content}).status in (OpStatus.COMMITTED, OpStatus.MERGED)

    def _settle_create(self, op_id: str, agent: str, uid: str, f: Any, step_id: Optional[str],
                       produce_type: str) -> None:
        if uid in self.objects and not self.objects[uid].retracted:      # 名义上新建、账本里已有：按修改记
            obj = self.objects[uid]
            if _fp(obj.content) != _fp(f):
                self._commit_version(obj, f, self._actor(agent), op_id, step_id)
            return
        op = Operation(op_id, agent, produces=[uid], step_id=step_id)
        self.submit(op)
        self.complete(op_id, produces={uid: f}, produce_types={uid: produce_type})

    def _settle_candidate(self, op_id: str, agent: str, uid: str, r: Any, step_id: Optional[str]) -> None:
        """R6 "留作候选"：场景里保留人的，Agent 的版本记进候选区。"""
        obj = self.objects[uid]
        self._seq += 1
        obj.candidates.append(VersionRecord(version=-1, content=r, author_id=agent, author_kind=ActorKind.AGENT,
                                            op_id=op_id, step_id=step_id, parent_version=obj.version,
                                            seq=self._seq))
        self._emit("result/candidate", opId=op_id, objects=[uid])

    def _settle_breach(self, op_id: str, agent: str, actor: Actor, uid: str, f: Any, b: Any,
                       step_id: Optional[str]) -> None:
        """违规：规则要求保持，但应用里实际变了。账本照实记下（P1），标明违规；"人碰过"的标记保留。"""
        if uid not in self.objects:
            self.objects[uid] = ObjectState(uid, "blob")
        obj = self.objects[uid]
        prior = obj.human_touched
        op = Operation(op_id, agent, targets=[Target(uid, obj.version)], step_id=step_id,
                       actor_kind=actor.kind, status=OpStatus.BREACH)
        self.ops[op_id] = op
        rec = self._commit_version(obj, f if f is not None else _deleted_like(b), actor, op_id, step_id)
        rec.breach = True
        obj.human_touched = prior
        self._emit("op/breach", opId=op_id, agent=agent, objectId=uid, version=rec.version,
                   deleted=f is None)
        self._emit("object/changed", objectId=uid, version=rec.version, author=agent, opId=op_id, breach=True)

    def resume_agent(self, actor_id: str, agent_id: str) -> None:
        """人处理完违规之后，让这个 Agent 继续。"""
        self._require_human(actor_id)
        if self.suspended.pop(agent_id, None) is not None:
            self._emit("agent/resumed", agent=agent_id, by=actor_id)

    def guarded(self, for_agent: Optional[str] = None) -> set[str]:
        """人碰过或正被占用、还在的对象；for_agent 时再加上预留给别的 Agent 的。（显示和兼容用）"""
        out = {oid for oid, o in self.objects.items()
               if (o.human_touched is not None or o.occupancy is not None)
               and not o.retracted and not _is_deleted(o.content)}
        if for_agent is not None:
            self._expire_reservations()
            out |= {oid for oid, (h, _) in self.reservations.items() if h != for_agent}
        return out

    # ------------------------------------------------------------------
    # 人直接修改（R2、R5、R8、R9、R12）
    # ------------------------------------------------------------------
    def human_edit(self, actor_id: str, object_id: str, content: Any, occupy: bool = True) -> VersionRecord:
        """occupy=False：修改是事后（例如存盘时）才发现的，无法知道人是否还在改，
        所以只记"人碰过"，不产生占用（R8，和外部修改同样的理由）。"""
        actor = self._require_human(actor_id)
        obj = self.objects[object_id]
        rec = self._commit_version(obj, content, actor)               # R5：总能提交
        self._emit("object/changed", objectId=object_id, version=rec.version, author=actor_id)
        self._invalidate_from(object_id)                              # R12
        if occupy and obj.occupancy is None:                          # R8：第一次修改即占用
            obj.occupancy = Occupancy(holder=actor_id, since_seq=self._seq)
            self._emit("occupancy/acquired", objectId=object_id, holder=actor_id)
            self._block_readers(object_id)                            # R9
        return rec

    def view(self, actor_id: str, object_id: str) -> None:
        """R8：只看不产生占用。"""
        self._actor(actor_id)
        self._emit("object/viewed", objectId=object_id, actor=actor_id)

    def ui_event(self, actor_id: str, kind: str, object_id: Optional[str] = None) -> None:
        """R10：切换窗口、关闭窗口、长时间无操作，都不算释放。这里只记日志。"""
        self._emit("ui/event", actor=actor_id, kind=kind, objectId=object_id)

    def release(self, actor_id: str, object_id: str) -> None:
        """R10：只有占用者明确释放才结束占用。"""
        self._require_human(actor_id)
        obj = self.objects[object_id]
        if obj.occupancy is None or obj.occupancy.holder != actor_id:
            raise ProtocolError(f"{actor_id} 没有占用 {object_id}，不能释放")
        obj.occupancy = None
        self._emit("occupancy/released", objectId=object_id, holder=actor_id)
        for step in self.steps.values():
            if step.state == StepState.BLOCKED and not self._inputs_occupied(step):
                self._set_step(step, StepState.PENDING)

    def hand_back(self, actor_id: str, object_id: str) -> None:
        """R7：人明确允许 AI 再修改这个对象。"""
        self._require_human(actor_id)
        obj = self.objects[object_id]
        obj.human_touched = None
        self._emit("object/handback", objectId=object_id, actor=actor_id)

    def external_change(self, object_id: str, content: Any, source: str = "file_watch") -> VersionRecord:
        """第 2 级：只靠文件监视发现的修改，记为 external。外部修改不产生占用。"""
        ext_id = f"external:{source}"
        if ext_id not in self.actors:
            self.register_actor(ext_id, ActorKind.EXTERNAL)
        obj = self.objects[object_id]
        # R21（第 2 级）：内容和某个更早的版本相同，说明很可能用旧副本覆盖了较新的版本
        overwritten = obj.version
        for rec in obj.versions[:-1]:
            if rec.content == content and rec.content != obj.content:
                self._emit("conflict/external_overwrite", objectId=object_id,
                           overwrittenVersion=overwritten, matchesVersion=rec.version)
                break
        rec = self._commit_version(obj, content, self.actors[ext_id])
        self._emit("object/changed", objectId=object_id, version=rec.version, author=ext_id)
        self._invalidate_from(object_id)
        return rec

    def report_observed_change(self, actor_id: str, element_id: str, snapshot: Any,
                               parent_of: dict[str, str]) -> VersionRecord:
        """R18：适配器报告观察到的变化。观察不到对象这一级时，向上归并到最近的外层对象。"""
        target = element_id
        while target not in self.objects:
            if target not in parent_of:
                raise ObservationError(f"找不到 {element_id} 所属的对象，不能静默丢弃")
            target = parent_of[target]
        self._emit("observation/resolved", element=element_id, objectId=target)
        actor = self._actor(actor_id)
        if actor.kind == ActorKind.HUMAN:
            return self.human_edit(actor_id, target, snapshot)
        return self.external_change(target, snapshot)

    # ------------------------------------------------------------------
    # 分工（R11）
    # ------------------------------------------------------------------
    def claim_step(self, actor_id: str, step_id: str) -> None:
        self._require_human(actor_id)
        step = self.steps[step_id]
        if step.state not in (StepState.PENDING, StepState.STALE, StepState.UNDONE):
            raise ProtocolError(f"{step_id} 当前状态为 {step.state.value}，不能认领")
        step.claimed_by = actor_id
        self._set_step(step, StepState.CLAIMED)

    def complete_human_step(self, actor_id: str, step_id: str, outputs: dict[str, Any]) -> None:
        actor = self._require_human(actor_id)
        step = self.steps[step_id]
        for oid, content in outputs.items():
            if oid in self.objects and not self.objects[oid].retracted:
                self._commit_version(self.objects[oid], content, actor, step_id=step_id)
            else:
                obj = self.objects.get(oid) or ObjectState(oid, "blob", provenance={"stepId": step_id})
                self.objects[oid] = obj
                self._commit_version(obj, content, actor, step_id=step_id)
            self._emit("object/changed", objectId=oid, version=self.objects[oid].version, author=actor_id)
        step.done_seq = self._seq
        self._set_step(step, StepState.DONE_BY_HUMAN)
        for oid in outputs:
            self._invalidate_from(oid, exclude_step=step_id)

    def reopen_step(self, actor_id: str, step_id: str) -> None:
        """R14：被撤回的步骤只能由人重新发起。"""
        self._require_human(actor_id)
        step = self.steps[step_id]
        if step.state != StepState.UNDONE:
            raise ProtocolError(f"{step_id} 不是撤回状态")
        self._set_step(step, StepState.PENDING)

    # ------------------------------------------------------------------
    # 失效传播与阻塞（R9、R12）
    # ------------------------------------------------------------------
    def _readers(self, object_id: str) -> list[Step]:
        return [s for s in self.steps.values() if object_id in s.inputs]

    def _inputs_occupied(self, step: Step) -> bool:
        return any(self.objects.get(i) is not None and self.objects[i].occupancy is not None
                   for i in step.inputs)

    def _set_step(self, step: Step, state: StepState) -> None:
        if step.state != state:
            step.state = state
            self._emit("step/state", stepId=step.step_id, state=state.value)

    def _invalidate_from(self, object_id: str, exclude_step: Optional[str] = None) -> None:
        """R12：只把直接或间接依赖这个对象的、已完成的步骤标为过期。"""
        queue = [object_id]
        seen: set[str] = set()
        while queue:
            oid = queue.pop()
            for step in self._readers(oid):
                if step.step_id in seen or step.step_id == exclude_step:
                    continue
                seen.add(step.step_id)
                if step.state in (StepState.DONE, StepState.DONE_BY_HUMAN):
                    self._set_step(step, StepState.STALE)
                    queue.extend(step.outputs)

    def _block_readers(self, object_id: str) -> None:
        for step in self._readers(object_id):
            if step.state in (StepState.PENDING, StepState.RUNNING, StepState.STALE):
                self._set_step(step, StepState.BLOCKED)

    # ------------------------------------------------------------------
    # 撤回（R14）
    # ------------------------------------------------------------------
    def undo_last_agent_step(self, actor_id: str) -> dict:
        done = [s for s in self.steps.values() if s.state == StepState.DONE and s.committed_ops]
        if not done:
            raise ProtocolError("没有可以撤回的 AI 步骤")
        latest = max(done, key=lambda s: s.done_seq)
        return self.undo_step(actor_id, latest.step_id)

    def undo_step(self, actor_id: str, step_id: str) -> dict:
        actor = self._require_human(actor_id)
        step = self.steps[step_id]
        if step.state != StepState.DONE:
            raise ProtocolError(f"{step_id} 当前状态为 {step.state.value}，不能撤回")
        ops = set(step.committed_ops)
        restored, retracted, skipped = [], [], []
        for obj in self.objects.values():
            mine = [v for v in obj.versions if v.op_id in ops and v.author_kind == ActorKind.AGENT]
            if not mine:
                continue
            if obj.human_touched is not None:                         # R14 + R6：人碰过的不动
                skipped.append({"objectId": obj.object_id, "reason": "human_touched"})
                continue
            if obj.versions[-1].op_id not in ops:                     # 之后又被别的操作改过
                skipped.append({"objectId": obj.object_id, "reason": "changed_after"})
                continue
            first = mine[0]
            if first.parent_version is None:                          # 这一步新建的对象：撤掉
                obj.retracted = True
                retracted.append(obj.object_id)
                self._emit("object/retracted", objectId=obj.object_id, opId=first.op_id)
            else:
                old = obj.get_version(first.parent_version)
                self._commit_version(obj, old.content, actor, restored_from=old.version)
                # 撤回产生的版本，"人碰过"状态继承自被恢复的那个版本
                obj.human_touched = (HumanTouched(old.author_id, obj.version)
                                     if old.author_kind in (ActorKind.HUMAN, ActorKind.EXTERNAL) else None)
                restored.append(obj.object_id)
        self._set_step(step, StepState.UNDONE)
        self._emit("step/undone", stepId=step_id, by=actor_id, restored=restored,
                   retracted=retracted, skipped=skipped)
        for oid in restored + retracted:
            self._invalidate_from(oid, exclude_step=step_id)
        return {"restored": restored, "retracted": retracted, "skipped": skipped}

    # ------------------------------------------------------------------
    # 暂停与恢复（R17）
    # ------------------------------------------------------------------
    def pause(self, actor_id: str) -> None:
        self._require_human(actor_id)
        self.paused = True
        for op in self.ops.values():
            if op.status == OpStatus.EXECUTING:
                meta = self.capabilities.get(op.capability) if op.capability else None
                if meta is None or meta.cancellable:
                    op.status = OpStatus.CANCELLED
                    self._emit("op/result", opId=op.op_id, status=op.status.value)
        self._emit("run/paused", by=actor_id)

    def resume(self, actor_id: str) -> None:
        self._require_human(actor_id)
        self.paused = False
        self._emit("run/resumed", by=actor_id)
        for op in list(self.ops.values()):
            if op.status == OpStatus.HELD:
                result, op.held_result = op.held_result, None
                op.status = OpStatus.EXECUTING
                self._finalize(op, result)
        for op in list(self.ops.values()):
            if op.status == OpStatus.QUEUED:
                self._dispatch(op)
        for step in self.steps.values():               # 被取消的操作可能让步骤停在运行中
            if step.state == StepState.RUNNING and not any(
                    o.step_id == step.step_id and o.status in IN_FLIGHT for o in self.ops.values()):
                self._set_step(step, StepState.DONE if step.committed_ops else StepState.PENDING)

    # ------------------------------------------------------------------
    # 集合与引用（R13）
    # ------------------------------------------------------------------
    def is_referenced(self, object_id: str, excluding_step: Optional[str] = None) -> bool:
        return any(object_id in s.inputs for s in self.steps.values() if s.step_id != excluding_step)

    def retract_object(self, object_id: str, reason: str) -> None:
        obj = self.objects[object_id]
        obj.retracted = True
        self._emit("object/retracted", objectId=object_id, reason=reason)

    # ------------------------------------------------------------------
    # 日志（R20）
    # ------------------------------------------------------------------
    def log_since(self, seq: int) -> list[Event]:
        return [e for e in self.events if e.seq > seq]

    def events_of(self, type_: str) -> list[Event]:
        return [e for e in self.events if e.type == type_]


def _is_deleted(content: Any) -> bool:
    """接入层用 {"deleted": True, ...} 记录"对象已被删除"。"""
    return isinstance(content, dict) and content.get("deleted") is True


_IDENTITY_KEYS = ("name", "type", "parent", "kind", "display")


def _deleted_like(before: Any) -> dict:
    """"已删除"的内容：保留名字、类型、父对象（认回同一个对象、拦下补回人删掉的对象时要用）。"""
    keep = {k: before[k] for k in _IDENTITY_KEYS if isinstance(before, dict) and before.get(k) is not None}
    return {"deleted": True, **keep}


def _group_rows(units: dict[str, Any]) -> dict[str, dict]:
    """单元 → 应用对象的身份行 {对象编号: {"name", "type", "parent", "kind", "users"}}（认回时用）。"""
    out: dict[str, dict] = {}
    for uid, c in sorted(units.items()):
        g = group_of(uid)
        if g in out or not isinstance(c, dict):
            continue
        out[g] = {k: c.get(k) for k in ("name", "type", "parent", "kind", "users")}
    return out


def _fp(content: Any) -> Any:
    """核对用的指纹：内容是带 "fp" 的 dict 时取 fp，否则就是内容本身。None 表示不存在。"""
    if isinstance(content, dict) and "fp" in content:
        return content["fp"]
    return content
