"""数据模型：对应规范第 5 节。

核心用到的（操作者、单元和版本记录、许可单、结算）在 rightofway/core/model.py，这里原样导出；
这里另外定义早期版本的操作模型（逐项提交的操作、步骤、能力元数据），给 runtime.py 里的旧流程
（实时同步、文件兜底、旧场景的测试）用。所有类都是普通的 dataclass，没有任何外部依赖，方便阅读和调试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .core.model import (KEEP_HUMAN, KEEP_LOCKED, KEEP_OTHER, KEEP_OTHER_AGENT, KEEP_RESERVED,
                         KEEP_SELECTED, Actor, ActorKind, Authority, AuthorityKind, Event, HumanTouched, ObjectState,
                         Occupancy, Permit, Settlement, VersionRecord, face_of, group_of)

__all__ = ["KEEP_HUMAN", "KEEP_LOCKED", "KEEP_OTHER", "KEEP_OTHER_AGENT", "KEEP_RESERVED", "KEEP_SELECTED", "Actor",
           "ActorKind", "Authority", "AuthorityKind", "Event", "HumanTouched", "ObjectState", "Occupancy", "Permit",
           "Settlement", "VersionRecord", "face_of", "group_of", "Channel", "CapabilityMeta", "Target", "OpStatus",
           "Operation", "OpResult", "StepState", "Step"]


# ---------------------------------------------------------------------------
# 5.4 能力元数据
# ---------------------------------------------------------------------------
class Channel(str, Enum):
    """能力通过哪条通道写入对象（用于 R21 权威副本检查）。"""
    SESSION = "session"  # 经由应用会话写入
    STORAGE = "storage"  # 直接写存储副本（例如命令行改磁盘文件）


@dataclass
class CapabilityMeta:
    name: str
    effect_level: int = 1          # 0 只产生新对象；1 可回退的修改；2 外部状态可查询；3 对外不可逆
    cancellable: bool = True
    idempotent: bool = False
    channel: Channel = Channel.SESSION
    verified: bool = True          # R19：违反契约后会被标为未验证


# ---------------------------------------------------------------------------
# 5.5 操作
# ---------------------------------------------------------------------------
@dataclass
class Target:
    """操作要原地修改的对象，以及操作者所依据的基准版本。"""
    object_id: str
    base_version: int


class OpStatus(str, Enum):
    QUEUED = "queued"                                  # 暂停期间提交，等待恢复后再派发
    PENDING_CONFIRMATION = "pending_confirmation"      # 等待人确认（副作用等级 3）
    EXECUTING = "executing"                            # 已派发，正在执行
    HELD = "held"                                      # 暂停期间执行完成，结果暂存
    COMMITTED = "committed"
    MERGED = "merged"
    REJECTED_STALE = "rejected_stale"
    REJECTED_HUMAN_TOUCHED = "rejected_human_touched"
    REJECTED_OCCUPIED = "rejected_occupied"
    REJECTED_RESERVED = "rejected_reserved"            # R23：预留给了另一个 Agent
    REJECTED_SUSPENDED = "rejected_suspended"          # 这个 Agent 上次违规后被暂停，等人处理
    REJECTED_IN_DOUBT = "rejected_in_doubt"            # 有一次 Agent 执行的结果还没查清（design_v0.5.md 14.2）
    BREACH = "breach"                                  # 违规：要保持的部分在应用里被改了，没能恢复；按实际状态记账
    REJECTED_BY_HUMAN = "rejected_by_human"
    REJECTED_STEP_ASSIGNMENT = "rejected_step_assignment"  # 步骤已被人认领或分配给人
    REJECTED_AUTHORITY = "rejected_authority"              # 权威在会话里却想直接写存储副本
    CANDIDATE = "candidate"                            # "留作候选"策略：结果另存，不成为当前版本
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def is_rejection(self) -> bool:
        return self.value.startswith("rejected")


@dataclass
class Operation:
    op_id: str
    actor_id: str
    capability: Optional[str] = None
    targets: list[Target] = field(default_factory=list)   # 原地修改的对象（写集）
    reads: list[Target] = field(default_factory=list)     # 读取的对象及读取时的版本（读集）
    produces: list[str] = field(default_factory=list)     # 会新建的对象
    step_id: Optional[str] = None
    # 以下字段由运行时填写，操作者自己填的值会被忽略
    actor_kind: Optional[ActorKind] = None
    effect_level: int = 1
    status: OpStatus = OpStatus.EXECUTING
    held_result: Optional[dict] = None
    reason: Optional[dict] = None


@dataclass
class OpResult:
    op_id: str
    status: OpStatus
    reason: Optional[dict] = None
    new_versions: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 5.6 步骤
# ---------------------------------------------------------------------------
class StepState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    CLAIMED = "claimed"
    DONE = "done"
    DONE_BY_HUMAN = "done_by_human"
    STALE = "stale"
    UNDONE = "undone"


@dataclass
class Step:
    step_id: str
    inputs: list[str]
    outputs: list[str]
    assignee: str = "agent"        # agent | human | any
    state: StepState = StepState.PENDING
    claimed_by: Optional[str] = None
    committed_ops: list[str] = field(default_factory=list)   # 最近一次执行中已提交的操作
    done_seq: int = 0                                         # 完成时的事件序号，用于"撤回最近一步"


