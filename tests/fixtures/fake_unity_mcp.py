"""一个假的 Unity MCP 服务器：提供 Unity_RunCommand，返回和真实 Unity 一样格式的结果。
只用来测试 RelayTransport 的收发，不执行 C#。"""
import json
import re

from mcp.server.fastmcp import FastMCP

app = FastMCP("fake-unity")


@app.tool(name="Unity_RunCommand")
def run_command(Code: str, Title: str = "") -> str:
    fn = re.search(r'const string FN = "([^"]+)"', Code).group(1)
    payload = {"setup": {"scene_handle": "42", "closed": 0},
               "poll": {"records": {}, "labels": {}}}.get(fn, {"status": "ok"})
    payload["echo_fn"] = fn
    logs = "[Log] [<<<COWORK_JSON>>>" + json.dumps(payload) + "<<<COWORK_END>>>]"
    return json.dumps({"success": True, "message": "Command executed successfully.",
                       "data": {"isCompilationSuccessful": True, "isExecutionSuccessful": True,
                                "compilationLogs": "", "executionLogs": logs}})


if __name__ == "__main__":
    app.run()
