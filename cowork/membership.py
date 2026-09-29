"""R13：集合成员变化时，算出要新增处理的成员和可以撤掉的产出。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .runtime import Runtime


@dataclass
class CollectionPlan:
    added: list[str] = field(default_factory=list)       # 需要新增处理的成员
    removed: list[str] = field(default_factory=list)     # 被移除的成员
    retract: list[str] = field(default_factory=list)     # 可以撤掉的产出
    keep: list[str] = field(default_factory=list)        # 被移除成员的产出中，仍被其他步骤引用、不能撤的


def plan_collection_update(runtime: Runtime, step_id: str, old_members: Iterable[str],
                           new_members: Iterable[str],
                           outputs_by_member: dict[str, list[str]]) -> CollectionPlan:
    old, new = list(old_members), list(new_members)
    plan = CollectionPlan(
        added=[m for m in new if m not in old],
        removed=[m for m in old if m not in new],
    )
    for member in plan.removed:
        for out in outputs_by_member.get(member, []):
            # 撤之前 MUST 确认这些产出没有被其他步骤引用（Veloso 等 1998 脚注 2）
            if runtime.is_referenced(out, excluding_step=step_id):
                plan.keep.append(out)
            else:
                plan.retract.append(out)
    return plan


def apply_collection_plan(runtime: Runtime, plan: CollectionPlan) -> None:
    for out in plan.retract:
        runtime.retract_object(out, reason="collection_member_removed")
