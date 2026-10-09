"""实验指标：拿"最终的场景"和"运行时记录的每个对象应该是什么样"对照。

运行时里每个对象的最新版本就是规则认定的结果：
- 最新版本是人做的：最终场景里必须和人的版本一致，否则算"人的修改被覆盖"。
- 最新版本是 AI 做的：最终场景里必须和 AI 的版本一致，否则算"AI 的修改丢失"。
- 最新版本是"违规"版本（规则要求保持，但应用没能恢复，运行时照实记下）：账本和场景是一致的，
  但被违规改掉的那个版本确实丢了，按它的作者算"人的修改被覆盖"或"AI 的修改丢失"，并单独列为违规。
比较的是内容指纹（cfp）；只有名字不同单独列出来。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..model import ActorKind
from ..runtime import Runtime
from .records import OBJECT, Records, describe_unit, display, is_deleted, split_unit, units


@dataclass
class Evaluation:
    human_overwritten: list[str] = field(default_factory=list)
    ai_lost: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)      # 场景里有、运行时不知道的对象
    breach: list[str] = field(default_factory=list)         # 其中已经如实报告为违规的

    def summary(self) -> str:
        parts = [f"人的修改被覆盖 {len(self.human_overwritten)} 处"
                 + (f"（{'、'.join(self.human_overwritten)}）" if self.human_overwritten else ""),
                 f"AI 的修改丢失 {len(self.ai_lost)} 处"
                 + (f"（{'、'.join(self.ai_lost)}）" if self.ai_lost else "")]
        if self.renamed:
            parts.append("名字变了：" + "、".join(self.renamed))
        if self.breach:
            parts.append(f"其中 {len(self.breach)} 处已如实报告为违规（{'、'.join(self.breach)}）")
        if self.untracked:
            parts.append("运行时不知道的对象：" + "、".join(self.untracked))
        return "；".join(parts)


def evaluate(rt: Runtime, final: Records, human: str, baseline_actor: str = "baseline",
             granularity: str = OBJECT, labels: dict | None = None) -> Evaluation:
    """final 是最终场景的对象记录；granularity 要和运行时里记录的单元一致。"""
    ev = Evaluation()
    fu = units(final, granularity)
    hit_h, hit_a = set(), set()
    for uid, obj in sorted(rt.objects.items()):
        if obj.retracted or not obj.versions:
            continue
        last = obj.versions[-1]
        expected = last.content
        got = fu.get(uid)
        name = display(expected, uid) if isinstance(expected, dict) else uid
        if getattr(last, "breach", False) and len(obj.versions) > 1:
            lost = obj.versions[-2]                   # 被违规改掉的那个版本
            label = name if (is_deleted(expected) or got is None) else describe_unit(uid, name, labels)
            if lost.author_id == human and label not in hit_h:
                ev.human_overwritten.append(label)
                ev.breach.append(label)
                hit_h.add(label)
            elif (lost.author_kind == ActorKind.AGENT and lost.author_id not in (baseline_actor, last.author_id)
                  and label not in hit_a):                # 违规的 AI 改掉的是它自己之前的修改，不算"丢失"
                ev.ai_lost.append(label)
                ev.breach.append(label)
                hit_a.add(label)
            continue
        if is_deleted(expected):
            ok = got is None
        else:
            ok = got is not None and got["cfp"] == expected["cfp"]
            if ok and got["name"] != expected["name"] and split_unit(uid)[1] is None:
                ev.renamed.append(f"{expected['name']} → {got['name']}")
        if ok:
            continue
        # 整个对象多了或少了，按一处算；对象还在、只是某个面不对，按面算
        label = name if (is_deleted(expected) or got is None) else describe_unit(uid, name, labels)
        if last.author_id == human and label not in hit_h:
            ev.human_overwritten.append(label)
            hit_h.add(label)
        elif last.author_kind == ActorKind.AGENT and last.author_id != baseline_actor and label not in hit_a:
            ev.ai_lost.append(label)
            hit_a.add(label)
    ev.untracked = sorted(r["name"] for cid, r in final.items() if split_unit(cid)[0] not in
                          {split_unit(u)[0] for u in rt.objects})
    ev.human_overwritten.sort()
    ev.ai_lost.sort()
    return ev
