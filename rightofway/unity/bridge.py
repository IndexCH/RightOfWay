"""运行时和 Unity 之间怎么传话：通过现成的 Unity MCP（Unity AI Assistant 自带的 Unity_RunCommand）。

Unity 和它的 MCP 都不用改。运行时把 UnitySide.cs.txt 填好参数，作为一段 C# 交给 Unity_RunCommand 执行，
再从执行日志里取出结果。接口和 Blender 那边一样：call(函数名, 参数) → 结果 dict，
所以 rightofway.blender.shared_session.SharedSession 可以原样用在 Unity 上。

传话的方式（transport）有两种：
- RelayTransport：自己启动 Unity 的 MCP 中继（relay_win.exe --mcp），作为 MCP 客户端调用。需要 pip install mcp。
- ReplayTransport：把每次要执行的 C# 写成文件，由别人（例如 Claude 通过它自己的 Unity 工具）执行后把结果放回来。
  用于在没有直接连接的环境里一步步跑实验。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

TEMPLATE_PATH = Path(__file__).with_name("UnitySide.cs.txt")
MARK_BEGIN = "<<<RIGHTOFWAY_JSON>>>"
MARK_END = "<<<RIGHTOFWAY_END>>>"


class UnityError(RuntimeError):
    pass


class NeedResponse(Exception):
    """ReplayTransport：这一步还没有执行结果。"""

    def __init__(self, index: int, code_path: Path):
        super().__init__(f"第 {index} 步需要在 Unity 里执行：{code_path}")
        self.index, self.code_path = index, code_path


def build_command(fn: str, args: dict, agent_code: str = "") -> str:
    """UnitySide.cs.txt 填上函数名、参数（JSON，放进 C# 逐字字符串）和 AI 的代码。"""
    payload = json.dumps(args, ensure_ascii=True).replace('"', '""')
    src = TEMPLATE_PATH.read_text(encoding="utf-8")
    return src.replace("__FN__", fn).replace("__ARGS__", payload).replace("__AGENT__", agent_code or "")


def parse_tool_output(text: str) -> dict:
    """Unity_RunCommand 的返回是一段 JSON，执行日志在 data.executionLogs 里；编译失败时给出编译日志。"""
    logs = text
    try:
        obj = json.loads(text)
        data = obj.get("data", obj) if isinstance(obj, dict) else {}
        if isinstance(data, dict):
            if data.get("isCompilationSuccessful") is False:
                raise UnityError("C# 编译失败：\n" + str(data.get("compilationLogs", ""))[:3000])
            logs = data.get("executionLogs", text)
            if data.get("isExecutionSuccessful") is False and MARK_BEGIN not in str(logs):
                raise UnityError("执行失败：\n" + str(logs)[:3000])
    except (json.JSONDecodeError, TypeError):
        pass
    start, end = logs.rfind(MARK_BEGIN), logs.rfind(MARK_END)
    if start < 0 or end < start:
        raise UnityError("Unity 没有返回结果。输出：\n" + logs[-2000:])
    result = json.loads(logs[start + len(MARK_BEGIN):end])
    if isinstance(result, dict) and result.get("status") == "error":
        raise UnityError("Unity 里执行出错：\n" + str(result.get("error"))[:3000])
    return result


class UnityBridge:
    """和 Blender 的 bridge 一样的接口：call(函数名, 参数)。"""

    def __init__(self, transport: Callable[[str], str]) -> None:
        self.transport = transport
        self.scene_handle: Optional[str] = None

    def setup(self) -> dict:
        res = self.call("setup", {})
        self.scene_handle = res["scene_handle"]
        return res

    def call(self, fn: str, args: dict) -> dict:
        args = dict(args)
        code = args.pop("code", "")
        args.pop("scene", None)
        if fn != "setup":
            if self.scene_handle is None:
                raise UnityError("还没有建立实验场景：先调用 setup()")
            args["scene_handle"] = self.scene_handle
        args.setdefault("granularity", "aspect")
        args.setdefault("protect", True)
        args.setdefault("protected", {})
        args.setdefault("known", {})
        return parse_tool_output(self.transport(build_command(fn, args, code)))

    def execute(self, code: str) -> dict:
        """执行一段不受保护的代码（模拟人的操作）。"""
        return self.call("exec", {"code": code})


