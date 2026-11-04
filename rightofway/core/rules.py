"""判断规则（spec/core.md 第 3、4 节）。核心里所有"能不能改"都在这里算，别处只调用（P9）。

核心只认识单元：有编号、有指纹、值能写成文字。一个单元是属性、行还是字符，由应用侧决定；这里不知道。

判断表（单元和墓碑都用它；墓碑看的是"删掉它的是谁"）：

  这个单元最后是谁改的                 这个 AI 能不能改
  没人改过（基线），或者已经交还       能
  它自己                               能
  人，或者来源不明                     不能，直到人交还（人的提交算交还：适配侧观察到提交时调用交还）
  别的 AI，或者 git                    先看过这一版，才能改

"人或来源不明改的、还没交还"是一种状态（hold），和"最后是谁改的"分开记：AI 违规改了人改过的东西，账本照实记这个 AI
改的，但这个单元仍然是人的（第 6 节）。所以先看 hold，再看最后是谁改的。

暂时不能改的情况（temporary）：人正在改（占用；可选"选中就算在改"），两个 AI 冲突后预留给其中一个（默认 30 秒）。
总闸（gate）：人暂停了全部 AI、有一次执行的结果还没查清、这个 AI 违规后被暂停——什么都不放行。

意图层的出口（tighten）：许可单可以被一个可选的层再收紧（多保持一些单元），任何东西都不能放松它。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

# ---------------------------------------------------------------------------
# 统一术语：谁
# ---------------------------------------------------------------------------
HUMAN, AGENT, EXTERNAL = "human", "agent", "external"      # 操作者的种类
BASELINE = "baseline"                 # 基线：没人改过（开始协作前就在的样子）
GIT = "git"                           # 人做的 git 操作带来的变化：和别的 AI 一样，只防按旧印象改
EXTERNAL_PREFIX = "external:"         # 来源不明：external:<来源>，和人的修改一样保护
UNATTRIBUTED = "external:unattributed"
SOURCES = {                           # 来源不明的几种来源（给人看的说法由各媒介自己写）
    "external:unattributed": "结果没收到的那次执行前后",
    "external:offline": "RightOfWay 没运行期间",
    "external:reopened": "应用重新打开时",
    "external:missed": "增量观察漏掉、全量核对时才发现的",
}

# ---------------------------------------------------------------------------
# 统一术语：为什么要保持（许可单里每个要保持的单元带一个原因）
# ---------------------------------------------------------------------------
KEEP_HUMAN = "human"                  # 人改过（或来源不明），或者人正在改（占用）
KEEP_SELECTED = "selected"            # 人正选中着（"选中就算在改"选项）
KEEP_OTHER_AGENT = "other_agent"      # 别的 AI（或 git）在这个 AI 上次看之后改过（后到的让先到的）
KEEP_RESERVED = "reserved"            # 预留给了另一个 AI
KEEP_LOCKED = "locked"                # 只有人能改的单元（例如 RightOfWay 自己的配置）
KEEP_OTHER = "other"


def is_external(author: Optional[str]) -> bool:
    return bool(author) and author.startswith(EXTERNAL_PREFIX)


def category(author: Optional[str], kind: Optional[str] = None) -> str:
    """作者的类别：baseline | human | external | git | agent。kind 是登记的操作者种类（有的话以它为准）。"""
    if author is None or author == BASELINE:
        return BASELINE
    if author == GIT:
        return GIT
    if kind == EXTERNAL or is_external(author):
        return EXTERNAL
    if kind == HUMAN:
        return HUMAN
    if kind is None and author == HUMAN:
        return HUMAN
    return AGENT


@dataclass(frozen=True)
class Last:
    """判断一个单元（或一个墓碑）要知道的：最后是谁改的（删的）、那是第几版、现在是不是还归人（hold）。

    author  最后改它的操作者；None 或 BASELINE 表示没人改过（基线）。
    kind    登记的操作者种类（human / agent / external）；不知道时按 author 推断。
    stamp   这一版的版本号，和 AI 看过的版本比（同一个单元、同一个容器里单调增加）。
    held    人或来源不明改过、还没交还时，是谁（人的编号或 external:<来源>）；None 表示不归人。
    """
    author: Optional[str] = None
    kind: Optional[str] = None
    stamp: int = 0
    held: Optional[str] = None


def judge(last: Last, agent: str, seen: Optional[int]) -> Optional[dict]:
    """判断表：这个 AI 现在能不能改这个单元。返回 None 表示能；否则是要保持的原因
    （{"reason": ..., "by"?: 先改的 AI, "source"?: 来源不明的来源}）。
    seen：这个 AI 看过的这个单元（或容器）的版本；None 表示从没看过。"""
    if last.held:
        return {"reason": KEEP_HUMAN, **({"source": last.held} if is_external(last.held) else {})}
    cat = category(last.author, last.kind)
    if cat == BASELINE or last.author == agent:
        return None
    if cat in (HUMAN, EXTERNAL):
        return None                                   # 人改的、已经交还
    if seen is not None and seen >= last.stamp:
        return None                                   # 别的 AI、git：这一版它看过了
    return {"reason": KEEP_OTHER_AGENT, "by": last.author}


def judge_tombstone(last: Last, agent: str, seen: Optional[int]) -> Optional[dict]:
    """墓碑（删掉的单元、对象、文件）：AI 能不能把它补回来。和单元用同一张表，看的是删掉它的是谁。"""
    return judge(last, agent, seen)


# ---------------------------------------------------------------------------
# 暂时不能改的情况
# ---------------------------------------------------------------------------
def temporary(agent: str, *, selected: bool = False, reserved: Optional[tuple[str, float]] = None,
              occupied: Optional[str] = None, now: Optional[float] = None) -> Optional[dict]:
    """人选中着（选项打开时才传 selected=True）、预留给别的 AI（没到期）、人正在改（占用）。顺序就是报告原因的顺序。"""
    if selected:
        return {"reason": KEEP_SELECTED}
    if reserved is not None and reserved[0] != agent and reserved[1] > (time.time() if now is None else now):
        return {"reason": KEEP_RESERVED, "by": reserved[0]}
    if occupied:
        return {"reason": KEEP_HUMAN}
    return None


def locked() -> dict:
    """只有人能改的单元（人永远不交还的那种，例如 RightOfWay 自己的配置）。"""
    return {"reason": KEEP_LOCKED}


def gate(agent: str, *, paused: bool = False, in_doubt: Optional[dict] = None,
         suspended: Optional[dict] = None) -> Optional[dict]:
    """总闸：这个 AI 现在能不能开始一次新的执行。返回不许的原因，None 表示可以。
      paused     人暂停了全部 AI；
      in_doubt   有一次执行发出去了、结果还没查清：查清之前谁的差异都说不准，所以谁都不开始新的执行；
      suspended  这个 AI 上次违规（或可能违规），被暂停了，等人处理。"""
    if paused:
        return {"reason": "paused"}
    if in_doubt:
        pid = sorted(in_doubt)[0]
        return {"reason": "in_doubt", "permitId": pid, "agent": in_doubt[pid]["agent"]}
    if suspended and agent in suspended:
        return {"reason": "suspended", **suspended[agent]}
    return None


# ---------------------------------------------------------------------------
# 意图层的出口：只能收紧
# ---------------------------------------------------------------------------
Tightener = Callable[[str, dict], dict]


def tighten(agent: str, keep: dict, layer: Optional[Tightener]) -> dict:
    """可选的一层（以后的意图层）看过许可单之后，可以多要求保持一些单元，不能少：它交回的结果里，
    原来要保持的都还在、原因不变；新加的才用它给的原因。没有这一层（None）时原样返回，保护照样成立。"""
    if layer is None:
        return keep
    extra = layer(agent, dict(keep)) or {}
    out = dict(keep)
    for uid, why in extra.items():
        out.setdefault(uid, dict(why))
    return out


# ---------------------------------------------------------------------------
# 谁做的：观察到的变化记在谁名下
# ---------------------------------------------------------------------------
def observed_author(human: str, source: Optional[str] = None, git: bool = False) -> str:
    """AI 的执行之外看到的变化：都是人的——除了两种。说不准是谁的（结果没查清的那次执行前后、应用重开过、RightOfWay
    没运行、增量观察漏掉……）记为来源不明 external:<来源>，和人的修改一样保护；人的版本控制操作带来的（变成了 git 里的样子）
    记为 git，和别的 AI 一样只防按旧印象改。"""
    if source:
        return source if source.startswith(EXTERNAL_PREFIX) else EXTERNAL_PREFIX + source
    if git:
        return GIT
    return human


# ---------------------------------------------------------------------------
# 结算：不能原子执行、事后才知道改了什么的执行（例如一条命令）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WindowDecision:
    author: str                  # "agent"：算这个 AI 的；"unattributed"：说不准是谁的
    restore: bool = False        # 放回执行前的样子（整个容器），AI 的版本另存一份
    backup_before: bool = False  # 不放回，但把执行前的样子另存一份（可能是人刚打的字），提醒人


def settle_window(*, hits: bool, quick: bool, from_reference: bool, deleted: bool, lost: bool,
                  human_only: bool = False) -> WindowDecision:
    """一次不能原子执行的执行（执行期间人也可能在改）结束后，一个容器里的变化算谁的、要不要放回。
    hits：这个变化碰到了这个 AI 不能改的单元；quick：执行很快就结束了（人插进来的可能很小）；
    from_reference：变化之后的样子正好是一个已知的参照（例如 git 的 HEAD 或暂存区：多半是命令做的）；
    deleted：整个容器被删掉了；lost：没等到结束通知（结果没收到），变化一律说不准是谁的；
    human_only：只有人能改的容器，碰到了就放回，不管多久。"""
    if human_only and hits:
        return WindowDecision("agent", restore=True)
    if not lost and (quick or from_reference or (deleted and hits)):
        return WindowDecision("agent", restore=hits)
    return WindowDecision("unattributed", backup_before=hits)


@dataclass(frozen=True)
class EditDecision:
    status: str                  # unchanged | applied | applied_extra | restore
    note: str = ""


def settle_predicted(*, actual: Optional[str], known: Optional[str], predicted: Optional[str],
                     hits_if_unpredicted: bool) -> EditDecision:
    """一次事先能算出结果的执行（例如按字符串替换的编辑）结束后，按实际状态结算。参数都是指纹（None：不存在）。
      和账本里一样            → 没有变化；
      和事先算的一样          → 都是这个 AI 的；
      不一样（执行期间别的也在改，或者工具实际做的和算的不一样）→ 算出来的那部分是 AI 的，多出来的说不准是谁的；
      事先没能算出结果        → 按实际差异核对：碰到了不能动的单元就放回（AI 的版本另存一份），否则都是 AI 的。"""
    if actual == known:
        return EditDecision("unchanged")
    if predicted is not None and actual == predicted:
        return EditDecision("applied")
    if predicted is not None:
        return EditDecision("applied_extra")
    if hits_if_unpredicted:
        return EditDecision("restore")
    return EditDecision("applied")


def refuse_whole(hits: dict, separable: bool) -> bool:
    """执行前就知道会改哪些单元时（事先算出了结果、类型化的命令）：碰到了不能改的单元怎么办。
    separable=False（一次编辑里的几行互相关联，例如改名）：整次拒绝，一个单元都不改；
    separable=True（每个单元各自独立，例如一条命令设的几个属性）：去掉不能改的，其余照常。"""
    return bool(hits) and not separable


def opaque_blocked(kept: dict, *, around: bool, restore_deleted: bool, destructive: bool,
                   no_delete: bool, keep_alive: Optional[dict] = None) -> Optional[dict]:
    """一次不知道会改哪些单元的工具调用，执行前按对象预检查（design_v0.5.md 12.8 第 1 步）：
    整个对象都要保持的，不执行；应用侧事后恢复不了的（不能包在快照和恢复之间，或者恢复不了删除而这个调用可能删除），不执行。
    kept：这个对象要保持的面 {面或 "*": 原因}。返回不执行的原因，None 表示可以执行。"""
    if "*" in kept:
        return kept["*"]
    if kept and not around:
        return next(iter(kept.values()))
    if destructive and no_delete and not restore_deleted:
        return next(iter(kept.values()), None) or (keep_alive or None)
    return None


# ---------------------------------------------------------------------------
# 结果没收到：查清的办法（design_v0.5.md 14.2、14.8）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LostDecision:
    action: str                  # settle | finish | not_executed | unattributed | wait
    executed: bool = False       # unattributed 时：确知执行了（开始了、没交回结果）
    why: str = ""


def resolve_lost(*, journal: int, status: Optional[str], same_boot: bool, seen: bool, has_token: bool,
                 app_restarted: bool) -> LostDecision:
    """一次发出去、结果没收到的执行，按应用侧记的日志查清。只有两种情况能说"没执行"：应用记了 not_executed；
    或者应用执行前就记"开始了"（journal ≥ 2），而这次启动以来根本没收到过这张许可单。开始了、没交回结果的，是
    "执行了，结果说不清"，不是"没执行"。查不回来的差异说不准是谁的，按人的修改保护。"""
    if not journal:
        return LostDecision("unattributed", why="接入代码不记执行结果")
    if status == "done":
        return LostDecision("settle")
    if status == "begun" and has_token and same_boot:
        return LostDecision("finish")
    never_arrived = status is None and same_boot and not seen and journal >= 2
    if status == "not_executed" or never_arrived:
        return LostDecision("not_executed")
    if status in ("started", "failed", "begun") or (same_boot and seen):
        why = {"started": "执行开始了，但没有交回结果", "failed": "执行了，但收尾出错，结果交不回来",
               "begun": "工具调用开始了，收尾的状态没了"}.get(status, "收到过这张许可单，结果已经不在日志里")
        return LostDecision("unattributed", executed=True, why=why)
    why = ("应用重新打开过，记的结果没了" if app_restarted
           else "接入代码执行前不记\"开始了\"，查不到结果不能说明没执行" if status is None else "执行状态说不清")
    return LostDecision("unattributed", why=why)


def resolve_failed_finish(status: Optional[str]) -> LostDecision:
    """补做工具调用的收尾时出错了：看应用侧的日志。收尾在应用里出错（记了 failed）→ 执行了，结果说不清；
    记了 done → 照常结算；还是 begun → 请求没到应用（传输的问题），继续等。"""
    if status == "failed":
        return LostDecision("unattributed", executed=True, why="工具调用之后的收尾出错，结果交不回来")
    if status == "done":
        return LostDecision("settle")
    return LostDecision("wait")


def suspects(plan: dict[str, list[str]], keep_alive: Iterable[str], before: dict, after: dict) -> list[str]:
    """确知执行了、结果却说不清时：许可单里要保持的（以及不许删的祖先），现在和发出前不一样的对象。
    可能是 AI 改了、没恢复成：和违规一样提醒人、暂停这个 AI，人看过再让它继续（14.8）。
    before / after：{对象编号: {"fp", "aspects": {面: 指纹}}}。"""
    out = set()
    for g, faces in (plan or {}).items():
        b, a = before.get(g), after.get(g)
        if b is None:
            continue
        if a is None:
            out.add(g)
        elif "*" in faces:
            if a.get("fp") != b.get("fp"):
                out.add(g)
        elif any((a.get("aspects") or {}).get(f) != (b.get("aspects") or {}).get(f) for f in faces):
            out.add(g)
    out |= {g for g in keep_alive or () if g in before and g not in after}
    return sorted(out)
