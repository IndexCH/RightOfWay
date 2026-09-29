"""测试用的小工具：造一个运行时、让 Agent 提交和完成操作。"""
from __future__ import annotations

from typing import Iterable, Optional

from cowork import ActorKind, CapabilityMeta, Operation, Runtime, Target

HUMAN = "yuan"
HUMAN2 = "bob"
AGENT = "agent"


def make_runtime(policy: str = "discard") -> Runtime:
    rt = Runtime(human_touched_policy=policy)
    rt.register_actor(HUMAN, ActorKind.HUMAN)
    rt.register_actor(HUMAN2, ActorKind.HUMAN)
    rt.register_actor(AGENT, ActorKind.AGENT)
    rt.register_capability(CapabilityMeta("edit", effect_level=1))
    return rt


def agent_op(rt: Runtime, op_id: str, targets: Iterable[str] = (), reads: Iterable[str] = (),
             produces: Iterable[str] = (), step: Optional[str] = None, cap: str = "edit",
             actor: str = AGENT) -> Operation:
    """按对象的当前版本填好基准版本，构造一个操作（还没有提交）。"""
    return Operation(
        op_id=op_id, actor_id=actor, capability=cap, step_id=step,
        targets=[Target(o, rt.objects[o].version) for o in targets],
        reads=[Target(o, rt.objects[o].version) for o in reads],
        produces=list(produces),
    )


def submit(rt: Runtime, *args, **kwargs):
    op = agent_op(rt, *args, **kwargs)
    return rt.submit(op)


def run_agent_step(rt: Runtime, step_id: str, op_id: str, outputs: dict, reads: Iterable[str] = ()):
    """让 Agent 执行一个步骤：已存在的输出放进 targets，新的放进 produces，然后立即完成。"""
    existing = [o for o in outputs if o in rt.objects and not rt.objects[o].retracted]
    new = [o for o in outputs if o not in existing]
    op = agent_op(rt, op_id, targets=existing, reads=reads, produces=new, step=step_id)
    res = rt.submit(op)
    assert res.status.value == "executing", res
    return rt.complete(op_id, writes={o: outputs[o] for o in existing},
                       produces={o: outputs[o] for o in new})


def event_types(rt: Runtime) -> list[str]:
    return [e.type for e in rt.events]
