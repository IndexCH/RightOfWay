"""告知（P8，prior_art_solutions.md 第 3 项）：失效的事实、人的值就是新的目标、结构化结果、循环检测。

这些都是告知，不是安全手段（P2）：人的修改由许可单和核对保护，这里只是让 AI 的认知跟上实际的场景。
"""
from rightofway.blender.shared_session import SharedSession
from rightofway.fakeapp import FakeApp, FakeBridge
from rightofway.invariants import check_session
from rightofway.runtime import Runtime
from experiments import scenario_fake as sf

ADJUST = sf._code(sf._ADJUST)
REBUILD = sf._code(sf._REBUILD, plan=sf.PLAN_ADJUST)


def _scene(rt=None, **kw):
    app = FakeApp(**kw)
    s = SharedSession(rt or Runtime(), FakeBridge(app))
    s.start()
    s.run_agent(sf.ai_build(), "build")
    sf.human_a(app)                    # 删掉 Rock_2、把 Leaf_3 往上挪、新建 Cube、选中 Leaf_1
    s.poll()
    return app, s


def test_human_values_are_stated_as_the_new_target():
    app, s = _scene(unique_names=True)
    rep = s.run_agent(REBUILD, "清空重建")
    assert "以现在的值为准" in rep.text and "新的目标" in rep.text
    assert "Leaf_3 的Location = (-0.64721, 0.47023, 2.2)（人定的）" in rep.text
    assert "Cube 还在场景里" in rep.text and "Rock_2 已被人删除" in rep.text
    assert "请按上面的值重新核对" in rep.text                                   # 失效的事实：按旧值算的要重新核对
    kinds = {(f["object"], f["fact"]) for f in rep.facts}
    assert {("Leaf_3", "value"), ("Cube", "exists"), ("Rock_2", "deleted")} <= kinds
    assert check_session(s, rep) == []


def test_structured_result_has_one_entry_per_conflicting_face():
    app, s = _scene(unique_names=True)
    rep = s.run_agent(ADJUST, "调整")
    out = s.payload(rep)
    leaf3 = [c for c in out["conflicts"] if c["object"] == "Leaf_3"]
    assert leaf3 == [{"object": "Leaf_3", "label": "Location", "owner": "human", "reason": "human",
                      "now": "(-0.64721, 0.47023, 2.2)", "wanted": "(-0.64721, 0.47023, 1.6)",
                      "delete_refused": False, "precheck": False}]
    assert out["outcome"] == "committed" and "Trunk" in out["applied"] and SharedSession.is_error(rep)
    clean = s.run_agent("scene.set('Trunk', 'scale', 1.4)", "只改树干")
    assert not SharedSession.is_error(clean) and s.payload(clean)["conflicts"] == []


def test_typed_refusals_are_in_the_structured_result():
    app, s = _scene(restore_deleted=False, transfer_ids=False)
    rep = s.run_commands([{"action": "delete", "target": "Leaf_3"},
                          {"action": "modify", "target": "Nope", "set": {"scale": 2}}], "批量")[0]
    out = s.payload(rep)
    assert [c["status"] for c in out["commands"]] == ["refused", "failed"]
    assert any(c["object"] == "Leaf_3" and c["delete_refused"] and c["precheck"] for c in out["conflicts"])


def test_repeated_futile_edits_are_detected_from_the_ledger():
    rt = Runtime(loop_repeat=3, loop_window=5)
    app, s = _scene(rt, unique_names=True)
    alerts = []
    for i in range(4):
        rep = s.run_agent(ADJUST, f"调整 {i}")                              # 每次都想把 Leaf_3 拉回 1.6
        alerts.append(rep.alert)
    assert sorted(x["object"] for x in rep.loops if x["label"] == "Location") == ["Cube", "Leaf_3"]
    assert next(x for x in rep.loops if x["object"] == "Leaf_3")["times"] == 4
    assert "反复想改" in rep.text
    assert [bool(a) for a in alerts] == [False, False, True, False]            # 第 3 次提醒人一次，之后不重复
    assert "反复改你改过的部分" in alerts[2] and "Leaf_3 的Location" in alerts[2]
    events = [e for e in rt.events if e.type == "alert/human" and e.data.get("kind") == "loop"]
    assert len(events) == 1
    assert not s.rt.suspended                                                  # 只提醒，不暂停


def test_loop_ends_when_the_agent_stops_and_alerts_again_if_it_restarts():
    rt = Runtime(loop_repeat=2, loop_window=2)
    app, s = _scene(rt, unique_names=True)
    s.run_agent(ADJUST, "1")
    assert s.run_agent(ADJUST, "2").loop_alert
    calm = s.run_agent("scene.set('Trunk', 'scale', 1.4)", "3")
    assert calm.loops == []
    s.run_agent(ADJUST, "4")
    assert s.run_agent(ADJUST, "5").loop_alert                                 # 又开始反复：重新提醒


def test_copied_material_and_human_material_losing_users_are_reported():
    """派生改动：AI 复制了共用材质（LeafMat → LeafMat.001）并换给两片叶子；人改过 LeafMat 的颜色，用它的叶子变少了。"""
    app, s = _scene(unique_names=True)
    app.set("LeafMat", "color", "紫")                                          # 人改了共用材质
    s.poll()
    rep = s.run_agent("scene.create('LeafMat', {'color': '橙'}, kind='MATERIAL', data=True)\n"
                      "scene.link('Leaf_1', 'material', 'LeafMat.001')\nscene.link('Leaf_2', 'material', 'LeafMat.001')\n",
                      "复制材质")
    kinds = {(d["kind"], d["object"]) for d in rep.derived}
    assert ("copy", "LeafMat.001（MATERIAL）") in kinds and ("fewer_users", "LeafMat（MATERIAL）") in kinds
    assert "的一份副本" in rep.text and "从 5 个变成了 3 个" in rep.text
    assert s.payload(rep)["derived"] == rep.derived
    assert check_session(s, rep) == []
