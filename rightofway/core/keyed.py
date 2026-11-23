"""按编号的容器（spec/core.md 第 2.1 节）：3D 的面，以后的 Excel 单元格。单元按编号查，墓碑落在编号上。

这是核心里唯一做决定的地方之一（另一个是按位置的容器，positional.py；两者的判断都用 rules.judge）：
开许可单（admit）、执行后按实际状态核对和记账（settle）、许可单的生命周期（spec/core.md 第 5 节）、
人的修改和交还、来源不明的变化、违规后暂停、反复改改不动的提醒、认回同一个对象的核对（identity.py）、账本落盘。

阅读建议：先看 admit()（准入 → 每个单元：暂时不能改的情况 → 判断表），再看 settle()（逐个单元归类：
已生效 / 已保留 / 违规 / 改回去了 / 连带变化），然后是生命周期（dispatched、mark_in_doubt、resolve_unattributed、
close_permit）和落盘（to_state、take_delta）。

早期的操作模型（逐项提交 submit / complete、步骤、候选、确认、三方合并）不在核心里：在 rightofway/runtime.py，
它继承这个类，通过下面几个钩子（_on_changed、_on_settled……）接上，规则仍然用这里的。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict
from typing import Any, Callable, Iterable, Optional

from . import loops as LOOPS
from . import rules as R
from .identity import suffix_pattern
from .identity import verify as verify_identity
from .model import (KEEP_HUMAN, KEEP_OTHER, KEEP_OTHER_AGENT, Actor, ActorKind,
                    Authority, AuthorityKind, Event, HumanTouched, ObjectState, Occupancy, Permit, Settlement,
                    VersionRecord, group_of)

REIDENTIFY_RULE = "name+type+parent"   # 认回同一个对象：同一个父对象下名字、类型一样（core/identity.py）


class ProtocolError(Exception):
    """调用方违反协议（例如 Agent 冒充人、释放别人占用的对象）。"""


class ObservationError(ProtocolError):
    """R18：适配器报告的变化找不到可归属的对象，不能静默丢弃。"""


_MISSING = object()


class _Ledger(dict):
    """账本里的单元 {编号: ObjectState}。

    稀疏账本（Runtime.set_baseline，design_v0.5.md 第 16 节）：没人改过的单元不建记录，是"基线"（版本 0，没有作者，
    内容由接入层提供）。第一次用到（被改、被交还……）时才建记录（_materialize）。所以：
      ledger[uid]、ledger.get(uid)   基线单元也取得到（顺便建记录）；
      uid in ledger                 基线单元也算在；
      迭代、len、items()             只包括建了记录的；
      ledger.peek(uid)              只看建了记录的，不建。
    没有设基线时和普通的 dict 一样。"""

    def __init__(self, rt: "KeyedLedger") -> None:
        super().__init__()
        self._rt = rt

    def __missing__(self, key: str) -> ObjectState:
        obj = self._rt._materialize(key)
        if obj is None:
            raise KeyError(key)
        return obj

    def __contains__(self, key: object) -> bool:
        return dict.__contains__(self, key) or (isinstance(key, str) and self._rt._baseline_content(key) is not None)

    def get(self, key: str, default: Any = None) -> Any:
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        obj = self._rt._materialize(key)
        return default if obj is None else obj

    def peek(self, key: str) -> Optional[ObjectState]:
        return dict.get(self, key)


class SeenMap:
    """一个 Agent 上次看到的版本（R3）。看的那一刻只记下事件序号（mark_seen），某个单元当时是第几版，用到时再从它的历史里算：
    那一刻之前的最后一个版本；从基线来、那时还没人改过的是 0；那时还不存在的返回 default。
    explicit：直接给出的版本（mark_seen(versions=...)、旧的逐项提交里 Agent 刚写进去的版本），优先。
    用起来和 {编号: 版本} 的 dict 一样（get、[]、in），但看一次不用复制全部单元的版本（以前每次 O(单元数)）。"""
    __slots__ = ("rt", "seq", "explicit")

    def __init__(self, rt: "KeyedLedger", seq: Optional[int] = None, explicit: Optional[dict[str, int]] = None) -> None:
        self.rt, self.seq, self.explicit = rt, seq, dict(explicit or {})

    def get(self, uid: str, default: Any = None) -> Any:
        if uid in self.explicit:
            return self.explicit[uid]
        if self.seq is None:
            return default
        obj = self.rt.objects.peek(uid)
        if obj is not None:
            for v in reversed(obj.versions):
                if v.seq <= self.seq:
                    return v.version
            return 0 if obj.baseline is not None else default
        return 0 if self.rt._baseline_content(uid) is not None else default

    def __getitem__(self, uid: str) -> int:
        v = self.get(uid, _MISSING)
        if v is _MISSING:
            raise KeyError(uid)
        return v

    def __contains__(self, uid: object) -> bool:
        return isinstance(uid, str) and self.get(uid, _MISSING) is not _MISSING

    def update(self, versions: dict[str, int]) -> None:
        self.explicit.update(versions)

    def copy(self) -> "SeenMap":
        return SeenMap(self.rt, self.seq, self.explicit)

    def items(self):
        """建了记录的单元里，这个 Agent 看到过的 (编号, 版本)（显示、兼容用）。"""
        for uid in list(self.rt.objects) + [u for u in self.explicit if not dict.__contains__(self.rt.objects, u)]:
            v = self.get(uid, _MISSING)
            if v is not _MISSING:
                yield uid, v

    def keys(self):
        return [u for u, _ in self.items()]

    def __iter__(self):
        return iter(self.keys())

    def to_json(self) -> dict:
        return {"seq": self.seq, "explicit": dict(self.explicit)}

    @classmethod
    def from_json(cls, rt: "KeyedLedger", d: Any) -> "SeenMap":
        if isinstance(d, dict) and set(d) <= {"seq", "explicit"} and "explicit" in d:
            return cls(rt, d.get("seq"), d.get("explicit"))
        return cls(rt, None, dict(d or {}))            # 旧账本（format 1）：直接存的 {编号: 版本}


class KeyedLedger:
    """按编号的容器的账本和流程（规则见 rules.py）。早期的操作模型在 runtime.Runtime（继承这个类）。"""

    def __init__(self, selection_occupies: bool = False, reserve_seconds: float = 30.0, pause_on_breach: bool = True,
                 reidentify: bool = True, loop_repeat: int = 3, loop_window: int = 5,
                 tighten: Optional[R.Tightener] = None) -> None:
        """reidentify：认回同一个对象（design_v0.5.md 12.4、12.5，默认打开）。一次执行里 Agent 删掉一个对象、
        又新建了同一个父对象下名字（或去掉应用自动加的重名后缀后的名字）和类型都一样的对象时，按同一个对象处理：
        新对象接过旧对象的编号。同一条规则还用来拦下"补回人删掉的对象"（墓碑）、
        把"之前想删、因为人改过而保留下来的对象"和后来新建的同名对象认成一个。
        只有接入代码在连接时的自测（learn_app）里说明支持，才会写进许可单。
        tighten：可选的一层（以后的意图层），可以让许可单多保持一些单元，不能少（rules.tighten）。"""
        self.reidentify = reidentify
        self.app: dict = {}                                   # 连接时探测出的应用能力（learn_app）
        self.wanted_gone: dict[str, dict[str, dict]] = {}     # Agent → {它想删、因为人改过而保留下来的对象: 行}
        self.identity_log: list[dict] = []                    # 认回的记录：哪次执行、哪个对象、置信等级
        # 循环检测（loops.detect）：同一个单元在这个 Agent 最近 loop_window 次执行里被保持了 loop_repeat 次，提醒人
        self.loop_repeat, self.loop_window = loop_repeat, loop_window
        self.kept_history: dict[str, list[set[str]]] = {}      # Agent → 最近几次执行里被保持的单元
        self.looping: dict[str, set[str]] = {}                 # Agent → 已经提醒过的、正在反复的单元
        self.selection_occupies = selection_occupies          # 选项："选中就算在改"
        self.reserve_seconds = reserve_seconds                # 两个 AI 冲突后，给被拒的那个预留多少秒
        self.tighten_layer = tighten                          # 可选的收紧层（意图层的出口）
        self.seen: dict[str, SeenMap] = {}                    # 每个 Agent 上次看到的每个单元的版本
        self.selection: dict[str, set[str]] = {}              # 人 → 选中的应用对象（group_of 的结果）
        self.reservations: dict[str, tuple[str, float]] = {}  # 单元 → (预留给哪个 Agent, 到期时间)
        self.permits: dict[str, Permit] = {}
        self.in_doubt: dict[str, dict] = {}                   # 结果没收到的许可单 → {agent, reason, since}（14.2）
        self._n_permit = 0                                    # 许可单编号：落盘后接着往下数，不会重复
        self.nonce = uuid.uuid4().hex[:8]                     # 这个账本的编号：许可单编号全局唯一，
        #                                                       接入代码按它记执行结果、只执行一次（14.2）
        self.pause_on_breach = pause_on_breach                # 违规后暂停这个 Agent，等人处理
        self.suspended: dict[str, dict] = {}                  # 被暂停的 Agent → 原因
        self.actors: dict[str, Actor] = {}
        self.objects: _Ledger = _Ledger(self)
        self.events: list[Event] = []
        self.paused = False                                   # 人暂停了全部 AI（总闸）
        self._seq = 0
        self._subscribers: list[Callable[[Event], None]] = []
        # 稀疏账本（set_baseline）：没人改过的单元是基线，内容由接入层提供，不建记录
        self._base_content: Optional[Callable[[str], Any]] = None
        self._base_units: Optional[Callable[[], Iterable[str]]] = None
        self._base_units_of: Optional[Callable[[str], Iterable[str]]] = None
        self._base_row: Optional[Callable[[str], Optional[dict]]] = None
        self.baseline_owner: Optional[str] = None       # 基线算谁改的：None 表示没人改过；人 → 全部按人改过保护
        self.baseline_actor = "baseline"
        self.baseline_type = "blob"
        # 落盘只追加这一步的变化（take_delta）：上次落盘之后改过的单元，以及小字段上次落盘时的样子
        self._dirty_objects: set[str] = set()
        self._saved_small: dict[str, str] = {}
        self._saved_log = 0

    # ------------------------------------------------------------------
    # 给早期操作模型（runtime.Runtime）留的钩子：核心里什么也不做
    # ------------------------------------------------------------------
    def _on_changed(self, object_id: str, exclude_step: Optional[str] = None) -> None:
        """一个单元有了新版本（R12：依赖它的步骤过期）。"""

    def _on_occupied(self, object_id: str) -> None:
        """人开始占用一个单元（R9：读它的步骤阻塞）。"""

    def _on_released(self, object_id: str) -> None:
        """人释放了占用。"""

    def _on_settle_start(self, step_id: Optional[str]) -> None:
        pass

    def _on_settle_end(self, step_id: Optional[str]) -> None:
        pass

    def _on_settled(self, op_id: str, agent: str, obj: ObjectState, rec: VersionRecord, step_id: Optional[str],
                    produced: bool) -> None:
        """结算时按实际状态记了一个单元（早期模型在这里补操作记录、步骤）。"""

    def _on_breach(self, op_id: str, agent: str, uid: str, base: int, step_id: Optional[str]) -> None:
        pass

    def _decorate_keep(self, oid: str, held: bool, why: dict) -> dict:
        """要保持的原因；早期模型的"留作候选"策略在这里给人改过的单元加上 candidate。"""
        return why

    def _keep_candidate(self, op_id: str, agent: str, uid: str, raw: Any, step_id: Optional[str]) -> None:
        """保持了人的版本、AI 的版本另存为候选（只在"留作候选"策略下用）。"""

    def _small_extra(self) -> dict:
        return {}

    def _state_extra(self) -> dict:
        return {}

    def _load_extra(self, state: dict) -> None:
        pass

    def _delta_extra(self) -> dict:
        return {}

    def _apply_extra(self, d: dict) -> None:
        pass

    def _mark_saved_extra(self) -> None:
        pass

    def register_actor(self, actor_id: str, kind: ActorKind) -> Actor:
        """R1：操作者身份由运行时登记，之后一律以登记的身份为准。"""
        actor = Actor(actor_id, ActorKind(kind))
        self.actors[actor_id] = actor
        return actor

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

    def set_baseline(self, content: Callable[[str], Any], units: Callable[[], Iterable[str]],
                     units_of: Callable[[str], Iterable[str]], row: Optional[Callable[[str], Optional[dict]]] = None,
                     owner: Optional[str] = None, actor: str = "baseline", type_: str = "blob") -> None:
        """没人改过的单元不建记录：它是基线（版本 0），内容由接入层按需提供（content(编号) → 内容，不在时 None）。
        units()：全部基线单元的编号；units_of(应用对象)：它的基线单元；row(应用对象)：{"name", "type", "parent", ...}。
        owner：基线算谁的。None（默认）表示没人改过，Agent 可以改；人的编号表示全部按人改过保护（initial_owner="human"），
        用规则表示（admit 时展开），不逐个单元建记录。"""
        self._base_content, self._base_units, self._base_units_of = content, units, units_of
        self._base_row = row
        self.baseline_owner, self.baseline_actor, self.baseline_type = owner, actor, type_

    @property
    def sparse(self) -> bool:
        return self._base_content is not None

    def _baseline_content(self, uid: str) -> Any:
        return None if self._base_content is None else self._base_content(uid)

    def _materialize(self, uid: str) -> Optional[ObjectState]:
        """给一个基线单元建记录（版本 0 = 基线的内容）。不是基线单元返回 None。"""
        content = self._baseline_content(uid)
        if content is None:
            return None
        owner = self.baseline_owner
        actor = self.actors.get(owner or self.baseline_actor)
        obj = ObjectState(uid, self.baseline_type, baseline=content, baseline_author=owner or self.baseline_actor,
                          baseline_kind=actor.kind if actor is not None else ActorKind.AGENT,
                          human_touched=HumanTouched(by=owner, since_version=0) if owner else None)
        dict.__setitem__(self.objects, uid, obj)
        self._dirty_objects.add(uid)
        return obj

    def content_of(self, uid: str) -> Any:
        """账本里这个单元现在的内容（基线单元也算），不建记录。不在账本里返回 None。"""
        obj = self.objects.peek(uid)
        return obj.content if obj is not None else self._baseline_content(uid)

    def touched_by_human(self, uid: str) -> bool:
        """人碰过（或来源不明）、还没交还的单元（基线单元按 owner 算），不建记录。"""
        obj = self.objects.peek(uid)
        if obj is not None:
            return obj.human_touched is not None
        return self.baseline_owner is not None and self._baseline_content(uid) is not None

    def _touch(self, uid: str) -> None:
        self._dirty_objects.add(uid)

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

    def _commit_version(self, obj: ObjectState, content: Any, author: Actor,
                        op_id: Optional[str] = None, step_id: Optional[str] = None,
                        restored_from: Optional[int] = None) -> VersionRecord:
        self._seq += 1
        # 父版本：前一个版本；第一个版本没有父版本，除非它是从基线（版本 0）改出来的
        parent = obj.version if obj.versions else (0 if obj.baseline is not None else None)
        rec = VersionRecord(
            version=obj.version + 1, content=content, author_id=author.actor_id,
            author_kind=author.kind, op_id=op_id, step_id=step_id,
            parent_version=parent, seq=self._seq, restored_from=restored_from,
        )
        obj.versions.append(rec)
        obj.retracted = False
        self._dirty_objects.add(obj.object_id)
        # "人碰过"（hold）：人或来源不明的修改时设置，AI 修改时清除；违规时由调用方放回（_settle_breach）
        if author.kind in (ActorKind.HUMAN, ActorKind.EXTERNAL):
            obj.human_touched = HumanTouched(by=author.actor_id, since_version=rec.version)
        else:
            obj.human_touched = None
        return rec

    def mark_seen(self, agent_id: str, versions: Optional[dict[str, int]] = None) -> None:
        """R3：记下这个 Agent 看到了哪些版本（它读了场景，或者它的一次执行完成了）。
        之后它按旧印象做的修改，以这些版本为基准判断是否过时（R4）。"""
        self._actor(agent_id)
        # 只记下看的那一刻（事件序号），某个单元当时是第几版用到时再算（SeenMap），不复制全部单元的版本
        self.seen[agent_id] = SeenMap(self, explicit=versions) if versions is not None else SeenMap(self, self._seq)

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
        if obj.human_touched is not None and obj.human_touched.by.startswith("external:"):
            return {"reason": KEEP_HUMAN, "source": obj.human_touched.by}     # 来源不明，按人的修改保护（14.2）
        if obj.human_touched is not None or obj.occupancy is not None:
            return {"reason": KEEP_HUMAN}
        last = obj.versions[-1] if obj.versions else None
        if last is not None and last.author_kind == ActorKind.HUMAN:
            return {"reason": KEEP_HUMAN}
        if last is not None and last.author_kind == ActorKind.AGENT and last.author_id != agent_id:
            return {"reason": KEEP_OTHER_AGENT, "by": last.author_id}
        return {"reason": KEEP_OTHER}

    def gate(self, agent_id: str) -> Optional[dict]:
        """总闸：这个 Agent 现在能不能开始一次新的执行（rules.gate）。许可单（admit）和早期的逐项提交共用。"""
        return R.gate(agent_id, paused=self.paused, in_doubt=self.in_doubt, suspended=self.suspended)

    # ------------------------------------------------------------------
    # 判断（rules：暂时不能改的情况 → 判断表）
    # ------------------------------------------------------------------
    def _last(self, obj: ObjectState) -> R.Last:
        """判断要知道的：最后是谁改的、第几版、现在归不归人（人改过、还没交还）。"""
        held = obj.human_touched.by if obj.human_touched is not None else None
        if obj.versions:
            v = obj.versions[-1]
            return R.Last(v.author_id, v.author_kind.value, v.version, held)
        return R.Last(obj.baseline_author, obj.baseline_kind.value if obj.baseline_kind is not None else None, 0, held)

    def _selected_by(self, oid: str) -> Optional[str]:
        """"选中就算在改"打开时，选中了这个单元所在的应用对象的人。"""
        if not self.selection_occupies:
            return None
        g = group_of(oid)
        for holder, groups in self.selection.items():
            if g in groups:
                return holder
        return None

    def _temporary(self, agent_id: str, oid: str, obj: Optional[ObjectState] = None) -> Optional[dict]:
        occupied = obj.occupancy.holder if obj is not None and obj.occupancy is not None else None
        return R.temporary(agent_id, selected=self._selected_by(oid) is not None,
                           reserved=self.reservations.get(oid), occupied=occupied)

    def verdict(self, agent_id: str, oid: str, obj: Optional[ObjectState] = None,
                seen: Optional["SeenMap"] = None) -> Optional[dict]:
        """这个 Agent 现在能不能改这个单元：返回要保持的原因，None 表示能。seen 默认是它上次看到的版本。"""
        if obj is None:
            obj = self.objects.peek(oid)
        why = self._temporary(agent_id, oid, obj)
        if why is not None:
            return why
        if obj is None:
            if self.baseline_owner is not None and self._baseline_content(oid) is not None:
                return self._decorate_keep(oid, True, {"reason": KEEP_HUMAN})
            return None
        if seen is None:
            seen = self.seen.get(agent_id) or SeenMap(self)
        why = R.judge(self._last(obj), agent_id, seen.get(oid, -1))
        return None if why is None else self._decorate_keep(oid, obj.human_touched is not None, why)

    def admit(self, agent_id: str) -> Permit:
        """为这个 Agent 的一次执行开许可单（spec/core.md 第 5 节）：总闸 → 每个单元：暂时不能改的情况（选中、预留、
        占用）→ 判断表（rules.judge）→ 可选的收紧层（只能多保持）。
        没建记录的基线单元（没人改过，版本 0）：只有被选中、被预留，或者基线算人的（initial_owner="human"，按规则展开）
        时才要保持。"""
        actor = self._actor(agent_id)
        if actor.kind != ActorKind.AGENT:
            raise ProtocolError(f"{agent_id} 不是 Agent，不需要许可")
        self._expire_reservations()
        refusal = self.gate(agent_id)
        if refusal is not None:
            self._n_permit += 1
            permit = Permit(f"permit-{self._n_permit}-{self.nonce}", agent_id, refused=refusal, state="refused")
            self.permits[permit.permit_id] = permit
            self._emit("permit/refused", permitId=permit.permit_id, agent=agent_id, reason=refusal["reason"])
            return permit
        seen = self.seen.get(agent_id) or SeenMap(self)
        keep: dict[str, dict] = {}
        for oid, obj in self.objects.items():
            if obj.retracted or _is_deleted(obj.content):
                continue
            why = self.verdict(agent_id, oid, obj, seen)
            if why is not None:
                keep[oid] = why
        if self.sparse:
            extra = {u for u in self.reservations if not dict.__contains__(self.objects, u)}
            if self.selection_occupies:
                for groups in self.selection.values():
                    for g in groups:
                        extra.update(self._base_units_of(g))
            if self.baseline_owner is not None:
                extra.update(self._base_units())
            for oid in sorted(extra):
                if oid in keep or dict.__contains__(self.objects, oid) or self._baseline_content(oid) is None:
                    continue
                why = self.verdict(agent_id, oid, None, seen)
                if why is not None:
                    keep[oid] = why
        keep = R.tighten(agent_id, keep, self.tighten_layer)
        self._n_permit += 1
        permit = Permit(f"permit-{self._n_permit}-{self.nonce}", agent_id, keep, seen.copy(),
                        reidentify=self.identity_rule())
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
        if self._base_row is not None:
            # 稀疏账本：没建记录的对象不在账本里，父对象取接入层最近看到的（I1：账本和场景一致）
            def parent_of(g: str) -> Optional[str]:
                return (self._base_row(g) or {}).get("parent")

            def known(g: str) -> bool:
                return self._base_row(g) is not None
        else:
            parents: dict[str, Optional[str]] = {}
            for oid, obj in self.objects.items():
                c = obj.content
                if obj.retracted or _is_deleted(c) or not isinstance(c, dict):
                    continue
                parents.setdefault(group_of(oid), c.get("parent"))
            parent_of, known = parents.get, parents.__contains__
        kept = {group_of(uid) for uid in keep}
        out: dict[str, dict] = {}
        for g in sorted(kept):
            p, seen = parent_of(g), {g}
            while p and p not in seen and known(p):
                seen.add(p)
                if p not in kept:
                    out.setdefault(p, {"reason": KEEP_HUMAN, "ancestor": True, "of": g})
                p = parent_of(p)
        return out

    def _tombstones(self, agent_id: str, seen: dict[str, int]) -> dict[str, dict]:
        """墓碑（design_v0.5.md 12.5、14.4）：判断表看的是删掉它的是谁（rules.judge_tombstone）。人或来源不明删掉的，
        人交还（hand_back）之前，Agent 不能补回来；它新建了同一个（同一条认回规则），接入代码在恢复之后把新建的删掉。
        Agent 看没看到这次删除都一样："知道你删了"不等于"你允许它建回来"。

        按编号的容器靠认回（名字、类型、父对象）认出"补回"，所以只对这个 Agent 见过还在的东西立墓碑：从没见过的，
        新建同名的不是"补回"。数据块按能看到的事实区分：删的时候还有对象在用 → 有意删除；没对象用了之后才消失 → 清理。
        认回只用来拦人（或来源不明）删掉、还没交还的：别的 AI 删掉的，按名字认错了会删掉一个 AI 新建的正常对象，
        所以这里不认（能力报告里如实写明，spec/core.md 第 8 节）。"""
        out: dict[str, dict] = {}
        for oid, obj in sorted(self.objects.items()):
            content = obj.content
            if obj.retracted or not _is_deleted(content) or not obj.versions:
                continue
            why = R.judge_tombstone(self._last(obj), agent_id, seen.get(oid, -1))
            if why is None or why["reason"] != KEEP_HUMAN:
                continue                          # Agent 自己删的、人已经交还；别的 AI 删的（见上）
            if content.get("kind") == "data" and not (content.get("users") or 0):
                continue                          # 没人用了之后才消失：清理
            base = seen.get(oid, -1)
            existed = any(not _is_deleted(v.content) for v in obj.versions if v.version <= base) or (
                base >= 0 and obj.baseline is not None and not _is_deleted(obj.baseline))     # 基线（版本 0）就在
            if not existed:
                continue                          # 这个 Agent 从没见过它还在
            g = group_of(oid)
            out.setdefault(g, {"name": content.get("name"), "type": content.get("type"),
                               "parent": content.get("parent"), "kind": content.get("kind")})
        return out

    OPEN_STATES = ("issued", "dispatched", "in_doubt")

    def dispatched(self, permit: Permit) -> None:
        """接入层把许可单发给应用之前调用（design_v0.5.md 14.1）。之后这张许可单要么结算、要么关闭、要么记为结果未知。"""
        if permit.state != "issued":
            raise ProtocolError(f"{permit.permit_id} 的状态是 {permit.state}，不能发出")
        permit.state = "dispatched"
        self._emit("permit/dispatched", permitId=permit.permit_id, agent=permit.agent)

    def mark_in_doubt(self, permit: Permit, reason: str) -> None:
        """发出去了，结果没收到（超时、断连、代理出错，design_v0.5.md 14.2）。可能执行了，也可能没有。
        查清之前不给任何 Agent 开新的许可单（gate），接入层也不把这期间看到的差异算成人的。"""
        if permit.state not in ("dispatched", "in_doubt"):
            raise ProtocolError(f"{permit.permit_id} 的状态是 {permit.state}，不能记为结果未知")
        first = permit.state != "in_doubt"
        permit.state = "in_doubt"
        self.in_doubt[permit.permit_id] = {"agent": permit.agent, "reason": reason, "since": time.time()}
        if first:
            self._emit("permit/in_doubt", permitId=permit.permit_id, agent=permit.agent, reason=reason)
            self._emit("alert/human", kind="in_doubt", agent=permit.agent, permitId=permit.permit_id, reason=reason)

    def resolve_unattributed(self, permit: Permit, objects: Iterable[str], suspect: Iterable[str] = ()) -> None:
        """结果查不回来（应用重启过、接入代码不记结果）：差异已经由接入层记为来源不明（record_external），这里结束许可单。
        suspect：确知执行了（开始了、没交回结果，design_v0.5.md 14.8），许可单里要保持、现在又不一样的对象。
        可能是 Agent 改了、没恢复成：和违规一样提醒人、暂停这个 Agent（pause_on_breach 时），人看过再让它继续。"""
        if permit.state not in self.OPEN_STATES:
            raise ProtocolError(f"{permit.permit_id} 已经结束（{permit.state}）")
        self.in_doubt.pop(permit.permit_id, None)
        permit.state = "unattributed"
        objs, sus = sorted(set(objects)), sorted(set(suspect))
        self._emit("permit/unattributed", permitId=permit.permit_id, agent=permit.agent, objects=objs, suspect=sus)
        if objs:
            self._emit("alert/human", kind="unattributed", agent=permit.agent, permitId=permit.permit_id, objects=objs)
        if sus:
            self._emit("alert/human", kind="possible_breach", agent=permit.agent, permitId=permit.permit_id, objects=sus)
            if self.pause_on_breach:
                self.suspended[permit.agent] = {"permitId": permit.permit_id, "objects": sus, "possible": True}
                self._emit("agent/suspended", agent=permit.agent, permitId=permit.permit_id, objects=sus)

    def close_permit(self, permit: Permit, refused: Iterable[str], state: str = "closed") -> list[str]:
        """一次执行结束：settled（settle 调用）或 closed（没有执行）。每张许可单正好结束一次。
        refused：Agent 改到了、但按许可单保持原样的对象。
        R23：这个 Agent 之前的预留用完了；这次因为别的 Agent 先改而被拒的，给它预留一段时间重试。"""
        if permit.state not in self.OPEN_STATES:
            raise ProtocolError(f"{permit.permit_id} 已经结束（{permit.state}），不能再关闭")
        self.in_doubt.pop(permit.permit_id, None)
        permit.state = state
        self.reservations = {u: v for u, v in self.reservations.items() if v[0] != permit.agent}
        out = []
        if self.reserve_seconds > 0:
            until = time.time() + self.reserve_seconds
            for oid in sorted(set(refused)):
                k = permit.keep.get(oid)
                if k is not None and k["reason"] == KEEP_OTHER_AGENT:
                    self.reservations[oid] = (permit.agent, until)
                    out.append(oid)
        self._emit("permit/closed", permitId=permit.permit_id, reserved=out, state=state)
        return out

    def settle(self, permit: Permit, before: dict[str, Any], raw: dict[str, Any], final: dict[str, Any],
               step_id: Optional[str] = None, produce_type: str = "blob",
               refused: Iterable[str] = (), refused_deletes: Iterable[str] = (),
               identity: Optional[dict] = None,
               rows: Optional[tuple[dict[str, dict], dict[str, dict], dict[str, dict]]] = None) -> Settlement:
        """一次执行结束：按应用交回的实际状态核对，再记账（spec/core.md 第 5 节；design_v0.5.md 2.3–2.5、第 6 节）。

        before / raw / final：执行前、执行后（恢复前）、最终的单元 {编号: 内容}，内容是带 "fp" 的 dict。
        - 要保持的单元（许可单里的；以及删掉了不许删的对象时，这个对象的全部单元，R6 按整个对象）：
          最终和执行前一样 → 保持（kept）；不一样 → 违规（breach），按实际状态记账并标明违规。
        - 其余单元：最终状态和执行前不一样的，一律按最终状态提交（applied / side_effects）；
          Agent 改了、但最终又被改回去的，记为 reverted。
        refused / refused_deletes：类型化命令执行前就按许可单拒掉的单元、拒掉删除的应用对象（Permit.screen）。
          算作 Agent 想改、按许可单保持，同样要核对场景里确实没变。
        identity：接入代码认回同一个对象的结果（{"pairs", "kinds", "fresh"}），由运行时按同一条规则重算核对。
          配上的旧编号在 raw / final 里已经是新对象的内容，所以照常按面核对：人改过的面要么还是人的，要么是违规。
          补回人删掉的对象（墓碑）被接入代码删掉了：记为保持（kept，"recreate"）；按规则是补回、最后却还在的
          （例如接入代码收尾出错，design_v0.5.md 14.8）：记为违规。
        rows：(执行前, 执行后, 最终) 全部应用对象的身份行 {对象编号: {"name", "type", "parent", "kind", "users"}}。
          给了 rows，before / raw / final 可以只含变了的单元（以及类型化命令执行前拒掉的、删除被拒的对象的单元）：
          三份里都一样的单元在下面的循环里什么也不做，哪些对象在、认回要的身份行从 rows 里取（design_v0.5.md 第 16 节）。
        账本只记实际看到的（P1）：结束后账本里每个单元都和 final 一样。"""
        if permit.state not in self.OPEN_STATES:
            raise ProtocolError(f"{permit.permit_id} 已经结束（{permit.state}），不能再结算")
        agent = permit.agent
        actor = self._actor(agent)
        st = Settlement(permit.permit_id, agent)
        refused, pre_deleted = set(refused), set(refused_deletes)
        self._on_settle_start(step_id)
        rows_b, rows_r, rows_f = rows if rows is not None else (_group_rows(before), _group_rows(raw), _group_rows(final))
        before_groups, raw_groups = set(rows_b), set(rows_r)
        tomb_hits: set[str] = set()
        if permit.reidentify is not None:
            expected = self._settle_identity(st, permit, rows_b, rows_r, identity, pre_deleted)
            # 按运行时自己的规则，补回了墓碑（人删掉、还没交还）的对象：接过旧编号的、或者还是新编号的（14.8）
            for old, (new, _level) in expected.items():
                if old in permit.no_recreate and old not in before_groups:
                    tomb_hits |= {old, new}
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
                if group_of(uid) in tomb_hits:     # 补回人删掉的对象，最后还在（例如接入代码收尾出错）：违规
                    st.breach[uid] = {"reason": KEEP_HUMAN, "recreate": True, "deleted": False}
                    self._settle_breach(op_id, agent, actor, uid, f, b, step_id)
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
                            self._keep_candidate(op_id, agent, uid, r, step_id)
                    continue
                st.breach[uid] = {**req, "deleted": f is None}
                self._settle_breach(op_id, agent, actor, uid, f, b, step_id)
                continue
            if _fp(f) == _fp(b):
                if attempted:
                    st.reverted.append(uid)
                continue
            why = self._settle_write(op_id, agent, uid, f, b, permit.base.get(uid, -1), step_id)
            if why is None:
                (st.applied if attempted and _fp(f) == _fp(r) else st.side_effects).append(uid)
            else:   # 执行期间规则状态变了（例如接入代码没核对、人的修改是事后才记的）：如实记为违规
                st.breach[uid] = {**why, "deleted": f is None, "late": True}
                self._settle_breach(op_id, agent, actor, uid, f, b, step_id)

        st.reserved = self.close_permit(permit, list(st.kept), state="settled")
        self._on_settle_end(step_id)
        st.holds = sorted({group_of(u) for u, k in st.kept.items() if k.get("recreate")})
        if permit.reidentify is not None:
            self._track_wanted_gone(permit, st, before, raw, rows_b, set(rows_r), set(rows_f), pre_deleted)
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
        """从账本算（loops.detect）：同一个单元在这个 Agent 最近 loop_window 次执行里被保持（它想改、没生效）了几次。
        到 loop_repeat 次记进 st.loops；新进入循环的单元提醒人一次。补回人删掉的对象不算（看到之后再建就是有意的）。"""
        agent = st.agent
        history = self.kept_history.setdefault(agent, [])
        alerted = self.looping.setdefault(agent, set())
        st.loops, new = LOOPS.detect(history, alerted, {u for u, k in st.kept.items() if not k.get("recreate")},
                                     self.loop_repeat, self.loop_window)
        st.loop_alert = new
        if new:
            self._emit("alert/human", kind="loop", agent=agent, permitId=st.permit_id, objects=new,
                       times={u: st.loops[u] for u in new}, window=self.loop_window)

    def _settle_identity(self, st: Settlement, permit: Permit, rows_b: dict[str, dict], rows_r: dict[str, dict],
                         identity: Optional[dict], pre_deleted: set[str]) -> dict:
        """核对接入代码认回的结果（design_v0.5.md 12.4）：只用执行前、执行后（恢复前）的记录，按许可单里的同一条规则
        自己重算一遍（rightofway/identity.py 的 verify）。一致的记进 st.reidentified 和认回记录（带置信等级）；
        不一致的照实记下、提醒人。账本仍按场景的实际状态记（P1）；这是接入代码的问题，不暂停 Agent。
        返回按规则应有的配对 {旧编号: [新编号, 等级]}：结算据此认出没被删掉的墓碑补回（14.8）。"""
        identity = identity or {}
        pairs = {o: list(v) for o, v in (identity.get("pairs") or {}).items()}
        kinds = identity.get("kinds") or {}
        wanted = set(permit.rebind) | set(pre_deleted)
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
            # 都是推断（按名字、类型、父对象），不是应用自己保持下来的身份：人可以撤销（unmatch，14.5）
            st.reidentified[old] = {"name": name, "level": level, "kind": kind, "inferred": True}
            self.identity_log.append({"permitId": permit.permit_id, "agent": permit.agent, "object": old,
                                      "name": name, "new": new, "level": level, "kind": kind, "inferred": True})
            self._emit("identity/matched", permitId=permit.permit_id, agent=permit.agent, objectId=old,
                       level=level, kind=kind)
        st.identity_errors = errors
        if errors:
            self._emit("alert/human", kind="identity", agent=permit.agent, permitId=permit.permit_id,
                       errors=list(errors))
        return expected

    def mark_unmatched(self, actor_id: str, permit_id: str, old: str, new: str) -> None:
        """人说"不是同一个"（design_v0.5.md 14.5）：接入层已经把对象拆开、记好账，这里在认回记录上注明撤销了。"""
        self._require_human(actor_id)
        for e in self.identity_log:
            if e["permitId"] == permit_id and e["object"] == old and not e.get("undone"):
                e["undone"], e["splitTo"] = True, new
        self.wanted_gone.get(next((e["agent"] for e in self.identity_log if e["permitId"] == permit_id), ""), {}).pop(old, None)
        self._emit("identity/unmatched", permitId=permit_id, objectId=old, new=new, by=actor_id)

    def _track_wanted_gone(self, permit: Permit, st: Settlement, before: dict[str, Any], raw: dict[str, Any],
                           rows_b: dict[str, dict], raw_groups: set[str], final_groups: set[str],
                           pre_deleted: set[str]) -> None:
        """记下 Agent 想删、因为人改过而保留下来的对象：之后它新建同名同类型的对象，就认成这一个（清空和重建分两次执行）。
        认上了、被删掉了、或者 Agent 又去改它（说明它知道这个对象还在）时，不再记。"""
        wanted = self.wanted_gone.setdefault(permit.agent, {})
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

    def _settle_commit(self, op_id: str, agent: str, obj: ObjectState, content: Any, step_id: Optional[str],
                       produced: bool = False) -> VersionRecord:
        """结算时按实际状态记一个单元（design_v0.5.md 14.1）。准入在开许可单时已经判断过，
        结算只记实际发生了什么，不会再"拒绝结算"（暂停、排队这些只管还没开始的执行）。"""
        actor = self._actor(agent)
        rec = self._commit_version(obj, content, actor, op_id, step_id)
        self._emit("op/result", opId=op_id, status="committed", newVersions={obj.object_id: rec.version})
        self._emit("object/changed", objectId=obj.object_id, version=rec.version, author=agent, opId=op_id)
        self._on_settled(op_id, agent, obj, rec, step_id, produced)
        return rec

    def _settle_write(self, op_id: str, agent: str, uid: str, f: Any, b: Any, base: int,
                      step_id: Optional[str]) -> Optional[dict]:
        """按最终状态提交一个单元。开许可单之后规则状态又变了（例如人的修改是事后才记的）：返回要保持的原因，
        由调用方记为违规；否则返回 None。"""
        obj = self.objects[uid]
        why = self.verdict(agent, uid, obj, _Pinned(uid, base))
        if why is not None:
            return why
        self._settle_commit(op_id, agent, obj, f if f is not None else _deleted_like(b), step_id)
        return None

    def _settle_create(self, op_id: str, agent: str, uid: str, f: Any, step_id: Optional[str],
                       produce_type: str) -> None:
        obj = self.objects.get(uid)
        if obj is not None and not obj.retracted:                         # 名义上新建、账本里已有：按修改记
            if _fp(obj.content) != _fp(f):
                self._settle_commit(op_id, agent, obj, f, step_id)
            return
        if obj is None:
            obj = ObjectState(uid, produce_type, provenance={"opId": op_id, "stepId": step_id})
            self.objects[uid] = obj
        self._settle_commit(op_id, agent, obj, f, step_id, produced=True)

    def _settle_breach(self, op_id: str, agent: str, actor: Actor, uid: str, f: Any, b: Any,
                       step_id: Optional[str]) -> None:
        """违规：规则要求保持，但应用里实际变了。账本照实记下（P1），标明违规；"人碰过"的标记保留。"""
        if uid not in self.objects:
            self.objects[uid] = ObjectState(uid, "blob")
        obj = self.objects[uid]
        prior = obj.human_touched
        self._on_breach(op_id, agent, uid, obj.version, step_id)
        rec = self._commit_version(obj, f if f is not None else _deleted_like(b), actor, op_id, step_id)
        rec.breach = True
        obj.human_touched = prior
        self._emit("op/breach", opId=op_id, agent=agent, objectId=uid, version=rec.version,
                   deleted=f is None)
        self._emit("object/changed", objectId=uid, version=rec.version, author=agent, opId=op_id, breach=True)

    def hand_back_group(self, actor_id: str, group: str, base_units: Iterable[str] = ()) -> list[str]:
        """人把整个应用对象交还给 AI（交还是人的决定）：它的每个单元结束占用、清掉"人碰过"。
        base_units：基线算人的时候，这个对象没建记录的基线单元（一起交还，建上记录）。返回交还了的单元。"""
        self._require_human(actor_id)
        uids = {u for u in self.objects if group_of(u) == group} | set(base_units)
        done = []
        for uid in sorted(uids):
            obj = self.objects.get(uid)
            if obj is None:
                continue
            if obj.occupancy is not None and obj.occupancy.holder == actor_id:
                self.release(actor_id, uid)
            if obj.human_touched is not None:
                self.hand_back(actor_id, uid)
                done.append(uid)
        return done

    @staticmethod
    def rebind_rows(permit: Permit, refused_deletes: Iterable[str], row_of: Callable[[str], Optional[dict]]) -> dict:
        """认回时可以重新绑定的旧对象：之前想删、因为人改过而保留下来的（permit.rebind），加上这一批执行前就拒掉了
        删除的对象（清空和重建在同一批里时，新建的同名对象就是它）。"""
        rebind = dict(permit.rebind)
        for g in sorted(refused_deletes):
            row = row_of(g)
            if row is not None:
                rebind.setdefault(g, {k: row.get(k) for k in ("name", "type", "parent")})
        return rebind

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
        if self.sparse and self.baseline_owner is not None:      # 基线算人的：没建记录的基线单元也是
            out |= {u for u in self._base_units() if not dict.__contains__(self.objects, u)}
        if for_agent is not None:
            self._expire_reservations()
            out |= {oid for oid, (h, _) in self.reservations.items() if h != for_agent}
        return out

    def human_edit(self, actor_id: str, object_id: str, content: Any, occupy: bool = True) -> VersionRecord:
        """occupy=False：修改是事后（例如存盘时）才发现的，无法知道人是否还在改，
        所以只记"人碰过"，不产生占用（R8，和外部修改同样的理由）。"""
        actor = self._require_human(actor_id)
        obj = self.objects[object_id]
        rec = self._commit_version(obj, content, actor)               # R5：总能提交
        self._emit("object/changed", objectId=object_id, version=rec.version, author=actor_id)
        self._on_changed(object_id)                                   # R12
        if occupy and obj.occupancy is None:                          # R8：第一次修改即占用
            obj.occupancy = Occupancy(holder=actor_id, since_seq=self._seq)
            self._touch(object_id)
            self._emit("occupancy/acquired", objectId=object_id, holder=actor_id)
            self._on_occupied(object_id)                              # R9
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
        self._touch(object_id)
        self._emit("occupancy/released", objectId=object_id, holder=actor_id)
        self._on_released(object_id)

    def hand_back(self, actor_id: str, object_id: str) -> None:
        """R7：人明确允许 AI 再修改这个对象。"""
        self._require_human(actor_id)
        obj = self.objects[object_id]
        obj.human_touched = None
        self._touch(object_id)
        self._emit("object/handback", objectId=object_id, actor=actor_id)

    def record_external(self, object_id: str, content: Any, source: str, type_: str = "blob") -> VersionRecord:
        """来源说不准的变化（design_v0.5.md 14.2、14.3）：结果没收到的那次执行前后、代理没运行期间、应用重新打开过。
        记在 external:<source> 名下，和人的修改一样保护（人碰过），不产生占用；人交还之后 Agent 才能改。
        和 external_change 不同，对象可以是新出现的。"""
        ext_id = f"external:{source}"
        if ext_id not in self.actors:
            self.register_actor(ext_id, ActorKind.EXTERNAL)
        obj = self.objects.get(object_id)
        if obj is None:
            obj = self.objects[object_id] = ObjectState(object_id, type_)
        rec = self._commit_version(obj, content, self.actors[ext_id])
        self._emit("object/changed", objectId=object_id, version=rec.version, author=ext_id)
        self._on_changed(object_id)
        return rec

    def to_state(self) -> dict:
        """运行时的全部记录，只含基本类型（可以存成 JSON）。重启后用 load_state 接着用。
        事件日志不存（它只是记录，序号接着往下数）；规则选项不存（由创建运行时的人决定）；还开着的许可单要存。
        稀疏账本里只有建了记录的单元；基线由接入层自己存（design_v0.5.md 第 16 节）。"""
        objects = {oid: _obj_json(o) for oid, o in self.objects.items()}
        return {"format": 2, **self._small_state(), "objects": objects, **self._state_extra(),
                "identity_log": list(self.identity_log)}

    def snapshot(self) -> dict:
        """整份记录（to_state），并把它当作盘上的样子：之后 take_delta 只交出从这里开始的变化。"""
        out = self.to_state()
        self._mark_saved()
        return out

    def _small_state(self) -> dict:
        return {"seq": self._seq, "n_permit": self._n_permit, "nonce": self.nonce, "paused": self.paused,
                "actors": {a: x.kind.value for a, x in self.actors.items()}, **self._small_extra(),
                "seen": {a: sm.to_json() for a, sm in self.seen.items()},
                "selection": {h: sorted(g) for h, g in self.selection.items()},
                "suspended": self.suspended, "app": self.app, "wanted_gone": self.wanted_gone,
                "kept_history": {a: [sorted(x) for x in h] for a, h in self.kept_history.items()},
                "looping": {a: sorted(v) for a, v in self.looping.items()},
                "permits": {pid: _permit_json(pm) for pid, pm in self.permits.items() if pm.state in self.OPEN_STATES},
                "in_doubt": self.in_doubt}

    def _mark_saved(self) -> None:
        """现在的样子就是盘上的样子：之后 take_delta 只交出从这里开始的变化。"""
        self._dirty_objects.clear()
        self._mark_saved_extra()
        self._saved_small = {k: json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
                             for k, v in self._small_state().items()}
        self._saved_log = len(self.identity_log)

    def take_delta(self) -> dict:
        """上次落盘（to_state / take_delta / load_state）之后的变化，追加进账本用（design_v0.5.md 第 16 节）：
        改过的单元、步骤整条重写；小字段变了才写；认回记录只追加新的。和场景大小无关，只和这一步的改动有关。"""
        d: dict = {}
        for k, v in self._small_state().items():
            js = json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
            if self._saved_small.get(k) != js:
                d[k] = v
                self._saved_small[k] = js
        objs = {}
        for oid in sorted(self._dirty_objects):
            o = self.objects.peek(oid)
            if o is not None:
                objs[oid] = _obj_json(o)
        self._dirty_objects.clear()
        if objs:
            d["objects"] = objs
        d.update(self._delta_extra())
        if len(self.identity_log) > self._saved_log:
            d["identity_log_add"] = self.identity_log[self._saved_log:]
            self._saved_log = len(self.identity_log)
        return d

    def apply_delta(self, d: dict) -> None:
        """把 take_delta 交出的一段变化接到 load_state 读进来的记录上（重启时按顺序重放账本）。"""
        if "seq" in d:
            self._seq = d["seq"]
        if "n_permit" in d:
            self._n_permit = d["n_permit"]
        if d.get("nonce"):
            self.nonce = d["nonce"]
        if "paused" in d:
            self.paused = bool(d["paused"])
        if "actors" in d:
            self.actors = {a: Actor(a, ActorKind(k)) for a, k in d["actors"].items()}
        if "seen" in d:
            self.seen = {a: SeenMap.from_json(self, v) for a, v in d["seen"].items()}
        if "selection" in d:
            self.selection = {h: set(g) for h, g in d["selection"].items()}
        if "suspended" in d:
            self.suspended = dict(d["suspended"])
        if "app" in d:
            self.app = dict(d["app"])
        if "wanted_gone" in d:
            self.wanted_gone = {a: dict(v) for a, v in d["wanted_gone"].items()}
        if "kept_history" in d:
            self.kept_history = {a: [set(x) for x in h] for a, h in d["kept_history"].items()}
        if "looping" in d:
            self.looping = {a: set(v) for a, v in d["looping"].items()}
        if "permits" in d:
            self.permits = {pid: _permit_from(self, x) for pid, x in d["permits"].items()}
        if "in_doubt" in d:
            self.in_doubt = dict(d["in_doubt"])
        for oid, x in (d.get("objects") or {}).items():
            dict.__setitem__(self.objects, oid, _obj_from(oid, x))
        self._apply_extra(d)
        self.identity_log.extend(d.get("identity_log_add") or ())

    def load_state(self, state: dict) -> None:
        """接着 to_state 存下的记录用。预留（R23）是短时间的，不恢复。format 1（v0.5 第 14 节，整份重写的账本）也认。"""
        if state.get("format") not in (1, 2):
            raise ProtocolError(f"不认识的账本格式：{state.get('format')}")
        self._seq = state["seq"]
        self._n_permit = state.get("n_permit", 0)
        self.nonce = state.get("nonce") or self.nonce
        self.actors = {a: Actor(a, ActorKind(k)) for a, k in state["actors"].items()}
        self.objects = _Ledger(self)
        for oid, d in state["objects"].items():
            dict.__setitem__(self.objects, oid, _obj_from(oid, d))
        self._load_extra(state)
        self.seen = {a: SeenMap.from_json(self, v) for a, v in state["seen"].items()}
        self.selection = {h: set(g) for h, g in state["selection"].items()}
        self.suspended = dict(state["suspended"])
        self.paused = bool(state["paused"])
        self.app = dict(state["app"])
        self.wanted_gone = {a: dict(v) for a, v in state["wanted_gone"].items()}
        self.identity_log = list(state["identity_log"])
        self.kept_history = {a: [set(x) for x in h] for a, h in state["kept_history"].items()}
        self.looping = {a: set(v) for a, v in state["looping"].items()}
        self.permits = {pid: _permit_from(self, d) for pid, d in state["permits"].items()}
        self.in_doubt = dict(state["in_doubt"])
        self.reservations = {}
        self.ops = {}
        self._mark_saved()
        self._emit("ledger/loaded", objects=len(self.objects), openPermits=sorted(self.permits))

    def log_since(self, seq: int) -> list[Event]:
        return [e for e in self.events if e.seq > seq]

    def events_of(self, type_: str) -> list[Event]:
        return [e for e in self.events if e.type == type_]



class _Pinned:
    """结算时核对用的"看到的版本"：许可单开出时这个 Agent 看到的那一版（permit.base）。"""
    __slots__ = ("uid", "base")

    def __init__(self, uid: str, base: int) -> None:
        self.uid, self.base = uid, base

    def get(self, uid: str, default: Any = None) -> Any:
        return self.base if uid == self.uid else default


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


def _ver_json(v: VersionRecord) -> dict:
    d = asdict(v)
    d["author_kind"] = v.author_kind.value
    return d


def _ver_from(d: dict) -> VersionRecord:
    return VersionRecord(**{**d, "author_kind": ActorKind(d["author_kind"])})


def _obj_json(o: ObjectState) -> dict:
    return {"type": o.type, "versions": [_ver_json(v) for v in o.versions],
            "authority": {"kind": o.authority.kind.value, "adapter_id": o.authority.adapter_id},
            "provenance": o.provenance, "mergeable": o.mergeable, "retracted": o.retracted,
            "human_touched": asdict(o.human_touched) if o.human_touched else None,
            "occupancy": asdict(o.occupancy) if o.occupancy else None,
            "candidates": [_ver_json(v) for v in o.candidates],
            **({"baseline": o.baseline, "baseline_author": o.baseline_author,
                "baseline_kind": o.baseline_kind.value if o.baseline_kind else None}
               if o.baseline is not None else {})}


def _obj_from(oid: str, d: dict) -> ObjectState:
    return ObjectState(
        oid, d["type"], versions=[_ver_from(v) for v in d["versions"]],
        authority=Authority(AuthorityKind(d["authority"]["kind"]), d["authority"]["adapter_id"]),
        provenance=d["provenance"], mergeable=d["mergeable"], retracted=d["retracted"],
        human_touched=HumanTouched(**d["human_touched"]) if d["human_touched"] else None,
        occupancy=Occupancy(**d["occupancy"]) if d["occupancy"] else None,
        candidates=[_ver_from(v) for v in d["candidates"]],
        baseline=d.get("baseline"), baseline_author=d.get("baseline_author"),
        baseline_kind=ActorKind(d["baseline_kind"]) if d.get("baseline_kind") else None)


def _permit_json(pm: Permit) -> dict:
    base = pm.base
    out = {f: getattr(pm, f) for f in pm.__dataclass_fields__ if f != "base"}
    out["base"] = base.to_json() if isinstance(base, SeenMap) else dict(base or {})
    return json.loads(json.dumps(out, ensure_ascii=False, default=str))


def _permit_from(rt: KeyedLedger, d: dict) -> Permit:
    pm = Permit(**{**d, "base": {}})
    pm.base = SeenMap.from_json(rt, d.get("base"))
    return pm

