"""方式一、方式二里不依赖 Blender 的部分：比较记录、推断基准、规划合并、和插件通信。
这些测试在任何 Python 上都能跑。"""
import json
import socket
import threading

import pytest

from rightofway.blender.bridge import MARK_BEGIN, MARK_END, BridgeError, SocketBridge
from rightofway.blender.filemerge import MergeVersion, check_result, infer_base, plan_merge
from rightofway.blender.records import diff_records, units

# 面的名字由观察那一层决定，运行时不认识任何一个。这里随便取几个，和 Blender 的属性名一样
FACES = ("location", "scale", "data", "material_slots", "modifiers", "name")
from rightofway.blender.shared_session import SharedSession
from rightofway.model import OpStatus
from rightofway.runtime import Runtime


def rec(cid, name, cfp, **aspects):
    """cfp 是整体内容；aspects 可以单独指定某几个面的指纹，没指定的面都等于 "base"。"""
    asp = {a: "base" for a in FACES}
    asp.update(aspects)
    asp["name"] = name
    if not aspects:
        asp["location"] = cfp           # 没指定时，把整体变化记在"位置"这个面上
    return {"id": cid, "name": name, "cfp": cfp, "fp": name + ":" + cfp, "collections": ["Scene Collection"],
            "parent": None, "editing": False, "type": "MESH", "aspects": asp}


def recs(*items):
    return {r["id"]: r for r in items}


# ---------------------------------------------------------------------------
def test_diff_records_finds_created_deleted_modified():
    old = recs(rec("a", "A", "1"), rec("b", "B", "1"))
    new = recs(rec("a", "A", "2"), rec("c", "C", "1"))
    d = diff_records(old, new)
    assert set(d.created) == {"c"} and set(d.deleted) == {"b"} and set(d.modified) == {"a"}
    assert diff_records(old, old).ids == set()


def test_rename_counts_as_change():
    d = diff_records(recs(rec("a", "A", "1")), recs(rec("a", "A2", "1")))
    assert set(d.modified) == {"a"}


# ---------------------------------------------------------------------------
# 推断"这份文件是从第几版打开的"
# ---------------------------------------------------------------------------
def _history():
    v0 = MergeVersion(0, "v0", recs(rec("x", "X", "0")))
    v1 = MergeVersion(1, "v1", recs(rec("x", "X", "0"), rec("t", "Trunk", "ai1")), from_ai={"t"})
    v2 = MergeVersion(2, "v2", recs(rec("x", "X", "h1"), rec("t", "Trunk", "ai1")), from_human={"x"})
    v3 = MergeVersion(3, "v3", recs(rec("x", "X", "h1"), rec("t", "Trunk", "ai2")), from_ai={"t"})
    return [v0, v1, v2, v3]


def test_infer_base_human_who_loaded_v1_and_saved():
    h = recs(rec("x", "X", "h1"), rec("t", "Trunk", "ai1"))
    # 有第 1 版里 AI 的修改（Trunk=ai1）；第 2 版没有 AI 的修改，顺延；第 3 版 AI 的修改（ai2）没有
    assert infer_base(_history(), h, other="ai") == 2


def test_infer_base_human_who_reloaded_latest():
    h = recs(rec("x", "X", "h1"), rec("t", "Trunk", "ai2"))
    assert infer_base(_history(), h, other="ai") == 3


def test_infer_base_ai_that_never_reloaded():
    a = recs(rec("x", "X", "0"), rec("t", "Trunk", "ai-new"))
    # AI 那一份里没有第 2 版人的修改（X=h1），所以停在第 1 版
    assert infer_base(_history()[:3], a, other="human") == 1


# ---------------------------------------------------------------------------
# 规划合并
# ---------------------------------------------------------------------------
def test_plan_human_wins_ai_only_taken_and_stale_parts_kept():
    current = recs(rec("x", "X", "0"), rec("t", "Trunk", "ai1"), rec("r", "Rock", "0"))
    human = recs(rec("x", "X", "h"), rec("t", "Trunk", "old"), rec("r", "Rock", "0"))   # 人这一份是旧的：Trunk 还是 old
    ai = recs(rec("x", "X", "a"), rec("t", "Trunk", "ai1"), rec("r", "Rock", "a"))
    h_diff = diff_records(recs(rec("x", "X", "0"), rec("t", "Trunk", "old"), rec("r", "Rock", "0")), human)
    a_diff = diff_records(recs(rec("x", "X", "0"), rec("t", "Trunk", "ai1"), rec("r", "Rock", "0")), ai)
    p = plan_merge(current, human, ai, h_diff, a_diff, protected={"x"}, keep_candidates=True)
    assert p.result == {"x": "human", "r": "ai", "t": "current"}
    assert p.rejected == ["x"] and p.candidates == ["x"]
    took = {(t["id"], t["source"], t["candidate"]) for t in p.take}
    assert took == {("x", "ai", True), ("r", "ai", False), ("t", "current", False)}
    assert p.expected["t"]["cfp"] == "ai1"      # 旧文件里没改的 Trunk 不会覆盖 AI 的新版本


