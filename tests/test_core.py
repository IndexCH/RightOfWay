"""核心（rightofway/core，spec/core.md）：判断表、意图层的出口、按位置的容器、存盘，以及一致性测试。

一致性测试（rightofway/conformance.py）：同一套和媒介无关的场景，在每个应用侧上跑，结果分三档；
随机的差分测试：同一串随机操作，应用侧的结果和一个独立写的判断表模型比，每一步都查三条不变量。
"""
import json
import os

import pytest

from rightofway import conformance as C
from rightofway.core import rules as R
from rightofway.core.positional import PositionalLedger
from rightofway.core.store import AppendLog


# ---------------------------------------------------------------------------
# 判断表（spec/core.md 第 3 节）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("last,agent,seen,want", [
    (R.Last(None), "a1", None, None),                                             # 没人改过（基线）
    (R.Last(R.BASELINE, stamp=0), "a1", -1, None),
    (R.Last("yuan", R.HUMAN, 3, held="yuan"), "a1", 9, R.KEEP_HUMAN),            # 人改的、还没交还：看过也不能改
    (R.Last("external:offline", R.EXTERNAL, 3, held="external:offline"), "a1", 9, R.KEEP_HUMAN),  # 来源不明
    (R.Last("yuan", R.HUMAN, 3, held=None), "a1", -1, None),                    # 已经交还：没看过也能改
    (R.Last("a1", R.AGENT, 3), "a1", -1, None),                                  # 它自己
    (R.Last("a2", R.AGENT, 3), "a1", 2, R.KEEP_OTHER_AGENT),                     # 别的 AI：没看过这一版
    (R.Last("a2", R.AGENT, 3), "a1", 3, None),                                   # 看过了
    (R.Last(R.GIT, None, 3), "a1", 2, R.KEEP_OTHER_AGENT),                       # git 和别的 AI 一样
    (R.Last("a1", R.AGENT, 3, held="yuan"), "a1", 9, R.KEEP_HUMAN),             # 违规改了人的：仍然归人
])
def test_the_judgment_table(last, agent, seen, want):
    got = R.judge(last, agent, seen)
    assert (got or {}).get("reason") == want
    if want == R.KEEP_HUMAN and last.held.startswith("external:"):
        assert got["source"] == last.held
    if want == R.KEEP_OTHER_AGENT:
        assert got["by"] == last.author


def test_a_tombstone_is_judged_by_who_deleted_it():
    """AI 违规删掉了人改过的东西：单元仍归人，但删掉它的是这个 AI——它能不能建回来按删的人算。"""
    assert R.judge_tombstone(R.Last("a1", R.AGENT, 4, held="yuan"), "a1", 4) is None
    assert R.judge_tombstone(R.Last("yuan", R.HUMAN, 4, held="yuan"), "a1", 9)["reason"] == R.KEEP_HUMAN


def test_temporary_blocks_and_the_gate():
    assert R.temporary("a1", selected=True)["reason"] == R.KEEP_SELECTED
    assert R.temporary("a1", reserved=("a2", 1e12))["reason"] == R.KEEP_RESERVED
    assert R.temporary("a2", reserved=("a2", 1e12)) is None                      # 留给它自己的
    assert R.temporary("a1", reserved=("a2", 0.0)) is None                       # 到期了
    assert R.temporary("a1", occupied="yuan")["reason"] == R.KEEP_HUMAN
    assert R.gate("a1", paused=True)["reason"] == "paused"
    assert R.gate("a1", in_doubt={"p1": {"agent": "a2"}})["reason"] == "in_doubt"
    assert R.gate("a1", suspended={"a1": {"objects": []}})["reason"] == "suspended"
    assert R.gate("a1", suspended={"a2": {}}) is None


def test_the_intent_layer_can_only_tighten():
    keep = {"x#location": {"reason": R.KEEP_HUMAN}}

    def layer(agent, k):                    # 想放松（去掉 x）、改原因、再多保持 y
        return {"x#location": {"reason": "intent"}, "y#color": {"reason": "intent"}}
    out = R.tighten("a1", keep, layer)
    assert out["x#location"] == {"reason": R.KEEP_HUMAN} and out["y#color"] == {"reason": "intent"}
    assert R.tighten("a1", keep, lambda a, k: {}) == keep and R.tighten("a1", keep, None) == keep


