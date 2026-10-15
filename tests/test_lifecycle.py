"""执行生命周期（design_v0.5.md 14.1–14.3）和身份、意图（14.4、14.5）。

- 一个准入判断：人暂停全部 AI 时，新流程照样不执行；结算不再走旧流程，不会"允许执行、拒绝结算"；
  每张许可单正好结束一次。
- 结果没收到：接入代码按许可单编号记着结果，查回来照常结算（AI 的改动算 AI 的）；没执行的就关掉；
  查不回来（应用重新打开过）的差异记为来源不明，按人的修改保护；查清之前不把差异算成人的，也不开始新的执行。
  同一张许可单只执行一次。
- 账本落盘：重启后接着用；代理没运行期间的变化记为来源不明；执行到一半代理退出的，重启后查清。
- 人删掉的，人交还之前不能补回（数据块：删的时候还有人用才算）；交还来自应用里的按钮，AI 的代码伪造不了。
- 认回是推断：告诉人，人可以说"不是同一个"，拆回两个对象。
"""
import pytest

from rightofway.blender.bridge import BridgeError
from rightofway.blender.shared_session import SharedSession
from rightofway.fakeapp import FakeApp, FakeBridge, _show
from rightofway.invariants import check_ledger, check_session
from rightofway.runtime import ProtocolError, Runtime
from experiments import scenario_fake as sf

REBUILD = sf._code(sf._REBUILD, plan=sf.PLAN_ADJUST)
SCALE = "scene.set('Trunk', 'scale', {v})"


class Flaky(FakeBridge):
    """可以模拟传输出问题的桥：after(fn) 返回 True 时，应用执行完了但结果没送回来；
    before(fn) 返回 True 时，请求根本没到应用；down=True 时什么都连不上。"""

    def __init__(self, app, before=None, after=None):
        super().__init__(app)
        self.before, self.after, self.down = before, after, False

    def call(self, fn, args=None):
        if self.down or (self.before and self.before(fn)):
            raise BridgeError(f"连不上应用（{fn}）")
        out = super().call(fn, args)
        if self.after and self.after(fn):
            raise BridgeError(f"超时：没收到 {fn} 的结果")
        return out


def _scene(bridge_cls=FakeBridge, rt=None, ledger=None, **kw):
    app = FakeApp(unique_names=True, **kw)
    s = SharedSession(rt or Runtime(), bridge_cls(app))
    s.start(ledger=ledger)
    s.run_agent(sf.ai_build(), "build")
    return app, s


def _val(app, name, face):
    return app.objects[app.find(name)]["faces"][face]


def _named(app, name):
    return [o for o in app.objects.values() if o["name"] == name]


def _author(s, name, face):
    obj = s.rt.objects[f"{s.find(name)}#{face}"]
    return obj.versions[-1].author_id, (obj.human_touched.by if obj.human_touched else None)


# ---------------------------------------------------------------------------
# 14.1 一个准入判断；许可单正好结束一次
# ---------------------------------------------------------------------------
def test_pause_refuses_new_executions_and_nothing_runs():
    app, s = _scene()
    s.rt.pause("yuan")
    rep = s.run_agent(SCALE.format(v=1.7), "暂停中")
    assert rep.status == "refused" and "暂停" in rep.text and _val(app, "Trunk", "scale") == 1.0
    s.rt.resume("yuan")
    rep = s.run_agent(SCALE.format(v=1.7), "恢复之后")
    assert rep.status == "ok" and _val(app, "Trunk", "scale") == 1.7
    assert {p.state for p in s.rt.permits.values()} <= {"settled", "closed", "refused"}


def test_a_pause_during_execution_does_not_break_settlement():
    """人在 AI 这一步执行时暂停：这一步照实结算（暂停只管还没开始的），下一步不执行。"""
    app = FakeApp(unique_names=True)
    rt = Runtime()
    real = app.run_agent

    def run(args):
        rt.pause("yuan")                                   # 执行期间人按了暂停
        return real(args)
    app.run_agent = run
    s = SharedSession(rt, FakeBridge(app))
    s.start()
    rep = s.run_agent(sf.ai_build(), "build")
    assert rep.status == "ok" and rep.outcome == "committed" and _author(s, "Trunk", "scale")[0] == "agent"
    assert s.run_agent(SCALE.format(v=2.0), "暂停之后").status == "refused"
    assert check_ledger(rt, s.scene_records(), s.granularity) == []