def test_plan_deletions_both_sides():
    base = recs(rec("a", "A", "0"), rec("b", "B", "0"))
    human = recs(rec("b", "B", "0"))                 # 人删了 A
    ai = recs(rec("a", "A", "0"))                    # AI 删了 B
    p = plan_merge(base, human, ai, diff_records(base, human), diff_records(base, ai), set(), False)
    assert p.expected == {"a": None, "b": None}
    assert p.delete == ["b"]


def test_plan_ai_change_to_object_human_deleted_is_rejected():
    base = recs(rec("r", "Rock_2", "0"))
    human = {}
    ai = recs(rec("r", "Rock_2", "moved"))
    p = plan_merge(base, human, ai, diff_records(base, human), diff_records(base, ai), {"r"}, False)
    assert p.rejected == ["r"] and p.expected == {"r": None} and not p.take


def test_check_result_reports_problems():
    expected = {"a": rec("a", "A", "1"), "b": None, "c": rec("c", "C", "1")}
    final = recs(rec("a", "A", "2"), rec("b", "B", "1"), rec("c", "C.001", "1"), rec("z", "Z", "1"))
    errors, renamed = check_result(expected, final)
    assert len(errors) == 3 and renamed == ["C → C.001"]


# ---------------------------------------------------------------------------
# 方式一的记账：用一个假的 Blender 返回固定结果
# ---------------------------------------------------------------------------
class FakeBridge:
    def __init__(self):
        self.world = {}
        self.next = None
        self.sent = []

    def call(self, fn, args):
        self.sent.append((fn, args))
        if fn == "probe":                       # 连接时的自测：这个假接入不会认回同一个对象
            return {"names": ["X", "X"], "identity": False}
        if fn == "poll":
            return {"records": dict(self.world)}
        result, self.next = self.next, None
        self.world = dict(result["after"])
        return result


def test_shared_session_accounting():
    br = FakeBridge()
    rt = Runtime()
    s = SharedSession(rt, br, granularity="object")
    br.world = recs(rec("a", "A", "0"), rec("b", "B", "0"))
    s.start()
    br.world = recs(rec("a", "A", "h"), rec("b", "B", "0"))   # 人改了 A
    assert set(s.poll().modified) == {"a"}
    assert s.protected_ids() == ["a"]
    # AI 的脚本改了 A 和 B，新建了 C；A 被恢复
    before = dict(br.world)
    raw = recs(rec("a", "A", "ai"), rec("b", "B", "ai"), rec("c", "C", "0"))
    after = recs(rec("a", "A", "h"), rec("b", "B", "ai"), rec("c", "C", "0"))
    br.next = {"status": "ok", "before": before, "after_raw": raw, "after": after, "human_since": [],
               "protected": ["a"], "restored": ["a"], "inexact": [], "renamed": [], "dangling": [],
               "candidates": [], "ai_created": ["c"], "error": None, "stdout": "", "undo_pushed": False,
               "seconds": 0.01}
    rep = s.run_agent("print('x')")
    assert br.sent[-1][1]["protected"] == {"a": ["*"]}
    assert sorted(rep.committed) == ["b", "c"]
    assert rep.skipped == [{"id": "a", "name": "A", "reason": "human"}]      # 人改过、正在改：原因统一报告为"人"
    assert rt.objects["b"].content["cfp"] == "ai" and rt.objects["a"].content["cfp"] == "h"
    assert rt.objects["a"].human_touched is not None
    # 交还之后，A 不再受保护
    s.hand_back("a")
    assert s.protected_ids() == []


def test_shared_session_human_change_right_before_agent_is_attributed_to_human():
    br = FakeBridge()
    rt = Runtime()
    s = SharedSession(rt, br, granularity="object")
    br.world = recs(rec("a", "A", "0"))
    s.start()
    before = recs(rec("a", "A", "h"))                    # 上次 poll 之后人刚改了 A
    br.next = {"status": "ok", "before": before, "after_raw": before, "after": before, "human_since": ["a"],
               "protected": ["a"], "restored": [], "inexact": [], "renamed": [], "dangling": [],
               "candidates": [], "ai_created": [], "error": None, "stdout": "", "undo_pushed": False,
               "seconds": 0.01}
    rep = s.run_agent("pass")
    assert set(rep.human_since.modified) == {"a"}
    assert rt.objects["a"].versions[-1].author_id == "yuan"