def test_the_runtime_uses_the_intent_layer():
    from rightofway import ActorKind
    from rightofway.runtime import Runtime
    rt = Runtime(tighten=lambda agent, keep: {"y#v": {"reason": "intent"}})
    rt.register_actor("yuan", ActorKind.HUMAN)
    rt.register_actor("a1", ActorKind.AGENT)
    for oid in ("x#v", "y#v"):
        rt.create_object(oid, "blob", {"fp": "0"}, "a1")
    rt.mark_seen("a1")
    rt.human_edit("yuan", "x#v", {"fp": "1"})
    p = rt.admit("a1")
    assert set(p.keep) == {"x#v", "y#v"} and p.keep["x#v"]["reason"] == R.KEEP_HUMAN


@pytest.mark.parametrize("kw,want", [
    (dict(hits=True, quick=True, from_reference=False, deleted=False, lost=False), ("agent", True, False)),
    (dict(hits=False, quick=True, from_reference=False, deleted=False, lost=False), ("agent", False, False)),
    (dict(hits=True, quick=False, from_reference=False, deleted=False, lost=False), ("unattributed", False, True)),
    (dict(hits=True, quick=False, from_reference=True, deleted=False, lost=False), ("agent", True, False)),
    (dict(hits=True, quick=False, from_reference=False, deleted=True, lost=False), ("agent", True, False)),
    (dict(hits=True, quick=True, from_reference=False, deleted=False, lost=True), ("unattributed", False, True)),
    (dict(hits=True, quick=True, from_reference=False, deleted=False, lost=True, human_only=True), ("agent", True, False)),
])
def test_settling_a_non_atomic_execution(kw, want):
    d = R.settle_window(**kw)
    assert (d.author, d.restore, d.backup_before) == want


# ---------------------------------------------------------------------------
# 按位置的容器不认识"行"；存盘
# ---------------------------------------------------------------------------
def test_marks_follow_characters():
    led = PositionalLedger()
    led.baseline("doc", {})
    import difflib
    old, new = "abc", "aXbc"
    ops = difflib.SequenceMatcher(None, old, new).get_opcodes()
    m = led.record("doc", ops, "yuan", len(old))
    assert led.owners("doc", 4) == [None, m, None, None]
    ops2 = difflib.SequenceMatcher(None, new, "aXc").get_opcodes()           # 删掉 b
    m2 = led.record("doc", ops2, "yuan", len(new), describe=lambda i, j: list(new[i:j]))
    assert led.containers["doc"]["points"][0]["text"] == ["b"]
    hits = lambda a, b: led.hits("doc", difflib.SequenceMatcher(None, a, b).get_opcodes(), "a1", len(a))  # noqa: E731
    assert set(hits("aXc", "ZXc")) == set()                                   # 改别的字符：不算碰到
    assert set(hits("aXc", "aYc")) == {m, m2}                                 # 改人写的 X（它后面紧挨着人删 b 的地方）
    assert set(hits("aXc", "aXbc")) == {m2}                                   # 在人删掉的地方写回去