def test_each_permit_ends_exactly_once():
    app, s = _scene()
    app.set("Leaf_1", "scale", 2.0)                         # 人刚改过、还没轮询到：执行前核对发现，重开许可单
    rep = s.run_agent("scene.set('Leaf_2', 'scale', 1.2)", "重开")
    assert rep.status == "ok"
    states = [p.state for p in s.rt.permits.values()]
    assert "closed" in states and set(states) <= {"settled", "closed"}
    done = next(p for p in s.rt.permits.values() if p.state == "settled")
    with pytest.raises(ProtocolError):
        s.rt.settle(done, {}, {}, {})


# ---------------------------------------------------------------------------
# 14.2 结果没收到
# ---------------------------------------------------------------------------
def test_a_lost_result_is_queried_back_and_counted_as_the_agents():
    app, s = _scene(lambda a: Flaky(a, after=lambda fn: fn == "run_agent"))
    rep = s.run_agent(SCALE.format(v=1.7), "丢了结果")
    assert rep.status == "ok" and "查清了" in rep.text and _val(app, "Trunk", "scale") == 1.7
    assert _author(s, "Trunk", "scale") == ("agent", None)              # 不是人的
    s.bridge.after = None
    assert not s.poll()
    again = s.run_agent(SCALE.format(v=2.0), "再改")
    assert again.conflicts == [] and _val(app, "Trunk", "scale") == 2.0


def test_a_request_that_never_reached_the_app_is_closed_not_executed():
    app, s = _scene(lambda a: Flaky(a))
    s.bridge.before = lambda fn: fn == "run_agent"
    rep = s.run_agent(SCALE.format(v=1.7), "没送到")
    s.bridge.before = None
    assert rep.status == "not_executed" and _val(app, "Trunk", "scale") == 1.0
    assert s.run_agent(SCALE.format(v=1.7), "重试").status == "ok" and _val(app, "Trunk", "scale") == 1.7


def test_while_in_doubt_nothing_is_attributed_and_nothing_new_runs():
    app, s = _scene(lambda a: Flaky(a))
    s.bridge.after = lambda fn: fn == "run_agent"
    first = s.run_agent(SCALE.format(v=1.7), "执行了，连接断了")
    assert first.status == "ok"                                         # 马上查回来了
    bridge = s.bridge

    def executed_then_down(fn):
        if fn == "run_agent":
            bridge.down = True                                         # 执行了，然后连接彻底断开
            return True
        return False
    bridge.after = executed_then_down
    rep = s.run_agent(SCALE.format(v=1.9), "断开期间")
    bridge.after = None
    assert rep.status == "unknown" and "结果没收到" in rep.text
    app.set("Leaf_1", "scale", 3.0)                                    # 人在这期间改了
    assert not s.poll()                                                # 查清之前不记
    blocked = s.run_agent("scene.set('Leaf_2', 'scale', 1.1)", "又一次")
    assert blocked.status == "refused" and "还没收到" in blocked.text
    s.bridge.down = False
    s.poll()                                                           # 先查清（AI 的那次），再记人的
    assert _author(s, "Trunk", "scale") == ("agent", None) and _val(app, "Trunk", "scale") == 1.9
    assert _author(s, "Leaf_1", "scale")[0] == "yuan"
    nxt = s.run_agent("scene.set('Leaf_2', 'scale', 1.1)", "连上之后")
    assert nxt.status == "ok" and "你之前有一次执行的结果没收到" in nxt.text


