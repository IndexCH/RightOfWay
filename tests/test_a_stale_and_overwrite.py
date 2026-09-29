"""A 类：过时结果与覆盖（R3、R4、R6、R7）。"""
from cowork import OpStatus, POLICY_CANDIDATE, POLICY_MERGE, StepState
from helpers import AGENT, HUMAN, agent_op, make_runtime, run_agent_step, submit


def test_S_A1_late_agent_result_after_human_edit():
    """S-A1 人先改了，AI 的旧结果才到：整个操作作废，保留人的版本。"""
    rt = make_runtime()
    rt.create_object("frame_07", "png", "ai-v1", AGENT)
    submit(rt, "A1", targets=["frame_07"])            # 基准版本 1，开始执行
    rt.human_edit(HUMAN, "frame_07", "human-fix")     # 人修改 → 版本 2
    res = rt.complete("A1", writes={"frame_07": "ai-late"})

    assert res.status == OpStatus.REJECTED_STALE
    assert rt.objects["frame_07"].content == "human-fix"
    assert rt.objects["frame_07"].version == 2
    assert any(e.type == "result/discarded" and e.data["objectId"] == "frame_07" for e in rt.events)


def test_S_A1_stale_read_set_also_rejected():
    """读集过时同样作废：AI 读的输入在它执行期间被人改了。"""
    rt = make_runtime()
    rt.create_object("raw", "png", "raw-v1", AGENT)
    rt.create_object("cut", "png", "cut-v1", AGENT)
    submit(rt, "A1", targets=["cut"], reads=["raw"])
    rt.human_edit(HUMAN, "raw", "raw-fixed")
    res = rt.complete("A1", writes={"cut": "cut-from-old-raw"})
    assert res.status == OpStatus.REJECTED_STALE
    assert res.reason["set"] == "reads"
    assert rt.objects["cut"].content == "cut-v1"


DOC = "第一段\n\n第二段\n\n第三段"


def test_S_A2_default_policy_rejects_full_replace():
    """S-A2 默认策略：人改过的文档，AI 基于旧版本的整份替换被拒绝，人的第二段保留。"""
    rt = make_runtime()
    rt.create_object("doc", "text", DOC, AGENT, mergeable=True)
    submit(rt, "A2", targets=["doc"])
    rt.human_edit(HUMAN, "doc", "第一段\n\n人改的第二段\n\n第三段")
    res = rt.complete("A2", writes={"doc": "第一段\n\n第二段\n\nAI 改的第三段"})

    assert res.status == OpStatus.REJECTED_STALE
    assert "人改的第二段" in rt.objects["doc"].content


def test_S_A2_merge_policy_merges_non_conflicting_paragraphs():
    """S-A2 合并策略：人改完并释放后，AI 的结果才到；两边改的是不同段落，合并后都保留。"""
    rt = make_runtime(POLICY_MERGE)
    rt.create_object("doc", "text", DOC, AGENT, mergeable=True)
    submit(rt, "A2", targets=["doc"])
    rt.human_edit(HUMAN, "doc", "第一段\n\n人改的第二段\n\n第三段")
    rt.release(HUMAN, "doc")
    res = rt.complete("A2", writes={"doc": "第一段\n\n第二段\n\nAI 改的第三段"})

    assert res.status == OpStatus.MERGED
    assert rt.objects["doc"].content == "第一段\n\n人改的第二段\n\nAI 改的第三段"


def test_S_A2_merge_policy_no_merge_while_human_still_editing():
    """合并策略下，人还占用着文档时不合并：结果按过时作废。"""
    rt = make_runtime(POLICY_MERGE)
    rt.create_object("doc", "text", DOC, AGENT, mergeable=True)
    submit(rt, "A2", targets=["doc"])
    rt.human_edit(HUMAN, "doc", "第一段\n\n人改的第二段\n\n第三段")
    res = rt.complete("A2", writes={"doc": "第一段\n\n第二段\n\nAI 改的第三段"})
    assert res.status == OpStatus.REJECTED_STALE


def test_S_A2_merge_policy_conflict_keeps_human_version():
    """合并策略下，两边改了同一段：合并失败，按过时拒绝，人的版本保留。"""
    rt = make_runtime(POLICY_MERGE)
    rt.create_object("doc", "text", DOC, AGENT, mergeable=True)
    submit(rt, "A2", targets=["doc"])
    rt.human_edit(HUMAN, "doc", "第一段\n\n人改的第二段\n\n第三段")
    res = rt.complete("A2", writes={"doc": "第一段\n\nAI 改的第二段\n\n第三段"})

    assert res.status == OpStatus.REJECTED_STALE
    assert "人改的第二段" in rt.objects["doc"].content


