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

from dataclasses import dataclass
from typing import Any, Optional

from .blender.records import OBJECT, Records, describe_unit, display, is_deleted, split_unit, units
from .runtime import Runtime

I1, I2, I3 = "I1", "I2", "I3"
RULES = {
    I1: "账本和场景不一致",
    I2: "要保持的部分被改了，却没有记为违规",
    I3: "告诉 Agent 的和实际不一致",
}


@dataclass(frozen=True)
class Violation:
    rule: str            # I1 | I2 | I3
    unit: str            # 单元编号：对象编号，或"对象编号#面"
    where: str           # 给人看的位置，例如"Leaf_3 的Location"
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule} {RULES[self.rule]}] {self.where}：{self.detail}"


def _fp(u: Optional[dict]) -> Optional[str]:
    return None if u is None else u.get("fp")


def _where(uid: str, name: str, labels: Optional[dict]) -> str:
    return describe_unit(uid, name, labels)


# ---------------------------------------------------------------------------
# I1
# ---------------------------------------------------------------------------
def check_ledger(rt: Runtime, scene: Records, granularity: str, labels: Optional[dict] = None) -> list[Violation]:
    """账本里每个单元的最新版本，和场景里实际的单元比较。"""
    out: list[Violation] = []
    su = units(scene, granularity)
    tracked = set()
    for uid, obj in sorted(rt.objects.items()):
        if obj.retracted or not obj.versions:
            continue
        tracked.add(uid)
        led = obj.content
        got = su.get(uid)
        name = (display(led) if isinstance(led, dict) else None) or display(got, uid)
        where = _where(uid, name, labels)
        if is_deleted(led):
            if got is not None:
                out.append(Violation(I1, uid, where, "账本记为已删除，场景里还在"))
        elif got is None:
            out.append(Violation(I1, uid, where, f"账本里还在（最后由 {obj.versions[-1].author_id} 写入），场景里没有"))
        elif _fp(got) != _fp(led):
            out.append(Violation(I1, uid, where, f"内容不一致（账本最后由 {obj.versions[-1].author_id} 写入）"))
    for uid, u in sorted(su.items()):
        if uid not in tracked:
            out.append(Violation(I1, uid, _where(uid, display(u, uid), labels), "场景里有，账本里没有"))
    return out


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

    # I2：许可要求保持的单元
    if getattr(report, "protected", True):
        for uid in _plan_units(report.plan or {}, before, granularity):
            if uid in breach:
                continue
            if _fp(uf.get(uid)) != _fp(ub.get(uid)):
                what = "被删掉了，没有恢复" if uid not in uf else "被改了，没有恢复"
                out.append(Violation(I2, uid, where(uid), what))

    # I3：说"已生效"的
    for uid in report.committed:
        want, got = ur.get(uid), uf.get(uid)
        if want is None and got is not None:
            out.append(Violation(I3, uid, where(uid), "说 Agent 的删除已生效，但场景里还在"))
        elif want is not None and _fp(got) != _fp(want):
            out.append(Violation(I3, uid, where(uid), "说 Agent 的修改已生效，但场景里不是它改成的样子"
                                 + ("（场景里没有）" if got is None else "")))
    # I3：说"没有生效、保留了原来的"
    for s in report.skipped:
        uid = s["id"]
        was, got = ub.get(uid), uf.get(uid)
        if uid in breach:
            continue
        if _fp(got) != _fp(was):
            out.append(Violation(I3, uid, where(uid), f"说没有生效、保留了原来的（{s.get('reason')}），但场景里"
                                 + ("没有了" if got is None else "已经变了")))
        obj = rt.objects.get(uid)
        if obj is not None and not is_deleted(obj.content) and _fp(obj.content) != _fp(was):
            out.append(Violation(I3, uid, where(uid), "说保留了原来的，但账本里不是原来的"))

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


def _dedupe(vs: list[Violation]) -> list[Violation]:
    seen, out = set(), []
    for v in vs:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


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
