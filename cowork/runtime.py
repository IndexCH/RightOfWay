"""运行时：执行规范第 6 节的全部规则（R1–R21）。

阅读建议：
  1. 先看 submit()  —— Agent 提交操作时做哪些检查
  2. 再看 complete() / _finalize() —— 操作执行完之后怎么决定"提交还是作废"
  3. 再看 human_edit() —— 人修改时发生什么（占用、人碰过、失效传播）
  4. 其余方法对应释放、交还、认领、撤回、暂停、确认、观察、日志

每个关键判断旁边都标了对应的规则编号，可以和 spec/spec_v0.2.md 对照着看。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from .merge import three_way_merge
from .model import (
    Actor,
    ActorKind,
    Authority,
    AuthorityKind,
    CapabilityMeta,
    Channel,
    Event,
    HumanTouched,
    ObjectState,
    Occupancy,
    Operation,
    OpResult,
    OpStatus,
    Step,
    StepState,
    Target,
    VersionRecord,
)

# 仍在进行中的操作状态（步骤以此判断是否全部完成）
IN_FLIGHT = {OpStatus.QUEUED, OpStatus.PENDING_CONFIRMATION, OpStatus.EXECUTING, OpStatus.HELD}

# "人碰过"对象遇到 AI 写入时的处理策略（R6）
POLICY_DISCARD = "discard"      # 默认：丢弃 AI 的结果
POLICY_CANDIDATE = "candidate"  # 另存为候选，不成为当前版本
POLICY_MERGE = "merge"          # 可合并对象尝试三方合并，失败再丢弃


class ProtocolError(Exception):
    """调用方违反协议（例如 Agent 冒充人、释放别人占用的对象）。"""


class ObservationError(ProtocolError):
    """R18：适配器报告的变化找不到可归属的对象，不能静默丢弃。"""


class Runtime:
    def __init__(self, human_touched_policy: str = POLICY_DISCARD) -> None:
        assert human_touched_policy in (POLICY_DISCARD, POLICY_CANDIDATE, POLICY_MERGE)
        self.human_touched_policy = human_touched_policy
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
            obj = self.objects[t.object_id]
            stale = t.base_version != obj.version
            touched = obj.human_touched is not None
            if touched and self.human_touched_policy == POLICY_CANDIDATE:
                continue                                  # R6 替代策略：执行完再存为候选
            if obj.mergeable and stale and (not touched or self.human_touched_policy == POLICY_MERGE):
                continue                                  # 执行完再尝试合并
            if stale:                                     # R4
                return OpStatus.REJECTED_STALE, {"objectId": t.object_id, "baseVersion": t.base_version,
                                                 "currentVersion": obj.version, "set": "targets"}
            if touched:                                   # R6
                return OpStatus.REJECTED_HUMAN_TOUCHED, {"objectId": t.object_id, "by": obj.human_touched.by}
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