def test_R4_merge_exception_between_two_agent_ops():
    """R4 例外：两个 AI 操作基于同一版本改不同段落，可合并对象可以合并。"""
    rt = make_runtime()
    rt.create_object("doc", "text", DOC, AGENT, mergeable=True)
    submit(rt, "X", targets=["doc"])
    submit(rt, "Y", targets=["doc"])
    assert rt.complete("X", writes={"doc": "X 改的第一段\n\n第二段\n\n第三段"}).status == OpStatus.COMMITTED
    res = rt.complete("Y", writes={"doc": "第一段\n\n第二段\n\nY 改的第三段"})
    assert res.status == OpStatus.MERGED
    assert rt.objects["doc"].content == "X 改的第一段\n\n第二段\n\nY 改的第三段"


def _frames_setup(rt):
    rt.add_step("cutout", inputs=[f"raw_{i:02d}" for i in range(24)],
                outputs=[f"cut_{i:02d}" for i in range(24)])
    rt.add_step("sheet", inputs=[f"cut_{i:02d}" for i in range(24)], outputs=["atlas"])
    for i in range(24):
        rt.create_object(f"raw_{i:02d}", "png", f"raw{i}", AGENT)
        rt.create_object(f"cut_{i:02d}", "png", f"cut{i}-v1", AGENT)


def test_S_A3_agent_rerun_skips_human_touched_object():
    """S-A3 AI 主动重做 24 帧：人改过的第 7 帧不动，其余 23 帧照常提交。"""
    rt = make_runtime()
    _frames_setup(rt)
    rt.human_edit(HUMAN, "cut_07", "human-fixed-edge")
    rt.release(HUMAN, "cut_07")

    results = {}
    for i in range(24):                               # 批量步骤：每个成员一个操作
        results[i] = submit(rt, f"redo_{i:02d}", targets=[f"cut_{i:02d}"],
                            reads=[f"raw_{i:02d}"], step="cutout")
    for i in range(24):
        if results[i].status == OpStatus.EXECUTING:
            rt.complete(f"redo_{i:02d}", writes={f"cut_{i:02d}": f"cut{i}-v2"})

    assert results[7].status == OpStatus.REJECTED_HUMAN_TOUCHED
    assert rt.objects["cut_07"].content == "human-fixed-edge"
    assert all(rt.objects[f"cut_{i:02d}"].content == f"cut{i}-v2" for i in range(24) if i != 7)
    assert rt.steps["cutout"].state == StepState.DONE
    assert rt.steps["sheet"].state == StepState.PENDING   # 下游等待执行，读到的第 7 帧是人的版本


def test_S_A3_candidate_policy_keeps_ai_result_aside():
    """R6 替代策略"留作候选"：AI 的结果另存，不成为当前版本。"""
    rt = make_runtime(POLICY_CANDIDATE)
    rt.create_object("cut_07", "png", "v1", AGENT)
    rt.human_edit(HUMAN, "cut_07", "human")
    rt.release(HUMAN, "cut_07")
    assert submit(rt, "redo", targets=["cut_07"]).status == OpStatus.EXECUTING
    res = rt.complete("redo", writes={"cut_07": "ai-v2"})
    assert res.status == OpStatus.CANDIDATE
    assert rt.objects["cut_07"].content == "human"
    assert rt.objects["cut_07"].candidates[0].content == "ai-v2"


def test_S_A4_hand_back_allows_agent_again():
    """S-A4 人交还之后，AI 可以再改。"""
    rt = make_runtime()
    rt.create_object("cut_07", "png", "v1", AGENT)
    rt.human_edit(HUMAN, "cut_07", "human")
    rt.release(HUMAN, "cut_07")
    assert submit(rt, "before", targets=["cut_07"]).status == OpStatus.REJECTED_HUMAN_TOUCHED

    rt.hand_back(HUMAN, "cut_07")
    assert submit(rt, "after", targets=["cut_07"]).status == OpStatus.EXECUTING
    assert rt.complete("after", writes={"cut_07": "ai-v3"}).status == OpStatus.COMMITTED
    assert rt.objects["cut_07"].content == "ai-v3"
