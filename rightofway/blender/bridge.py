"""运行时和 Blender 之间怎么传话。三种方式，接口相同：call(函数名, 参数) → 结果 dict。

- SocketBridge：连接现成的 Blender MCP 插件（ahujasid/blender-mcp，默认 localhost:9876），
  通过它的 execute_code 命令把 blender_side.py 的源码发进正在运行的 Blender。方式一用。
- InProcessBridge：当前 Python 能直接 import bpy（pip install bpy）时，在本进程里执行。
  用于自动测试和无界面模拟。方式一、方式二都能用。
- SubprocessBlender：启动无界面的 Blender 可执行文件执行。方式二用（每次调用启动一次 Blender）。

三种方式执行的都是同一份 blender_side.py 源码。
"""
from __future__ import annotations

import contextlib
import glob
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

SOURCE_PATH = Path(__file__).with_name("blender_side.py")
MARK_BEGIN = "<<<RIGHTOFWAY_JSON>>>"
MARK_END = "<<<RIGHTOFWAY_END>>>"


class BridgeError(RuntimeError):
    """和 Blender 通信失败，或者 Blender 里执行出错。"""


def blender_source() -> str:
    return SOURCE_PATH.read_text(encoding="utf-8")


def build_call(fn: str, args: dict) -> str:
    """blender_side.py 的全部源码，末尾加一行调用。参数先转成 JSON 字符串再嵌进去。"""
    payload = json.dumps(json.dumps(args, ensure_ascii=True))   # 纯 ASCII，路径和中文都转义
    return blender_source() + f"\n\n_rightofway_out({fn}(json.loads({payload})))\n"


def parse_output(text: str) -> dict:
    start = text.rfind(MARK_BEGIN)
    end = text.rfind(MARK_END)
    if start < 0 or end < start:
        raise BridgeError("Blender 没有返回结果。输出的最后部分：\n" + text[-2000:])
    return json.loads(text[start + len(MARK_BEGIN):end])


class SocketBridge:
    """通过现成的 Blender MCP 插件执行代码。插件那边不需要任何修改。"""

    # 用 127.0.0.1 而不是 localhost：Windows 上 localhost 会先尝试 IPv6（::1），插件只监听 IPv4，
    # 每次连接要等约 2 秒失败后才退回 IPv4
    def __init__(self, host: str = "127.0.0.1", port: int = 9876, timeout: float = 60.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout

    def execute(self, code: str) -> str:
        """发送一段代码，返回它打印出来的内容。"""
        request = json.dumps({"type": "execute_code", "params": {"code": code}}).encode("utf-8")
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.sendall(request)
                buf = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    try:
                        response = json.loads(buf.decode("utf-8"))
                        break
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue      # 插件的回复没有分隔符，收到完整的 JSON 为止
                else:
                    response = None
        except OSError as e:
            raise BridgeError(
                f"连不上 Blender MCP 插件（{self.host}:{self.port}）：{e}\n"
                "请确认 Blender 已打开，并在 3D 视图右侧栏的 BlenderMCP 标签里点了连接/启动服务器。") from e
        if response is None:
            raise BridgeError("Blender MCP 插件关闭了连接，没有返回结果")
        if response.get("status") != "success":
            raise BridgeError("Blender 里执行出错：" + str(response.get("message")))
        result = response.get("result", {})
        return result.get("result", "") if isinstance(result, dict) else str(result)

    def call(self, fn: str, args: dict) -> dict:
        return parse_output(self.execute(build_call(fn, args)))


class InProcessBridge:
    """当前 Python 能 import bpy 时，直接在本进程执行。行为和插件里的 execute_code 一样：
    每次用一个新的命名空间执行，捕获打印输出。"""

    def __init__(self) -> None:
        try:
            import bpy  # noqa: F401
        except ImportError as e:
            raise BridgeError("当前 Python 不能 import bpy。需要 Python 3.11 并 pip install bpy，"
                              "或者改用真实 Blender（SocketBridge / SubprocessBlender）。") from e

    def execute(self, code: str) -> str:
        import bpy
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(code, {"bpy": bpy})
        return out.getvalue()

    def call(self, fn: str, args: dict) -> dict:
        return parse_output(self.execute(build_call(fn, args)))

    @staticmethod
    def reset() -> None:
        """清空成一个没有任何对象的场景（测试用）。"""
        import bpy
        bpy.ops.wm.read_factory_settings(use_empty=True)


def find_blender() -> Optional[str]:
    """找 Blender 可执行文件：先看环境变量 BLENDER_EXE，再看常见安装位置。"""
    env = os.environ.get("BLENDER_EXE")
    if env and Path(env).exists():
        return env
    candidates: list[str] = []
    if sys.platform.startswith("win"):
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            candidates += glob.glob(os.path.join(root, "Blender Foundation", "Blender*", "blender.exe"))
        # Steam 版：默认 Steam 目录，以及任意盘符下的 SteamLibrary
        candidates += glob.glob(os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                                             "Steam", "steamapps", "common", "Blender", "blender.exe"))
        for drive in "CDEFGHIJ":
            candidates += glob.glob(rf"{drive}:\SteamLibrary\steamapps\common\Blender\blender.exe")
            candidates += glob.glob(rf"{drive}:\Steam\steamapps\common\Blender\blender.exe")
    elif sys.platform == "darwin":
        candidates += ["/Applications/Blender.app/Contents/MacOS/Blender"]
    found = shutil.which("blender")
    if found:
        candidates.append(found)
    existing = [c for c in candidates if Path(c).exists()]
    return sorted(existing)[-1] if existing else None


class SubprocessBlender:
    """每次调用启动一次无界面 Blender。只能调用会自己打开文件的函数（dump_file、edit_file、merge_file）。"""

    def __init__(self, blender_exe: Optional[str] = None, timeout: float = 300.0) -> None:
        self.exe = blender_exe or find_blender()
        if not self.exe:
            raise BridgeError("找不到 Blender。请安装 Blender 4.2 或更高版本，"
                              "或者设置环境变量 BLENDER_EXE 指向 blender.exe。")
        self.timeout = timeout

    def call(self, fn: str, args: dict) -> dict:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(build_call(fn, args))
            script = f.name
        try:
            proc = subprocess.run(
                [self.exe, "--background", "--factory-startup", "--python-exit-code", "1", "--python", script],
                capture_output=True, timeout=self.timeout, encoding="utf-8", errors="replace")
        finally:
            os.remove(script)
        try:
            return parse_output(proc.stdout)
        except BridgeError:
            raise BridgeError(f"Blender 执行 {fn} 失败（退出码 {proc.returncode}）：\n"
                              + (proc.stdout[-1500:] + "\n" + proc.stderr[-1500:]))


def make_file_runner(spec: str = "auto"):
    """方式二用：'inprocess'、'auto'（能 import bpy 就在本进程，否则找 Blender），或 Blender 可执行文件路径。"""
    if spec == "inprocess":
        return InProcessBridge()
    if spec == "auto":
        try:
            return InProcessBridge()
        except BridgeError:
            return SubprocessBlender()
    return SubprocessBlender(spec)


def json_safe(obj: Any) -> Any:
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