def test_a_restarted_app_turns_the_difference_into_unattributed_changes():
    app = FakeApp(unique_names=True)
    bridge = Flaky(app)
    s = SharedSession(Runtime(), bridge)
    s.start()
    s.run_agent(sf.ai_build(), "build")
    real = app.run_agent

    def run_then_restart(args):
        out = real(args)
        app.restart()                                                   # 执行完，Blender 崩了重新打开（场景存过盘）
        return out
    app.run_agent = run_then_restart
    bridge.after = lambda fn: fn == "run_agent"
    rep = s.run_agent(SCALE.format(v=1.7), "崩了")
    app.run_agent, bridge.after = real, None
    assert rep.status == "unknown" and "查不回来" in rep.text and "说不准" in rep.alert
    assert _author(s, "Trunk", "scale") == ("external:unattributed", "external:unattributed")
    assert [a["kind"] for a in rep.actions] == ["release"]
    kept = s.run_agent(SCALE.format(v=2.0), "想改")
    assert _val(app, "Trunk", "scale") == 1.7 and "来源不明" in kept.text
    app.request("release", s.find("Trunk"), "Trunk")                   # 人在提醒里点了"交还"
    s.poll()
    assert s.run_agent(SCALE.format(v=2.0), "交还之后").conflicts == [] and _val(app, "Trunk", "scale") == 2.0


def test_a_tool_call_whose_finish_was_lost_is_finished_on_requery():
    app, s = _scene(lambda a: Flaky(a))
    s.capabilities["around"] = True
    s.bridge.before = lambda fn: fn == "finish_agent"
    rep, _ = s.run_tool(lambda: app.set("Trunk", "scale", 1.8), label="工具", targets=[s.find("Trunk")])
    assert rep.status == "unknown"
    s.bridge.before = None
    s.poll()
    assert _author(s, "Trunk", "scale") == ("agent", None) and not s.rt.in_doubt


def test_the_same_permit_is_never_executed_twice():
    app, s = _scene()
    args = s._args(code="scene.set('Trunk', 'scale', scene.get('Trunk', 'scale') + 1)", protected={},
                   permit_id="permit-x")
    first = app.run_agent(dict(args))
    second = app.run_agent(dict(args))
    assert _val(app, "Trunk", "scale") == 2.0 and second.get("replayed") and not first.get("replayed")


# ---------------------------------------------------------------------------
# 14.3 账本落盘
# ---------------------------------------------------------------------------
def test_the_ledger_survives_a_restart(tmp_path):
    ledger = str(tmp_path / "ledger.json")
    app, s = _scene(ledger=ledger)
    sf._move_leaf3(app)
    s.poll()
    s2 = SharedSession(Runtime(), FakeBridge(app))                     # 代理重启：新的运行时，同一个应用
    s2.start(ledger=ledger)
    rep = s2.run_agent("scene.set('Leaf_3', 'location', [0, 0, 1.6])", "重启之后")
    assert rep.conflicts and rep.conflicts[0]["owner"] == "human" and _val(app, "Leaf_3", "location")[2] == 2.2
    assert s2.start_alert == "" and check_ledger(s2.rt, s2.scene_records(), s2.granularity) == []


def test_changes_while_rightofway_was_not_running_are_protected(tmp_path):
    ledger = str(tmp_path / "ledger.json")
    app, s = _scene(ledger=ledger)
    app.set("Trunk", "scale", 2.5)                                     # 代理没运行期间
    s2 = SharedSession(Runtime(), FakeBridge(app))
    s2.start(ledger=ledger)
    assert "Trunk" in s2.start_alert and _author(s2, "Trunk", "scale")[1] == "external:offline"
    rep = s2.run_agent(SCALE.format(v=1.0), "想改回去")
    assert _val(app, "Trunk", "scale") == 2.5 and "RightOfWay 没运行期间" in rep.text


def test_an_execution_interrupted_by_a_crash_is_settled_after_restart(tmp_path):
    ledger = str(tmp_path / "ledger.json")
    app, s = _scene(ledger=ledger)
    real = app.run_agent

    def run_then_crash(args):
        real(args)
        raise KeyboardInterrupt                                        # 代理在等结果时被关掉
    app.run_agent = run_then_crash
    with pytest.raises(KeyboardInterrupt):
        s.run_agent(SCALE.format(v=1.7), "执行到一半")
    app.run_agent = real
    s2 = SharedSession(Runtime(), FakeBridge(app))
    s2.start(ledger=ledger)
    assert _author(s2, "Trunk", "scale") == ("agent", None) and not s2.rt.in_doubt
    assert s2.start_alert == ""
    assert "你之前有一次执行的结果没收到" in s2.run_agent("scene.set('Leaf_1', 'scale', 1.1)", "之后").text


