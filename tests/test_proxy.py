"""MCP 代理（rightofway/proxy，design_v0.5.md 第 9 节，prior_art_solutions.md 第 4 项）。

上游是一个假的"应用 MCP 服务器"（rightofway/proxy/fake_upstream.py）：它的工具直接改内存里的假应用，
仿照 BlenderMCP（一个执行代码的工具 + 几个类型化工具）。测试拿着同一个假应用，扮演人在界面里改东西。
需要 pip install mcp（没有就整个文件跳过）。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from rightofway.blender.shared_session import SharedSession  # noqa: E402
from rightofway.fakeapp import FakeApp  # noqa: E402
from rightofway.invariants import check_session  # noqa: E402
from rightofway.proxy import fake_upstream  # noqa: E402
from rightofway.proxy.client import SyncMCPClient  # noqa: E402
from rightofway.proxy.core import Proxy  # noqa: E402
from rightofway.proxy.sources import FakeSource, MCPBridge, detect_code_tool  # noqa: E402
from rightofway.runtime import Runtime  # noqa: E402
from experiments import scenario_fake as sf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def made():
    clients = []

    def make(**app_kw):
        app = FakeApp(**app_kw)
        client = SyncMCPClient.in_memory(fake_upstream.build(app))
        clients.append(client)
        s = SharedSession(Runtime(), MCPBridge(client, "execute_code", "code", FakeSource()))
        s.start()
        p = Proxy(s, client)
        assert not p.call_tool("execute_code", {"code": sf.ai_build()}).isError
        return app, p
    yield make
    for c in clients:
        c.close()


def _text(result):
    return "\n".join(c.text for c in result.content)


def _loc(app, name):
    return app.objects[app.find(name)]["faces"]["location"]


def test_code_tool_is_detected_and_goes_through_the_permit(made):
    app, p = made(unique_names=True)
    assert (p.code_tool, p.code_param) == ("execute_code", "code")
    sf.human_e(app)                                                       # 人在界面里把 Leaf_3 往上挪
    r = p.call_tool("execute_code", {"code": "scene.set('Leaf_3', 'location', [0, 0, 1.6])\nprint('done')"})
    assert r.isError and "脚本输出：\ndone" in _text(r) and "Leaf_3 的Location" in _text(r)
    assert _loc(app, "Leaf_3")[2] == 2.2
    conflicts = r.meta["rightofway"]["conflicts"]
    assert conflicts[0]["owner"] == "human" and conflicts[0]["now"].endswith("2.2)")
    assert check_session(p.s) == []


def test_read_tool_gets_what_the_agent_missed(made):
    app, p = made(unique_names=True)
    sf.human_a(app)
    r = p.call_tool("get_scene_info", {})
    assert not r.isError and '"Cube"' in r.content[0].text                # 上游自己的结果原样保留
    assert "人 删除了 Rock_2" in _text(r) and "【RightOfWay】" in _text(r)
    again = p.call_tool("get_scene_info", {})
    assert "【RightOfWay】" not in _text(again)                             # 已经看过了


def test_typed_write_on_a_human_face_is_put_back_after_the_call(made):
    app, p = made(unique_names=True)
    sf.human_e(app)
    r = p.call_tool("set_object_property", {"object_name": "Leaf_3", "property": "location", "value": [0, 0, 1.6]})
    assert r.content[0].text.startswith("Leaf_3.location")                 # 上游确实执行了
    assert r.isError and _loc(app, "Leaf_3")[2] == 2.2 and "以现在的值为准" in _text(r)
    ok = p.call_tool("set_object_property", {"object_name": "Rock_1", "property": "scale", "value": 2.0})
    assert not ok.isError and "【RightOfWay】" not in _text(ok)            # 没撞上：结果和上游一样
    assert check_session(p.s) == []


def test_unrecoverable_typed_delete_is_refused_before_the_call(made):
    """应用恢复不了删除（和 Unity 一样）：可能删除的工具碰到不许删的对象，执行前就拒绝，上游根本没被调用。"""
    app, p = made(restore_deleted=False, transfer_ids=False)
    sf.human_e(app)
    r = p.call_tool("delete_object", {"object_name": "Leaf_3"})
    assert r.isError and r.meta["rightofway"]["status"] == "blocked"
    assert any(o["name"] == "Leaf_3" for o in app.objects.values())
    trunk = p.call_tool("delete_object", {"object_name": "Trunk"})          # 祖先也不许删
    assert trunk.isError and "下面有人改过的对象" in _text(trunk)
    fine = p.call_tool("delete_object", {"object_name": "Rock_2"})
    assert not fine.isError and not any(o["name"] == "Rock_2" for o in app.objects.values())
    assert check_session(p.s) == []


def test_restorable_typed_delete_is_executed_and_put_back(made):
    app, p = made(unique_names=True)
    sf.human_e(app)
    r = p.call_tool("delete_object", {"object_name": "Leaf_3"})
    assert r.content[0].text == "deleted Leaf_3" and r.isError
    assert _loc(app, "Leaf_3")[2] == 2.2 and "不能删除" in _text(r)
    assert check_session(p.s) == []


def test_own_small_tools_refuse_before_execution(made):
    app, p = made(restore_deleted=False, transfer_ids=False)
    sf.human_e(app)
    r = p.call_tool("rightofway_set_property", {"object": "Leaf_3", "property": "location", "value": [0, 0, 0]})
    assert r.isError and r.meta["rightofway"]["conflicts"][0]["wanted"] == "[0, 0, 0]"
    assert r.meta["rightofway"]["commands"][0]["status"] == "refused" and _loc(app, "Leaf_3")[2] == 2.2
    ok = p.call_tool("rightofway_set_property", {"object": "Leaf_3", "property": "scale", "value": 2.0})
    assert not ok.isError and app.objects[app.find("Leaf_3")]["faces"]["scale"] == 2.0
    made_ = p.call_tool("rightofway_create_object", {"name": "Bird", "kind": "MESH", "properties": {"scale": 0.5}})
    assert not made_.isError and app.objects[app.find("Bird")]["faces"]["scale"] == 0.5
    obs = p.call_tool("rightofway_observe", {})
    assert "现在不能改的部分" in _text(obs) and "Leaf_3" in _text(obs)
    assert check_session(p.s) == []


def test_breach_alerts_the_human_in_the_app(made):
    """执行代码删掉了人改过的对象，应用又恢复不了：如实报违规，给人的提醒显示在应用里（notify）。"""
    app, p = made(restore_deleted=False, transfer_ids=False)
    sf.human_e(app)
    r = p.call_tool("execute_code", {"code": "scene.delete('Leaf_3')"})
    assert r.isError and r.meta["rightofway"]["outcome"] == "breach"
    assert p.alerts and app.notifications and "Leaf_3" in app.notifications[0]


def test_code_tool_detection_from_schemas():
    from mcp import types
    tools = [types.Tool(name="get_scene_info", inputSchema={"type": "object", "properties": {}}),
             types.Tool(name="execute_blender_code", inputSchema={
                 "type": "object", "properties": {"code": {"type": "string"}, "user_prompt": {"type": "string"}},
                 "required": ["code"]}),
             types.Tool(name="Unity_RunCommand", inputSchema={
                 "type": "object", "properties": {"Code": {"type": "string"}, "Title": {"type": "string"}},
                 "required": ["Code"]})]
    assert detect_code_tool(tools[:2]) == ("execute_blender_code", "code")
    assert detect_code_tool([tools[0], tools[2]]) == ("Unity_RunCommand", "Code")
    assert detect_code_tool(tools[:1]) == (None, None)


def test_proxy_speaks_mcp(made):
    """代理作为 MCP 服务器：工具列表里有上游的和自己的；调用结果带 isError 和 _meta.rightofway。"""
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session
    from rightofway.proxy.server import build_server
    app, p = made(unique_names=True)
    sf.human_e(app)

    async def go():
        async with create_connected_server_and_client_session(build_server(p)) as c:
            names = [t.name for t in (await c.list_tools()).tools]
            r = await c.call_tool("execute_code", {"code": "scene.set('Leaf_3', 'location', [0, 0, 1.6])"})
            return names, r
    names, r = anyio.run(go)
    assert "execute_code" in names and "rightofway_observe" in names
    assert r.isError and r.meta["rightofway"]["conflicts"][0]["object"] == "Leaf_3"


def test_stdio_end_to_end_through_two_hops():
    """AI 客户端 ──stdio──▶ python -m rightofway.proxy ──stdio──▶ 假的应用 MCP 服务器。"""
    cmd = [sys.executable, "-m", "rightofway.proxy", "--app", "fake", "--",
           sys.executable, "-m", "rightofway.proxy.fake_upstream"]
    client = SyncMCPClient.stdio(cmd, env={"PYTHONPATH": str(ROOT)}, cwd=str(ROOT))
    try:
        names = [t.name for t in client.list_tools()]
        assert {"execute_code", "delete_object", "rightofway_set_property"} <= set(names)
        r = client.call_tool("execute_code", {"code": sf.ai_build()})
        assert not r.isError and "新建 Ground" in r.content[0].text
        r = client.call_tool("rightofway_delete_object", {"object": "Rock_2"})
        assert not r.isError and "删除 Rock_2" in r.content[0].text
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 真的 BlenderMCP 服务器（可选）：设置环境变量 RIGHTOFWAY_BLENDER_MCP 为启动它的命令（例如
# "python -m blender_mcp.server"），并且当前 Python 能 import bpy（用 local_server 冒充开着插件的 Blender）
# ---------------------------------------------------------------------------
def test_real_blender_mcp_upstream():
    upstream = os.environ.get("RIGHTOFWAY_BLENDER_MCP")
    if not upstream:
        pytest.skip("没有设置 RIGHTOFWAY_BLENDER_MCP")
    pytest.importorskip("bpy")
    import shlex
    import socket
    from rightofway.blender.bridge import SocketBridge
    from experiments import scenario_tree as sc
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    blender = subprocess.Popen([sys.executable, "-m", "rightofway.blender.local_server", "--port", str(port)],
                               cwd=str(ROOT), stdout=subprocess.PIPE, text=True)
    try:
        while not blender.stdout.readline().startswith("READY"):    # bpy 启动时会先打印几行
            assert blender.poll() is None
        cmd = [sys.executable, "-m", "rightofway.proxy", "--env", f"BLENDER_PORT={port}",
               "--env", "DISABLE_TELEMETRY=1", "--", *shlex.split(upstream)]
        client = SyncMCPClient.stdio(cmd, env={"PYTHONPATH": str(ROOT)}, cwd=str(ROOT))
        try:
            assert "execute_blender_code" in [t.name for t in client.list_tools()]
            r = client.call_tool("execute_blender_code", {"code": sc.ai_build()})
            assert not r.isError, r.content[0].text
            human = SocketBridge(port=port)
            human.execute(sc.human_edit("move", "Leaf_3"))                # 人在 Blender 里把 Leaf_3 往上挪
            time.sleep(0.2)
            r = client.call_tool("execute_blender_code", {"code": sc.ai_adjust()})
            assert r.isError and "Leaf_3" in r.content[0].text and "以现在的值为准" in r.content[0].text
            info = client.call_tool("get_scene_info", {})                  # 上游的类型化工具：照样转发、事后核对
            assert "Trunk" in info.content[0].text and not info.isError
        finally:
            client.close()
    finally:
        blender.kill()