def default_relay_path() -> str:
    home = Path.home()
    if sys.platform.startswith("win"):
        return str(home / ".unity" / "relay" / "relay_win.exe")
    if sys.platform == "darwin":
        import platform
        arch = "arm64" if platform.machine() == "arm64" else "x64"
        return str(home / ".unity" / "relay" / f"relay_mac_{arch}.app" / "Contents" / "MacOS" / f"relay_mac_{arch}")
    return str(home / ".unity" / "relay" / "relay_linux")


class RelayTransport:
    """作为 MCP 客户端启动 Unity 的中继（和 Claude Desktop 配置里的那个一样），调用 Unity_RunCommand。"""

    def __init__(self, relay_path: Optional[str] = None, args=("--mcp",), tool: str = "Unity_RunCommand",
                 timeout: float = 180.0, command: Optional[list[str]] = None) -> None:
        try:
            import mcp  # noqa: F401
        except ImportError as e:
            raise UnityError("需要先安装 MCP 客户端：pip install mcp") from e
        self.command = command or [relay_path or default_relay_path(), *args]
        if command is None and not Path(self.command[0]).exists():
            raise UnityError(f"找不到 Unity MCP 中继：{self.command[0]}\n"
                             "请确认 Unity 6 装了 AI Assistant（com.unity.ai.assistant），并在 Unity 里启用过 MCP。")
        self.tool, self.timeout = tool, timeout
        self._req: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._err: Optional[BaseException] = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self._thread.start()
        if not self._ready.wait(60):
            raise UnityError("60 秒内没有连上 Unity MCP 中继")
        if self._err:
            raise UnityError(f"连接 Unity MCP 中继失败：{self._err}")

    async def _main(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        try:
            params = StdioServerParameters(command=self.command[0], args=self.command[1:])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = [t.name for t in (await session.list_tools()).tools]
                    if self.tool not in names:
                        raise UnityError(f"中继里没有 {self.tool} 工具，只有：{names}")
                    self._ready.set()
                    loop = asyncio.get_running_loop()
                    while True:
                        item = await loop.run_in_executor(None, self._req.get)
                        if item is None:
                            break
                        code, fut = item
                        try:
                            res = await session.call_tool(self.tool, {"Code": code, "Title": "RightOfWay"})
                            fut.set_result("\n".join(getattr(c, "text", "") for c in res.content))
                        except BaseException as e:   # noqa: BLE001
                            fut.set_exception(e)
        except BaseException as e:                   # noqa: BLE001
            self._err = e
            self._ready.set()

    def __call__(self, code: str) -> str:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._req.put((code, fut))
        return fut.result(timeout=self.timeout)

    def close(self) -> None:
        self._req.put(None)


class ReplayTransport:
    """第 k 次调用：如果 dir/k.out.json 已经有了，就返回它；否则把要执行的 C# 写到 dir/k.cs，并抛出 NeedResponse。
    整个实验每次从头重放，所以前面几步的代码必须一模一样（它们只取决于之前的结果）。"""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def __call__(self, code: str) -> str:
        self.n += 1
        out = self.dir / f"{self.n:02d}.out.json"
        cs = self.dir / f"{self.n:02d}.cs"
        if cs.exists() and cs.read_text(encoding="utf-8") != code and out.exists():
            raise UnityError(f"第 {self.n} 步的代码和上次不一样，重放失败（删掉 {self.dir} 重新开始）")
        cs.write_text(code, encoding="utf-8")
        if out.exists():
            return out.read_text(encoding="utf-8")
        raise NeedResponse(self.n, cs)