# ---------------------------------------------------------------------------
# 14.4 人删掉的，人交还之前不能补回
# ---------------------------------------------------------------------------
def test_release_in_the_app_lets_the_agent_rebuild():
    app, s = _scene()
    rock = s.find("Rock_2")
    app.delete("Rock_2")
    s.poll()
    rep = s.run_agent(REBUILD, "清空重建")
    assert rep.recreate_blocked == ["Rock_2"] and "建回来" in rep.alert
    assert any(a["kind"] == "release" and a["id"] == rock for a in rep.actions)
    again = s.run_agent(REBUILD, "再一次")
    assert again.recreate_blocked == ["Rock_2"] and "建回来" not in again.alert      # 同一件事只提醒一次
    app.request("release", rock, "Rock_2")
    s.poll()
    third = s.run_agent(REBUILD, "交还之后")
    assert third.recreate_blocked == [] and len(_named(app, "Rock_2")) == 1 and "交还" in third.text
    assert check_session(s, third) == []


def test_a_material_deleted_while_in_use_is_held():
    app, s = _scene()
    app.delete("LeafMat")                                              # 人删掉一个正在用的材质
    s.poll()
    rep = s.run_agent("scene.create('LeafMat', {'color': '红'}, kind='MATERIAL', data=True)", "建回来")
    assert rep.recreate_blocked and not _named(app, "LeafMat")


def test_a_material_that_vanished_after_losing_all_users_is_not_held():
    app, s = _scene()
    app.add("SpareMat", {"color": "灰"}, kind="MATERIAL", data=True)  # 没有对象用它
    s.poll()
    s.observe()                                                        # AI 看到了它
    app.delete("SpareMat")                                             # 清理没人用的材质
    s.poll()
    rep = s.run_agent("scene.create('SpareMat', {'color': '灰'}, kind='MATERIAL', data=True)", "新建")
    assert rep.recreate_blocked == [] and len(_named(app, "SpareMat")) == 1


# ---------------------------------------------------------------------------
# 14.5 认回是推断，人可以撤销
# ---------------------------------------------------------------------------
def test_the_human_can_split_an_inferred_match():
    app, s = _scene()
    leaf3 = s.find("Leaf_3")
    sf._move_leaf3(app)
    s.poll()
    human_loc = _val(app, "Leaf_3", "location")
    rep = s.run_agent(REBUILD, "清空重建")
    assert "Leaf_3" in rep.reidentified and "推断" in rep.text and "不是同一个" in rep.alert
    offer = next(a for a in rep.actions if a["kind"] == "unmatch" and a["id"] == leaf3)
    ai_loc = rep.after_raw[leaf3]["values"]["location"]
    app.request("unmatch", leaf3, "Leaf_3", permit=offer["permit"])  # 人点了"不是同一个"
    s.poll()
    leaves = sorted(_named(app, "Leaf_3") + _named(app, "Leaf_3.001"), key=lambda o: o["name"])
    assert len(leaves) == 2
    assert app.objects[leaf3]["faces"]["location"] == human_loc        # 原来的：执行前的样子
    other = next(cid for cid, o in app.objects.items() if o["name"] == "Leaf_3.001")
    assert other != leaf3 and _show(app.objects[other]["faces"]["location"]) == ai_loc     # AI 的那个
    assert any(e.get("undone") for e in s.rt.identity_log if e["object"] == leaf3)
    assert check_ledger(s.rt, s.scene_records(), s.granularity) == []
    nxt = s.run_agent("scene.set('Trunk', 'scale', 1.1)", "之后")
    assert "不是你删掉又新建的那个" in nxt.text


