"""用 pip 装的 bpy 冒充"一个开着 MCP 插件的 Blender"。

协议和现成的 Blender MCP 插件（ahujasid/blender-mcp）一样：收到 {"type": "execute_code", "params": {"code": ...}}
就执行，把打印出来的内容放在 {"status": "success", "result": {"executed": true, "result": ...}} 里返回。

用途：在没有 Blender 界面的地方（云端、自动测试）跑需要两个 Blender 的实验（实验 B 实时同步版）。
每个进程是一个独立的 Blender，互相看不到对方的数据，和真的开两个 Blender 一样。需要 Python 3.11 + pip install bpy。

    python -m cowork.blender.local_server --port 9877
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import socket
import subprocess
import sys
import threading
import time


def serve(port: int, host: str = "127.0.0.1") -> None:
    import bpy
    bpy.ops.wm.read_factory_settings(use_empty=True)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(5)
    print(f"READY {port}", flush=True)
    while True:
        conn, _ = srv.accept()
        with conn:
            buf, req = b"", None
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                try:
                    req = json.loads(buf.decode("utf-8"))
                    break
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
            if req is None:
                continue
            if req.get("type") == "shutdown":
                conn.sendall(b'{"status": "success", "result": {}}')
                break
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    exec(req.get("params", {}).get("code", ""), {"bpy": bpy})
                resp = {"status": "success", "result": {"executed": True, "result": out.getvalue()}}
            except Exception as e:                 # noqa: BLE001
                resp = {"status": "error", "message": f"Error executing code: {e}"}
            conn.sendall(json.dumps(resp).encode("utf-8"))
    srv.close()


def spawn(port: int, timeout: float = 60.0) -> subprocess.Popen:
    """在后台起一个本地 Blender（bpy）进程，等它准备好。"""
    proc = subprocess.Popen([sys.executable, "-m", "cowork.blender.local_server", "--port", str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"本地 Blender 进程（端口 {port}）启动失败")
            continue
        if line.startswith("READY"):
            # Blender 自己还会往标准输出打印信息；一直读掉，免得管道写满卡住进程
            threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()
            return proc
    proc.kill()
    raise RuntimeError(f"本地 Blender 进程（端口 {port}）{timeout:.0f} 秒内没有准备好")


def shutdown(port: int, proc: subprocess.Popen | None = None, host: str = "127.0.0.1") -> None:
    try:
        with socket.create_connection((host, port), timeout=5) as s:
            s.sendall(b'{"type": "shutdown"}')
            s.recv(1024)
    except OSError:
        pass
    if proc is not None:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="用 bpy 冒充一个开着 MCP 插件的 Blender")
    ap.add_argument("--port", type=int, default=9877)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    serve(a.port, a.host)