# ---------------------------------------------------------------------------
# 和 Blender MCP 插件通信：用一个假的插件测试收发格式
# ---------------------------------------------------------------------------
def _fake_addon(reply: dict, split: bool = False):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got = {}

    def serve():
        conn, _ = srv.accept()
        buf = b""
        while True:
            buf += conn.recv(65536)
            try:
                got["request"] = json.loads(buf)
                break
            except json.JSONDecodeError:
                continue
        data = json.dumps(reply).encode()
        if split:                                    # 插件的回复没有分隔符，可能分几次到达
            conn.sendall(data[:10])
            conn.sendall(data[10:])
        else:
            conn.sendall(data)
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return port, got


def test_socket_bridge_roundtrip():
    out = MARK_BEGIN + json.dumps({"records": {}}) + MARK_END
    port, got = _fake_addon({"status": "success", "result": {"executed": True, "result": out}}, split=True)
    res = SocketBridge(port=port, timeout=5).call("poll", {})
    assert res == {"records": {}}
    assert got["request"]["type"] == "execute_code"
    assert "_rightofway_out(poll(" in got["request"]["params"]["code"]


def test_socket_bridge_error_status():
    port, _ = _fake_addon({"status": "error", "message": "boom"})
    with pytest.raises(BridgeError, match="boom"):
        SocketBridge(port=port, timeout=5).call("poll", {})


def test_socket_bridge_not_running():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(BridgeError, match="连不上"):
        SocketBridge(port=port, timeout=2).execute("pass")


def test_shared_session_aspects_human_moved_ai_recolored():
    """按面：人挪了 A（位置），AI 改了 A 的材质和位置。材质生效，位置保留人的。"""
    br = FakeBridge()
    rt = Runtime()
    s = SharedSession(rt, br)                                     # 默认按面
    br.world = recs(rec("a", "A", "0", location="t0", material_slots="m0"))
    s.start()
    br.world = recs(rec("a", "A", "1", location="t-human", material_slots="m0"))
    s.poll()
    assert s.protected() == {"a": ["location"]}
    before = dict(br.world)
    raw = recs(rec("a", "A", "2", location="t-ai", material_slots="m-ai"))
    after = recs(rec("a", "A", "3", location="t-human", material_slots="m-ai"))
    br.next = {"status": "ok", "before": before, "after_raw": raw, "after": after, "restored": [],
               "merged": {"a": {"restored": ["location"], "kept": ["material_slots"]}}, "inexact": [], "renamed": [],
               "dangling": [], "candidates": [], "ai_created": [], "error": None, "stdout": "",
               "undo_pushed": False, "seconds": 0.01}
    rep = s.run_agent("pass")
    assert rep.committed == ["a#material_slots"]
    assert [x["id"] for x in rep.skipped] == ["a#location"]
    assert rep.partial == {"A": {"kept": ["material_slots"], "restored": ["location"]}}
    assert "部分生效：A" in rep.text


def test_plan_aspects_merges_different_parts_of_same_object():
    base = recs(rec("x", "Leaf", "0", location="t0", material_slots="m0"))
    human = recs(rec("x", "Leaf", "h", location="t-h", material_slots="m0"))
    ai = recs(rec("x", "Leaf", "a", location="t0", material_slots="m-ai"))
    hd = diff_records(units(base, "aspect"), units(human, "aspect"))
    ad = diff_records(units(base, "aspect"), units(ai, "aspect"))
    p = plan_merge(base, human, ai, hd, ad, protected={"x#location"}, keep_candidates=False, granularity="aspect")
    assert p.result["x#location"] == "human" and p.result["x#material_slots"] == "ai"
    assert p.take == [{"id": "x", "source": "ai", "name": "Leaf", "collections": ["Scene Collection"],
                       "candidate": False, "aspects": ["material_slots"], "record": ai["x"]}]
    assert not p.rejected


def test_plan_aspects_same_part_conflict_keeps_human():
    base = recs(rec("x", "Leaf", "0", location="t0"))
    human = recs(rec("x", "Leaf", "h", location="t-h"))
    ai = recs(rec("x", "Leaf", "a", location="t-ai"))
    hd = diff_records(units(base, "aspect"), units(human, "aspect"))
    ad = diff_records(units(base, "aspect"), units(ai, "aspect"))
    p = plan_merge(base, human, ai, hd, ad, protected={"x#location"}, keep_candidates=False, granularity="aspect")
    assert p.rejected == ["x#location"] and p.take == [] and p.expected["x#location"]["fp"] == "t-h"


def test_plan_ai_cannot_delete_object_human_partly_changed():
    base = recs(rec("x", "Leaf", "0", location="t0"))
    human = recs(rec("x", "Leaf", "h", location="t-h"))
    ai = {}
    hd = diff_records(units(base, "aspect"), units(human, "aspect"))
    ad = diff_records(units(base, "aspect"), units(ai, "aspect"))
    p = plan_merge(base, human, ai, hd, ad, protected={"x#location"}, keep_candidates=False, granularity="aspect")
    assert p.delete == [] and all(v is not None for v in p.expected.values())
    assert len(p.rejected) == len(FACES)
