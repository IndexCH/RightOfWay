"""运行时自己的调用（probe、poll、run_agent、begin_agent……）怎么经过上游的"执行代码"工具发进应用。

应用的 MCP 服务器几乎都有一个执行代码的工具（BlenderMCP 的 execute_blender_code、Unity 的 Unity_RunCommand）。
MCPBridge 把运行时的调用编成那门语言的一段代码交给这个工具，再从它打印的结果里取出 JSON，
和 SocketBridge 一样的接口：call(函数名, 参数) → dict。所以接入代码只有一份，代理和实验用的是同一份。

Source 是"那门语言"：
    BlenderSource  Python + bpy（identity.py + blender_side.py，和 SocketBridge 发的一样）
    FakeSource     内存里的假应用（rightofway/fakeapp.py），测试用
Unity（C#）的接入代码要先指定实验场景（UnityBridge.setup），代理还没有接 Unity。
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from ..blender.bridge import BridgeError
from ..blender.bridge import build_call as _blender_call
from ..blender.bridge import parse_output as _blender_parse
from ..blender.commands import command_results as _blender_results
from ..blender.commands import compile_commands as _blender_compile

MARK_BEGIN = "<<<RIGHTOFWAY_JSON>>>"
MARK_END = "<<<RIGHTOFWAY_END>>>"
CODE_PARAMS = ("code", "script", "source", "python", "csharp", "cs")       # 常见的"代码"参数名（找执行代码的工具用）


class BlenderSource:
    language = "python"

    def build(self, fn: str, args: dict) -> str:
        return _blender_call(fn, args)

    def parse(self, text: str) -> dict:
        return _blender_parse(text)

    def compile_commands(self, commands: list, scene: Optional[str] = None) -> str:
        return _blender_compile(commands, scene)

    def command_results(self, stdout: str) -> dict:
        return _blender_results(stdout)


class FakeSource:
    """假应用：上游的执行代码工具在一个有 app（FakeApp）的命名空间里执行 Python。"""
    language = "python"

    def build(self, fn: str, args: dict) -> str:
        payload = json.dumps(json.dumps(args, ensure_ascii=True))
        return (f"import json as _j\n_r = app.{fn}(_j.loads({payload}))\n"
                f"print({MARK_BEGIN!r} + _j.dumps(_r, ensure_ascii=False) + {MARK_END!r})\n")

    def parse(self, text: str) -> dict:
        start, end = text.rfind(MARK_BEGIN), text.rfind(MARK_END)
        if start < 0 or end < start:
            raise BridgeError("应用没有返回结果：" + text[-1000:])
        return json.loads(text[start + len(MARK_BEGIN):end])

    def compile_commands(self, commands: list, scene: Optional[str] = None) -> str:
        from ..fakeapp import FakeBridge
        return FakeBridge.compile_commands(commands, scene)

    def command_results(self, stdout: str) -> dict:
        from ..fakeapp import FakeBridge
        return FakeBridge.command_results(stdout)


def _text(result: Any) -> str:
    return "\n".join(getattr(c, "text", "") for c in (getattr(result, "content", None) or []))


class MCPBridge:
    """经过上游 MCP 服务器的执行代码工具和应用对话。"""

    def __init__(self, client, code_tool: str, code_param: str = "code", source=None) -> None:
        self.client, self.code_tool, self.code_param = client, code_tool, code_param
        self.source = source or BlenderSource()

    def call(self, fn: str, args: Optional[dict] = None) -> dict:
        res = self.client.call_tool(self.code_tool, {self.code_param: self.source.build(fn, args or {})})
        text = _text(res)
        if MARK_BEGIN not in text:
            raise BridgeError(f"应用的 MCP 服务器执行 {fn} 失败（{self.code_tool}）：{text[-1500:]}")
        return self.source.parse(text)

    def compile_commands(self, commands: list, scene: Optional[str] = None) -> str:
        return self.source.compile_commands(commands, scene)

    def command_results(self, stdout: str) -> dict:
        return self.source.command_results(stdout)


def detect_code_tool(tools: Iterable[Any]) -> tuple[Optional[str], Optional[str]]:
    """在上游的工具里找"执行代码"的那个：有一个必填的字符串参数，参数名像代码（code、script……），
    工具名里有 execute / run / code。找不到返回 (None, None)，可以用 --code-tool 指定。"""
    best = None
    for t in tools:
        schema = getattr(t, "inputSchema", None) or {}
        props, required = schema.get("properties", {}), set(schema.get("required", []))
        params = [p for p in props if p.lower() in CODE_PARAMS and props[p].get("type") == "string" and p in required]
        if not params:
            continue
        score = sum(w in t.name.lower() for w in ("execute", "run", "code", "script"))
        if best is None or score > best[0]:
            best = (score, t.name, params[0])
    return (best[1], best[2]) if best else (None, None)
