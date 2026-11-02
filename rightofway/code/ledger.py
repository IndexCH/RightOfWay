"""代码的账本（spec/design_code.md）：人和编程 Agent 同时改同一个目录里的文件时，谁改了哪几行。

和 Blender 的运行时同样的原则，单位换成了行：
- 人改过、还没交还（也还没提交）的行，任何 Agent 都不能改（R1）。人删掉的地方（删除点）也一样，Agent 不能在那里写回去。
- 另一个 Agent（或者人做的 git 操作）在这个 Agent 上次读这个文件之后改的行，这个 Agent 不能按旧印象改（R3）。
- 说不准是谁改的（例如执行期间别的东西也在改），记为来源不明，和人改的一样保护（P1、P5）。
- 规则只在这里决定（P9）；接入层（hooks.py）只收集事实、照做、写说明。

账本不放在项目里：默认在 ~/.rightofway/code/<项目路径的哈希>/（环境变量 RIGHTOFWAY_HOME 可以改）。
每次 hook 调用都是一个新进程，所以每次都加锁、读账本、做完存回去。文件内容存成按内容寻址的块（blobs/），
和 git 暂存区一样的文件不另存，需要时用 git cat-file 读。"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import zlib
from typing import Iterator, Optional

from . import lines as L

HUMAN = "human"
UNATTRIBUTED = "external:unattributed"     # 说不准是谁改的：按人的修改保护
GIT = "git"                                # 人做的 git 操作（切分支、拉取……）带来的变化：和另一个 Agent 一样，只防按旧印象改
MAX_TEXT = 2_000_000                       # 超过这个大小、或者二进制的文件，整个文件算一行
MAX_TRACK = 50_000_000                     # 超过这个大小的文件不管（例如大的贴图、模型）
MAX_PLAIN = 20_000                         # 不是 git 仓库时最多管这么多个文件
LOCKED = (".claude/settings.json", ".claude/settings.local.json")    # hooks 的配置：只有人能改（Agent 改了就改回）
# 命令（Bash）执行超过这么多秒，这期间不是 git 带来的变化说不准是命令改的还是人改的（spec/design_code.md 3.4）
BASH_WINDOW = float(os.environ.get("RIGHTOFWAY_BASH_WINDOW", "3"))
FORMAT = 1


def store_dir(root: str, home: Optional[str] = None) -> str:
    home = home or os.environ.get("RIGHTOFWAY_HOME") or os.path.join(os.path.expanduser("~"), ".rightofway")
    key = hashlib.sha1(os.path.normcase(os.path.realpath(root)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(home, "code", key)


def find_root(cwd: str) -> str:
    """项目的根目录：git 仓库的顶层；不是 git 仓库就是 cwd 本身。"""
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True,
                             timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return os.path.realpath(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.realpath(cwd)


def sha256(data: Optional[bytes]) -> Optional[str]:
    return None if data is None else hashlib.sha256(data).hexdigest()


def git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def is_opaque(data: Optional[bytes]) -> bool:
    return data is not None and (len(data) > MAX_TEXT or b"\0" in data[:8192])


def as_lines(data: Optional[bytes]) -> list[bytes]:
    """按行切开；二进制或太大的文件整个算一行（内容的哈希）。"""
    if data is None:
        return []
    if is_opaque(data):
        return [b"<opaque " + sha256(data).encode() + b">"]
    return L.split_lines(data)


class FileLock:
    """跨平台的文件锁（hook 可能同时有好几个在跑）。"""

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


class CodeLedger:
    def __init__(self, root: str, home: Optional[str] = None) -> None:
        self.root = os.path.realpath(root)
        self.dir = store_dir(self.root, home)
        self.state: dict = {}
        self._git: Optional[bool] = None
        self._head_cache: Optional[str] = None

    # ------------------------------------------------------------------
    # 打开、存盘
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def open(self) -> Iterator["CodeLedger"]:
        with FileLock(os.path.join(self.dir, "lock")):
            self._load()
            yield self
            self._save()

    def _load(self) -> None:
        path = os.path.join(self.dir, "state.json")
        try:
            with open(path, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            st = None
        if not st or st.get("format") != FORMAT or os.path.normcase(st.get("root", "")) != os.path.normcase(self.root):
            st = {"format": FORMAT, "root": self.root, "n": 0, "head": None, "initialized": False, "files": {},
                  "marks": {}, "agents": {}, "pending": {}, "notes": {}, "log": []}
        self.state = st

    def _save(self) -> None:
        self._gc()
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, "state.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, ensure_ascii=False)
        os.replace(tmp, path)

    @property
    def files(self) -> dict:
        return self.state["files"]

    @property
    def marks(self) -> dict:
        return self.state["marks"]

    @property
    def agents(self) -> dict:
        return self.state["agents"]

    @property
    def pending(self) -> dict:
        return self.state["pending"]

    def log(self, kind: str, **kw) -> None:
        self.state["log"].append({"time": round(time.time(), 3), "kind": kind, **kw})
        del self.state["log"][:-200]

    # ------------------------------------------------------------------
    # 文件内容：按内容寻址的块；和 git 暂存区一样的用 git 读
    # ------------------------------------------------------------------
    def _blob_path(self, sha: str) -> str:
        return os.path.join(self.dir, "blobs", sha[:2], sha)

    def put(self, data: bytes) -> str:
        sha = sha256(data)
        path = self._blob_path(sha)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(zlib.compress(data, 6))
            os.replace(tmp, path)
        return sha

    def blob(self, sha: Optional[str]) -> Optional[bytes]:
        if sha is None:
            return None
        try:
            with open(self._blob_path(sha), "rb") as fh:
                return zlib.decompress(fh.read())
        except OSError:
            return None

    def content(self, path: str) -> Optional[bytes]:
        """账本里这个文件现在的内容（None：不存在）。"""
        e = self.files.get(path)
        if e is None or e.get("sha") is None:
            return None
        data = self.blob(e["sha"])
        if data is None and e.get("git"):
            data = self._git_cat(e["git"])
            if data is not None and sha256(data) != e["sha"]:
                data = None
        if data is None:
            raise RuntimeError(f"RightOfWay 的账本里找不到 {path} 的内容")
        return data

    def read_disk(self, path: str) -> Optional[bytes]:
        full = self.abspath(path)
        try:
            with open(full, "rb") as fh:
                return fh.read()
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None

    def abspath(self, path: str) -> str:
        return os.path.join(self.root, *path.split("/"))

    def relpath(self, full: str) -> Optional[str]:
        """绝对路径 → 项目里的相对路径（/ 分隔）；不在项目里的返回 None。"""
        try:
            rel = os.path.relpath(os.path.realpath(full), self.root)
        except ValueError:                           # Windows：不在同一个盘
            return None
        if rel == os.curdir or rel.startswith(os.pardir + os.sep) or rel == os.pardir or os.path.isabs(rel):
            return None
        return rel.replace(os.sep, "/")

    # ------------------------------------------------------------------
    # git
    # ------------------------------------------------------------------
    def _run_git(self, *args: str, input_: Optional[bytes] = None) -> Optional[bytes]:
        try:
            out = subprocess.run(["git", *args], cwd=self.root, input=input_, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout if out.returncode == 0 else None

    @property
    def git(self) -> bool:
        if self._git is None:
            self._git = os.path.exists(os.path.join(self.root, ".git")) and self._run_git("rev-parse", "--git-dir") is not None
        return self._git

    def head(self) -> Optional[str]:
        if not self.git:
            return None
        out = self._run_git("rev-parse", "--verify", "-q", "HEAD")
        return out.decode().strip() if out else None

    def _git_cat(self, sha: str) -> Optional[bytes]:
        return self._run_git("cat-file", "blob", sha)

    def _git_index(self) -> dict[str, str]:
        out = self._run_git("ls-files", "-s", "-z")
        res: dict[str, str] = {}
        for rec in (out or b"").split(b"\0"):
            if not rec:
                continue
            meta, _, path = rec.partition(b"\t")
            parts = meta.split()
            if len(parts) >= 3 and parts[2] == b"0":
                res[path.decode("utf-8", "surrogateescape")] = parts[1].decode()
        return res

    def _head_tree(self) -> dict[str, str]:
        out = self._run_git("ls-tree", "-r", "-z", "HEAD")
        res: dict[str, str] = {}
        for rec in (out or b"").split(b"\0"):
            if not rec:
                continue
            meta, _, path = rec.partition(b"\t")
            parts = meta.split()
            if len(parts) >= 3 and parts[1] == b"blob":
                res[path.decode("utf-8", "surrogateescape")] = parts[2].decode()
        return res

    # ------------------------------------------------------------------
    # 扫描：磁盘和账本哪里不一样
    # ------------------------------------------------------------------
    def tracked(self) -> set[str]:
        """要管的文件：git 管的、加上没被忽略的新文件；不是 git 仓库就是目录里所有不在隐藏目录里的文件。"""
        locked = {p for p in LOCKED if os.path.isfile(self.abspath(p))}    # 被 git 忽略也要管
        if self.git:
            out = self._run_git("ls-files", "-z", "-co", "--exclude-standard")
            return {p.decode("utf-8", "surrogateescape") for p in (out or b"").split(b"\0") if p} | locked
        found = set(locked)
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                rel = self.relpath(os.path.join(dirpath, name))
                if rel is not None:
                    found.add(rel)
            if len(found) > MAX_PLAIN:
                # 不是 git 仓库、文件又很多（例如整个工程目录连缓存一起）：不知道哪些该管，不猜（P4）
                raise RuntimeError(f"{self.root} 不是 git 仓库，文件又超过 {MAX_PLAIN} 个，RightOfWay 不知道该管哪些。"
                                   "请在 git 仓库里用（git init，并用 .gitignore 排除缓存目录）")
        return found

    def scan(self, only: Optional[set[str]] = None, skip: frozenset = frozenset()) -> list[tuple[str, Optional[bytes], Optional[bytes]]]:
        """[(路径, 账本里的内容, 磁盘上的内容)]：磁盘和账本不一样的文件（None 表示不存在）。
        第一次扫描（账本是空的）只记下现状，不算变化。"""
        listed = self.tracked() if only is None else set(only)
        paths = sorted((listed | (set(self.files) if only is None else set())) - set(skip))
        first = not self.state.get("initialized")
        index = self._git_index() if first and self.git else {}
        out = []
        for p in paths:
            e = self.files.get(p)
            full = self.abspath(p)
            try:
                st = os.stat(full)
                if not os.path.isfile(full):
                    st = None
            except OSError:
                st = None
            if st is None:
                if e is not None and e.get("sha") is not None:
                    out.append((p, self.content(p), None))
                continue
            stat = [st.st_mtime_ns, st.st_size]
            if e is not None and e.get("stat") == stat and e.get("sha") is not None:
                continue
            if st.st_size > MAX_TRACK and e is None:
                continue
            data = self.read_disk(p)
            if data is None:
                continue
            sha = sha256(data)
            if e is not None and e.get("sha") == sha:
                e["stat"] = stat
                continue
            if first:
                self._baseline(p, data, stat, index.get(p))
                continue
            out.append((p, self.content(p) if e is not None else None, data))
        if first:
            self.state["initialized"] = True
            self.state["head"] = self.head()
        return out

    def _baseline(self, path: str, data: bytes, stat: list, index_blob: Optional[str]) -> None:
        """第一次看到时的样子：没有作者（不保护）。和 git 暂存区一样的不另存内容。"""
        sha = sha256(data)
        git_id = index_blob if index_blob and index_blob == git_blob_id(data) else None
        if git_id is None:
            self.put(data)
        self.files[path] = {"sha": sha, "git": git_id, "stat": stat, "version": 0, "owners": [], "points": []}

    # ------------------------------------------------------------------
    # 记一次修改
    # ------------------------------------------------------------------
    def _new_mark(self, author: str, path: str, version: int, **extra) -> str:
        self.state["n"] += 1
        mid = f"m{self.state['n']}"
        self.marks[mid] = {"author": author, "path": path, "version": version, "time": round(time.time(), 3),
                           "released": False, **extra}
        return mid

    def apply_change(self, path: str, new: Optional[bytes], author: str, **extra) -> Optional[str]:
        """把账本里的 path 改成 new，作者是 author。返回这次修改的标记（没有变化时返回 None）。"""
        e = self.files.get(path)
        old = self.content(path) if e is not None else None
        if sha256(old) == sha256(new):
            return None
        old_lines, new_lines = as_lines(old), as_lines(new)
        ops = L.diff(old_lines, new_lines)
        version = (e or {}).get("version", 0) + 1
        mark = self._new_mark(author, path, version, **extra)
        owners, points = L.apply(self._owners(path, len(old_lines)), (e or {}).get("points") or [], ops, mark,
                                 old_lines)
        if new is None:                              # 整个文件删掉了：留一个删除点（文件的墓碑）
            owners, points = [], [p for p in points if p["id"] != mark] + [
                {"id": mark, "pos": 0, "count": len(old_lines), "file": True,
                 "text": [x.decode("utf-8", "replace").rstrip("\r\n") for x in old_lines[:3]]}]
        stat = None
        if new is not None:
            self.put(new)
            try:
                st = os.stat(self.abspath(path))
                if self.read_disk(path) == new:
                    stat = [st.st_mtime_ns, st.st_size]
            except OSError:
                pass
        self.files[path] = {"sha": sha256(new), "git": None, "stat": stat, "version": version,
                            "owners": L.rle(owners), "points": points}
        self.log("change", path=path, author=author, mark=mark)
        return mark

    # ------------------------------------------------------------------
    # 规则
    # ------------------------------------------------------------------
    def protected(self, mark: str, agent: str, path: str) -> bool:
        """这个标记的行，agent 能不能改：人改的、来源不明的（没交还）不能；别的 Agent、git 操作改的，
        这个 Agent 上次读这个文件之后才改的不能（按旧印象写）；自己改的可以。"""
        meta = self.marks.get(mark)
        if meta is None or meta.get("released"):
            return False
        author = meta["author"]
        if author == agent:
            return False
        if author == HUMAN or author.startswith("external:"):
            return True
        seen = self.agents.get(agent, {}).get("seen", {}).get(path, -1)
        return meta["version"] > seen

    def reason(self, mark: str) -> str:
        if mark == "locked":
            return "locked"
        author = self.marks.get(mark, {}).get("author", "")
        if author == HUMAN:
            return "human"
        if author.startswith("external:"):
            return "unattributed"
        return "stale"

    def check(self, agent: str, path: str, new: Optional[bytes]) -> dict[str, dict]:
        """agent 把 path 改成 new，会碰到哪些不能动的行：{标记: {"lines", "points"}}（现在的坐标）。"""
        e = self.files.get(path)
        old = self.content(path) if e is not None else None
        old_lines = as_lines(old)
        ops = L.diff(old_lines, as_lines(new))
        if not L.changed(ops) and sha256(old) == sha256(new):
            return {}
        if path in LOCKED:                           # RightOfWay 自己的配置：Agent 一行都不能改
            changed = [k for tag, i1, i2, _j1, _j2 in ops if tag != "equal" for k in range(i1, i2)]
            return {"locked": {"lines": changed, "points": [] if changed else [{"id": "locked", "pos": 0, "count": 0}]}}
        return L.hits(self._owners(path, len(old_lines)), (e or {}).get("points") or [], ops,
                      lambda m: self.protected(m, agent, path))

    def _owners(self, path: str, n: int) -> list[Optional[str]]:
        """每一行的标记，长度正好是 n（没记过的行是基线：None）。"""
        owners = L.unrle((self.files.get(path) or {}).get("owners"))
        return (owners + [None] * n)[:n]

    def protected_regions(self, agent: Optional[str] = None) -> dict[str, list[dict]]:
        """现在受保护的部分：{路径: [{"mark", "author", "lines": [(起, 止)], "points": [...]}]}。
        agent 为 None 时只列人和来源不明的（对所有 Agent 都受保护的）。"""
        out: dict[str, list[dict]] = {}
        for path, e in sorted(self.files.items()):
            owners = L.unrle(e.get("owners"))
            by: dict[str, dict] = {}
            for k, m in enumerate(owners):
                if m is not None and self._prot_for(m, agent, path):
                    by.setdefault(m, {"lines": [], "points": []})["lines"].append(k)
            for p in e.get("points") or ():
                if self._prot_for(p["id"], agent, path):
                    by.setdefault(p["id"], {"lines": [], "points": []})["points"].append(p)
            for m, d in sorted(by.items(), key=lambda kv: (kv[1]["lines"] or [p["pos"] for p in kv[1]["points"]])[0]):
                out.setdefault(path, []).append({"mark": m, "author": self.marks[m]["author"],
                                                 "lines": L.ranges(d["lines"]), "points": d["points"]})
        return out

    def _prot_for(self, mark: str, agent: Optional[str], path: str) -> bool:
        if agent is not None:
            return self.protected(mark, agent, path)
        meta = self.marks.get(mark) or {}
        a = meta.get("author", "")
        return not meta.get("released") and (a == HUMAN or a.startswith("external:"))

    # ------------------------------------------------------------------
    # Agent 的视图
    # ------------------------------------------------------------------
    def agent(self, agent: str, name: Optional[str] = None) -> dict:
        a = self.agents.setdefault(agent, {"name": name or agent, "seen": {}, "told": [], "refusals": {},
                                           "since": round(time.time(), 3)})
        if name:
            a["name"] = name
        a["last"] = round(time.time(), 3)
        return a

    def see(self, agent: str, path: str) -> None:
        """agent 读了这个文件：之前别人改的，它都看到了。"""
        e = self.files.get(path)
        if e is not None:
            self.agent(agent)["seen"][path] = e.get("version", 0)

    def unseen(self, agent: str, path: Optional[str] = None, untold: bool = False) -> dict[str, list[dict]]:
        """别人（人、别的 Agent、git 操作）在 agent 上次读之后改的、现在还在的部分：{路径: [区域]}。
        untold=True 时只要还没告诉过它的。"""
        a = self.agent(agent)
        told = set(a.get("told") or ())
        out: dict[str, list[dict]] = {}
        for p, e in sorted(self.files.items()):
            if path is not None and p != path:
                continue
            seen = a["seen"].get(p, -1)
            owners = L.unrle(e.get("owners"))
            by: dict[str, dict] = {}

            def fresh(m: str) -> bool:
                meta = self.marks.get(m) or {}
                return (meta.get("author") not in (None, agent) and meta.get("version", 0) > seen
                        and not (untold and m in told))
            for k, m in enumerate(owners):
                if m is not None and fresh(m):
                    by.setdefault(m, {"lines": [], "points": []})["lines"].append(k)
            for pt in e.get("points") or ():
                if fresh(pt["id"]):
                    by.setdefault(pt["id"], {"lines": [], "points": []})["points"].append(pt)
            for m, d in by.items():
                out.setdefault(p, []).append({"mark": m, "author": self.marks[m]["author"],
                                              "lines": L.ranges(d["lines"]), "points": d["points"],
                                              "protected": self.protected(m, agent, p)})
        return out

    def tell(self, agent: str, regions: dict[str, list[dict]]) -> None:
        a = self.agent(agent)
        a["told"] = sorted(set(a.get("told") or ()) | {r["mark"] for rs in regions.values() for r in rs})

    def note(self, agent: str, text: str) -> None:
        self.state["notes"].setdefault(agent, []).append(text)

    def take_notes(self, agent: str) -> list[str]:
        return self.state["notes"].pop(agent, [])

    # ------------------------------------------------------------------
    # 观察：执行之外的变化
    # ------------------------------------------------------------------
    def busy_paths(self) -> set[str]:
        return {op["path"] for op in self.pending.values() if op.get("kind") == "edit"}

    def bash_running(self) -> bool:
        now = time.time()
        return any(op.get("kind") == "bash" and op.get("until", 0) > now for op in self.pending.values())

    def observe(self) -> list[tuple[str, str, str]]:
        """Agent 的执行之外发生的变化：[(路径, 标记, 作者)]。都算人的——除了两种：
        人做的 git 操作（HEAD 变了、文件和新的 HEAD 一样）记为 git；有 Agent 的命令在执行（Bash，不是原子的）时，
        先不记（说不准是谁的），等那条命令结算。人提交（commit）之后，提交进去的行不再保护。"""
        self._expire()
        if self.bash_running():
            return []
        changes = self.scan(skip=frozenset(self.busy_paths()))
        head = self.head()
        moved = self.git and head != self.state.get("head")
        tree = self._head_tree() if moved and head else {}
        out = []
        for path, _old, new in changes:
            author = GIT if moved and new is not None and tree.get(path) == git_blob_id(new) else HUMAN
            mark = self.apply_change(path, new, author)
            if mark:
                out.append((path, mark, author))
        if moved:
            self.state["head"] = head
            self.release_committed(tree)
        return out

    def _expire(self) -> None:
        """没等到结束通知的执行：编辑过了一会儿还没结算的，照实际结果结算；命令（Bash）超时的，
        这期间的变化说不准是谁的，记为来源不明。"""
        now = time.time()
        for tid, op in sorted(self.pending.items()):
            if op.get("kind") == "edit" and now - op.get("time", now) > 300:
                self.settle_edit(tid)
            elif op.get("kind") == "bash" and op.get("until", now) <= now:
                self.settle_bash(tid, author=UNATTRIBUTED)

    def release_committed(self, tree: Optional[dict[str, str]] = None) -> list[str]:
        """人提交之后：和 HEAD 一样的行（以及已经提交的删除）不再保护（变成没有作者的基线）。"""
        tree = self._head_tree() if tree is None else tree
        released = []
        for path, e in sorted(self.files.items()):
            owners = L.unrle(e.get("owners"))
            points = e.get("points") or []
            if not any(m is not None and self._prot_for(m, None, path) for m in owners) and \
                    not any(self._prot_for(p["id"], None, path) for p in points):
                continue
            cur = self.content(path)
            head_blob = self._git_cat(tree[path]) if path in tree else None
            if cur is None and head_blob is None:
                if points:
                    e["points"] = []
                    released.append(path)
                continue
            if head_blob is None:
                continue
            cur_lines = as_lines(cur)
            owners = self._owners(path, len(cur_lines))
            ops = L.diff(as_lines(head_blob), cur_lines)
            same = [(j1, j2) for tag, _i1, _i2, j1, j2 in ops if tag == "equal"]
            hit = False
            for j1, j2 in same:
                for k in range(j1, j2):
                    if owners[k] is not None and self._prot_for(owners[k], None, path):
                        owners[k] = None
                        hit = True
            keep = []
            for p in points:                         # 删除点：HEAD 在这里也没有多出来的行，删除就已经提交了
                gone = not any(tag != "equal" and j1 <= p["pos"] <= j2 for tag, _i1, _i2, j1, j2 in ops)
                if self._prot_for(p["id"], None, path) and gone:
                    hit = True
                    continue
                keep.append(p)
            if hit:
                e["owners"], e["points"] = L.rle(owners), keep
                released.append(path)
        if released:
            self.log("commit_release", paths=released)
        return released

    # ------------------------------------------------------------------
    # Agent 的一次执行
    # ------------------------------------------------------------------
    def begin_edit(self, agent: str, tid: str, path: str, intended: Optional[bytes], tool: str) -> None:
        e = self.files.get(path)
        self.pending[tid] = {"kind": "edit", "agent": agent, "tool": tool, "path": path,
                             "base": (e or {}).get("sha"), "intended": self.put(intended) if intended is not None else None,
                             "time": round(time.time(), 3)}

    def begin_bash(self, agent: str, tid: str, timeout_s: float) -> None:
        self.pending[tid] = {"kind": "bash", "agent": agent, "time": round(time.time(), 3),
                             "until": round(time.time() + timeout_s + 60, 3), "head": self.head()}

    def cancel(self, tid: str) -> None:
        self.pending.pop(tid, None)

    def settle_edit(self, tid: str) -> dict:
        """一次编辑（Edit、Write……）结束：按磁盘上的实际内容记账。
        和执行前算出的结果一样 → 都是这个 Agent 的；不一样（执行期间别的也在改，或者工具实际做的和算的不一样）→
        预计的部分算 Agent 的，多出来的部分说不准是谁的，按人的修改保护；事先没能算出结果的，按实际差异核对，
        碰到不能动的行就把这个文件改回去（Agent 的版本另存一份）。"""
        op = self.pending.pop(tid, None)
        if op is None:
            return {"status": "unknown"}
        agent, path = op["agent"], op["path"]
        disk = self.read_disk(path)
        known = self.content(path) if path in self.files else None
        if sha256(disk) == sha256(known):
            return {"status": "unchanged", "path": path}
        if op.get("intended") and sha256(disk) == op["intended"]:
            mark = self.apply_change(path, disk, agent)
            return {"status": "applied", "path": path, "mark": mark}
        if op.get("intended"):
            intended = self.blob(op["intended"])
            m1 = self.apply_change(path, intended, agent)
            m2 = self.apply_change(path, disk, UNATTRIBUTED)
            return {"status": "applied", "path": path, "mark": m1, "unattributed": m2}
        hits = self.check(agent, path, disk)
        if hits:
            backup = self.revert(path, known, disk, tid)
            return {"status": "reverted", "path": path, "hits": hits, "backup": backup}
        mark = self.apply_change(path, disk, agent)
        return {"status": "applied", "path": path, "mark": mark}

    def settle_bash(self, tid: str, author: Optional[str] = None) -> dict:
        """一条命令（Bash）结束，按磁盘上的变化记账。命令不是原子的：执行期间人也可能存盘，所以分三种：
        - 变成了 git 里的样子（和 HEAD 或暂存区一样：checkout、reset、stash、pull……）、或者命令很快就结束了
          （不超过 BASH_WINDOW 秒，人插进来的可能很小）：算这个 Agent 的。改到了不能动的行的文件，整个改回命令执行前
          的样子（整次修改拒绝，按文件），Agent 的版本另存一份；其余照常生效。
        - 命令执行了比较久、内容又不是 git 里的：说不准是命令改的还是人改的，记为来源不明，按人的修改保护；不改回
          （可能正是人刚打的字），改动之前的版本另存一份，提醒人。
        - 删掉了有受保护的行的文件：不管多久都放回去（放回不会丢东西）。
        Agent 的命令里做的提交（commit）不算人交还。author=UNATTRIBUTED：命令没等到结束通知，变化一律说不准是谁的。"""
        op = self.pending.pop(tid, None)
        if op is None:
            return {"status": "unknown", "files": [], "reverted": [], "unsure": []}
        agent = op["agent"]
        changes = self.scan(skip=frozenset(self.busy_paths()))
        took = time.time() - op.get("time", time.time())
        quick = author is None and took <= BASH_WINDOW
        refs: dict[str, set] = {}
        if changes and self.git:
            for table in (self._head_tree(), self._git_index()):
                for p, blob in table.items():
                    refs.setdefault(p, set()).add(blob)
        files, reverted, unsure = [], [], []
        for path, old, new in changes:
            hits = self.check(agent, path, new)
            from_git = new is not None and git_blob_id(new) in refs.get(path, ())
            if path in LOCKED and hits:              # RightOfWay 自己的配置：不管谁、多久，都改回
                reverted.append({"path": path, "hits": hits, "backup": self.revert(path, old, new, tid)})
                continue
            if author is None and (quick or from_git or (new is None and hits)):
                if hits:
                    backup = self.revert(path, old, new, tid)
                    reverted.append({"path": path, "hits": hits, "backup": backup})
                    continue
                if self.apply_change(path, new, agent):
                    files.append(path)
                continue
            backup = self._backup(path, old, tid, "before") if hits and old is not None else None
            if self.apply_change(path, new, UNATTRIBUTED):
                unsure.append({"path": path, "hits": hits, "backup": backup})
        head = self.head()
        if head != self.state.get("head"):
            self.state["head"] = head                 # Agent 的 git 操作：不算人提交，不解除保护
        return {"status": "done", "files": files, "reverted": reverted, "unsure": unsure,
                "head_moved": head != op.get("head"), "seconds": round(took, 2)}

    def _backup(self, path: str, data: bytes, tid: str, kind: str) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(self.dir, "backups", f"{stamp}-{tid[-8:]}-{kind}", *path.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
        return dest

    def revert(self, path: str, old: Optional[bytes], new: Optional[bytes], tid: str) -> Optional[str]:
        """把磁盘上的 path 改回 old（账本里的样子）。改回之前，把现在的（Agent 的）版本存一份，返回存放的路径。"""
        backup = self._backup(path, new, tid, "agent") if new is not None else None
        full = self.abspath(path)
        if old is None:
            with contextlib.suppress(OSError):
                os.remove(full)
        else:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".rightofway-", dir=os.path.dirname(full))
            with os.fdopen(fd, "wb") as fh:
                fh.write(old)
            with contextlib.suppress(OSError):
                shutil.copymode(full, tmp)
            os.replace(tmp, full)
        e = self.files.get(path)
        if e is not None:
            try:
                st = os.stat(full)
                e["stat"] = [st.st_mtime_ns, st.st_size] if old is not None else None
            except OSError:
                e["stat"] = None
        self.log("revert", path=path, backup=backup)
        return backup

    # ------------------------------------------------------------------
    # 人交还
    # ------------------------------------------------------------------
    def release(self, path: Optional[str] = None, first: Optional[int] = None, last: Optional[int] = None) -> list[dict]:
        """人交还：path 为 None 时全部；只给 path 时整个文件；给了行号（从 1 开始、两端都含）时只交还这几行。
        返回交还了的 [{"path", "lines": [(起, 止)], "points": n}]。"""
        out = []
        for p, e in sorted(self.files.items()):
            if path is not None and p != path:
                continue
            owners = L.unrle(e.get("owners"))
            lines_done, pts = [], 0
            lo = (first - 1) if first else 0
            hi = last if last else len(owners)
            new_mark = None
            for k in range(lo, min(hi, len(owners))):
                m = owners[k]
                if m is not None and self._prot_for(m, None, p):
                    if new_mark is None:
                        new_mark = self._new_mark(HUMAN, p, e.get("version", 0), released=True)
                    owners[k] = new_mark
                    lines_done.append(k)
            keep = []
            for pt in e.get("points") or []:
                if self._prot_for(pt["id"], None, p) and (first is None or lo <= pt["pos"] <= hi):
                    pts += 1
                    continue
                keep.append(pt)
            if lines_done or pts:
                e["owners"], e["points"] = L.rle(owners), keep
                out.append({"path": p, "lines": L.ranges(lines_done), "points": pts})
        if out:
            self.log("release", items=[o["path"] for o in out])
        return out

    # ------------------------------------------------------------------
    def _gc(self) -> None:
        """整理：别的 Agent、git 操作留下的标记，一天内活动过的 Agent 都看过了，就不用再留（变成基线）；
        不再被任何行、删除点引用的标记删掉；一天没动静的 Agent、一天前的未结算执行也删掉。"""
        now = time.time()
        for a in [a for a, v in self.agents.items() if now - v.get("last", now) > 86400]:
            del self.agents[a]
        for tid in [t for t, op in self.pending.items() if now - op.get("time", now) > 86400]:
            del self.pending[tid]

        def settled(m: Optional[str], path: str) -> bool:
            meta = self.marks.get(m or "") or {}
            a = meta.get("author", "")
            if not m or a == HUMAN or a.startswith("external:"):
                return False
            return all(v.get("seen", {}).get(path, -1) >= meta.get("version", 0)
                       for name, v in self.agents.items() if name != a)
        used = set()
        for path, e in self.files.items():
            owners = L.unrle(e.get("owners"))
            if any(settled(m, path) for m in set(owners)):
                owners = [None if settled(m, path) else m for m in owners]
                e["owners"] = L.rle(owners)
            e["points"] = [p for p in e.get("points") or () if not settled(p["id"], path)]
            used |= {m for m in owners if m is not None}
            used |= {p["id"] for p in e["points"]}
        for m in list(self.marks):
            if m not in used:
                del self.marks[m]
        for v in self.agents.values():
            v["told"] = [m for m in v.get("told") or () if m in self.marks]
