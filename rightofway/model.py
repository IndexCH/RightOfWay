"""数据模型：对应规范第 5 节。

所有类都是普通的 dataclass，没有任何外部依赖，方便阅读和调试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 5.1 操作者
# ---------------------------------------------------------------------------
class ActorKind(str, Enum):
    HUMAN = "human"        # 人
    AGENT = "agent"        # AI
    EXTERNAL = "external"  # 来源无法确认的外部修改（例如只靠文件监视发现的修改）


@dataclass
class Actor:
    actor_id: str
    kind: ActorKind


# ---------------------------------------------------------------------------
# 5.2 对象 / 5.3 版本记录
# ---------------------------------------------------------------------------
class AuthorityKind(str, Enum):
    STORAGE = "storage"  # 权威副本在磁盘或远端存储
    SESSION = "session"  # 权威副本在某个应用会话里（例如 PS 里打开的文档）


@dataclass
class Authority:
    kind: AuthorityKind = AuthorityKind.STORAGE
    adapter_id: Optional[str] = None


@dataclass
class HumanTouched:
    """'人碰过'标记：对象的最新版本由人或外部修改产生。"""
    by: str
    since_version: int


@dataclass
class Occupancy:
    """占用：人正在修改这个对象。"""
    holder: str
    since_seq: int


@dataclass
class VersionRecord:
    version: int
    content: Any
    author_id: str
    author_kind: ActorKind
    op_id: Optional[str] = None
    step_id: Optional[str] = None
    parent_version: Optional[int] = None
    seq: int = 0                       # 在全局事件序列中的位置
    restored_from: Optional[int] = None  # 撤回产生的版本：恢复自哪个版本


@dataclass
class ObjectState:
    object_id: str
    type: str
    versions: list[VersionRecord] = field(default_factory=list)
    authority: Authority = field(default_factory=Authority)
    provenance: Optional[dict] = None
    human_touched: Optional[HumanTouched] = None
    occupancy: Optional[Occupancy] = None
    mergeable: bool = False            # 对象类型是否支持三方合并（例如纯文本）
    retracted: bool = False            # 是否已被撤掉（撤回或集合缩小）
    candidates: list[VersionRecord] = field(default_factory=list)  # R6 "留作候选"策略下保存的 AI 结果

    @property
    def version(self) -> int:
        return self.versions[-1].version if self.versions else 0

    @property
    def content(self) -> Any:
        return self.versions[-1].content if self.versions else None

    def get_version(self, v: int) -> VersionRecord:
        for rec in self.versions:
            if rec.version == v:
                return rec
        raise KeyError(f"{self.object_id} 没有版本 {v}")


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


# ---------------------------------------------------------------------------
# 事件（对应规范第 7 节的消息）
# ---------------------------------------------------------------------------
@dataclass
class Event:
    seq: int
    type: str
    data: dict
