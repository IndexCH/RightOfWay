"""RightOfWay 参考实现。

规范见 spec/spec_v0.4.md，v0.5 的架构设计见 spec/design_v0.5.md，测试场景见 scenarios/scenarios_v0.md。
"""
from .model import (
    Actor,
    ActorKind,
    Authority,
    AuthorityKind,
    CapabilityMeta,
    Channel,
    Event,
    ObjectState,
    Operation,
    OpResult,
    OpStatus,
    Permit,
    Step,
    StepState,
    Target,
)
from .runtime import (
    POLICY_CANDIDATE,
    POLICY_DISCARD,
    POLICY_MERGE,
    ObservationError,
    ProtocolError,
    Runtime,
)
from .membership import CollectionPlan, apply_collection_plan, plan_collection_update

__all__ = [
    "Actor", "ActorKind", "Authority", "AuthorityKind", "CapabilityMeta", "Channel", "Event",
    "ObjectState", "Operation", "OpResult", "OpStatus", "Permit", "Step", "StepState", "Target",
    "Runtime", "ProtocolError", "ObservationError",
    "POLICY_DISCARD", "POLICY_CANDIDATE", "POLICY_MERGE",
    "CollectionPlan", "plan_collection_update", "apply_collection_plan",
]
