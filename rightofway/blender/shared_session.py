"""方式一：人和 AI 改同一份（同一个打开着的 Blender），实时。

AI 通过现成的 Blender MCP 执行脚本。这个类站在 AI 和 Blender 之间（也就是 harness 的位置），
只负责传话，不判断规则（P9）：AI 的每段脚本都交给 run_agent()，
先由运行时开许可单（哪些要保持、为什么），再交给应用照单原子地执行（核对 → 快照 → 执行 → 比较 → 恢复），
最后按许可单把结果记进运行时。人在 Blender 界面里的修改由 poll() 定时发现。

两种"上次看到的场景"分开记：
- 运行时上次看到的（self.view）：每次轮询都更新，用来判断哪些变化是人做的。
- 每个 AI 上次看到的：只在这个 AI 执行完、或者它读场景（observe）时更新。
  看到的版本记在运行时里（Runtime.seen），用来判断它的修改是不是按旧印象做的；
  看到的样子和值记在这里（self.agent_views），用来告诉它错过了什么。

多个 AI：用 add_agent() 登记，run_agent(..., agent=名字)。某个 AI 因为别的 AI 刚改过而被拒时，
运行时给它预留一小段时间（reserve_seconds），期间别的 AI 不能改，避免两个 AI 来回互相作废。

选项 occupy_selection：人选中的对象也算"正在改"，AI 不能动，取消选中就解除（由运行时判断）。

粒度（granularity）：
- "aspect"（默认）：每个 Blender 对象拆成几个面（位置、材质、网格……），运行时按面记录谁改过。
- "object"：整个 Blender 对象算一个。

应用和它的 MCP 插件都不用改：用到的只有插件已有的"执行代码"命令。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from ..core import rules as R
from ..core.identity import strip_suffix
from ..model import (KEEP_HUMAN, KEEP_OTHER, KEEP_OTHER_AGENT, KEEP_RESERVED, KEEP_SELECTED, ActorKind,
                     CapabilityMeta, Operation, OpStatus, Target)
from ..runtime import POLICY_CANDIDATE, Runtime
from .changes import Values, describe_changes
from ..core import explain as X
from ..core.store import AppendLog
from . import messages as MSG
from .records import (ASPECT, DELETED, OBJECT, Diff, Records, diff_records, display, label, split_unit, units)

OBJECT_TYPE = "blender.object"
CAPABILITY = "blender.execute_code"
BASELINE_ACTOR = "baseline"     # 开始协作前场景里已有的内容，记在这个名下（不算"人碰过"）

# AI 的修改没生效的原因：由运行时的许可单给出（rightofway/model.py），这里只是别名
R_HUMAN = KEEP_HUMAN            # 人改过，或者人正在改（占用）
R_SELECTED = KEEP_SELECTED      # 人正选中着（"选中即占用"选项）
R_OTHER_AI = KEEP_OTHER_AGENT   # 别的 AI 在这个 AI 上次看之后改过（后到的让先到的）
R_RESERVED = KEEP_RESERVED      # 别的 AI 刚被拒、正在重试，暂时留给它
R_OTHER = KEEP_OTHER
R_REVERTED = "reverted"         # 不在许可单里、但应用把它改回去了（接入代码多恢复了）
# 来源说不准的变化（design_v0.5.md 14.2、14.3），记在 external:<来源> 名下，和人的修改一样保护
SOURCES = {"external:unattributed": "结果没收到的那次执行前后", "external:offline": "RightOfWay 没运行期间",
           "external:reopened": "应用重新打开时",
           "external:missed": "增量观察漏掉、全量核对时才发现的"}      # design_v0.5.md 第 16 节


@dataclass
class AgentView:
    """某个 AI 上次看到的场景（用来告诉它错过了什么）。看到的版本以运行时的 Runtime.seen 为准。
    不复制整个场景（以前每看一次复制一份，design_v0.5.md 第 16 节）：只在之后某个对象变了的时候，
    记下它看的那一刻这个对象的样子（old，None 表示那时还没有）和值（old_values）。没记的就和现在一样。"""
    old: dict[str, Optional[dict]] = field(default_factory=dict)
    old_values: dict[str, dict] = field(default_factory=dict)
    time: float = 0.0

    def remember(self, cid: str, rec: Optional[dict]) -> bool:
        if cid in self.old:
            return False
        self.old[cid] = rec
        return True

    def remember_values(self, cid: str, values: Optional[dict]) -> bool:
        if cid in self.old_values:
            return False
        self.old_values[cid] = dict(values or {})
        return True


@dataclass
class ObserveResult:
    lines: list[str]            # 它错过的变化（带具体的值）
    diff: Diff                  # 同样的内容，结构化的
    text: str


@dataclass
class AgentReport:
    status: str                                   # ok | deferred | refused（被暂停）
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
    # 给不变量检查用（rightofway.invariants）：应用交回的执行前、执行后（恢复前）、最终的状态，
    # 以及运行时许可单里要保持的部分 {对象编号: [面]}，"*" 表示整个对象
    before: Records = field(default_factory=dict)
    after_raw: Records = field(default_factory=dict)
    after: Records = field(default_factory=dict)
    plan: dict[str, list[str]] = field(default_factory=dict)
    # 第 3 步：运行时按实际状态核对后的结论（rightofway.model.Settlement）
    outcome: str = "committed"                    # committed | breach
    breach: list[str] = field(default_factory=list)              # 违规的单元：要保持，但场景里变了、没能恢复
    breach_detail: dict[str, dict] = field(default_factory=dict)
    side_effects: list[str] = field(default_factory=list)        # AI 没直接改、但场景里连带变了的单元（已记下）
    suspended: bool = False                       # 因为违规，这个 AI 被暂停了
    alert: str = ""                               # 给人的提醒（有违规时）
    settlement: Optional[object] = None
    # 类型化命令（run_commands）：每条命令的结果 [{index, command, status: ok|partial|refused|failed, ...}]
    commands: list[dict] = field(default_factory=list)
    # 认回同一个对象（运行时核对过的，design_v0.5.md 12.4、12.5）
    reidentified: list[str] = field(default_factory=list)       # 删掉又新建、认回成同一个对象的（名字）
    rebound: list[str] = field(default_factory=list)            # 之前想删、因为人改过而保留下来，这次又新建了同一个的
    recreate_blocked: list[str] = field(default_factory=list)   # 补回人删掉的对象，已拦下（删掉了）
    identity_levels: dict[str, str] = field(default_factory=dict)  # 名字 → 置信等级 exact | suffix | global
    identity_errors: list[str] = field(default_factory=list)    # 接入代码的配对和规则不一致
    # 告知（prior_art_solutions.md 第 3 项）：每个没生效的面一条，像 K8s 的 409（面、所有者、现在的值、想改成的值）
    conflicts: list[dict] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)             # 以现在的值为准的事实（请 AI 当作新的目标）
    loops: list[dict] = field(default_factory=list)             # 反复改、一直没生效的面：{id, object, face, label, times}
    loop_alert: list[str] = field(default_factory=list)         # 这次新进入循环的（已提醒人）
    # 派生改动（prior_art_solutions.md 问题三）：AI 新建的数据块副本（X → X.001）；人改过的数据块少了使用者
    derived: list[dict] = field(default_factory=list)
    # 给人的按钮（design_v0.5.md 14.4、14.5）：{"kind": "release" | "unmatch", "id": 对象编号, "label", "permit"?}
    actions: list[dict] = field(default_factory=list)
    # 执行之后的收尾（认回、恢复、核对）出错了（14.8）：这时按场景的实际状态记账，这里是出错的那一行
    finish_error: Optional[str] = None


@dataclass
class _Prepared:
    """一次执行要交给应用的代码；类型化命令还带着预检查的结果。"""
    code: str
    refused: set = field(default_factory=set)            # 执行前就按许可单拒掉的单元
    refused_deletes: set = field(default_factory=set)    # 执行前就拒掉删除的应用对象
    commands: Optional[list[dict]] = None                # 每条命令的结果（run_commands）
    executed: list[dict] = field(default_factory=list)   # 交给应用执行的命令（按执行顺序），对应 commands 里的项
    blocked: Optional[list[dict]] = None                 # 整个调用执行前就被拒（不透明的工具调用没法只执行一部分）


def _rows(recs: Records) -> dict[str, dict]:
    """全部应用对象的身份行（运行时核对认回、判断哪些对象在用）。"""
    return {cid: {k: r.get(k) for k in ("name", "type", "parent", "kind", "users")} for cid, r in recs.items()}


def _overlay(base: Records, delta: dict) -> Records:
    out = dict(base)
    out.update(delta.get("changed") or {})
    for cid in delta.get("deleted") or ():
        out.pop(cid, None)
    return out


def _changed_groups(base: Records, *others: Records) -> set[str]:
    """和 base 比，记录（指纹或身份行）不一样的对象，以及多出来、少了的。"""
    out: set[str] = set()
    keys = ("fp", "name", "type", "parent", "kind", "users")
    for other in others:
        for cid in set(base) | set(other):
            a, b = base.get(cid), other.get(cid)
            if a is None or b is None or any(a.get(k) != b.get(k) for k in keys):
                out.add(cid)
    return out


class SharedSession:
    def __init__(self, runtime: Runtime, bridge, human: str = "yuan", agent: str = "agent",
                 scene: Optional[str] = None, initial_owner: str = "open", granularity: str = ASPECT,
                 occupy_selection: bool = False, reserve_seconds: float = 30.0, prefix: str = "h-",
                 data_units: bool = True, sparse: bool = True, incremental: bool = True) -> None:
        """initial_owner：开始时场景里已有的对象算谁的。
        'open'（默认）表示 AI 可以改；'human' 表示全部视为人碰过，AI 不能改，直到人交还。
        data_units：对象引用的数据块（材质、网格……）各自是一个单元（默认）。人改一次共用材质只记一次，
        恢复时写回同一个数据块。接入代码不支持时忽略这个参数。
        sparse：稀疏账本（默认，design_v0.5.md 第 16 节）：没人改过的单元不建记录，是基线；第一次被改时才建。
        incremental：接入代码支持时（probe 报 delta），只传变化：接入代码在应用里缓存指纹、只重算变了的对象，
        双方按观察的代号（gen）对齐。实时同步（live_sync）两个都关掉，和以前一样。"""
        assert granularity in (ASPECT, OBJECT)
        self.rt, self.bridge = runtime, bridge
        self.data_units = data_units
        self.sparse, self.incremental = sparse, incremental
        self.human, self.agent, self.scene = human, agent, scene
        self.initial_owner, self.granularity = initial_owner, granularity
        self.occupy_selection, self.reserve_seconds, self.prefix = occupy_selection, reserve_seconds, prefix
        if occupy_selection:
            runtime.selection_occupies = True     # R8 选项由运行时判断
        runtime.reserve_seconds = reserve_seconds
        for actor, kind in ((human, ActorKind.HUMAN), (BASELINE_ACTOR, ActorKind.AGENT)):
            if actor not in runtime.actors:
                runtime.register_actor(actor, kind)
        if CAPABILITY not in runtime.capabilities:
            # 执行脚本属于可回退的修改（等级 1）；Blender 里的脚本执行不能中途取消
            runtime.register_capability(CapabilityMeta(CAPABILITY, effect_level=1, cancellable=False))
        self._view: Records = {}                   # 运行时上次看到的场景（只经过 view 的 setter 和 _set_records 改）
        self.values: Values = {}                   # 每个面现在的值（文字），增量更新（只经过 _learn 改）
        self.baseline: Records = {}                # 稀疏账本的基线：上次整份落盘（开始、压缩）时的场景；没人改过的单元按它算
        self.obs_gen: Optional[int] = None         # 接入代码的观察代号（只传变化时用）：双方对齐到同一个代号
        self._dirty_view: set[str] = set()         # 上次落盘之后 view 里变了的对象
        self._dirty_values: set[str] = set()
        self._av_dirty: dict[str, dict] = {}       # 上次落盘之后每个 AI 的视图记下了什么
        self._log: Optional[AppendLog] = None       # 账本文件（core.store：追加写的日志）
        self._saved_ss: dict[str, str] = {}         # 小字段上次落盘时的样子（只追加变了的）
        self.selected: set[str] = set()            # 人现在选中的对象
        self.agent_views: dict[str, AgentView] = {}
        self.agent_names: dict[str, str] = {}
        self.human_deleted: dict[str, str] = {}    # 人删掉的对象：编号 → 名字
        self.detections: list[dict] = []           # 每次发现人的修改：时间、内容
        self.labels: dict[str, str] = {}           # 面的显示名（来自应用）
        self.capabilities: dict = {}               # 连接时探测出的应用能力（Runtime.learn_app）
        self.aliases: dict[str, str] = {}          # 编号不能转交的应用（Unity）：应用编号 → 运行时编号
        # 执行生命周期（design_v0.5.md 14.1–14.3）
        self.app_boot: Optional[str] = None        # 应用进程的启动编号（接入代码报的）：变了说明应用重新打开过
        self._doubts: dict[str, dict] = {}          # 发出去了、结果没收到的执行：许可单编号 → 查清之后结算要用的东西
        self._tokens: dict[str, str] = {}           # begin_agent 交回的 token（按许可单编号）：结果没收到时补做 finish
        self._notes: dict[str, list[str]] = {}      # 下一次要告诉某个 AI 的话（例如它上次没收到的结果已经查清）
        self._alerted: set = set()                  # 已经提醒过人的 (种类, AI, 对象)：同一件事只弹一次
        self.ledger_path: Optional[str] = None      # 账本落盘的位置；None 表示不落盘
        self.start_alert = ""                       # 接着旧账本时发现的变化：给人的提醒（代理启动后显示）
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
            self._see(agent)

    @property
    def _incr(self) -> bool:
        """接入代码支持只传变化（probe 报 delta），并且这个会话没关掉（incremental）、数据块单独成单元。"""
        return bool(self.incremental and self.data_units and self.capabilities.get("delta"))

    @property
    def reservations(self) -> dict[str, tuple[str, float]]:
        """R23 的预留，由运行时管理：单元 → (预留给哪个 AI, 到期时间)。"""
        self.rt._expire_reservations()
        return dict(self.rt.reservations)

    def _args(self, **kw) -> dict:
        if self.scene:
            kw["scene"] = self.scene
        kw.setdefault("prefix", self.prefix)
        if self.data_units:
            kw["data_units"] = True
        if self.aliases:
            kw["aliases"] = dict(self.aliases)
        return kw

    def _dargs(self, **kw) -> dict:
        """调接入代码观察或执行：只传变化时带上 delta 和这一边对齐到的代号（since，design_v0.5.md 第 16 节）。"""
        kw = self._args(**kw)
        if self._incr:
            kw["delta"] = True
            kw.setdefault("since", self.obs_gen)
        return kw

    def _units(self, recs: Records) -> dict[str, dict]:
        return units(recs, self.granularity)

    @staticmethod
    def _known(recs: Records) -> dict[str, dict]:
        return {cid: r["aspects"] for cid, r in recs.items()}

    # ------------------------------------------------------------------
    # 运行时看到的场景（view）、值（values）、每个 AI 看到的（agent_views）：只经过这几个函数改
    # ------------------------------------------------------------------
    @property
    def view(self) -> Records:
        return self._view

    @view.setter
    def view(self, recs: Records) -> None:
        """整份换掉（和现在比，只记变了的对象）。"""
        old = self._view
        changed = {c: r for c, r in recs.items() if old.get(c) is not r and old.get(c) != r}
        self._set_records(changed, [c for c in old if c not in recs])

    def _set_records(self, changed: Records, deleted=()) -> None:
        """场景里这些对象变了（changed：新的记录）、没了（deleted）。每个 AI 的视图、每次结果没收到的执行，
        记下它们那一刻这个对象的样子（只记第一次），所以不用每次复制整个场景。"""
        for cid, rec in changed.items():
            if "values" in rec:
                rec = {k: v for k, v in rec.items() if k != "values"}
            self._remember(cid)
            self._view[cid] = rec
            self._dirty_view.add(cid)
        for cid in deleted:
            if cid in self._view:
                self._remember(cid)
                del self._view[cid]
                self._dirty_view.add(cid)

    def _remember(self, cid: str) -> None:
        old = self._view.get(cid)
        for a, av in self.agent_views.items():
            if av.remember(cid, old):
                self._av_dirty.setdefault(a, {"old": set(), "old_values": set()})["old"].add(cid)
        for d in self._doubts.values():
            d.setdefault("old", {}).setdefault(cid, old)

    def _learn(self, recs: Records) -> None:
        """记录里带的值（变了的面才带）记进 self.values。"""
        for cid, r in recs.items():
            new = r.get("values")
            if not new:
                continue
            cur = self.values.get(cid)
            changed = {f: v for f, v in new.items() if cur is None or cur.get(f) != v}
            if not changed:
                continue
            for a, av in self.agent_views.items():
                if av.remember_values(cid, cur):
                    self._av_dirty.setdefault(a, {"old": set(), "old_values": set()})["old_values"].add(cid)
            self.values.setdefault(cid, {}).update(changed)
            self._dirty_values.add(cid)

    def _see(self, agent: str) -> None:
        """这个 AI 看到了现在的场景：运行时记下它看到的版本（R3），这里从头记它之后错过了什么。"""
        self.rt.mark_seen(agent)
        self.agent_views[agent] = AgentView(time=time.time())
        self._av_dirty[agent] = {"reset": True, "old": set(), "old_values": set()}

    def _since_look(self, agent: str, now: Optional[Records] = None) -> tuple[Records, Records, Values, Values]:
        """这个 AI 上次看之后变了的对象：(那时的记录, 现在的记录, 那时的值, 现在的值)，只含变了的。
        describe_changes、diff_records 对没变的对象什么也不做，所以只给变了的，结果和给整个场景一样。"""
        av, now = self.agent_views[agent], self._view if now is None else now
        old = {c: r for c, r in av.old.items() if r is not None}
        new = {c: now[c] for c in av.old if c in now}
        old_values = {c: av.old_values.get(c, self.values.get(c, {})) for c in old}
        new_values = {c: self.values.get(c, {}) for c in new}
        return old, new, old_values, new_values

    def agent_scene(self, agent: str) -> tuple[Records, Values]:
        """这个 AI 上次看到的整个场景和值（实验、显示用）。"""
        av = self.agent_views[agent]
        values = {c: dict(v) for c, v in self.values.items()}
        for c, v in av.old_values.items():
            values[c] = dict(v)
        return self._agent_recs(agent), values

    def _agent_recs(self, agent: str) -> Records:
        """这个 AI 上次看到的整个场景（只在接入代码要整份 known 时用：不支持只传变化的接入代码）。"""
        av = self.agent_views[agent]
        out = dict(self._view)
        for c, r in av.old.items():
            if r is None:
                out.pop(c, None)
            else:
                out[c] = r
        return out

    # ------------------------------------------------------------------
    # 稀疏账本的基线（design_v0.5.md 第 16 节）：交给运行时，按需取
    # ------------------------------------------------------------------
    def _base_unit(self, uid: str) -> Optional[dict]:
        cid, face = split_unit(uid)
        r = self.baseline.get(cid)
        if r is None:
            return None
        if self.granularity == OBJECT:
            return r if face is None else None
        fp = r["aspects"].get(face) if face is not None else None
        if fp is None:
            return None
        meta = {k: r[k] for k in ("type", "parent", "kind", "display", "users") if r.get(k) is not None}
        return {"id": cid, "aspect": face, "name": r["name"], "fp": fp, "cfp": fp, **meta}

    def _base_units_of(self, cid: str) -> list[str]:
        r = self.baseline.get(cid)
        if r is None:
            return []
        return [cid] if self.granularity == OBJECT else [f"{cid}#{a}" for a in r["aspects"]]

    def _base_units(self):
        for cid in list(self.baseline):
            yield from self._base_units_of(cid)

    def _base_row(self, cid: str) -> Optional[dict]:
        r = self._view.get(cid)
        return None if r is None else {k: r.get(k) for k in ("name", "type", "parent", "kind", "users")}

    def _use_baseline(self, recs: Records) -> None:
        """这些记录就是基线：没人改过的单元不在运行时里建记录。"""
        self.baseline = dict(recs)
        if self.sparse:
            owner = None if self.initial_owner == "open" else self.human
            self.rt.set_baseline(self._base_unit, self._base_units, self._base_units_of, row=self._base_row,
                                 owner=owner, actor=BASELINE_ACTOR, type_=OBJECT_TYPE)

    def _set_selected(self, selected) -> None:
        self.selected = set(selected)
        self.rt.set_selection(self.human, self.selected)

    def _observed(self, res: dict, source: Optional[str] = None) -> Diff:
        """接入代码交回的一次观察（只传变化时）：记下这之间的变化（source 为 None 是人做的，否则来源说不准），
        更新 view、值、选中状态、代号。交回的是整份（full：代号对不上）就和以前一样整份比。
        全量核对发现增量漏掉的（missed）：说不准是谁、什么时候改的，记为来源不明（external:missed），和人的修改一样保护。"""
        if res.get("labels"):
            self.labels = res["labels"]
        if res.get("full"):
            recs = res["records"]
            self._learn(recs)
            d = self._absorb_human(self.view, recs, source=source)
            self.view = recs
        else:
            ch, dl = res.get("changed") or {}, res.get("deleted") or []
            missed = set(res.get("missed") or ())
            old = {c: self._view[c] for c in set(ch) | set(dl) if c in self._view}
            self._learn(ch)
            d = Diff()
            for group, src in ((missed, "missed"), (None, source)):
                o = {c: r for c, r in old.items() if (c in missed) == (group is not None)}
                n = {c: r for c, r in ch.items() if (c in missed) == (group is not None)}
                part = self._absorb_human(o, n, source=src)
                d.created.update(part.created)
                d.deleted.update(part.deleted)
                d.modified.update(part.modified)
            if missed:                                  # 照实告诉每个 AI：这些地方说不准是谁改的，按人的修改保护
                names = "、".join(sorted(display(ch.get(c) or old.get(c), c) for c in missed)[:12])
                for a in self.agent_names:
                    self._notes.setdefault(a, []).append(
                        f"核对时发现 {names} 和之前记下的不一样（增量观察漏掉了，说不准是谁、什么时候改的），"
                        "已按人的修改保护：人交还之后才能改。")
            self._set_records(ch, dl)
        if res.get("gen") is not None:
            self.obs_gen = res["gen"]
        if res.get("sel") is not None:
            self._sync_selected(res["sel"])
        return d

    def _sync_selected(self, sel) -> None:
        """选中状态（只传变化时单独交回）：view 里对象记录的 selected 跟着改，和整份交回时一样。"""
        sel = set(sel)
        fix = {c: {**r, "selected": c in sel} for c, r in self._view.items()
               if r.get("kind") != "data" and bool(r.get("selected")) != (c in sel)}
        if fix:
            self._set_records(fix)
        self._set_selected(sel)

    def _list_all(self) -> Records:
        """整份列一次（结果没收到、查清时用）。只传变化时也整份要（since=None），并对齐到交回的代号。"""
        if not self._incr:
            return self.bridge.call("poll", self._args())["records"]
        res = self.bridge.call("poll", self._dargs(since=None))
        self.obs_gen = res.get("gen")
        if res.get("labels"):
            self.labels = res["labels"]
        return res["records"]

    def start(self, ledger=None) -> Records:
        """连接。ledger：账本落盘的文件（design_v0.5.md 14.3），或者"文档 → 文件"的函数（按连接时测出的文档决定）。
        文件在、并且是同一个文档的，接着上次的账本，先和场景对一遍：上次之后的变化来源说不准（代理没运行），
        按人的修改保护；结果没查清的执行先查清。"""
        # 连接时的自测（design_v0.5.md 第 4 节）：重名后缀、接入代码会不会认回、编号能不能转交、记不记执行结果
        self.capabilities = self.rt.learn_app(self.bridge.call(
            "probe", self._args(**({"delta": True} if self.incremental and self.data_units else {}))))
        self.app_boot = self.capabilities.get("boot")
        if callable(ledger):
            ledger = ledger(self.capabilities.get("document"))
        self.ledger_path = ledger
        if ledger and os.path.exists(ledger) and self._load(ledger):
            return self._reconcile()
        res = self.bridge.call("poll", self._dargs(since=None))
        self.labels = res.get("labels", {})
        self.obs_gen = res.get("gen")
        self.view = res["records"]
        self._learn(res["records"])
        if self.sparse:
            self._use_baseline(self._view)
        else:
            owner = BASELINE_ACTOR if self.initial_owner == "open" else self.human
            for uid, content in self._units(res["records"]).items():
                if uid not in self.rt.objects:
                    self.rt.create_object(uid, OBJECT_TYPE, content, owner)
        self._set_selected(cid for cid, r in self.view.items() if r.get("selected"))
        self._started = True
        for a in self.agent_names:
            self._see(a)
        self._save()
        return self.view

    # ------------------------------------------------------------------
    # 观察
    # ------------------------------------------------------------------
    def poll(self) -> Diff:
        """列一次对象，和上次比。执行 AI 脚本之外发生的变化，都是人做的——除了两种说不准的情况（design_v0.5.md 14.2）：
        有一次执行结果还没查清（先查；查不清就先不记，免得把 AI 的改动算成人的），
        应用重新打开过（启动编号变了：变化来自重新打开，记为来源不明，按人的修改保护）。"""
        if self._doubts:
            self._resolve_in_doubt()
            if self._doubts:
                return Diff()
        if self._incr:
            res = self.bridge.call("poll", self._dargs())
            reopened = bool(res.get("boot") and self.app_boot and res["boot"] != self.app_boot)
            d = self._observed(res, source="reopened" if reopened else None)
            if res.get("boot"):
                self.app_boot = res["boot"]
        else:
            res = self.bridge.call("poll", self._args(known=self._known(self.view)))
            recs = res["records"]
            self._learn(recs)
            reopened = bool(res.get("boot") and self.app_boot and res["boot"] != self.app_boot)
            d = self._absorb_human(self.view, recs, source="reopened" if reopened else None)
            if res.get("boot"):
                self.app_boot = res["boot"]
            self.view = recs
            self._set_selected(cid for cid, r in recs.items() if r.get("selected"))
        applied = self._apply_requests(res)
        if d or reopened or applied:                  # 人只点了"交还"、场景没变，也要落盘（重启后交还还在）
            self._save()
        return d

    def observe(self, agent: Optional[str] = None) -> ObserveResult:
        """AI 读场景时调用（例如 MCP 代理收到它的读取请求）：告诉它上次看之后发生了什么，并更新它的视图。"""
        agent = agent or self.agent
        self.poll()
        if self._doubts:
            return ObserveResult([], Diff(), "之前有一次执行的结果还没收到，查清之前不能告诉你最新的变化"
                                              "（说不准哪些是谁做的）。稍后再看。")
        old, new, old_values, new_values = self._since_look(agent)
        lines = describe_changes(old, new, old_values, new_values, self.labels, who=self._who_for(agent))
        diff = diff_records(old, new)
        self._see(agent)
        text = ("你上次看场景之后，发生了这些变化：\n" + "\n".join("- " + x for x in lines)) if lines else "你上次看场景之后没有变化。"
        return ObserveResult(lines, diff, text)

    def _absorb_human(self, old: Records, new: Records, source: Optional[str] = None) -> Diff:
        """把 old → new 的变化记进运行时：source 为 None 时是人做的；否则来源说不准（design_v0.5.md 14.2、14.3），
        记在 external:<source> 名下，和人的修改一样保护。删除时记下当时有几个对象在用（数据块的墓碑要用，14.4）。"""
        d = diff_records(old, new)
        if not d:
            return d
        self.detections.append({"time": time.time(), "changes": d.describe(), **({"source": source} if source else {})})
        ids = d.ids                                  # 只展开变了的对象（以前整个场景的每个面都展开一遍）
        ud = diff_records(self._units({c: old[c] for c in ids if c in old}),
                          self._units({c: new[c] for c in ids if c in new}))

        author = R.observed_author(self.human, source)          # 谁做的由核心定

        def record(uid: str, content: dict) -> None:
            if author != self.human:
                self.rt.record_external(uid, content, author[len(R.EXTERNAL_PREFIX):], OBJECT_TYPE)
            elif uid in self.rt.objects:
                self.rt.human_edit(self.human, uid, content)
            else:
                self.rt.create_object(uid, OBJECT_TYPE, content, self.human)
        for uid, content in ud.created.items():
            record(uid, content)
        for uid, (_, content) in ud.modified.items():
            record(uid, content)
        for uid, content in ud.deleted.items():
            record(uid, {**DELETED, **{k: content[k] for k in ("name", "type", "parent", "kind", "display", "users")
                                       if content.get(k) is not None}})
        if source is None:
            for cid, rec in d.deleted.items():
                self.human_deleted[cid] = rec["name"]
        return d

    # ------------------------------------------------------------------
    # 谁改的、为什么没生效
    # ------------------------------------------------------------------
    def _display(self, actor: str, recipient: Optional[str]) -> str:
        return MSG.display_actor(self, actor, recipient)

    def _last_author(self, cid: str, face: Optional[str]) -> Optional[str]:
        if face is not None and self.granularity == ASPECT:
            obj = self.rt.objects.peek(f"{cid}#{face}")
        else:
            obj = self.rt.objects.peek(cid)
            if obj is None:
                obj = next((o for u, o in self.rt.objects.items() if split_unit(u)[0] == cid), None)
        if obj is not None and obj.versions:
            return obj.versions[-1].author_id
        return obj.baseline_author if obj is not None and obj.baseline_author != BASELINE_ACTOR else None

    def _who_for(self, recipient: Optional[str]):
        def who(cid: str, face: Optional[str]) -> str:
            a = self._last_author(cid, face)
            return self._display(a, recipient) if a else ""
        return who

    def _reason(self, uid: str, agent: str) -> dict:
        return self.rt.reason_for(agent, uid)

    def protected(self, for_agent: Optional[str] = None) -> dict[str, list[str]]:
        """人碰过或正被占用、并且还在场景里的部分：{对象编号: [面]}，"*" 表示整个对象（由运行时给出）。
        for_agent：再加上预留给别的 AI 的部分。真正执行时用的是运行时的许可单（Runtime.admit）。"""
        out: dict[str, set[str]] = {}
        for uid in self.rt.guarded(for_agent):
            cid, aspect = split_unit(uid)
            out.setdefault(cid, set()).add(aspect or "*")
        return {cid: sorted(a) for cid, a in sorted(out.items())}

    def protected_ids(self) -> list[str]:
        return sorted(self.protected())


    # ------------------------------------------------------------------
    # 执行 AI 的脚本
    # ------------------------------------------------------------------
    def run_agent(self, code: str, label: str = "", protect: bool = True, agent: Optional[str] = None,
                  max_replan: int = 3) -> AgentReport:
        """protect=False 用于对照组：不做保护，直接执行（相当于现在的 Blender MCP）。

        一次执行（design_v0.5.md 第 2 节）：
          1. 运行时开许可单：哪些部分要保持、为什么（人改过、人选中、别的 AI 刚改过、预留给别的 AI）。
          2. 应用照单执行：先核对场景和运行时以为的一样；不一样说明人刚改过，不执行，
             这边先把人的修改记进运行时，再重开许可单（最多 max_replan 次）。
          3. 按许可单把结果记进运行时：要保持的部分记为没生效，其余的提交。"""
        return self._execute(lambda permit: _Prepared(code), label, protect, agent, max_replan)

    def run_commands(self, commands: list[dict], label: str = "", agent: Optional[str] = None,
                     max_batch: int = 25, max_replan: int = 3) -> list[AgentReport]:
        """类型化命令（例如 Unity MCP 的 batch_execute：一批最多 25 条，不是事务）。
        AI 一批一批地发；每一批先由运行时开许可单，按许可单预检查每条命令（Permit.screen）：
        改到要保持的面的，去掉这些面再执行；整条都要保持的、删除不许删的对象的，不执行。
        其余交给应用执行，之后和代码一样按实际状态核对、记账。返回每一批的报告。"""
        compile_ = getattr(self.bridge, "compile_commands", None)
        if compile_ is None:
            raise ValueError("这个接入不支持类型化命令")
        reports = []
        for k in range(0, len(commands), max_batch):
            batch = commands[k:k + max_batch]
            n = k // max_batch + 1
            reports.append(self._execute(lambda permit, b=batch, k=k: self._screen(permit, b, k, compile_),
                                         f"{label}-{n}" if label else f"batch-{n}", True, agent, max_replan))
        return reports

    def run_tool(self, call, label: str = "", agent: Optional[str] = None, targets=(), destructive: bool = True,
                 max_replan: int = 3):
        """一次不透明的工具调用（例如 MCP 代理转发给应用 MCP 服务器的类型化工具）：不知道它会改哪些面。
        call()：真正去调用工具，返回工具的结果。targets：这次调用提到的对象（编号，由调用参数里的名字找出）。
        1. 运行时开许可单；按对象预检查（design_v0.5.md 12.8 第 1 步）：整个对象都要保持的，不执行；
           接入代码恢复不了的（能力表里没有 begin/finish，或者恢复不了删除而这个工具可能删除），不执行。
        2. 接入代码 begin_agent（核对、快照）→ call() → finish_agent（认回、恢复、核对），和执行脚本同一条流水线。
        返回 (AgentReport, 工具的结果；没执行时是 None)。"""
        holder: dict = {}
        prepare = lambda permit: _Prepared("", blocked=self._tool_precheck(permit, targets, destructive) or None)  # noqa: E731
        rep = self._execute(prepare, label or "tool", True, agent, max_replan,
                            executor=lambda args: self._around(args, call, holder))
        return rep, holder.get("result")

    def _tool_precheck(self, permit, targets, destructive: bool) -> list[dict]:
        caps = self.capabilities
        blocked = []
        for cid in sorted(set(targets)):
            if cid not in self.view:
                continue
            kept = {split_unit(u)[1] or "*": k for u, k in permit.keep.items() if split_unit(u)[0] == cid}
            why = R.opaque_blocked(kept, around=bool(caps.get("around")), restore_deleted=bool(caps.get("restore_deleted")),
                                   destructive=destructive, no_delete=cid in permit.no_delete,
                                   keep_alive=permit.keep_alive.get(cid))
            if why is not None:
                blocked.append({"id": cid, "name": display(self.view[cid], cid), "reason": why["reason"],
                                **({"by": why["by"]} if why.get("by") else {}),
                                **({"ancestor": True} if why.get("ancestor") else {})})
        return blocked

    def _around(self, args: dict, call, holder: dict) -> dict:
        """在接入代码的 begin_agent 和 finish_agent 之间做那一次工具调用。不是原子的：两次调用之间应用里的变化都算 Agent 的。
        接入代码不支持 begin/finish 时（例如现在的 Unity），只能前后各列一次对象、事后核对，变了要保持的就是违规。"""
        t0 = time.perf_counter()

        def invoke() -> Optional[str]:
            try:
                holder["result"] = call()
                return None
            except Exception as e:                       # noqa: BLE001
                return f"{type(e).__name__}: {e}"

        if self.capabilities.get("around"):
            b = self.bridge.call("begin_agent", args)
            if b.get("status") != "begun":
                return b                                  # replan / deferred
            if args.get("permit_id"):
                self._tokens[args["permit_id"]] = b["token"]   # 结果没收到时，用它补做 finish（14.2）
            error = invoke()
            res = self.bridge.call("finish_agent", self._args(token=b["token"], error=error))
            self._tokens.pop(args.get("permit_id"), None)
            return res
        before = self.bridge.call("poll", {k: v for k, v in args.items() if k not in ("delta", "since")})["records"]
        expected = args.get("expected")
        if expected is not None:
            changed = sorted((set(before) ^ set(expected))
                             | {c for c in before if c in expected and before[c]["fp"] != expected[c]})
            if changed:
                return {"status": "replan", "changed": changed, "before": before}
        error = invoke()
        res = self.bridge.call("poll", self._args(known={c: r["aspects"] for c, r in before.items()}))
        after = res["records"]
        return {"status": "ok", "before": before, "after_raw": after, "after": after, "restored": [], "merged": {},
                "fallback": [], "inexact": [], "renamed": [], "dangling": [], "candidates": [], "outside": {},
                "ai_created": sorted(set(after) - set(before)), "error": error, "stdout": "",
                "undo_pushed": False, "seconds": round(time.perf_counter() - t0, 4), "labels": res.get("labels", {})}

    def _blocked_report(self, agent: str, blocked: list[dict]) -> AgentReport:
        rep = AgentReport(status="blocked", agent=agent)
        for b in blocked:
            rep.conflicts.append({"id": b["id"], "object": b["name"], "face": None, "label": "整个对象",
                                  "reason": b["reason"], "owner": "human" if b["reason"] in (R_HUMAN, R_SELECTED)
                                  else f"agent:{b['by']}" if b.get("by") else b["reason"],
                                  "now": None, "wanted": None, "delete_refused": False, "precheck": True,
                                  "ancestor": bool(b.get("ancestor"))})
        names = "、".join(f"{b['name']}（{self._why(b)}）" for b in blocked)
        rep.text = ("这次没有执行：这个工具要改的对象按规则不能动，而这个应用没法在工具调用之后只把不能动的部分改回来："
                    + names + "。请换一种只改允许部分的方式（例如执行代码，或者一次只改一个属性）。")
        return rep

    def _screen(self, permit, batch: list[dict], offset: int, compile_) -> "_Prepared":
        """按许可单预检查一批命令。按名字找对象：同名的取最早的那个（和应用一样）；对象和数据块同名时先找对象。"""
        by_name: dict[str, str] = {}
        for cid, r in sorted(self.view.items(), key=lambda kv: kv[1].get("kind") == "data"):
            by_name.setdefault(r["name"], cid)
        admitted, results, refused, refused_deletes = [], [], set(), set()
        for i, c in enumerate(batch):
            entry = {"index": offset + i, "command": c, "status": "ok"}
            results.append(entry)
            cid = by_name.get(c.get("target", ""))
            if c.get("action") == "create" or cid is None:
                admitted.append((entry, c))                   # 新建；或者对象不在（让应用自己报错）
                continue
            if c["action"] == "delete":
                _, no = permit.screen(cid, delete=True)
                if no:
                    entry.update(status="refused", reason=no["*"]["reason"], by=no["*"].get("by", ""), faces=["*"],
                                 **({"ancestor": True} if no["*"].get("ancestor") else {}))
                    refused_deletes.add(cid)
                    continue
                admitted.append((entry, c))
                continue
            faces = list(c.get("set", {}))
            ok, no = permit.screen(cid, faces)
            if no:
                k = next(iter(no.values()))
                entry.update(reason=k["reason"], by=k.get("by", ""), faces=sorted(no))
                if "*" in no:
                    no = {f: no["*"] for f in faces}
                refused |= ({cid} if self.granularity == OBJECT else {f"{cid}#{f}" for f in no})
            if not ok:
                entry["status"] = "refused"
                continue
            if no:
                entry["status"] = "partial"
                c = {**c, "set": {f: v for f, v in c["set"].items() if f in ok}}
            admitted.append((entry, c))
        code = compile_([c for _, c in admitted], self.scene)
        return _Prepared(code, refused, refused_deletes, results, [e for e, _ in admitted])

    def _execute(self, prepare, label: str, protect: bool, agent: Optional[str], max_replan: int,
                 executor=None) -> AgentReport:
        """一次执行（design_v0.5.md 第 2 节、14.1）：许可 → 发出 → 结算，许可单正好结束一次。
        结果没收到时记为结果未知（14.2），查清之前不开始新的执行。"""
        agent = agent or self.agent
        if agent not in self.agent_names or agent not in self.agent_views:
            self.add_agent(agent)
            if agent not in self.agent_views:
                self._see(agent)
        if self._doubts:
            self._resolve_in_doubt()                  # 先查清上一次没收到结果的执行
        keep_cand = self.rt.human_touched_policy == POLICY_CANDIDATE
        known = None
        if not self._incr:                           # 不支持只传变化的接入代码：带上这个 AI 看到的整个场景（决定带回哪些值）
            known = {cid: {"fp": r["fp"], "aspects": r["aspects"]} for cid, r in self._agent_recs(agent).items()}
        replans = 0
        while True:
            permit = self.rt.admit(agent)
            if permit.refused is not None:
                return self._with_notes(self._refused_report(agent, permit.refused))
            plan = permit.keep_by_group()
            prep = prepare(permit)
            if prep.blocked:
                self.rt.close_permit(permit, [])
                return self._with_notes(self._blocked_report(agent, prep.blocked))
            args = dict(code=prep.code, protected=plan, protect=protect, keep_candidates=keep_cand,
                        granularity=self.granularity, permit_id=permit.permit_id)
            if known is not None:
                args["known"] = known
            if permit.keep_alive and protect:
                args["keep_alive"] = sorted(permit.keep_alive)       # 祖先：不许删，面可以改（12.6）
            if permit.reidentify and protect:
                # 认回同一个对象：规则、可以重新绑定的旧对象（之前想删的，加上这一批执行前拒掉删除的）、墓碑
                args["reidentify"] = permit.reidentify
                rebind = self.rt.rebind_rows(permit, prep.refused_deletes, self.view.get)
                if rebind:
                    args["rebind"] = rebind
                if permit.no_recreate:
                    args["no_recreate"] = permit.no_recreate
            if protect:
                if not self._incr:                   # 只传变化时，执行前的状态就是对齐到的代号（since）
                    args["expected"] = {cid: r["fp"] for cid, r in self.view.items()}
                if self.rt.selection_occupies:
                    args["expected_selection"] = sorted(self.selected)
            elif self._incr:
                args["check"] = False                 # 对照组：不核对，执行前没看到的变化随结果交回（pre）
            self.rt.dispatched(permit)
            self._doubts[permit.permit_id] = self._doubt_record(permit, prep, label, protect, agent, plan)
            self._save()                              # 发出之前落盘：代理在执行期间退出，重启后也知道这次结果未知
            try:
                res = (executor or (lambda a: self.bridge.call("run_agent", a)))(self._dargs(**args))
            except Exception as e:                    # noqa: BLE001  发出去了，结果没收到
                return self._lost(permit, e)
            if res["status"] not in ("replan", "deferred"):
                break
            del self._doubts[permit.permit_id]
            self.rt.close_permit(permit, [])           # 没有执行
            self._apply_requests(res)
            if res["status"] == "deferred":
                self._save()
                names = [res["before"][c]["name"] for c in res["editing"]]
                return self._with_notes(AgentReport(status="deferred", agent=agent, text=(
                    "这次没有执行：人正在编辑模式里修改 " + "、".join(names) + "。等人退出编辑模式后再试。")))
            # 人在上次轮询之后刚改过（或者刚换了选中）：先记下来，再重开许可单
            if res.get("delta"):
                self._observed(res)
                self._set_selected(res.get("selected", sorted(self.selected)))
            else:
                self.labels = res.get("labels", self.labels)
                self._learn(res["before"])
                self._absorb_human(self.view, res["before"])
                self.view = res["before"]
                self._set_selected(res.get("selected", [cid for cid, r in self.view.items() if r.get("selected")]))
            replans += 1
            if replans > max_replan:
                self._save()
                return self._with_notes(AgentReport(status="deferred", agent=agent, text=(
                    "这次没有执行：执行前场景一直在变（人正在连续修改）。稍后再试。")))
        rep = self._complete(permit, prep, res, label, protect, agent, plan)
        del self._doubts[permit.permit_id]
        self._save()
        return self._with_notes(rep)

    def _doubt_record(self, permit, prep, label: str, protect: bool, agent: str, plan: dict) -> dict:
        """结果没收到时，查清之后结算要用的东西。可以落盘（只有基本类型），代理重启后也能接着查。"""
        cmds = prep.commands
        # 发出前运行时以为的场景：不复制整个场景，之后哪个对象变了再记下它那一刻的样子（old，见 _remember）
        return {"permit": permit.permit_id, "agent": agent, "label": label, "protect": protect, "plan": plan,
                "keep_alive": sorted(permit.keep_alive) if protect else [],
                "old": {}, "gen": self.obs_gen, "since": time.time(), "boot": self.app_boot,
                "prep": {"code": "", "refused": sorted(prep.refused), "refused_deletes": sorted(prep.refused_deletes),
                         "commands": cmds, "executed": [cmds.index(e) for e in prep.executed] if cmds else []}}

    @staticmethod
    def _prep_from(d: dict) -> "_Prepared":
        p = d["prep"]
        cmds = p.get("commands")
        return _Prepared("", set(p.get("refused") or ()), set(p.get("refused_deletes") or ()), cmds,
                         [cmds[i] for i in p.get("executed") or ()] if cmds else [])

    def _result_states(self, res: dict) -> tuple[Records, Records, Records, set[str]]:
        """应用交回的执行前、执行后（恢复前）、最终的场景（整份，没变的对象和 view 里是同一个记录），
        以及变了的对象。只传变化的接入代码（delta）：执行前就是 view（执行前已经核对过代号），之后两份只带变了的。"""
        if res.get("delta"):
            before = dict(self._view)
            raw, after = _overlay(before, res["raw"]), _overlay(before, res["final"])
            touched = {c for d in (res["raw"], res["final"]) for c in list(d.get("changed") or ()) + list(d.get("deleted") or ())}
            return before, raw, after, touched
        before, raw, after = res["before"], res["after_raw"], res["after"]
        return before, raw, after, _changed_groups(before, raw, after)

    def _complete(self, permit, prep, res: dict, label: str, protect: bool, agent: str, plan: dict) -> AgentReport:
        """应用交回了结果：认回、核对、记账、写说明（第 2 节 ③④）。结果没收到、之后查回来的也走这里。"""
        self.aliases.update(res.get("aliases") or {})
        self.labels = res.get("labels") or self.labels
        # 1. 执行前已经核对过，这里正常为空；接入代码没有核对时（对照组、旧版本），按人的修改补记
        human_since = Diff()
        if res.get("delta") and res.get("pre"):
            human_since = self._observed(res["pre"])
        before, raw, after, touched = self._result_states(res)
        if not res.get("delta"):
            self._learn(before)
            human_since = self._absorb_human(self.view, before)
            self.view = before
        if "selected" in res:
            self._set_selected(res["selected"])
        # 2. 这个 AI 上次看之后错过的变化（人做的、别的 AI 做的），带具体的值
        old, new, old_values, new_values = self._since_look(agent)
        missed = describe_changes(old, new, old_values, new_values, self.labels, who=self._who_for(agent))
        missed_diff = diff_records(old, new)
        # 只看变了的对象（还有类型化命令执行前就拒掉的）：三份里都一样的单元，核对时什么也不做（design_v0.5.md 第 16 节）
        rel = set(touched) | {split_unit(u)[0] for u in prep.refused} | set(prep.refused_deletes)
        before_values = {cid: dict(self.values.get(cid, {})) for cid in rel}
        raw_values = {cid: dict(raw[cid].get("values") or {}) for cid in rel if cid in raw}
        self._learn({cid: after[cid] for cid in rel if cid in after})

        # 3. 按应用交回的实际状态核对、记账。由运行时决定（P9）；说明只从运行时的记录生成
        self._n_step += 1
        step_id = f"ai-{self._n_step}" + (f"-{label}" if label else "")
        sub = lambda recs: {c: recs[c] for c in rel if c in recs}         # noqa: E731
        ub, ua, ur = self._units(sub(before)), self._units(sub(after)), self._units(sub(raw))
        rows = (_rows(before), _rows(raw), _rows(after))
        self.rt.add_step(step_id, inputs=[], outputs=sorted(diff_records(ub, ua).ids))
        rep = AgentReport(status="ok", agent=agent, step_id=step_id, missed=missed, missed_diff=missed_diff,
                          human_since=human_since, outside=res.get("outside", {}),
                          inexact=res["inexact"], renamed=res["renamed"], fallback=res.get("fallback", []),
                          dangling=res["dangling"], candidates=res.get("candidates", []), error=res["error"],
                          stdout=res["stdout"], seconds=res["seconds"], undo_pushed=res["undo_pushed"],
                          protected=protect, before=before, after_raw=raw, after=after,
                          plan=plan if protect else {})
        if protect:
            st = self.rt.settle(permit, ub, ur, ua, step_id=step_id, produce_type=OBJECT_TYPE,
                                refused=prep.refused, refused_deletes=prep.refused_deletes,
                                identity=res.get("identity") if permit.reidentify else None, rows=rows)
            self._report_from(rep, st, before, raw, after)
        else:
            self._control_accounting(rep, permit, step_id, ub, ua, ur, before, raw)
        if prep.commands is not None:
            done = getattr(self.bridge, "command_results", lambda _: {})(res.get("stdout", ""))
            for j, entry in enumerate(prep.executed):
                r = done.get(j, "没有执行到（前面出错中断了）")
                if r != "ok":
                    entry.update(status="failed", error=r)
            rep.commands = prep.commands
        suffix = self.capabilities.get("suffix")
        rep.resurrections = X.resurrections(rep.committed, before, after, self.human_deleted.values(),
                                            lambda n: strip_suffix(n, suffix))
        if self.granularity == ASPECT:
            for cid, part in X.partial(rep.committed, rep.skipped).items():
                rep.partial[display(after.get(cid) or before.get(cid), cid)] = part
        self.view = after
        if res.get("delta"):
            self.obs_gen = res.get("gen", self.obs_gen)
            if res.get("sel") is not None:
                self._sync_selected(res["sel"])
        self._see(agent)
        rep.conflicts = self._conflicts(rep, before, raw, after, before_values, raw_values)
        rep.facts = self._facts(rep)
        if protect:
            rep.derived = self._derived(before, after, rel)
        rep.text = self._describe(rep, before, after, before_values, raw_values)
        if rep.commands:
            cl = self._command_lines(rep)
            if cl:
                rep.text += "\n命令：\n" + "\n".join("- " + x for x in cl)
        if res.get("forged_release"):
            rep.text += "\n注意：你的代码改了交还记录（只有人能交还），已经撤销。"
        if res.get("finish_error"):                   # 收尾出错（14.8）：上面的账是按场景的实际状态记的
            rep.finish_error = res["finish_error"].strip().splitlines()[-1]
            rep.text += ("\n注意：执行之后 RightOfWay 的收尾（认回、恢复、核对）出错了（" + rep.finish_error + "）。"
                         "上面是按场景的实际状态记的账：该恢复、没恢复成的部分记为违规。")
        self._apply_requests(res)
        rep.alert = self._alert(rep, before, raw)
        if rep.finish_error:
            rep.alert = "\n".join(x for x in (rep.alert, f"注意：AI 的一次执行之后，RightOfWay 的收尾出错了"
                                                          f"（{rep.finish_error}）。已按场景的实际状态记账。") if x)
        offers = self._offers(rep, permit) if protect else []
        if offers:
            rep.alert = "\n".join(x for x in [rep.alert] + offers if x)
        if res.get("forged_release"):
            rep.alert = "\n".join(x for x in (rep.alert, "注意：AI 的代码试图替你交还对象，已经撤销。") if x)
        return rep

    # ------------------------------------------------------------------
    # 结果没收到（design_v0.5.md 14.2）
    # ------------------------------------------------------------------
    def _lost(self, permit, error: Exception) -> AgentReport:
        """发出去了，结果没收到：可能执行了，也可能没有。记为结果未知，先试着马上查一次。"""
        d = self._doubts[permit.permit_id]
        d["token"] = self._tokens.pop(permit.permit_id, None)
        d["reason"] = f"{type(error).__name__}: {error}"
        self.rt.mark_in_doubt(permit, d["reason"])
        self._save()
        reports = self._resolve_in_doubt()
        if permit.permit_id in reports:
            return self._with_notes(reports[permit.permit_id], skip=permit.permit_id)
        return AgentReport(status="unknown", agent=d["agent"], text=(
            f"这次执行的结果没收到（{d['reason']}）：可能已经执行了，也可能没有。查清之前不会开始新的执行；"
            "稍后再调用一次，会先把结果查回来，再告诉你。"),
            alert=(f"注意：AI 的一次执行结果没收到（{d['reason']}）。查清之前不会开始新的执行；"
                   "应用重新连上之后会先查回结果。"))

    def _resolve_in_doubt(self) -> dict[str, AgentReport]:
        """把结果没收到的执行查清（接入代码按许可单编号记着结果）。返回查清了的 {许可单编号: 报告}；
        报告的要点也记进 _notes，下一次告诉那个 AI。连不上应用的，继续等。"""
        out: dict[str, AgentReport] = {}
        for pid in sorted(self._doubts):
            d = self._doubts[pid]
            permit = self.rt.permits.get(pid)
            if permit is None or permit.state not in self.rt.OPEN_STATES:
                del self._doubts[pid]
                continue
            if permit.state == "dispatched":          # 代理在执行期间退出过：重启后才发现
                self.rt.mark_in_doubt(permit, d.get("reason") or "代理在执行期间退出")
            try:
                rep = self._requery(pid, d, permit)
            except Exception:                          # noqa: BLE001  还是连不上：继续等
                continue
            del self._doubts[pid]
            out[pid] = rep
            self._notes.setdefault(d["agent"], []).append(rep.text)
        if out:
            self._save()
        return out

    def _requery(self, pid: str, d: dict, permit) -> AgentReport:
        """查清一次结果没收到的执行（design_v0.5.md 14.2、14.8）。只有两种情况能说"没执行"：
        应用记了 not_executed；或者接入代码在执行前就记"开始了"（journal 2），而这次启动以来根本没收到过这张许可单。
        开始了、没交回结果（started、failed、补做收尾也做不了）的，是"执行了，结果说不清"，不是"没执行"。"""
        agent = d["agent"]
        head = "你之前有一次执行的结果没收到（" + str(d.get("reason") or "") + "），现在查清了："
        journal = self.capabilities.get("journal") or 0
        if not journal:                                # 怎么查清由核心定（rules.resolve_lost）
            dec = R.resolve_lost(journal=0, status=None, same_boot=False, seen=False, has_token=False,
                                 app_restarted=False)
            return self._unattributed(pid, d, permit, self._list_all(), dec.why)
        q = self.bridge.call("result", self._args(permit_id=pid))
        entry, boot = q.get("entry"), q.get("boot")
        status = (entry or {}).get("status")
        same_boot = boot is not None and boot == d.get("boot")
        token = d.get("token") or (entry or {}).get("token")
        dec = R.resolve_lost(journal=journal, status=status, same_boot=same_boot, seen=bool(q.get("seen")),
                             has_token=bool(token), app_restarted=boot is not None and not same_boot)
        if dec.action == "settle":
            rep = self._complete(permit, self._prep_from(d), entry["result"], d["label"], d["protect"], agent, d["plan"])
            rep.text = head + "\n" + rep.text
            return rep
        if dec.action == "finish":
            try:
                res = self.bridge.call("finish_agent", self._args(token=token, error="结果没收到之后补做的收尾"))
            except Exception:                          # noqa: BLE001
                e2 = self.bridge.call("result", self._args(permit_id=pid)).get("entry") or {}
                d2 = R.resolve_failed_finish(e2.get("status"))
                if d2.action == "unattributed":
                    return self._unattributed(pid, d, permit, self._list_all(), d2.why, boot, executed=True)
                if d2.action == "wait":
                    raise
                res = e2["result"]
            rep = self._complete(permit, self._prep_from(d), res, d["label"], d["protect"], agent, d["plan"])
            rep.text = head + "\n" + rep.text
            return rep
        if dec.action == "not_executed":
            self.rt.close_permit(permit, [])           # 应用没执行（没收到，或者执行前场景变了）
            self._apply_requests((entry or {}).get("result") or {})
            return AgentReport(status="not_executed", agent=agent, text=head + "那次没有执行，场景没有被它改动。需要的话请重新执行。")
        return self._unattributed(pid, d, permit, self._list_all(), dec.why, boot, executed=dec.executed)

    def _unattributed(self, pid: str, d: dict, permit, recs: Records, why: str, boot: Optional[str] = None,
                      executed: bool = False) -> AgentReport:
        """查不回来：发出前运行时以为的场景（_doubt_view）和现在不一样的地方，说不准是这次执行改的还是人改的。
        如实记为来源不明，按人的修改保护（人交还之后 AI 才能改），提醒人。
        executed：确知 AI 的代码执行了（开始了、没交回结果，14.8）。这时许可单里要保持、现在又不一样的部分，
        可能是 AI 改了、没恢复成：和违规一样，单独告诉人，暂停这个 AI，等人看过再继续。"""
        diff = self._absorb_human(self._doubt_view(d), recs, source="unattributed")
        self._learn(recs)
        self.view = recs
        if boot is not None:
            self.app_boot = boot
        names = sorted({display(diff.created.get(c) or diff.deleted.get(c) or (diff.modified.get(c) or (None, {}))[1], c)
                        for c in diff.ids})
        groups = sorted(diff.ids)
        suspect = self._suspect(d, recs) if executed else []
        self.rt.resolve_unattributed(permit, groups, suspect=suspect)
        what = "、".join(names[:12]) + ("……" if len(names) > 12 else "") if names else "没有变化"
        if names:
            text = (f"场景里这些地方和执行前不一样，说不准是你改的还是人改的，已按人的修改保护，你现在不能改：{what}。"
                    "人交还之后才能再改。")
        else:
            text = "场景和执行前一样。" if executed else "场景和执行前一样，那次应该没有执行。"
        rep = AgentReport(status="unknown", agent=d["agent"], text=(
            f"你之前有一次执行的结果没收到（{d.get('reason') or ''}），查不回来（{why}）。" + text))
        if names:
            rep.alert = (f"注意：AI 的一次执行结果没收到，也查不回来（{why}）。场景里这些地方和执行前不一样，"
                         f"说不准是 AI 还是你改的，已按你的修改保护：{what}。如果是 AI 改的、你同意它继续改，请交还给它。")
            rep.actions = [{"kind": "release", "id": c, "label": f"交还 {self._group_name(c)}"} for c in groups[:5]]
        if suspect:
            sn = "、".join(self._group_name(g) for g in suspect)
            rep.suspended = d["agent"] in self.rt.suspended
            rep.text += (f"\n那次执行可能改到了按规则不能动的 {sn}，没能确认恢复。"
                         + ("你已被暂停，等人检查之后才能继续。" if rep.suspended else ""))
            rep.alert = "\n".join(x for x in (rep.alert, (
                f"注意：按规则要保持的 {sn} 现在和那次执行前不一样，可能是 AI 改了、没能恢复（收尾没完成）。请检查。"
                + ("AI 已暂停，处理好之后让它继续（resume_agent）。" if rep.suspended else ""))) if x)
        return rep

    def _doubt_view(self, d: dict) -> Records:
        """发出前运行时以为的场景：现在的 view，加上之后变了的对象那一刻的样子（old）。旧账本（format 1）里是整份 view。"""
        if "view" in d:
            return d["view"]
        out = dict(self._view)
        for c, r in (d.get("old") or {}).items():
            if r is None:
                out.pop(c, None)
            else:
                out[c] = r
        return out

    def _suspect(self, d: dict, recs: Records) -> list[str]:
        """许可单里要保持的部分（以及不许删的祖先），现在和发出前不一样的对象（14.8，rules.suspects）。"""
        return R.suspects(d.get("plan") or {}, d.get("keep_alive") or (), self._doubt_view(d), recs)

    def _offers(self, rep: AgentReport, permit) -> list[str]:
        """给人的提醒和按钮（哪些要提醒由核心定：core.explain.offers，每件事只提醒一次）：拦下了补回人删掉的对象 →
        "允许重建"（14.4）；推断是同一个对象、把人改过的部分带到了新对象上 → "不是同一个"（14.5）。"""
        st, notes = rep.settlement, []
        held, carried = X.offers(st, permit, rep.agent, self._alerted)
        names = []
        for cid in held:
            nm = self._group_name(cid)
            names.append(nm)
            rep.actions.append({"kind": "release", "id": cid, "label": f"允许 AI 重建 {nm}"})
        if names:
            notes.append(f"注意：AI 想把你删掉的 {'、'.join(names)} 建回来，已经拦下。你同意的话，点\"允许重建\"。")
        names = []
        for old in carried:
            v = st.reidentified[old]
            nm = display(rep.after.get(old) or rep.before.get(old), v.get("name") or old)
            names.append(nm)
            rep.actions.append({"kind": "unmatch", "id": old, "permit": permit.permit_id, "label": f"{nm} 不是同一个"})
        if names:
            notes.append(f"注意：AI 删掉又新建了 {'、'.join(names)}。按名字、类型、父对象推断是同一个对象，你改过的部分"
                         "已经带到新对象上。如果不是同一个，点\"不是同一个\"拆开。")
        return notes

    def _refused_report(self, agent: str, refused: dict) -> AgentReport:
        why = refused.get("reason")
        if why == "paused":
            text = "这次没有执行：人暂停了所有 AI。等人恢复之后再试。"
        elif why == "in_doubt":
            text = ("这次没有执行：之前有一次执行的结果还没收到，查清之前不会开始新的执行"
                    "（说不准哪些变化是谁做的）。稍后再试。")
        elif refused.get("possible"):
            names = "、".join(self._group_name(g) for g in refused.get("objects", []))
            text = (f"这次没有执行：你之前有一次执行没交回结果，可能改到了按规则不能动的 {names}，已被暂停。"
                    "等人检查之后才能继续。")
        else:
            names = "、".join(self._group_name(g) for g in refused.get("objects", []))
            text = f"这次没有执行：你上一次的操作违规（{names}），已被暂停。等人处理之后才能继续。"
        return AgentReport(status="refused", agent=agent, text=text)

    def _with_notes(self, rep: AgentReport, skip: Optional[str] = None) -> AgentReport:
        """把要告诉这个 AI 的话（例如它上次没收到的结果已经查清）放在说明前面。"""
        notes = self._notes.pop(rep.agent, [])
        if skip is not None and notes and notes[-1] == rep.text:
            notes = notes[:-1]
        if notes:
            rep.text = "\n\n".join(notes + [rep.text])
        return rep

    # ------------------------------------------------------------------
    # 账本落盘（design_v0.5.md 14.3）
    # ------------------------------------------------------------------
    # 账本是追加写的日志（core.store，spec/core.md 第 7 节）：第一行是整份记录（运行时 + 会话的小字段），场景的观察缓存
    # （view、values、每个 AI 的视图）是附属文件；之后每次只追加这一步的变化。日志长了再整份重写（压缩），
    # 先写临时文件再改名，存到一半退出也不会留下坏文件。
    COMPACT_LINES = 2000
    COMPACT_BYTES = 4 << 20

    def _small_state(self) -> dict:
        return {"document": self.capabilities.get("document"), "boot": self.app_boot, "scene": self.scene,
                "granularity": self.granularity, "human": self.human, "selected": sorted(self.selected),
                "agent_names": self.agent_names, "human_deleted": self.human_deleted, "aliases": self.aliases,
                "labels": self.labels, "n_op": self._n_op, "n_step": self._n_step, "doubts": self._doubts,
                "tokens": self._tokens, "notes": self._notes, "alerted": sorted(list(x) for x in self._alerted),
                "obs_gen": self.obs_gen, "av_time": {a: v.time for a, v in self.agent_views.items()}}

    def _apply_small(self, st: dict) -> None:
        if "selected" in st:
            self.selected = set(st["selected"])
        if "agent_names" in st:
            self.agent_names.update(st["agent_names"])
        for k, attr in (("human_deleted", "human_deleted"), ("aliases", "aliases"), ("labels", "labels"),
                        ("n_op", "_n_op"), ("n_step", "_n_step"), ("doubts", "_doubts"), ("tokens", "_tokens"),
                        ("notes", "_notes"), ("obs_gen", "obs_gen"), ("boot", "_saved_boot")):
            if k in st:
                setattr(self, attr, st[k])
        if "alerted" in st:
            self._alerted = {tuple(x) for x in st["alerted"]}
        for a, t in (st.get("av_time") or {}).items():
            self.agent_views.setdefault(a, AgentView()).time = t

    @staticmethod
    def _dumps(v) -> str:
        return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)

    def _save(self) -> None:
        """每次执行、发现变化之后存一次（core.store.AppendLog）：追加这一步的变化；还没有整份记录、换了文件、
        或者日志太长了，就整份重写。"""
        if not self.ledger_path:
            return
        if self._log is None or self._log.path != self.ledger_path:
            self._log = AppendLog(self.ledger_path, self.COMPACT_LINES, self.COMPACT_BYTES)
        if self._log.due():
            self._write_snapshot()
            return
        self._log.append(self._delta())

    def _delta(self) -> dict:
        d: dict = {}
        ss = {}
        for k, v in self._small_state().items():
            js = self._dumps(v)
            if self._saved_ss.get(k) != js:
                ss[k] = v
                self._saved_ss[k] = js
        if ss:
            d["ss"] = ss
        rt = self.rt.take_delta()
        if rt:
            d["rt"] = rt
        if self._dirty_view:
            d["view"] = {c: self._view.get(c) for c in sorted(self._dirty_view)}
        if self._dirty_values:
            d["values"] = {c: self.values.get(c) for c in sorted(self._dirty_values)}
        av = {}
        for a, x in self._av_dirty.items():
            v = self.agent_views.get(a)
            if v is None:
                continue
            av[a] = {"reset": bool(x.get("reset")), "old": {c: v.old[c] for c in x.get("old", ()) if c in v.old},
                     "old_values": {c: v.old_values[c] for c in x.get("old_values", ()) if c in v.old_values}}
        if av:
            d["av"] = av
        self._dirty_view, self._dirty_values, self._av_dirty = set(), set(), {}
        return d

    def _write_snapshot(self) -> None:
        """整份重写：观察缓存是附属文件，先写它，再写账本（第一行引用它），最后删掉旧的（core.store）。
        基线换成现在的场景：没建记录的单元从开始到现在都没变过，它的基线就是现在的样子（建了记录的自己带着版本 0）。"""
        obs = {"format": 2, "view": self._view, "values": self.values,
               "agent_views": {a: {"old": v.old, "old_values": v.old_values, "time": v.time}
                               for a, v in self.agent_views.items()}}
        if self.sparse:
            self.baseline = dict(self._view)
        small = self._small_state()
        self._log.write_snapshot({"format": 2, "ss": small, "rt": self.rt.snapshot()}, side=obs)
        self._dirty_view, self._dirty_values, self._av_dirty = set(), set(), {}
        self._saved_ss = {k: self._dumps(v) for k, v in small.items()}


    def _load(self, path: str) -> bool:
        """接着存下的账本用。必须是同一个文档（接入代码报的 document）、同样的粒度，否则不用（返回 False）。
        认两种：追加写的日志（format 2，core.store）；整份重写的旧账本（format 1，读进来之后下一次落盘整份重写成 format 2）。"""
        log = AppendLog(path, self.COMPACT_LINES, self.COMPACT_BYTES)
        got = log.read()
        if got is None:
            return False
        kind, head, deltas = got
        if kind == "whole":
            return isinstance(head, dict) and head.get("format") == 1 and self._load_v1(head)
        ss = head.get("ss") or {}
        doc = self.capabilities.get("document")
        if (head.get("format") != 2 or doc is None or ss.get("document") != doc
                or ss.get("granularity") != self.granularity or ss.get("scene") != self.scene):
            return False
        try:
            obs = log.read_side(head["obs"])
        except (OSError, ValueError, KeyError):
            return False
        app = dict(self.rt.app)                      # 这次连接测出来的能力为准
        self.rt.load_state(head["rt"])
        self._view, self.values = dict(obs["view"]), {c: dict(v) for c, v in obs["values"].items()}
        self.agent_views = {a: AgentView(dict(v["old"]), {c: dict(x) for c, x in v["old_values"].items()}, v["time"])
                            for a, v in obs["agent_views"].items()}
        self._apply_small(ss)
        if self.sparse:
            self._use_baseline(self._view)
        for d in deltas:
            self._replay(d)
        self.rt.app = app
        self.rt._mark_saved()
        self._finish_load()
        self._dirty_view, self._dirty_values, self._av_dirty = set(), set(), {}
        self._log = log
        self._saved_ss = {k: self._dumps(v) for k, v in self._small_state().items()}
        return True

    def _replay(self, d: dict) -> None:
        if d.get("rt"):
            self.rt.apply_delta(d["rt"])
        for c, r in (d.get("view") or {}).items():
            if r is None:
                self._view.pop(c, None)
            else:
                self._view[c] = r
        for c, v in (d.get("values") or {}).items():
            if v is None:
                self.values.pop(c, None)
            else:
                self.values[c] = dict(v)
        for a, x in (d.get("av") or {}).items():
            av = self.agent_views.setdefault(a, AgentView())
            if x.get("reset"):
                av.old, av.old_values = {}, {}
            av.old.update(x.get("old") or {})
            av.old_values.update({c: dict(v) for c, v in (x.get("old_values") or {}).items()})
        if d.get("ss"):
            self._apply_small(d["ss"])

    def _load_v1(self, st: dict) -> bool:
        """旧账本（format 1，整份重写）：照旧读进来，没人改过的单元也都建了记录（下一次落盘整份重写成 format 2）。"""
        doc = self.capabilities.get("document")
        if (doc is None or st.get("document") != doc
                or st.get("granularity") != self.granularity or st.get("scene") != self.scene):
            return False
        app = dict(self.rt.app)
        self.rt.load_state(st["runtime"])
        self.rt.app = app
        self._view, self.values = dict(st["view"]), {c: dict(v) for c, v in st["values"].items()}
        self.selected = set(st["selected"])
        self.agent_views = {}
        for a, v in st["agent_views"].items():           # 那时存的是整份：只留和现在不一样的
            av = self.agent_views[a] = AgentView(time=v["time"])
            recs, vals = v["recs"], v["values"]
            for c in set(recs) | set(self._view):
                if recs.get(c) != self._view.get(c):
                    av.old[c] = recs.get(c)
                    av.old_values[c] = dict(vals.get(c) or {})
        self.agent_names.update(st["agent_names"])
        self.human_deleted, self.aliases = st["human_deleted"], st["aliases"]
        self.labels, self._n_op, self._n_step = st["labels"], st["n_op"], st["n_step"]
        self._doubts, self._tokens, self._notes = st["doubts"], st["tokens"], st["notes"]
        self._alerted = {tuple(x) for x in st["alerted"]}
        if self.sparse:
            self._use_baseline(self._view)
        self._finish_load()
        self._log = None                                 # 下一次落盘整份重写
        return True

    def _finish_load(self) -> None:
        for actor, kind in ((self.human, ActorKind.HUMAN), (BASELINE_ACTOR, ActorKind.AGENT)):
            if actor not in self.rt.actors:
                self.rt.register_actor(actor, kind)
        for a in self.agent_names:
            if a not in self.rt.actors:
                self.rt.register_actor(a, ActorKind.AGENT)
        self._started = True
        for a in self.agent_names:
            if a not in self.agent_views or a not in self.rt.seen:
                self._see(a)

    def _reconcile(self) -> Records:
        """接着存下的账本，和现在的场景对一遍：先查清结果没收到的执行；剩下的差异是代理没运行期间发生的，
        说不准是谁做的（人，或者绕过代理的工具），记为来源不明，按人的修改保护（14.3）。"""
        self._resolve_in_doubt()
        if self._incr:                               # 同一个 Blender 进程还在：只收上次落盘之后的变化
            res = self.bridge.call("poll", self._dargs())
        else:
            res = self.bridge.call("poll", self._args(known=self._known(self.view)))
            self._learn(res["records"])
        if not self._doubts:
            if self._incr:
                d = self._observed(res, source="offline")
            else:
                d = self._absorb_human(self.view, res["records"], source="offline")
                self.view = res["records"]
            if d:
                names = sorted(r["name"] for r in list(d.created.values()) + list(d.deleted.values()))
                names += sorted(new["name"] for _, new in d.modified.values())
                self.start_alert = ("注意：RightOfWay 上次停下之后，场景里有这些变化（说不准是谁做的，已按你的修改保护）："
                                    + "、".join(names[:12]) + ("……" if len(names) > 12 else "")
                                    + "。如果是 AI 改的、你同意它继续改，请交还给它。")
        if res.get("boot"):
            self.app_boot = res["boot"]
        if not self._incr:
            self._set_selected(cid for cid, r in self.view.items() if r.get("selected"))
        self._apply_requests(res)
        self._save()
        return self.view

    # ------------------------------------------------------------------
    # 人交还（design_v0.5.md 14.4）
    # ------------------------------------------------------------------
    def _apply_requests(self, res: dict) -> bool:
        """人在应用里点了提醒里的按钮（接入代码记下，随结果交回）：
          release  交还：例如允许 AI 重建人删掉的对象（14.4），或者改来源不明的部分；
          unmatch  不是同一个：把一次认回拆开（14.5）。
        只有人能点：AI 的代码在执行期间改了这些记录的，接入代码会撤销并报告（forged_release）。
        返回有没有照着做（做了的都已落盘：hand_back、unmatch 自己存）。"""
        applied = False
        for r in res.get("requests") or ():
            cid = r.get("id")
            if not cid:
                continue
            name = r.get("name") or self._group_name(cid)
            if r.get("kind", "release") == "release":
                for a in self.agent_names:
                    self._notes.setdefault(a, []).append(f"人把 {name} 交还给了 AI：现在可以改它了（被人删掉的，也可以重建了）。")
                self.hand_back(cid)
                applied = True
            elif r.get("kind") == "unmatch":
                self.unmatch(cid, r.get("permit"))
                applied = True
        return applied

    def unmatch(self, old: str, permit_id: Optional[str] = None) -> str:
        """人说"不是同一个"（design_v0.5.md 14.5）：认回是按名字、类型、父对象推断的，可以撤销。
        原来的对象放回那次执行之前的样子（这是人的决定，记在人名下）；AI 新建的那个放回 AI 的版本，
        换一个新编号，记在那个 AI 名下。只拆这一个对象。返回结果说明。"""
        entries = [e for e in self.rt.identity_log if e["object"] == old and not e.get("undone")
                   and e["kind"] in ("gone", "rebind") and (permit_id is None or e["permitId"] == permit_id)]
        if not entries:
            return "找不到这次认回（或者已经拆开过）"
        e = entries[-1]
        res = self.bridge.call("unmatch", self._dargs(permit_id=e["permitId"], old=old))
        if res.get("pre"):                             # 只传变化：拆开之前运行时还没看到的变化，先照常记（人的修改）
            self._observed(res["pre"])
        if res.get("status") != "ok":
            return {"changed": "之后又改过，不能自动拆开", "unavailable": "拆开用的材料已经没有了"}.get(
                res.get("status"), str(res.get("status")))
        if "aliases" in res:
            self.aliases = dict(res["aliases"])
        before, after, new = res["before"], res["after"], res["new"]
        ub, ua = self._units(before), self._units(after)
        for uid, content in sorted(ua.items()):
            if split_unit(uid)[0] == new:
                self.rt.create_object(uid, OBJECT_TYPE, content, e["agent"],
                                      provenance={"splitFrom": old, "permitId": e["permitId"]})
            elif uid not in ub or ub[uid]["fp"] != content["fp"]:
                if uid in self.rt.objects:
                    self.rt.human_edit(self.human, uid, content, occupy=False)
                else:
                    self.rt.create_object(uid, OBJECT_TYPE, content, self.human)
        for uid, content in sorted(ub.items()):
            if uid not in ua and uid in self.rt.objects:
                self.rt.human_edit(self.human, uid, {**DELETED, **{k: content[k] for k in ("name", "type", "parent", "kind")
                                                                   if content.get(k) is not None}}, occupy=False)
        self._learn(after)
        if res.get("delta"):
            self._set_records(after, res.get("deleted") or ())
            self.obs_gen = res.get("gen", self.obs_gen)
            if res.get("sel") is not None:
                self._sync_selected(res["sel"])
        else:
            self.view = after
        self.rt.mark_unmatched(self.human, e["permitId"], old, new)
        name, new_name = display(after.get(old), old), display(after.get(new), new)
        self._notes.setdefault(e["agent"], []).append(
            f"人认为 {name} 不是你删掉又新建的那个，已经拆开：原来的 {name} 放回了那次执行之前的样子；"
            f"你新建的那个现在是另一个对象（{new_name}），你还没看过它，先看一下再改。")
        self._save()
        return "已拆开"

    def _conflicts(self, rep: AgentReport, before: Records, raw: Records, after: Records,
                   before_values: Values, raw_values: Values) -> list[dict]:
        """每个没生效的单元一条（核心算：core.explain.conflicts）。说明文字、失效的事实、MCP 代理的结构化结果都从这里来，
        这里又只来自运行时的核对结果。"""
        return X.conflicts([s_ for s_ in rep.skipped if s_["reason"] != R_REVERTED], rep.commands, before, raw, after,
                           before_values, raw_values, display,
                           lambda face: label(face, self.labels) if face else "整个对象")

    def _facts(self, rep: AgentReport) -> list[dict]:
        """失效的事实和新的目标（核心算：core.explain.facts）。"""
        return X.facts(rep.conflicts, rep.recreate_blocked)

    def _derived(self, before: Records, after: Records, changed=None) -> list[dict]:
        """派生改动，只告知（核心算：core.explain.derived）：AI 新建的数据块是已有的副本；人改过的数据块少了使用者。"""
        suffix = self.capabilities.get("suffix")
        return X.derived(before, after, set(before) | set(after) if changed is None else changed, suffix,
                         lambda n: strip_suffix(n, suffix), self._group_touched, display)

    def _group_touched(self, cid: str, rec: Optional[dict] = None) -> bool:
        """这个应用对象有没有人碰过（或来源不明）、还没交还的部分。"""
        rec = rec or self._view.get(cid)
        uids = {cid} if self.granularity == OBJECT else {f"{cid}#{a}" for a in (rec or {}).get("aspects", {})}
        uids |= {u for u in self.rt.objects if split_unit(u)[0] == cid}      # 建了记录的（只在少见的情况下用到）
        return any(self.rt.touched_by_human(u) for u in uids)

    def _fact_text(self, f: dict) -> str:
        return MSG.fact_text(self, f)

    def payload(self, rep: AgentReport) -> dict:
        """一次执行的结构化结果（给工具调用用，例如 MCP 代理的 structuredContent）。
        和说明文字同一个来源：运行时的核对结果（P1、P8）。"""
        groups = lambda uids: sorted({display((rep.after.get(c) or rep.before.get(c)), c)    # noqa: E731
                                      for c in {split_unit(u)[0] for u in uids}})
        return {
            "status": rep.status, "outcome": rep.outcome, "suspended": rep.suspended,
            "applied": groups(rep.committed), "breach": groups(rep.breach),
            "conflicts": [{k: c[k] for k in ("object", "label", "owner", "reason", "now", "wanted",
                                             "delete_refused", "precheck")} for c in rep.conflicts],
            "facts": [{k: v for k, v in f.items() if k != "id"} for f in rep.facts],
            "identity": {"reidentified": rep.reidentified, "rebound": rep.rebound,
                         "recreate_blocked": rep.recreate_blocked, "errors": rep.identity_errors},
            "loops": [{k: v for k, v in x.items() if k != "id"} for x in rep.loops],
            "derived": list(rep.derived),
            "missed": list(rep.missed),
            "commands": [{"index": e["index"], "status": e["status"],
                          **{k: e[k] for k in ("reason", "faces", "error") if e.get(k)}} for e in rep.commands],
            "error": rep.error.strip().splitlines()[-1] if rep.error else None,
            "finish_error": rep.finish_error,
        }

    @staticmethod
    def is_error(rep: AgentReport) -> bool:
        """这次调用对 AI 来说算不算"出错"（MCP 的 isError：模型可以据此自我纠正）：没执行、违规、
        有没生效的部分、命令失败、脚本出错。"""
        return (rep.status != "ok" or rep.outcome == "breach" or bool(rep.conflicts) or bool(rep.recreate_blocked)
                or any(e["status"] in ("refused", "partial", "failed") for e in rep.commands) or bool(rep.error)
                or bool(rep.finish_error))

    def _report_from(self, rep: AgentReport, st, before: Records, raw: Records, after: Records) -> None:
        """报告里的"生效 / 没生效 / 违规"全部取自运行时的核对结果。"""
        def name(uid: str) -> str:
            cid = split_unit(uid)[0]
            return display(before.get(cid) or raw.get(cid) or after.get(cid), cid)
        rep.settlement = st
        rep.outcome, rep.suspended, rep.reserved = st.outcome, st.suspended, st.reserved
        rep.committed = sorted(st.applied + st.created)
        level_text = {"exact": "同名", "suffix": "去掉重名后缀后同名", "global": "同名，父对象换了"}
        for old, v in sorted(st.reidentified.items(), key=lambda kv: str(kv[1].get("name"))):
            nm = display(before.get(old) or raw.get(old) or after.get(old), v.get("name") or old)
            {"gone": rep.reidentified, "rebind": rep.rebound, "tomb": rep.recreate_blocked}[v["kind"]].append(nm)
            rep.identity_levels[nm] = level_text.get(v["level"], v["level"])
        rep.identity_errors = list(st.identity_errors)
        for uid, n in sorted(st.loops.items()):
            cid, face = split_unit(uid)
            rep.loops.append({"id": cid, "object": name(uid), "face": face,
                              "label": label(face, self.labels) if face else "整个对象", "times": n})
        rep.loop_alert = [name(u) + (f" 的{label(split_unit(u)[1], self.labels)}" if split_unit(u)[1] else "")
                          for u in st.loop_alert]
        for uid, k in sorted(st.kept.items()):
            if k.get("recreate"):
                continue                               # 补回人删掉的对象：已经在 recreate_blocked 里
            rep.skipped.append({"id": uid, "name": name(uid), "reason": k["reason"],
                                **({"by": k["by"]} if "by" in k else {}),
                                **({"source": k["source"]} if k.get("source") else {}),
                                **({"whole_object": True} if k.get("whole_object") else {}),
                                **({"ancestor": True} if k.get("ancestor") else {}),
                                **({"precheck": True} if k.get("precheck") else {})})
        rep.skipped += [{"id": uid, "name": name(uid), "reason": R_REVERTED} for uid in st.reverted]
        rep.skipped.sort(key=lambda x: x["id"])
        rep.breach = sorted(st.breach)
        rep.breach_detail = dict(st.breach)
        rep.side_effects = list(st.side_effects)

    def _control_accounting(self, rep: AgentReport, permit, step_id: str, ub: dict, ua: dict, ur: dict,
                            before: Records, raw: Records) -> None:
        """对照组（没有保护，相当于现在的 Blender MCP）：场景里 AI 的修改全部生效。
        运行时照旧按规则记账（规则不允许的不记），用来对比"有没有 RightOfWay"。"""
        attempted = diff_records(ub, ur)
        for uid in sorted(attempted.ids):
            cid = split_unit(uid)[0]
            name = (before.get(cid) or raw.get(cid) or {}).get("name", cid)
            if uid in attempted.created:
                if uid not in ua:
                    continue
                self._n_op += 1
                op = Operation(f"op-{self._n_op}", rep.agent, CAPABILITY, produces=[uid], step_id=step_id)
                self.rt.submit(op)
                self.rt.complete(op.op_id, produces={uid: ua[uid]}, produce_types={uid: OBJECT_TYPE})
            elif uid in self.rt.objects and uid not in permit.keep:
                self._submit(rep.agent, uid, permit, step_id, ua.get(uid) or {**DELETED, "name": name})
            rep.committed.append(uid)
        self.rt.close_permit(permit, [])

    def _group_name(self, cid: str) -> str:
        if cid in self.view:
            return display(self.view[cid], cid)
        for uid, o in self.rt.objects.items():
            if split_unit(uid)[0] == cid and isinstance(o.content, dict) and o.content.get("name"):
                return display(o.content, cid)
        return cid

    def _alert(self, rep: AgentReport, before: Records, raw: Records) -> str:
        return MSG.alert(self, rep, before, raw)

    def _breach_alert(self, rep: AgentReport) -> str:
        return MSG.breach_alert(self, rep)

    def resume_agent(self, agent: Optional[str] = None) -> None:
        """人处理完违规之后，让这个 AI 继续。人的决定，马上落盘。"""
        self.rt.resume_agent(self.human, agent or self.agent)
        self._save()

    def _submit(self, agent: str, uid: str, permit, step_id: str, content: dict) -> bool:
        """把 AI 对一个单元的修改记进运行时，基准版本是许可单里这个 AI 看到的版本。返回是否生效。"""
        self._n_op += 1
        op = Operation(f"op-{self._n_op}", agent, CAPABILITY,
                       targets=[Target(uid, permit.base.get(uid, -1))], step_id=step_id)
        r = self.rt.submit(op)
        if r.status.is_rejection:
            return False
        r = self.rt.complete(op.op_id, writes={uid: content})
        return r.status in (OpStatus.COMMITTED, OpStatus.MERGED)

    # ------------------------------------------------------------------
    def _why(self, s: dict) -> str:
        return MSG.why_text(self, s)

    def _describe(self, rep: AgentReport, before: Records, after: Records,
                  before_values: Values, raw_values: Values) -> str:
        return MSG.describe(self, rep, before, after, before_values, raw_values)

    def _command_lines(self, rep: AgentReport) -> list[str]:
        return MSG.command_lines(self, rep)

    # ------------------------------------------------------------------
    # 人的操作
    # ------------------------------------------------------------------
    def hand_back(self, cid: str) -> None:
        """人把整个对象交还给 AI：同时结束占用。人的决定，马上落盘（只有交还、场景没变时也一样）。"""
        # 基线算人的：这个对象没建记录的基线单元也一起交还（建上记录）
        self.rt.hand_back_group(self.human, cid, self._base_units_of(cid) if self.rt.baseline_owner is not None else ())
        self._save()

    def scene_records(self) -> Records:
        """现在场景里的记录，和这个会话用同样的观察方式（例如数据块是否单独成单元）。核对、评估时用。"""
        return self.bridge.call("poll", self._args())["records"]

    def find(self, name: str) -> Optional[str]:
        for cid, rec in self.view.items():
            if rec["name"] == name:
                return cid
        return None