def test_inference_never_loosens_protection():
    """有没有认回，人改过的值最后都一样；认回只决定"是不是两份"。"""
    finals = []
    for reidentify in (True, False):
        app, s = _scene(rt=Runtime(reidentify=reidentify))
        sf._move_leaf3(app)
        s.poll()
        human = _val(app, "Leaf_3", "location")
        s.run_agent(REBUILD, "清空重建")
        finals.append([o["faces"]["location"] for o in app.objects.values()
                       if o["name"].startswith("Leaf_3") and o["faces"]["location"] == human])
    assert all(len(f) == 1 for f in finals)                            # 两种情况下，人放的那片叶子都在原处


# ---------------------------------------------------------------------------
# 在真的 Blender 里（需要 bpy）
# ---------------------------------------------------------------------------
def _blender(**kw):
    bpy = pytest.importorskip("bpy")
    from rightofway.blender.bridge import InProcessBridge
    from experiments import scenario_tree as sc
    InProcessBridge.reset()
    br = InProcessBridge()
    s = SharedSession(Runtime(**kw), br)
    s.start()
    s.run_agent(sc.ai_build(), "build")
    return bpy, br, s, sc


def test_blender_lost_result_is_queried_back():
    bpy, br, s, sc = _blender()
    real = br.call

    def lossy(fn, args=None):
        out = real(fn, args)
        if fn == "run_agent":
            raise BridgeError("超时")
        return out
    br.call = lossy
    try:
        rep = s.run_agent("import bpy\nbpy.data.objects['Trunk'].scale = (1.5, 1.5, 1.5)\n", "丢了结果")
    finally:
        br.call = real
    assert rep.status == "ok" and "查清了" in rep.text and tuple(bpy.data.objects["Trunk"].scale) == (1.5, 1.5, 1.5)
    unit = s.rt.objects[f"{s.find('Trunk')}#scale"]
    assert unit.versions[-1].author_id == "agent" and unit.human_touched is None


def test_blender_agent_code_cannot_forge_a_release():
    bpy, br, s, sc = _blender()
    br.execute(sc.human_edit("delete", "Rock_2"))
    s.poll()
    rock = next(u.split("#")[0] for u, o in s.rt.objects.items()
                if isinstance(o.content, dict) and o.content.get("name") == "Rock_2")
    forge = ("import sys\nsys.modules['_rightofway_state'].requests.append("
             f"{{'kind': 'release', 'id': {rock!r}, 'name': 'Rock_2'}})\n")
    rep = s.run_agent(forge, "伪造交还")
    assert "撤销" in rep.text and "替你交还" in rep.alert
    assert s.rt.objects[f"{rock}#location"].human_touched is not None  # 墓碑还在
    rebuild = s.run_agent(sc.ai_rebuild(), "清空重建")
    assert rebuild.recreate_blocked == ["Rock_2"] and "Rock_2" not in bpy.data.objects


def test_blender_human_can_split_an_inferred_match():
    bpy, br, s, sc = _blender()
    leaf3 = s.find("Leaf_3")
    br.execute(sc.human_edit("move", "Leaf_3"))
    s.poll()
    human_z = round(bpy.data.objects["Leaf_3"].location.z, 4)
    rep = s.run_agent(sc.ai_rebuild(), "清空重建")
    offer = next(a for a in rep.actions if a["kind"] == "unmatch" and a["id"] == leaf3)
    assert rep.outcome == "committed" and "不是同一个" in rep.alert
    br.execute("import sys\nsys.modules['_rightofway_state'].requests.append("        # 人点了"不是同一个"
               f"{{'kind': 'unmatch', 'id': {leaf3!r}, 'permit': {offer['permit']!r}, 'name': 'Leaf_3'}})\n")
    s.poll()
    leaves = sorted(o.name for o in bpy.data.objects if o.name.startswith("Leaf_3"))
    assert leaves == ["Leaf_3", "Leaf_3.001"]
    orig = next(o for o in bpy.data.objects if o.get("rightofway_id") == leaf3)
    other = bpy.data.objects["Leaf_3.001"] if orig.name == "Leaf_3" else bpy.data.objects["Leaf_3"]
    assert round(orig.location.z, 4) == human_z and other.get("rightofway_id") not in (None, leaf3)
    assert orig.data is not other.data                                 # 各自的网格
    assert check_ledger(s.rt, s.scene_records(), s.granularity) == []
