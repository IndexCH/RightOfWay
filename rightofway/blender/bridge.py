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
import hashlib
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
IDENTITY_PATH = Path(__file__).parent.parent / "identity.py"     # 认回同一个对象的规则：运行时和接入代码用同一份
MARK_BEGIN = "<<<RIGHTOFWAY_JSON>>>"
MARK_END = "<<<RIGHTOFWAY_END>>>"


class BridgeError(RuntimeError):
    """和 Blender 通信失败，或者 Blender 里执行出错。"""


class _Commands:
    """类型化命令（design_v0.5.md 第 9 节的小工具）：编成 Blender 脚本，由 run_agent 原子地执行。"""

    @staticmethod
    def compile_commands(commands: list, scene: Optional[str] = None) -> str:
        from .commands import compile_commands
        return compile_commands(commands, scene)

    @staticmethod
    def command_results(stdout: str) -> dict:
        from .commands import command_results
        return command_results(stdout)


CODE_MODULE = "_rightofway_code"          # Blender 里缓存接入代码的地方（sys.modules，只在这次打开期间有效）
NEED_SOURCE = "__rightofway_need_source__"   # 缓存里没有这一版接入代码：调用方要重发全文
_SOURCE: dict = {}


def blender_source() -> str:
    """发进 Blender 的源码：identity.py（只用标准库）在前，blender_side.py 在后。"""
    return _source()[0]


def _source() -> tuple[str, str]:
    """源码和它的哈希（按两个文件的修改时间缓存，改了文件就重新读）。"""
    key = tuple(os.stat(p).st_mtime_ns for p in (IDENTITY_PATH, SOURCE_PATH))
    if _SOURCE.get("key") != key:
        src = IDENTITY_PATH.read_text(encoding="utf-8") + "\n\n" + SOURCE_PATH.read_text(encoding="utf-8")
        _SOURCE.update(key=key, src=src, hash=hashlib.sha1(src.encode("utf-8")).hexdigest()[:16])
    return _SOURCE["src"], _SOURCE["hash"]


def _payload(args: dict) -> str:
    """参数：一层 JSON（纯 ASCII，路径和中文都转义），放在 Python 的原始字符串里原样交给 json.loads。
    JSON 里恰好有三引号、或者以反斜杠结尾时，退回两层 JSON（先转成 JSON 字符串再嵌进去）。"""
    text = json.dumps(args, ensure_ascii=True)
    for q in ("'''", '"""'):
        if q not in text and not text.endswith(("\\", q[0])):
            return f"r{q}{text}{q}"
    return json.dumps(text)


def build_call(fn: str, args: dict, cached: bool = False) -> str:
    """一次调用要执行的代码。
    cached=False：identity.py 和 blender_side.py 的全部源码，末尾加一行调用；同时把这一版接入代码按哈希
                  缓存在 Blender 里（sys.modules，不进文件）。
    cached=True：只有一小段代码，调用缓存里的接入代码；缓存里没有这一版（第一次、Blender 重新打开过、
                 接入代码改过）时返回 NEED_SOURCE，调用方再发一次全文（见 _CachedCalls）。"""
    src, digest = _source()
    payload = _payload(args)
    if cached:
        return ("import sys as _rw_s, json as _rw_j\n"
                f"_rw_c = _rw_s.modules.get({CODE_MODULE!r})\n"
                f"if getattr(_rw_c, 'hash', None) != {digest!r}:\n"
                f"    print({MARK_BEGIN!r} + _rw_j.dumps({{{NEED_SOURCE!r}: True}}) + {MARK_END!r})\n"
                "else:\n"
                f"    _rw_c.ns['_rightofway_out'](_rw_c.ns[{fn!r}](_rw_j.loads({payload})))\n")
    return (src + "\n\nimport sys as _rw_s, types as _rw_t\n"
            f"_rw_c = _rw_s.modules[{CODE_MODULE!r}] = _rw_t.ModuleType({CODE_MODULE!r})\n"
            f"_rw_c.hash, _rw_c.ns = {digest!r}, globals()\n"
            f"_rightofway_out({fn}(json.loads({payload})))\n")


def parse_output(text: str) -> dict:
    start = text.rfind(MARK_BEGIN)
    end = text.rfind(MARK_END)
    if start < 0 or end < start:
        raise BridgeError("Blender 没有返回结果。输出的最后部分：\n" + text[-2000:])
    return json.loads(text[start + len(MARK_BEGIN):end])


class _CachedCalls:
    """接入代码只在第一次（或者 Blender 重新打开过、接入代码改过）发全文，之后只发一小段调用代码。"""
    _source_sent = False

    def call(self, fn: str, args: dict) -> dict:
        res = parse_output(self.execute(build_call(fn, args, cached=self._source_sent)))
        if isinstance(res, dict) and res.get(NEED_SOURCE):
            res = parse_output(self.execute(build_call(fn, args)))
        self._source_sent = True
        return res


class SocketBridge(_CachedCalls, _Commands):
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
                response = receive_json(sock)
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


_WS = b" \t\r\n"


def receive_json(sock) -> Optional[dict]:
    """收插件的回复：一个 JSON 对象，没有分隔符，只能收到能完整解析为止。
    收的时候追加到 bytearray（不反复拷贝），只在收到的内容以 "}" 结尾时才试着解析一次：
    整段解析的次数和消息大小无关（以前每收 64 KB 就整段解析一遍，耗时随消息大小平方增长）。
    连接关闭了还没收到完整的 JSON，返回 None。"""
    buf = bytearray()
    while True:
        chunk = sock.recv(1 << 20)
        if not chunk:
            return None
        buf += chunk
        end = len(buf)
        while end and buf[end - 1] in _WS:
            end -= 1
        if not end or buf[end - 1] != 0x7D:      # "}"
            continue
        try:
            return json.loads(buf)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue


class InProcessBridge(_CachedCalls, _Commands):
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
