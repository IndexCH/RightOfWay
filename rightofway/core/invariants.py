"""三条不变量（spec/core.md 第 6 节；design_v0.5.md 第 3 节）：应用里的实际状态、账本、说明三者一致。

对所有媒介都一样，只比单元和指纹（单元编号 → 内容；内容是带 "fp" 的 dict，或者就是指纹本身）：
I1 账本 = 实际：账本里每个单元的指纹和应用里的一致；账本记为已删除的，应用里没有；应用里有的，账本里也有。
I2 保持的就是保持的：许可单要求保持的单元，执行后要么没变，要么被记成违规。
I3 说的都是真的：告诉 AI "已生效"的，应用里就是它改成的样子；"没有生效、保留了原来的"，应用里和账本里都还是原来的样子。

这里只读，不改任何东西。各媒介把自己的记录换成单元再调用（3D：rightofway/invariants.py；文本：一致性测试）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

I1, I2, I3 = "I1", "I2", "I3"
RULES = {
    I1: "账本和场景不一致",
    I2: "要保持的部分被改了，却没有记为违规",
    I3: "告诉 Agent 的和实际不一致",
}


@dataclass(frozen=True)
class Violation:
    rule: str            # I1 | I2 | I3
    unit: str            # 单元编号
    where: str           # 给人看的位置，例如"Leaf_3 的位置""app.py 第 2 行"
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule} {RULES[self.rule]}] {self.where}：{self.detail}"


def fp(u: Any) -> Any:
    """指纹：带 "fp" 的 dict 取 fp；None 表示不存在。"""
    if isinstance(u, dict):
        return u.get("fp")
    return u


def is_deleted(content: Any) -> bool:
    return isinstance(content, dict) and content.get("deleted") is True


def check_ledger(ledger: Iterable[tuple[str, Any, str]], actual: dict[str, Any],
                 where: Callable[[str, Any], str]) -> list[Violation]:
    """I1。ledger：账本里的单元 [(编号, 内容, 最后是谁写的)]（没建记录的基线单元也要在里面，按基线比，不放宽）；
    actual：应用里实际的单元 {编号: 内容}；where(编号, 内容)：给人看的位置。"""
    out: list[Violation] = []
    tracked = set()
    for uid, led, author in ledger:
        tracked.add(uid)
        got = actual.get(uid)
        w = where(uid, led)
        if is_deleted(led):
            if got is not None:
                out.append(Violation(I1, uid, w, "账本记为已删除，场景里还在"))
        elif got is None:
            out.append(Violation(I1, uid, w, f"账本里还在（最后由 {author} 写入），场景里没有"))
        elif fp(got) != fp(led):
            out.append(Violation(I1, uid, w, f"内容不一致（账本最后由 {author} 写入）"))
    for uid, u in sorted(actual.items()):
        if uid not in tracked:
            out.append(Violation(I1, uid, where(uid, u), "场景里有，账本里没有"))
    return out


def check_kept(kept: Iterable[str], before: dict[str, Any], final: dict[str, Any], breach: Iterable[str],
               where: Callable[[str], str]) -> list[Violation]:
    """I2：要保持的单元执行后没变，或者记成了违规。"""
    breach = set(breach)
    out = []
    for uid in kept:
        if uid in breach:
            continue
        if fp(final.get(uid)) != fp(before.get(uid)):
            out.append(Violation(I2, uid, where(uid), "被删掉了，没有恢复" if uid not in final else "被改了，没有恢复"))
    return out


def check_said(applied: Iterable[str], said_kept: Iterable[tuple[str, str]], before: dict[str, Any],
               raw: dict[str, Any], final: dict[str, Any], breach: Iterable[str], where: Callable[[str], str],
               ledger_content: Optional[Callable[[str], Any]] = None) -> list[Violation]:
    """I3。applied：说"已生效"的单元；said_kept：说"没有生效、保留了原来的"的 [(单元, 原因)]；
    before / raw / final：执行前、执行后（恢复前）、最终；ledger_content(编号)：账本里现在的内容（不知道就是 None）。"""
    breach = set(breach)
    out = []
    for uid in applied:
        want, got = raw.get(uid), final.get(uid)
        if want is None and got is not None:
            out.append(Violation(I3, uid, where(uid), "说 Agent 的删除已生效，但场景里还在"))
        elif want is not None and fp(got) != fp(want):
            out.append(Violation(I3, uid, where(uid), "说 Agent 的修改已生效，但场景里不是它改成的样子"
                                 + ("（场景里没有）" if got is None else "")))
    for uid, reason in said_kept:
        if uid in breach:
            continue
        was, got = before.get(uid), final.get(uid)
        if fp(got) != fp(was):
            out.append(Violation(I3, uid, where(uid), f"说没有生效、保留了原来的（{reason}），但场景里"
                                 + ("没有了" if got is None else "已经变了")))
        led = ledger_content(uid) if ledger_content is not None else None
        if led is not None and not is_deleted(led) and fp(led) != fp(was):
            out.append(Violation(I3, uid, where(uid), "说保留了原来的，但账本里不是原来的"))
    return out


def dedupe(vs: list[Violation]) -> list[Violation]:
    seen, out = set(), []
    for v in vs:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out
