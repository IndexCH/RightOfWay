"""B 类：占用与释放（R8、R9、R10）。"""
import pytest

from rightofway import OpStatus, ProtocolError, StepState
from helpers import AGENT, HUMAN, HUMAN2, make_runtime, submit


def _layout(rt):
    rt.create_object("hero_frame", "figma_node", "hero-v1", AGENT)
    rt.create_object("gallery_frame", "figma_node", "gallery-v1", AGENT)
    rt.add_step("layout", inputs=[], outputs=["hero_frame", "gallery_frame"])
    rt.add_step("review", inputs=["hero_frame"], outputs=["notes"])


def test_S_B1_co_editing_same_object():
    """S-B1 同一个对象上一起改：被占用的对象不再派发，其他对象照常，下游阻塞到释放。"""
    rt = make_runtime()
    _layout(rt)
    submit(rt, "op_hero", targets=["hero_frame"], step="layout")
    submit(rt, "op_gallery", targets=["gallery_frame"], step="layout")

    rt.human_edit(HUMAN, "hero_frame", "hero-by-human")
    assert rt.objects["hero_frame"].occupancy.holder == HUMAN
    assert rt.steps["review"].state == StepState.BLOCKED

    # 已在执行、针对 hero 的操作：结果作废
    assert rt.complete("op_hero", writes={"hero_frame": "hero-ai"}).status == OpStatus.REJECTED_STALE
    # 与它无关的 gallery 照常提交
    assert rt.complete("op_gallery", writes={"gallery_frame": "gallery-ai"}).status == OpStatus.COMMITTED
    # 新的、针对被占用对象的操作不派发
    assert submit(rt, "op_hero2", targets=["hero_frame"]).status == OpStatus.REJECTED_OCCUPIED

    rt.release(HUMAN, "hero_frame")
    assert rt.steps["review"].state == StepState.PENDING
    assert rt.objects["hero_frame"].content == "hero-by-human"


def test_S_B2_viewing_does_not_occupy():
    """S-B2 只看不算占用：AI 照常工作。"""
    rt = make_runtime()
    _layout(rt)
    rt.view(HUMAN, "hero_frame")
    assert rt.objects["hero_frame"].occupancy is None
    assert submit(rt, "op", targets=["hero_frame"]).status == OpStatus.EXECUTING


def test_S_B3_switching_window_is_not_release():
    """S-B3 切走窗口、关窗口都不算释放；只有占用者本人明确释放才算（R10）。"""
    rt = make_runtime()
    _layout(rt)
    rt.human_edit(HUMAN, "hero_frame", "hero-by-human")
    rt.ui_event(HUMAN, "focus_changed", "hero_frame")
    rt.ui_event(HUMAN, "window_closed", "hero_frame")
    assert rt.objects["hero_frame"].occupancy is not None

    with pytest.raises(ProtocolError):
        rt.release(HUMAN2, "hero_frame")              # 别人不能替他释放

    rt.release(HUMAN, "hero_frame")
    assert rt.objects["hero_frame"].occupancy is None
    # R10：释放后通知依赖它的步骤
    assert any(e.type == "step/state" and e.data == {"stepId": "review", "state": "pending"}
               for e in rt.events)


def test_R5_later_human_edit_wins_without_confirmation():
    """R5：两个人的修改之间，后提交的覆盖先提交的，不请求确认。"""
    rt = make_runtime()
    _layout(rt)
    rt.human_edit(HUMAN, "hero_frame", "by-yuan")
    rt.human_edit(HUMAN2, "hero_frame", "by-bob")
    assert rt.objects["hero_frame"].content == "by-bob"
    assert not rt.events_of("confirm/request")
