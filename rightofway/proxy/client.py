"""作为 MCP 客户端连接应用的 MCP 服务器（上游）。

运行时是同步的代码，MCP 的 Python SDK 是异步的。这里在一个后台线程里跑 asyncio 和 MCP 会话，
对外给同步的 list_tools() / call_tool()，在任何线程里都能调用（和 rightofway.unity.bridge.RelayTransport 同一个做法）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional


class UpstreamError(RuntimeError):
    pass


class SyncMCPClient:
    def __init__(self, connect: Callable[[], Any], timeout: float = 180.0, ready_timeout: float = 60.0) -> None:
        """connect：无参数的函数，返回一个 async context manager，进入后给出已经 initialize 过的 ClientSession。"""
        self.timeout = timeout
        self._req: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._err: Optional[BaseException] = None
        self.server_info: Any = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main(connect)), daemon=True)
        self._thread.start()
        if not self._ready.wait(ready_timeout):
            raise UpstreamError(f"{ready_timeout:.0f} 秒内没有连上应用的 MCP 服务器")
        if self._err is not None:
            raise UpstreamError(f"连接应用的 MCP 服务器失败：{self._err!r}") from self._err

    async def _main(self, connect) -> None:
        try:
            async with connect() as session:
                self._ready.set()
                loop = asyncio.get_running_loop()
                while True:
                    item = await loop.run_in_executor(None, self._req.get)
                    if item is None:
                        break
                    fn, fut = item
                    try:
                        fut.set_result(await fn(session))
                    except BaseException as e:     # noqa: BLE001
                        fut.set_exception(e)
        except BaseException as e:                 # noqa: BLE001
            self._err = e
            self._ready.set()

    def _run(self, fn) -> Any:
        if self._err is not None:
            raise UpstreamError(f"和应用的 MCP 服务器的连接已经断开：{self._err!r}")
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._req.put((fn, fut))
        return fut.result(timeout=self.timeout)

    def list_tools(self) -> list:
        async def all_tools(session):
            tools, cursor = [], None
            while True:
                res = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
                tools += list(res.tools)
                cursor = getattr(res, "nextCursor", None)
                if not cursor:
                    return tools
        return self._run(all_tools)

    def call_tool(self, name: str, arguments: Optional[dict] = None):
        return self._run(lambda session: session.call_tool(name, arguments or {}))

    def close(self) -> None:
        self._req.put(None)
        self._thread.join(timeout=5)

    # ------------------------------------------------------------------
    @classmethod
    def stdio(cls, command: list[str], env: Optional[dict] = None, cwd: Optional[str] = None,
              **kw) -> "SyncMCPClient":
        """启动上游（例如 ["uvx", "blender-mcp"]），通过标准输入输出连接。env 里的变量加在默认环境上。"""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import get_default_environment, stdio_client

        @asynccontextmanager
        async def connect():
            params = StdioServerParameters(command=command[0], args=list(command[1:]),
                                           env={**get_default_environment(), **(env or {})}, cwd=cwd)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        return cls(connect, **kw)

    @classmethod
    def in_memory(cls, server, **kw) -> "SyncMCPClient":
        """连接同一个进程里的 MCP 服务器对象（测试用）。"""
        from mcp.shared.memory import create_connected_server_and_client_session
        return cls(lambda: create_connected_server_and_client_session(server), **kw)
