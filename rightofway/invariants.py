"""不变量检查（design_v0.5.md 第 3 节）：场景、账本、说明三者是否一致。

每次操作之后都可以检查。三条：

I1 账本 = 场景：运行时账本里每个单元（对象或对象的面）的指纹，和应用里实际的一致；
   账本记为已删除的，场景里没有；场景里有的，账本里也有。
I2 保持的就是保持的：许可要求保持的面（v0.4 里是应用侧算出的保护集），执行后要么没变，
   要么被记成违规（v0.4 还没有"违规"这个结局，所以任何改变都算违反）。
I3 说的都是真的：告诉 Agent "已生效"的单元，场景里就是 Agent 改成的样子；
   告诉它"没有生效、保留了原来的"单元，场景里和账本里都还是原来的样子。
   v0.4 的说明文字由报告里的 committed / skipped 生成，这里检查的就是这两项。

这个模块只读，不改任何东西。纯 Python，不依赖 Blender 或 Unity。
"""
from __future__ import annotations

from typing import Any, Optional

from .blender.records import OBJECT, Records, describe_unit, display, split_unit, units
from .core import invariants as CI
from .core.invariants import I1, I2, I3, RULES, Violation, dedupe as _dedupe

__all__ = ["I1", "I2", "I3", "RULES", "Violation", "check_ledger", "check_run", "check_session", "objects",
           "summarize"]
from .runtime import Runtime


def _where(uid: str, name: str, labels: Optional[dict]) -> str:
    return describe_unit(uid, name, labels)


# ---------------------------------------------------------------------------
# I1
# ---------------------------------------------------------------------------
def check_ledger(rt: Runtime, scene: Records, granularity: str, labels: Optional[dict] = None) -> list[Violation]:
    """账本里每个单元的最新版本，和场景里实际的单元比较（core.invariants.check_ledger）。
    稀疏账本（design_v0.5.md 第 16 节）：没建记录的单元按基线（上次整份落盘时的样子）比，一样要和场景一致，
    所以接入层看到了变化、却没记进运行时，照样查得出来（不放宽）。"""
    su = units(scene, granularity)

    def ledger():
        tracked = set()
        for uid, obj in sorted(rt.objects.items()):
            if obj.retracted or (not obj.versions and obj.baseline is None):
                continue
            tracked.add(uid)
            yield uid, obj.content, (obj.versions[-1].author_id if obj.versions else f"基线（{obj.baseline_author}）")
        if rt.sparse:
            for uid in rt._base_units():
                if uid in tracked or dict.__contains__(rt.objects, uid):
                    continue
                led = rt._baseline_content(uid)
                if led is not None:
                    tracked.add(uid)
                    yield uid, led, "基线"

    def where(uid: str, content: Any) -> str:
        return _where(uid, (display(content) if isinstance(content, dict) else None) or display(su.get(uid), uid), labels)
    return CI.check_ledger(ledger(), su, where)


# ---------------------------------------------------------------------------
# I2、I3：针对一次 Agent 执行
# ---------------------------------------------------------------------------
def _plan_units(plan: dict[str, list[str]], before: Records, granularity: str) -> list[str]:
    """许可要求保持的单元。"*" 表示整个对象。"""
    out = []
    for cid, faces in sorted(plan.items()):
        rec = before.get(cid)
        if rec is None:
            continue
        if granularity == OBJECT:
            out.append(cid)
        elif "*" in faces:
            out += [f"{cid}#{a}" for a in sorted(rec["aspects"])]
        else:
            out += [f"{cid}#{a}" for a in sorted(faces) if a in rec["aspects"]]
    return out


def check_run(rt: Runtime, report: Any, granularity: str, scene: Optional[Records] = None,
              labels: Optional[dict] = None, ledger: bool = True) -> list[Violation]:
    """检查一次 Agent 执行。report 是 SharedSession.run_agent 返回的 AgentReport，
    需要带 before / after_raw / after / plan。scene 默认用 report.after（执行结束那一刻应用交回的实际状态）。
    ledger=False 时不查 I1（例如还要接着处理别的事、之后统一查）。"""
    if getattr(report, "status", "ok") != "ok":
        return []
    final = report.after if scene is None else scene
    before, raw = report.before, report.after_raw
    ub, ur, uf = units(before, granularity), units(raw, granularity), units(final, granularity)
    breach = set(getattr(report, "breach", []) or [])
    out: list[Violation] = []

    def name_of(uid: str) -> str:
        cid = split_unit(uid)[0]
        return display(before.get(cid) or raw.get(cid) or final.get(cid), cid)

    def where(uid: str) -> str:
        return _where(uid, name_of(uid), labels)

    # I2：许可要求保持的单元；I3：说"已生效"的、说"没有生效、保留了原来的"（core.invariants）
    if getattr(report, "protected", True):
        out += CI.check_kept(_plan_units(report.plan or {}, before, granularity), ub, uf, breach, where)
    out += CI.check_said(report.committed, [(x["id"], x.get("reason")) for x in report.skipped], ub, ur, uf, breach,
                         where, rt.content_of)                    # 账本里的内容：不建记录（基线单元按基线）

    if ledger:
        out += check_ledger(rt, final, granularity, labels)
    return _dedupe(out)


def check_session(session: Any, report: Any = None, poll: bool = True) -> list[Violation]:
    """方便函数。poll=True 时先让会话把人的修改记进账本，再拿账本和会话看到的场景比（I1）；
    给了 report 时再查这次执行（I2、I3）。"""
    out: list[Violation] = []
    if report is not None:
        out += check_run(session.rt, report, session.granularity, labels=session.labels, ledger=False)
    if poll:
        session.poll()
    out += check_ledger(session.rt, session.view, session.granularity, session.labels)
    return _dedupe(out)


def objects(vs: list[Violation]) -> set[str]:
    """违反涉及的应用里的对象（不管是哪个面）。"""
    return {split_unit(v.unit)[0] for v in vs}


def summarize(vs: list[Violation], limit: int = 8) -> str:
    if not vs:
        return "不变量：全部成立"
    parts = []
    for r in (I1, I2, I3):
        rv = [v for v in vs if v.rule == r]
        if rv:
            parts.append(f"{r} {len(rv)} 处（{len(objects(rv))} 个对象）")
    head = "不变量被违反：" + "，".join(parts)
    return head + "\n" + "\n".join("  " + str(v) for v in vs[:limit]) + ("\n  ……" if len(vs) > limit else "")