def test_the_append_log_replays_to_the_same_state(tmp_path):
    path = str(tmp_path / "led.jsonl")
    log = AppendLog(path, compact_lines=3)
    led = PositionalLedger()
    led.baseline("f", {"sha": "0"})
    log.write_snapshot(led.snapshot())
    for k in range(5):
        led.agent(f"a{k}")
        led.record("f", [("insert", 0, 0, 0, 1)], "yuan", k, entry={"sha": str(k)})
        if log.due():
            log.write_snapshot(led.snapshot())
        else:
            log.append(led.delta())
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"t": "d", "containers": {"f": ')                         # 存到一半退出：最后一行没写完
    kind, head, deltas = AppendLog(path).read()
    again = PositionalLedger({k: v for k, v in head.items() if k != "t"})
    for d in deltas:
        again.apply_delta(d)
    assert json.dumps(again.state, sort_keys=True) == json.dumps(led.state, sort_keys=True)


def test_the_code_ledger_reloads_to_the_same_state(tmp_path):
    from test_code import Harness
    h = Harness(tmp_path)
    h.read("app.py")
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    h.edit("app.py", "x * 0.2", "x * 0.25")
    h.prompt("交还 app.py:2")
    led = h.ledger()
    with open(os.path.join(led.dir, "state.json"), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert json.loads(lines[0])["t"] == "snap" and len(lines) > 1             # 追加写的日志
    snap = {k: v for k, v in json.loads(lines[0]).items() if k != "t"}
    re = PositionalLedger(snap)
    for line in lines[1:]:
        re.apply_delta(json.loads(line))
    assert json.dumps(re.state, sort_keys=True) == json.dumps(led.state, sort_keys=True)


def test_an_old_whole_file_code_ledger_is_still_read(tmp_path):
    from rightofway.code.ledger import CodeLedger
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "a.txt").write_text("x\n")
    led = CodeLedger(str(repo), str(tmp_path / "home"))
    os.makedirs(led.dir)
    old = {"format": 1, "root": led.root, "n": 1, "head": None, "initialized": True,
           "files": {"a.txt": {"sha": None, "git": None, "stat": None, "version": 1, "owners": [], "points": []}},
           "marks": {}, "agents": {}, "pending": {"t1": {"kind": "bash", "agent": "a", "time": 0, "until": 0}},
           "notes": {}, "log": []}
    with open(os.path.join(led.dir, "state.json"), "w") as fh:
        json.dump(old, fh)
    with led.open():
        assert "a.txt" in led.files and led.pending["t1"]["kind"] == "command"
        led.pending.clear()
        led.core.touch("permits", "t1")
    with open(os.path.join(led.dir, "state.json"), encoding="utf-8") as fh:
        assert json.loads(fh.readline())["t"] == "snap"                       # 下一次存盘整份重写成新格式


# ---------------------------------------------------------------------------
# 一致性测试：每个应用侧，三档结果和能力报告
# ---------------------------------------------------------------------------
EXPECT = {"fake_no_restore_deleted": {"AI 删人改过的东西": "reported"}}
DRIVERS = ["fake", "fake_no_restore_deleted", "text", "chars", "cross", "blender"]


def _make(key):
    makers = C.drivers()
    if key not in makers:
        pytest.skip("需要 Python 3.11 + pip install bpy")
    return makers[key]


@pytest.mark.parametrize("key", DRIVERS)
def test_conformance(key):
    rep = C.run(_make(key))
    want = {t: EXPECT.get(key, {}).get(t, "protected") for t in C.SCENARIOS}
    assert rep["scenarios"] == want, rep["details"]
    assert rep["passed"] and set(rep["capabilities"]) == set(__import__("rightofway.core.interfaces", fromlist=["x"]).CAPABILITIES)


@pytest.mark.parametrize("key", DRIVERS)
def test_random_differential(key):
    make = _make(key)
    seeds = range(4) if key in ("text", "cross") else range(12)
    problems = [p for s in seeds for p in C.differential(make, s, steps=20)]
    assert problems == []


def test_the_capability_report_says_what_cannot_be_protected():
    rep = C.run(C.drivers()["fake_no_restore_deleted"], only=["AI 删人改过的东西"])
    assert rep["capabilities"]["restore_deleted"] is False
    assert rep["scenarios"] == {"AI 删人改过的东西": "reported"} and "暂停" in rep["details"]["AI 删人改过的东西"]


def test_a_hook_style_ai_side_drives_a_keyed_app(tmp_path):
    """跨组合：模拟的 Claude Code（hooks）调一个 MCP 工具改模拟应用里的对象。人改过的面：执行之后放回，告诉 AI；
    别的照常生效；应用侧拆不开工具调用（没有 around）时，宁可不执行。"""
    from rightofway.blender.hook_session import KeyedSession
    from rightofway.blender.shared_session import SharedSession
    from rightofway.code.fakeagent import FakeClaudeCode
    from rightofway.fakeapp import FakeApp, FakeBridge
    from rightofway.invariants import check_session
    from rightofway.runtime import Runtime
    app = FakeApp()
    app.add("Leaf", {"location": [0, 0, 1.4], "color": "green"})
    s = SharedSession(Runtime(), FakeBridge(app))
    s.start()
    ks = KeyedSession(s)
    cc = FakeClaudeCode(str(tmp_path), home=str(tmp_path / "home"), app=ks)
    app.set("Leaf", "location", [0, 0, 2.2])                                   # 人挪了叶子
    r = cc.tool("mcp__app__set", {"object": "Leaf", "location": [9, 9, 9], "color": "orange"},
                lambda: (app.set("Leaf", "location", [9, 9, 9]), app.set("Leaf", "color", "orange")) and None)
    assert r.status == "ok" and "人改过" in r.text
    faces = app.objects[app.find("Leaf")]["faces"]
    assert faces["location"] == [0, 0, 2.2] and faces["color"] == "orange"
    assert check_session(s) == []
    s.capabilities["around"] = False                                           # 应用侧拆不开：执行前就拒
    r = cc.tool("mcp__app__set", {"object": "Leaf", "color": "red"}, lambda: app.set("Leaf", "color", "red"))
    assert r.status == "denied" and faces["color"] == "orange"
