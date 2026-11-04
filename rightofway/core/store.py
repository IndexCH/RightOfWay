"""账本存盘（spec/core.md 第 7 节）：追加写的日志，定期压缩；几个进程同时用时加文件锁。

账本文件第一行是整份记录（快照，{"t": "snap", ...}），之后每次存盘只追加一行这一步的变化（{"t": "d", ...}）。
日志长了（行数、字节数超过上限，或者比快照还大）就整份重写一次（压缩）：先写临时文件，再 os.replace 换掉，
存到一半退出也不会留下坏文件。最后一行没写完（追加到一半退出）就读到它前面为止。

快照可以带一个"附属文件"（side）：很大、只在压缩时才需要重写的部分（例如 3D 的观察缓存）单独存
（<账本>.obs.<编号>），快照里记着它的名字。先写新的附属文件，再换账本，最后删掉旧的附属文件。

只用标准库。"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional


class FileLock:
    """跨平台的文件锁（同一个账本可能有好几个进程同时在用，例如几个 hook 一起跑）。"""

    def __init__(self, path: str, timeout: float = 60.0) -> None:
        self.path, self.timeout, self.fh = path, timeout, None

    def __enter__(self) -> "FileLock":
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a+b")
        deadline = time.time() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() > deadline:
                    self.fh.close()
                    raise TimeoutError(f"RightOfWay 的账本被占用超过 {self.timeout:.0f} 秒：{self.path}")
                time.sleep(0.05)

    def __exit__(self, *exc) -> None:
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


class AppendLog:
    """一个账本文件。load() 读出 (快照, [变化...])；append() 追加一步；write_snapshot() 整份重写（压缩）。
    due() 说明该压缩了：还没有快照、换了文件、行数或字节数超过上限（字节数的上限至少和附属文件一样大）。"""

    def __init__(self, path: str, compact_lines: int = 2000, compact_bytes: int = 4 << 20,
                 head_ratio: float = 0.0) -> None:
        """head_ratio：追加的部分比快照大这么多倍时也压缩（0 表示不按快照算）。"""
        self.path = path
        self.compact_lines, self.compact_bytes, self.head_ratio = compact_lines, compact_bytes, head_ratio
        self.lines = 0                    # 快照之后追加了几行
        self.bytes = 0                    # 整个文件多大
        self.head_bytes = 0
        self.side_bytes = 0
        self.ready = False                # 这个文件已经有快照（读过或写过）

    # ---- 读
    def read(self) -> Optional[tuple[str, Any, list[dict]]]:
        """("log", 快照, [变化...])：追加写的日志；("whole", 记录, [])：整份的旧格式（一个 JSON）；文件不在或读不懂：None。"""
        try:
            with open(self.path, encoding="utf-8") as fh:
                first = fh.readline()
                try:
                    head = json.loads(first)
                except ValueError:
                    head = None
                if isinstance(head, dict) and head.get("t") == "snap":
                    rest = fh.read()
                else:
                    fh.seek(0)
                    return ("whole", json.load(fh), [])
        except (OSError, ValueError):
            return None
        deltas, nbytes = [], len(first.encode("utf-8"))
        for line in rest.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                break                      # 最后一行没写完（存到一半退出）：到这里为止
            deltas.append(d)
            nbytes += len(line.encode("utf-8")) + 1
        self.lines, self.bytes, self.head_bytes = len(deltas), nbytes, len(first.encode("utf-8"))
        side = head.get("obs")
        self.side_bytes = 0
        if side:
            try:
                self.side_bytes = os.path.getsize(self.side_path(side))
            except OSError:
                pass
        self.ready = True
        return ("log", head, deltas)

    def side_path(self, name: str) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(self.path)), name)

    def read_side(self, name: str) -> Any:
        with open(self.side_path(name), encoding="utf-8") as fh:
            return json.load(fh)

    # ---- 写
    def due(self) -> bool:
        limit = max(self.compact_bytes, self.side_bytes, int(self.head_ratio * self.head_bytes))
        return not self.ready or self.lines >= self.compact_lines or self.bytes >= limit

    def append(self, delta: dict) -> None:
        line = dumps({"t": "d", **delta}) + "\n"
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)
        self.lines += 1
        self.bytes += len(line.encode("utf-8"))

    def write_snapshot(self, head: dict, side: Optional[dict] = None) -> None:
        """整份重写。side：附属文件的内容（先写它，再换账本，最后删掉旧的附属文件）。"""
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        rec = {"t": "snap", **head}
        side_name = None
        if side is not None:
            snap = time.strftime("%Y%m%d%H%M%S") + "-" + os.urandom(3).hex()
            side_name = f"{os.path.basename(self.path)}.obs.{snap}"
            sp = self.side_path(side_name)
            with open(sp + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(side, fh, ensure_ascii=False)
            os.replace(sp + ".tmp", sp)
            rec["obs"] = side_name
            self.side_bytes = os.path.getsize(sp)
        line = dumps(rec) + "\n"
        with open(self.path + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(line)
        os.replace(self.path + ".tmp", self.path)
        if side_name is not None:
            self.drop_old_sides(keep=side_name)
        self.lines, self.bytes, self.head_bytes = 0, len(line.encode("utf-8")), len(line.encode("utf-8"))
        self.ready = True

    def drop_old_sides(self, keep: str) -> None:
        folder = os.path.dirname(os.path.abspath(self.path))
        prefix = os.path.basename(self.path) + ".obs."
        for name in os.listdir(folder):
            if name.startswith(prefix) and name != keep:
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass
