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
    breach: bool = False               # 违规产生的版本：规则要求保持，但应用里实际被改了，按实际状态记下


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
    REJECTED_RESERVED = "rejected_reserved"            # R23：预留给了另一个 Agent
    REJECTED_SUSPENDED = "rejected_suspended"          # 这个 Agent 上次违规后被暂停，等人处理
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


# ---------------------------------------------------------------------------
# 事件（对应规范第 7 节的消息）
# ---------------------------------------------------------------------------
@dataclass
class Event:
    seq: int
    type: str
    data: dict


# ---------------------------------------------------------------------------
# 许可单（design_v0.5.md 2.1）：一次 Agent 执行之前，运行时开出的"哪些可以改、哪些要保持"
# ---------------------------------------------------------------------------
# 要保持的原因
KEEP_HUMAN = "human"            # 人改过，或者人正在改（占用）
KEEP_SELECTED = "selected"      # 人正选中着（"选中即占用"选项）
KEEP_OTHER_AGENT = "other_agent"  # 别的 Agent 在这个 Agent 上次看之后改过（后到的让先到的）
KEEP_RESERVED = "reserved"      # 预留给了另一个 Agent
KEEP_OTHER = "other"


def group_of(object_id: str) -> str:
    """运行时里的对象可以是应用里一个对象的某个面，编号写成"对象编号#面"。返回它所属的应用对象。"""
    return object_id.partition("#")[0]


def face_of(object_id: str) -> Optional[str]:
    return object_id.partition("#")[2] or None


@dataclass
class Permit:
    permit_id: str
    agent: str
    keep: dict[str, dict] = field(default_factory=dict)   # 要保持的单元 → {"reason": ..., "by": ..., "candidate": ...}
    base: dict[str, int] = field(default_factory=dict)    # 每个单元的基准版本：这个 Agent 上次看到的版本
    refused: Optional[dict] = None                        # 整次不许执行（例如这个 Agent 被暂停了）：原因
    # 认回同一个对象（design_v0.5.md 12.4、12.5）。规则在 rightofway/identity.py，运行时和接入代码用同一份。
    reidentify: Optional[dict] = None                     # {"rule": ..., "suffix": 探测出的重名后缀}；None 表示不认
    rebind: dict[str, dict] = field(default_factory=dict)       # Agent 之前想删、因为人改过而保留下来的对象 → {name, type, parent}
    no_recreate: dict[str, dict] = field(default_factory=dict)  # 墓碑：人删掉、这个 Agent 还没看到的对象 → {name, type, parent}
    # 不许删、但面可以改的应用对象：要保持的对象的祖先（design_v0.5.md 12.6）→ {"reason": ..., "ancestor": True}
    keep_alive: dict[str, dict] = field(default_factory=dict)

    @property
    def no_delete(self) -> set[str]:
        """不许删的应用对象：有任何一个单元要保持的对象，整个不许删（R6 按整个对象）；还有它们的祖先（12.6）。"""
        return {group_of(uid) for uid in self.keep} | set(self.keep_alive)

    def keep_by_group(self) -> dict[str, list[str]]:
        """按应用对象分组：{对象编号: [面]}，"*" 表示整个对象。接入代码照这个恢复。"""
        out: dict[str, set[str]] = {}
        for uid in self.keep:
            out.setdefault(group_of(uid), set()).add(face_of(uid) or "*")
        return {g: sorted(v) for g, v in sorted(out.items())}

    def screen(self, group: str, faces=(), delete: bool = False) -> tuple[list[str], dict[str, dict]]:
        """类型化命令的预检查：一条命令要改应用对象 group 的这些面（delete=True：要删掉它）。
        返回 (可以执行的面, 不许执行的 {面: 原因})；整个对象都要保持、或者删除不许删的对象时，键是 "*"。
        和恢复用的是同一张许可单，规则只写一处（P9）。"""
        mine = {face_of(uid) or "*": k for uid, k in sorted(self.keep.items()) if group_of(uid) == group}
        if delete:
            if mine:
                return [], {"*": next(iter(mine.values()))}
            if group in self.keep_alive:
                return [], {"*": self.keep_alive[group]}
            return ["*"], {}
        if not mine:
            return list(faces), {}
        if "*" in mine:
            return [], {"*": mine["*"]}
        return [f for f in faces if f not in mine], {f: mine[f] for f in faces if f in mine}


@dataclass
class Settlement:
    """一次执行结束后，运行时按应用交回的实际状态核对、记账的结果（design_v0.5.md 2.3–2.5）。
    给 Agent 和人的说明只从这里生成。"""
    permit_id: str
    agent: str
    outcome: str = "committed"                                 # committed | breach
    applied: list[str] = field(default_factory=list)           # Agent 改的已生效：场景里就是它改成的样子
    created: list[str] = field(default_factory=list)           # Agent 新建的
    kept: dict[str, dict] = field(default_factory=dict)        # Agent 改到了、按许可单保持原样，场景里确实没变
    breach: dict[str, dict] = field(default_factory=dict)      # 要保持、但场景里变了（恢复失败或恢复不了）
    reverted: list[str] = field(default_factory=list)          # 不用保持、但场景里被改回去了（接入代码多恢复了）
    side_effects: list[str] = field(default_factory=list)      # Agent 没直接改、但场景里变了（例如恢复时连带的）
    reserved: list[str] = field(default_factory=list)          # R23：这次给这个 Agent 的预留
    suspended: bool = False                                    # 因为违规，这个 Agent 被暂停了
    # 认回同一个对象（运行时核对过的）：旧对象编号 → {"name", "level": exact|suffix|global,
    #   "kind": gone（删掉又新建）| rebind（之前想删、被保留的，又新建了）| tomb（补回人删掉的，已拦下）}
    reidentified: dict[str, dict] = field(default_factory=dict)
    identity_errors: list[str] = field(default_factory=list)   # 接入代码交回的配对和规则不一致
    loops: dict[str, int] = field(default_factory=dict)        # 反复改改不动的单元 → 最近几次执行里被保持了几次
    loop_alert: list[str] = field(default_factory=list)        # 这次新进入循环、已提醒人的单元
