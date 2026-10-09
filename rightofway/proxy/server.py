"""MCP 代理作为 MCP 服务器给 AI 客户端用。

    python -m rightofway.proxy -- uvx blender-mcp
    python -m rightofway.proxy --bridge socket -- uvx blender-mcp     运行时自己的调用直接连插件（端口 9876）

Claude Desktop 的配置（claude_desktop_config.json）里，把原来的 blender 服务器换成：
    "blender": {"command": "python", "args": ["-m", "rightofway.proxy", "--", "uvx", "blender-mcp"]}

"--" 后面是原来启动应用 MCP 服务器的命令，原样照搬。代理的标准输出是 MCP 通道，日志写到标准错误。
需要：pip install -e ".[proxy]"（Blender 一侧照旧只要 BlenderMCP 插件，不用改）。
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

INSTRUCTIONS = (
    "这个 MCP 服务器经过 RightOfWay：人也在同时编辑这个场景。人改过的部分你改不动，会保留人的；"
    "每次调用的结果会告诉你哪些没有生效、现在的值是多少——请把这些值当作新的目标写进你之后的计划，不要再改回去。"
    "动手之前可以先用 rightofway_observe 看看人改了什么、哪些现在不能改。"
    "只改一两个属性时，用 rightofway_set_property 之类的小工具：不能改的会在执行前就告诉你。"
)


def build_server(proxy):
    """把 Proxy 包成一个 MCP 服务器（低层 API：工具列表和调用原样转给 Proxy）。"""
    import anyio
    from mcp.server.lowlevel import Server

    server = Server("rightofway-proxy", instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools():
        return proxy.list_tools()

    @server.call_tool(validate_input=False)          # 参数由上游自己校验
    async def call_tool(name: str, arguments: dict):
        return await anyio.to_thread.run_sync(proxy.call_tool, name, arguments or {})

    return server


async def serve_stdio(proxy) -> None:
    from mcp.server.stdio import stdio_server
    server = build_server(proxy)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def make_proxy(upstream: list[str], app: str = "blender", bridge: str = "mcp", host: str = "127.0.0.1",
               port: int = 9876, code_tool: Optional[str] = None, code_param: Optional[str] = None,
               agent: str = "agent", granularity: str = "aspect", reidentify: bool = True,
               existing: str = "open", env: Optional[dict] = None):
    from ..blender.bridge import SocketBridge
    from ..blender.shared_session import SharedSession
    from ..runtime import Runtime
    from .client import SyncMCPClient
    from .core import Proxy
    from .sources import BlenderSource, FakeSource, MCPBridge, detect_code_tool

    client = SyncMCPClient.stdio(upstream, env=env)
    tools = client.list_tools()
    if code_tool is None:
        code_tool, code_param = detect_code_tool(tools)
    if bridge == "socket":
        br = SocketBridge(host, port)
    else:
        if code_tool is None:
            raise SystemExit("上游没有执行代码的工具，请用 --code-tool 指定，或者用 --bridge socket 直接连 Blender 插件")
        br = MCPBridge(client, code_tool, code_param or "code", FakeSource() if app == "fake" else BlenderSource())
    session = SharedSession(Runtime(reidentify=reidentify), br, agent=agent, granularity=granularity,
                            initial_owner=existing)
    session.start()
    return Proxy(session, client, code_tool=code_tool, code_param=code_param, agent=agent)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    upstream = []
    if "--" in argv:
        k = argv.index("--")
        argv, upstream = argv[:k], argv[k + 1:]
    ap = argparse.ArgumentParser(prog="python -m rightofway.proxy",
                                 description="RightOfWay MCP 代理：AI 客户端连它，它连应用现成的 MCP 服务器（-- 后面是启动命令）")
    ap.add_argument("--app", choices=["blender", "fake"], default="blender")
    ap.add_argument("--bridge", choices=["mcp", "socket"], default="mcp",
                    help="运行时自己的调用怎么进应用：mcp 经过上游的执行代码工具（默认）；socket 直接连 Blender 插件")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9876)
    ap.add_argument("--code-tool", help="上游执行代码的工具名（默认自动找）")
    ap.add_argument("--code-param", help="这个工具放代码的参数名（默认自动找）")
    ap.add_argument("--agent", default="agent", help="这个 AI 的名字（同一个运行时里有多个 AI 时区分）")
    ap.add_argument("--granularity", choices=["aspect", "object"], default="aspect")
    ap.add_argument("--no-reidentify", dest="reidentify", action="store_false")
    ap.add_argument("--existing", choices=["open", "human"], default="open",
                    help="开始时场景里已有的内容：open 表示 AI 可以改（默认）；human 表示都算人改过的，人交还之前 AI 不能改")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="给上游加的环境变量")
    args = ap.parse_args(argv)
    if not upstream:
        ap.error("缺少上游的启动命令，例如：python -m rightofway.proxy -- uvx blender-mcp")
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[RightOfWay] %(message)s")
    env = dict(x.split("=", 1) for x in args.env)
    import anyio
    proxy = make_proxy(upstream, app=args.app, bridge=args.bridge, host=args.host, port=args.port,
                       code_tool=args.code_tool, code_param=args.code_param, agent=args.agent,
                       granularity=args.granularity, reidentify=args.reidentify, existing=args.existing, env=env)
    logging.getLogger("rightofway.proxy").info(
        "已连接上游：%d 个工具，执行代码的工具：%s；能力：%s", len(proxy.upstream), proxy.code_tool,
        {k: v for k, v in proxy.s.capabilities.items() if k != "names"})
    anyio.run(serve_stdio, proxy)
    return 0
