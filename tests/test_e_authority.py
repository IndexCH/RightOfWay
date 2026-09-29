"""E 类：副本与权威、观察（R2、R18、R21）。"""
import pytest

from cowork import (Authority, AuthorityKind, CapabilityMeta, Channel, ObservationError, OpStatus,
                    StepState)
from helpers import AGENT, HUMAN, make_runtime, run_agent_step, submit


def test_S_E1a_storage_write_rejected_when_session_holds_authority():
    """S-E1a 文档在 LibreOffice 里打开（权威在会话），命令行直接写磁盘被拒绝。"""
    rt = make_runtime()
    rt.register_capability(CapabilityMeta("cli_patch_docx", effect_level=1, channel=Channel.STORAGE))
    rt.create_object("report", "docx", "v5", AGENT,
                     authority=Authority(AuthorityKind.SESSION, "libreoffice"))
    res = submit(rt, "patch", targets=["report"], cap="cli_patch_docx")
    assert res.status == OpStatus.REJECTED_AUTHORITY
    assert res.reason["authority"] == "libreoffice"


def test_S_E1b_level2_detects_stale_copy_overwrite():
    """S-E1b 只有文件监视：无法事前阻止，但能发现旧副本覆盖了新版本，且新版本仍在历史里。"""
    rt = make_runtime()
    rt.create_object("report", "docx", "content-v1", AGENT)
    rt.external_change("report", "content-v2")        # 命令行改了磁盘文件
    rt.external_change("report", "content-v1")        # 应用又从旧缓存保存了一次

    conflicts = rt.events_of("conflict/external_overwrite")
    assert len(conflicts) == 1
    assert conflicts[0].data["overwrittenVersion"] == 2
    assert rt.objects["report"].get_version(2).content == "content-v2"


def test_S_E2_changes_during_takeover_are_reported_exactly():
    """S-E2 人接管期间的修改由适配器报告，运行时给出准确清单，依赖它的步骤过期。"""
    rt = make_runtime()
    rt.create_object("P", "doc", "p1", AGENT)
    rt.add_step("make_q", inputs=["P"], outputs=["Q"])
    rt.add_step("use_q", inputs=["Q"], outputs=["R"])
    run_agent_step(rt, "make_q", "op_q", {"Q": "q1"}, reads=["P"])
    run_agent_step(rt, "use_q", "op_r", {"R": "r1"}, reads=["Q"])
    mark = rt.events[-1].seq

    rt.report_observed_change(HUMAN, "Q", "q-edited-in-app", parent_of={})
    changes = [e for e in rt.log_since(mark) if e.type == "object/changed"]
    assert [(e.data["objectId"], e.data["author"]) for e in changes] == [("Q", HUMAN)]
    assert rt.steps["use_q"].state in (StepState.STALE, StepState.BLOCKED)


def test_S_E3_nested_change_reported_on_enclosing_object():
    """S-E3 观察不到嵌套元素时，变化归到最近的外层对象；找不到归属时报错，不能静默丢弃。"""
    rt = make_runtime()
    rt.create_object("hero_frame", "figma_node", "hero-v1", AGENT)
    parents = {"button_3": "hero_inner", "hero_inner": "hero_frame"}
    rt.report_observed_change(HUMAN, "button_3", "hero-with-new-button", parent_of=parents)
    assert rt.objects["hero_frame"].content == "hero-with-new-button"
    assert rt.objects["hero_frame"].occupancy.holder == HUMAN

    with pytest.raises(ObservationError):
        rt.report_observed_change(HUMAN, "orphan_node", "x", parent_of={})
