"""Unity 接入里不需要 Unity 的部分：生成 C#、解析 Unity_RunCommand 的返回、重放、MCP 中继的收发。
Unity 那一侧的 C#（面、指纹、按面恢复）已经在真实的 Unity 6.4 里用 selftest 跑过。"""
import json
import sys
from pathlib import Path

import pytest

from rightofway.unity.bridge import (NeedResponse, ReplayTransport, UnityBridge, UnityError, build_command,
                                 parse_tool_output)
from rightofway.unity.selftest import build_selftest
from experiments import scenario_unity as su

# 真实 Unity 6.4 返回的格式（内容缩短）
REAL = json.dumps({"success": True, "message": "Command executed successfully.", "data": {
    "isCompilationSuccessful": True, "isExecutionSuccessful": True, "executionId": 1, "compilationLogs": "",
    "executionLogs": "[Log] [<<<RIGHTOFWAY_JSON>>>{\"records\":{},\"labels\":{\"m_Name\":\"Name\"}}<<<RIGHTOFWAY_END>>>]"}})
COMPILE_FAIL = json.dumps({"success": False, "error": "COMPILATION_FAILED: Code failed to compile.", "data": {
    "isCompilationSuccessful": False, "isExecutionSuccessful": False,
    "compilationLogs": "- Error Error CS0019: Operator '==' cannot be applied (Line: 542)"}})


def test_parse_real_output():
    assert parse_tool_output(REAL) == {"records": {}, "labels": {"m_Name": "Name"}}


def test_parse_compile_failure():
    with pytest.raises(UnityError, match="CS0019"):
        parse_tool_output(COMPILE_FAIL)


def test_parse_error_status():
    logs = "[Log] [<<<RIGHTOFWAY_JSON>>>{\"status\":\"error\",\"error\":\"boom\"}<<<RIGHTOFWAY_END>>>]"
    with pytest.raises(UnityError, match="boom"):
        parse_tool_output(json.dumps({"data": {"isCompilationSuccessful": True, "executionLogs": logs}}))


def test_build_command_embeds_args_and_code():
    code = build_command("run_agent", {"known": {"u1": {"fp": "x\"y"}}, "scene_handle": "7"}, "int z = 1;")
    assert 'const string FN = "run_agent";' in code
    assert '""fp"": ""x\\""y""' in code                      # JSON 里的引号在 C# 逐字字符串里要写两次
    assert "int z = 1;" in code
    assert "__FN__" not in code and "__ARGS__" not in code and "__AGENT__" not in code


def _syntax_errors(src: str) -> list:
    ts = pytest.importorskip("tree_sitter")
    tscs = pytest.importorskip("tree_sitter_c_sharp")
    tree = ts.Parser(ts.Language(tscs.language())).parse(src.encode())
    errs = []

    def walk(n):
        if n.type == "ERROR" or n.is_missing:
            errs.append(n.start_point)
        for c in n.children:
            walk(c)
    walk(tree.root_node)
    return errs


@pytest.mark.parametrize("fn,code", [("setup", ""), ("poll", ""), ("run_agent", su.AI_BUILD),
                                     ("run_agent", su.AI_ADJUST), ("exec", su.HUMAN_SIM),
                                     ("run_agent", su.AI_DELETE_LEAF3), ("exec", su.HUMAN_MOVE_LEAF3)])
def test_generated_csharp_parses(fn, code):
    assert _syntax_errors(build_command(fn, {"scene_handle": "1"}, code)) == []


def test_selftest_csharp_parses():
    assert _syntax_errors(build_selftest(su.AI_BUILD, su.HUMAN_SIM, su.AI_ADJUST)) == []


def test_replay_transport(tmp_path):
    t = ReplayTransport(tmp_path)
    b = UnityBridge(t)
    with pytest.raises(NeedResponse) as e:
        b.setup()
    assert e.value.index == 1 and (tmp_path / "01.cs").exists()
    setup_out = REAL.replace('{\\"records\\":{},\\"labels\\":{\\"m_Name\\":\\"Name\\"}}', '{\\"scene_handle\\":\\"9\\"}')
    (tmp_path / "01.out.json").write_text(setup_out, encoding="utf-8")
    t2 = ReplayTransport(tmp_path)
    b2 = UnityBridge(t2)
    assert b2.setup()["scene_handle"] == "9"
    (tmp_path / "02.out.json").write_text(REAL, encoding="utf-8")
    assert b2.call("poll", {})["labels"] == {"m_Name": "Name"}
    assert '""scene_handle"": ""9""' in (tmp_path / "02.cs").read_text(encoding="utf-8")


def test_relay_transport_with_fake_mcp_server():
    pytest.importorskip("mcp")
    from rightofway.unity.bridge import RelayTransport
    server = Path(__file__).parent / "fixtures" / "fake_unity_mcp.py"
    t = RelayTransport(command=[sys.executable, str(server)], timeout=60)
    try:
        b = UnityBridge(t)
        assert b.setup()["scene_handle"] == "42"
        assert b.call("poll", {})["echo_fn"] == "poll"
    finally:
        t.close()


def test_v04_code_is_valid_csharp():
    """v0.4：面的值、选中即占用、AI 读了提示后不补人删掉的对象。"""
    adjust = su.ai_adjust(["Rock_2"])
    assert 'var SKIP = new List<string> { "Rock_2" };' in adjust
    for src in (build_command("poll", {"known": {}}), build_command("run_agent", {"protect_selected": True}, adjust),
                build_selftest(su.AI_BUILD, su.HUMAN_SIM, adjust, protect_selected=True)):
        assert _syntax_errors(src) == []
    assert '""protect_selected"": true' in build_selftest(su.AI_BUILD, su.HUMAN_SIM, adjust, protect_selected=True)
