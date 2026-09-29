"""H 类：确认与暂停（R5、R16、R17）。"""
from cowork import CapabilityMeta, OpStatus
from helpers import AGENT, HUMAN, agent_op, make_runtime, submit


def _caps(rt):
    rt.register_capability(CapabilityMeta("upload_public", effect_level=3))
    rt.register_capability(CapabilityMeta("resize", effect_level=1))


def test_S_H1_irreversible_needs_confirmation_reversible_does_not():
    """S-H1 撤不回的操作要确认，能撤回的不要；副作用等级以能力元数据为准。"""
    rt = make_runtime()
    _caps(rt)
    rt.create_object("img", "png", "i1", AGENT)

    op = agent_op(rt, "up", targets=["img"], cap="upload_public")
    op.effect_level = 0                               # Agent 自报的等级会被忽略
    assert rt.submit(op).status == OpStatus.PENDING_CONFIRMATION
    assert len(rt.events_of("confirm/request")) == 1

    assert rt.confirm(HUMAN, "up", approved=True).status == OpStatus.EXECUTING
    assert rt.complete("up", writes={"img": "uploaded"}).status == OpStatus.COMMITTED

    assert submit(rt, "rs", targets=["img"], cap="resize").status == OpStatus.EXECUTING
    assert len(rt.events_of("confirm/request")) == 1  # resize 没有请求确认


def test_S_H1_human_can_reject():
    rt = make_runtime()
    _caps(rt)
    rt.create_object("img", "png", "i1", AGENT)
    submit(rt, "up", targets=["img"], cap="upload_public")
    assert rt.confirm(HUMAN, "up", approved=False).status == OpStatus.REJECTED_BY_HUMAN
    assert rt.objects["img"].content == "i1"


def test_S_H2_human_own_changes_need_no_approval():
    """S-H2 人自己改组合、人自己发起撤不回的操作，都不用本人再批准。"""
    rt = make_runtime()
    _caps(rt)
    rt.create_object("comp", "composition", {"steps": []}, AGENT)
    rt.human_edit(HUMAN, "comp", {"steps": ["frames"]})
    rt.create_object("img", "png", "i1", HUMAN)
    res = submit(rt, "up", targets=["img"], cap="upload_public", actor=HUMAN)
    assert res.status == OpStatus.EXECUTING
    assert not rt.events_of("confirm/request")


def test_S_H3_pause_and_resume():
    """S-H3 暂停：能取消的取消；不能取消的跑完但结果暂存；暂停期间提交的排队；恢复后再按规则检查。"""
    rt = make_runtime()
    rt.register_capability(CapabilityMeta("render", effect_level=1, cancellable=False))
    rt.register_capability(CapabilityMeta("blur", effect_level=1, cancellable=True))
    for name in ("a", "b", "c", "d"):
        rt.create_object(name, "png", f"{name}1", AGENT)

    submit(rt, "O1", targets=["a"], cap="render")
    submit(rt, "O2", targets=["b"], cap="render")
    submit(rt, "O4", targets=["d"], cap="blur")

    rt.pause(HUMAN)
    assert rt.ops["O4"].status == OpStatus.CANCELLED
    assert rt.complete("O1", writes={"a": "a2"}).status == OpStatus.HELD
    assert rt.objects["a"].content == "a1"            # 暂停期间不提交
    assert submit(rt, "O3", targets=["c"], cap="render").status == OpStatus.QUEUED
    rt.human_edit(HUMAN, "b", "b-by-human")           # 暂停期间人改了 b
    assert rt.complete("O2", writes={"b": "b2"}).status == OpStatus.HELD

    rt.resume(HUMAN)
    assert rt.ops["O1"].status == OpStatus.COMMITTED
    assert rt.ops["O2"].status == OpStatus.REJECTED_STALE
    assert rt.objects["b"].content == "b-by-human"
    assert rt.ops["O3"].status == OpStatus.EXECUTING
    assert rt.complete("O4", writes={"d": "d2"}).status == OpStatus.CANCELLED   # 迟到的结果被忽略
    assert rt.objects["d"].content == "d1"
