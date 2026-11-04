"""核心的数据模型（spec/core.md 第 2 节）：操作者、按编号的容器里的单元和它的标记（版本记录）、许可单、结算。

所有类都是普通的 dataclass，只用标准库。早期的操作模型（逐项提交的操作、步骤、能力元数据）不在这里，
在 rightofway/model.py（它也把这里的类原样导出）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .rules import KEEP_HUMAN, KEEP_LOCKED, KEEP_OTHER, KEEP_OTHER_AGENT, KEEP_RESERVED, KEEP_SELECTED  # noqa: F401

__all__ = ["ActorKind", "Actor", "AuthorityKind", "Authority", "HumanTouched", "Occupancy", "VersionRecord",
           "ObjectState", "Event", "KEEP_HUMAN", "KEEP_SELECTED", "KEEP_OTHER_AGENT", "KEEP_RESERVED", "KEEP_OTHER",
           "KEEP_LOCKED", "group_of", "face_of", "Permit", "Settlement"]


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
    # 稀疏账本（Runtime.set_baseline）：没人改过的单元不建记录，是"基线"（版本 0）。第一次被改时才建记录，
    # 版本 0 的内容和作者记在这里；版本 1 起照常记在 versions 里。None 表示这个单元不是从基线来的（新建的）。
    baseline: Any = None
    baseline_author: Optional[str] = None
    baseline_kind: Optional[ActorKind] = None

    @property
    def version(self) -> int:
        return self.versions[-1].version if self.versions else 0

    @property
    def content(self) -> Any:
        return self.versions[-1].content if self.versions else self.baseline

    def get_version(self, v: int) -> VersionRecord:
        for rec in self.versions:
            if rec.version == v:
                return rec
        if v == 0 and self.baseline is not None:
            return VersionRecord(0, self.baseline, self.baseline_author or "baseline",
                                 self.baseline_kind or ActorKind.AGENT)
        raise KeyError(f"{self.object_id} 没有版本 {v}")


# ---------------------------------------------------------------------------
# 事件（对应规范第 7 节的消息）
# ---------------------------------------------------------------------------
@dataclass
class Event:
    seq: int
    type: str
    data: dict



# ---------------------------------------------------------------------------
# 许可单（spec/core.md 第 5 节）：一次 AI 执行之前，核心开出的"哪些可以改、哪些要保持"
# ---------------------------------------------------------------------------
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
    # 这张许可单走到哪一步了（design_v0.5.md 14.1）。每张必须正好结束一次：
    #   issued → dispatched → settled（照实际状态记账）
    #                       → closed（没有执行：场景变了要重开、执行前就拒、或者查清了根本没执行）
    #                       → in_doubt（发出去了，结果没收到）→ settled / closed / unattributed（查不回来，差异记为来源不明）
    #   refused（准入就没过：人暂停了全部 AI、这个 AI 被暂停、或者有一次执行结果还没查清）
    state: str = "issued"

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
    #   "kind": gone（删掉又新建）| rebind（之前想删、被保留的，又新建了）| tomb（补回人删掉的，已拦下），
    #   "inferred": True（按名字、类型、父对象推断的，不是应用自己保持下来的身份；人可以撤销，design_v0.5.md 14.5）}
    reidentified: dict[str, dict] = field(default_factory=dict)
    identity_errors: list[str] = field(default_factory=list)   # 接入代码交回的配对和规则不一致
    holds: list[str] = field(default_factory=list)             # 拦下的补回：人删掉、还没交还的对象（应用对象编号）
    loops: dict[str, int] = field(default_factory=dict)        # 反复改改不动的单元 → 最近几次执行里被保持了几次
    loop_alert: list[str] = field(default_factory=list)        # 这次新进入循环、已提醒人的单元
