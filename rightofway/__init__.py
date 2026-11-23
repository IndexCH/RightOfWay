"""RightOfWay 参考实现。

规范见 spec/spec_v0.4.md，v0.5 的架构设计见 spec/design_v0.5.md，测试场景见 scenarios/scenarios_v0.md，
规则核心见 spec/core.md。

下面这些名字在第一次用到时才导入（PEP 562）：代码接入的 hook 每次是一个新进程，只用到核心和 rightofway.code，
不必为 3D 的运行时付导入的时间。`from rightofway import Runtime` 等写法照旧可用。
"""
from importlib import import_module as _import_module

_EXPORTS = {
    **dict.fromkeys(("Actor", "ActorKind", "Authority", "AuthorityKind", "CapabilityMeta", "Channel", "Event",
                     "ObjectState", "Operation", "OpResult", "OpStatus", "Permit", "Step", "StepState", "Target"),
                    ".model"),
    **dict.fromkeys(("POLICY_CANDIDATE", "POLICY_DISCARD", "POLICY_MERGE", "ObservationError", "ProtocolError",
                     "Runtime"), ".runtime"),
    **dict.fromkeys(("CollectionPlan", "apply_collection_plan", "plan_collection_update"), ".membership"),
}

__all__ = [
    "Actor", "ActorKind", "Authority", "AuthorityKind", "CapabilityMeta", "Channel", "Event",
    "ObjectState", "Operation", "OpResult", "OpStatus", "Permit", "Step", "StepState", "Target",
    "Runtime", "ProtocolError", "ObservationError",
    "POLICY_DISCARD", "POLICY_CANDIDATE", "POLICY_MERGE",
    "CollectionPlan", "plan_collection_update", "apply_collection_plan",
]


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(_import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
