"""影子执行（试验，experiments/exp_s_shadow.py）：AI 的脚本先在另一个 Blender 里执行，只把允许的改动同步进人的场景。
起两个 bpy 本地进程，需要 Python 3.11 + bpy。"""
import pytest

pytest.importorskip("bpy")

from rightofway.blender import local_server  # noqa: E402
from rightofway.blender.bridge import SocketBridge  # noqa: E402
from experiments import exp_s_shadow as es  # noqa: E402


@pytest.fixture(scope="module")
def two_blenders():
    procs = [(9890, local_server.spawn(9890)), (9891, local_server.spawn(9891))]
    try:
        yield SocketBridge(port=9890), SocketBridge(port=9891)
    finally:
        for port, p in procs:
            local_server.shutdown(port, p)


@pytest.mark.parametrize("habit", ["targeted", "rebuild"])
def test_shadow_execution_keeps_the_human_edits_without_touching_the_live_scene(two_blenders, habit):
    hb, ab = two_blenders
    s = es.shadow(hb, ab, habit)
    assert (s["human_overwritten"], s["ai_lost"], s["dups"], s["forks"], s["I1"]) == (0, 0, 0, 0, 0)
    assert s["errors"] == []
    if habit == "rebuild":
        assert s["reidentified"] >= 9                  # 在影子里清空重建，也按同一个对象同步回来，不出现两份
