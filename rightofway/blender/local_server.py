"""用 pip 装的 bpy 冒充"一个开着 MCP 插件的 Blender"。

协议和现成的 Blender MCP 插件（ahujasid/blender-mcp）一样：收到 {"type": "execute_code", "params": {"code": ...}}
就执行，把打印出来的内容放在 {"status": "success", "result": {"executed": true, "result": ...}} 里返回。

用途：在没有 Blender 界面的地方（云端、自动测试）跑需要两个 Blender 的实验（实验 B 实时同步版）。
每个进程是一个独立的 Blender，互相看不到对方的数据，和真的开两个 Blender 一样。需要 Python 3.11 + pip install bpy。

    python -m rightofway.blender.local_server --port 9877
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


def _handle(req: dict, bpy) -> dict:
    """execute_code：执行代码，返回打印的内容（和插件一样）。别的命令（插件自己的查询命令）这里不支持。"""
    if req.get("type", "execute_code") != "execute_code":
        return {"status": "error", "message": f"local_server 只支持 execute_code，不支持 {req.get('type')}"}
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(req.get("params", {}).get("code", ""), {"bpy": bpy})
        return {"status": "success", "result": {"executed": True, "result": out.getvalue()}}
    except Exception as e:                         # noqa: BLE001
        return {"status": "error", "message": f"Error executing code: {e}"}


def serve(port: int, host: str = "127.0.0.1") -> None:
    """和插件一样：可以同时有多个连接，一个连接上可以连续发多条命令（新版的 BlenderMCP 服务器会一直开着一个连接，
    握手时再开一个）；命令都在主线程上一条一条执行（bpy 不是线程安全的）。"""
    import queue

    import bpy
    bpy.ops.wm.read_factory_settings(use_empty=True)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(5)
    work: queue.Queue = queue.Queue()

    def reader(conn) -> None:
        decoder = json.JSONDecoder()
        raw = b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                raw += chunk
                try:
                    buf = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue                       # 一个字符被拆在两次接收之间
                while buf.strip():
                    try:
                        req, end = decoder.raw_decode(buf.lstrip())
                    except json.JSONDecodeError:
                        break                      # 还没收完
                    buf = buf.lstrip()[end:]
                    raw = buf.encode("utf-8")
                    done: queue.Queue = queue.Queue()
                    work.put((req, done))
                    resp = done.get()
                    try:
                        conn.sendall(json.dumps(resp).encode("utf-8"))
                    except OSError:
                        return

    def acceptor() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=reader, args=(conn,), daemon=True).start()

    threading.Thread(target=acceptor, daemon=True).start()
    print(f"READY {port}", flush=True)
    while True:
        req, done = work.get()
        if req.get("type") == "shutdown":
            done.put({"status": "success", "result": {}})
            break
        done.put(_handle(req, bpy))
    time.sleep(0.1)
    srv.close()


def spawn(port: int, timeout: float = 60.0) -> subprocess.Popen:
    """在后台起一个本地 Blender（bpy）进程，等它准备好。"""
    proc = subprocess.Popen([sys.executable, "-m", "rightofway.blender.local_server", "--port", str(port)],
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
