"""文本文件这一侧（spec/design_code.md）：人和编程 Agent 同时改同一个目录里的文件时，看见谁改了哪几行、放回文件。

规则都在核心里（rightofway/core，spec/core.md）：能不能改（判断表）、标记怎么跟着行走、墓碑、一次编辑或命令
结束后怎么结算、谁做的。这里只做文本文件这一侧的事（P9）：
- 单元是行（lines.py：切行、新旧两份的对应关系）；容器是文件；
- 看见变化：扫描磁盘和账本哪里不一样，人的 git 操作（HEAD 变了）；
- 放回：把文件整个改回去，AI 的版本另存一份；
- 账本放在哪、文件内容存成按内容寻址的块。

账本不放在项目里：默认在 ~/.rightofway/code/<项目路径的哈希>/（环境变量 RIGHTOFWAY_HOME 可以改）。
每次 hook 调用都是一个新进程，所以每次都加锁、读账本、做完存回去（core.store：追加写的日志，定期压缩）。
文件内容存成按内容寻址的块（blobs/），和 git 暂存区一样的文件不另存，需要时用 git cat-file 读。"""
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

from ..core import rules as R
from ..core.positional import PositionalLedger, empty_state, equal_spans
from ..core.store import AppendLog, FileLock   # noqa: F401  （FileLock 也从这里导出，兼容）
from . import lines as L

HUMAN = R.HUMAN
UNATTRIBUTED = R.UNATTRIBUTED              # 说不准是谁改的：按人的修改保护
GIT = R.GIT                                # 人做的 git 操作（切分支、拉取……）带来的变化：和另一个 Agent 一样，只防按旧印象改
MAX_TEXT = 2_000_000                       # 超过这个大小、或者二进制的文件，整个文件算一行
MAX_TRACK = 50_000_000                     # 超过这个大小的文件不管（例如大的贴图、模型）
MAX_PLAIN = 20_000                         # 不是 git 仓库时最多管这么多个文件
LOCKED = (".claude/settings.json", ".claude/settings.local.json")    # hooks 的配置：只有人能改（Agent 改了就改回）
# 命令（Bash）执行超过这么多秒，这期间不是 git 带来的变化说不准是命令改的还是人改的（spec/design_code.md 3.4）
BASH_WINDOW = float(os.environ.get("RIGHTOFWAY_BASH_WINDOW", "3"))
EDIT_EXPIRE = 300.0                        # 编辑开始之后这么久还没结算的，照实际结果结算
FORMAT = 2                                 # 账本格式：2 = 追加写的日志（core.store）；1 = 整份的 state.json（照样读）


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


def _migrate_v1(st: dict) -> dict:
    """整份的旧账本（format 1）：分区换成核心的叫法（files → containers，pending → permits，bash → command）。"""
    st = dict(st)
    st["containers"] = st.pop("files", {})
    pending = st.pop("pending", {})
    if isinstance(pending, dict):
        pending = {t: ({**op, "kind": "command"} if isinstance(op, dict) and op.get("kind") == "bash" else op)
                   for t, op in pending.items()}
    st["permits"] = pending
    st["format"] = FORMAT
    return st


class CodeLedger:
    def __init__(self, root: str, home: Optional[str] = None) -> None:
        self.root = os.path.realpath(root)
        self.dir = store_dir(self.root, home)
        self.core = PositionalLedger(self._fresh())
        self.file = AppendLog(os.path.join(self.dir, "state.json"), compact_lines=200, compact_bytes=256 << 10,
                              head_ratio=2.0)
        self._git: Optional[bool] = None

    # ------------------------------------------------------------------
    # 打开、存盘
    # ------------------------------------------------------------------
    def _fresh(self) -> dict:
        return {"format": FORMAT, "root": self.root, "head": None, "initialized": False, **empty_state()}

    @contextlib.contextmanager
    def open(self) -> Iterator["CodeLedger"]:
        with FileLock(os.path.join(self.dir, "lock")):
            self._load()
            yield self
            self._save()

    def _same(self, st) -> bool:
        return isinstance(st, dict) and os.path.normcase(st.get("root", "")) == os.path.normcase(self.root)

    def _load(self) -> None:
        got = self.file.read()
        st, deltas = None, []
        if got is not None and got[0] == "log" and got[1].get("format") == FORMAT and self._same(got[1]):
            st, deltas = {k: v for k, v in got[1].items() if k != "t"}, got[2]
        elif got is not None and got[0] == "whole" and isinstance(got[1], dict) and got[1].get("format") == 1 \
                and self._same(got[1]):
            st = _migrate_v1(got[1])
            self.file.ready = False                 # 下一次存盘整份重写成新格式
        if st is None:
            st = self._fresh()
            self.file.ready = False
        self.core = PositionalLedger(st)
        for d in deltas:
            self.core.apply_delta(d)
        self.core.mark_saved()
        self._top = (self.state.get("head"), self.state.get("initialized"))

    def _save(self) -> None:
        self.core.forget()
        os.makedirs(self.dir, exist_ok=True)
        if self.file.due():
            self.file.write_snapshot(self.core.snapshot())
        elif self.core.dirty() or self._top_changed():
            self.file.append(self.core.delta(top=("head", "initialized")))
        self._top = (self.state.get("head"), self.state.get("initialized"))

    def _top_changed(self) -> bool:
        return getattr(self, "_top", None) != (self.state.get("head"), self.state.get("initialized"))

    @property
    def state(self) -> dict:
        return self.core.state

    @property
    def files(self) -> dict:
        return self.core.containers

    @property
    def marks(self) -> dict:
        return self.core.marks

    @property
    def agents(self) -> dict:
        return self.core.agents

    @property
    def pending(self) -> dict:
        return self.core.permits

    def log(self, kind: str, **kw) -> None:
        self.core.log(kind, **kw)

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

    def describe(self, data: Optional[bytes]):
        """墓碑里记下删掉的前几行的文字。"""
        return L.text_of(as_lines(data))

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
                self.core.update_container(p, stat=stat)
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
        """第一次看到时的样子：没有作者（基线，不保护）。和 git 暂存区一样的不另存内容。"""
        sha = sha256(data)
        git_id = index_blob if index_blob and index_blob == git_blob_id(data) else None
        if git_id is None:
            self.put(data)
        self.core.baseline(path, {"sha": sha, "git": git_id, "stat": stat})

    # ------------------------------------------------------------------
    # 记一次修改
    # ------------------------------------------------------------------
    def apply_change(self, path: str, new: Optional[bytes], author: str, **extra) -> Optional[str]:
        """把账本里的 path 改成 new，作者是 author。返回这次修改的标记（没有变化时返回 None）。"""
        e = self.files.get(path)
        old = self.content(path) if e is not None else None
        if sha256(old) == sha256(new):
            return None
        old_lines, new_lines = as_lines(old), as_lines(new)
        stat = None
        if new is not None:
            self.put(new)
            try:
                st = os.stat(self.abspath(path))
                if self.read_disk(path) == new:
                    stat = [st.st_mtime_ns, st.st_size]
            except OSError:
                pass
        return self.core.record(path, L.diff(old_lines, new_lines), author, len(old_lines), whole_deleted=new is None,
                                describe=L.text_of(old_lines),
                                entry={"sha": sha256(new), "git": None, "stat": stat}, **extra)

    # ------------------------------------------------------------------
    # 判断（都由核心算：core.rules.judge）
    # ------------------------------------------------------------------
    def protected(self, mark: str, agent: str, path: str) -> bool:
        return self.core.protected(mark, agent, path)

    def reason(self, mark: str) -> str:
        return self.core.reason(mark)

    def check(self, agent: str, path: str, new: Optional[bytes]) -> dict[str, dict]:
        """agent 把 path 改成 new，会碰到哪些不能动的行：{标记: {"units", "points"}}（现在的坐标）。
        RightOfWay 自己的配置只有人能改：Agent 一行都不能改。"""
        e = self.files.get(path)
        old = self.content(path) if e is not None else None
        old_lines = as_lines(old)
        ops = L.diff(old_lines, as_lines(new))
        if not L.changed(ops) and sha256(old) == sha256(new):
            return {}
        return self.core.hits(path, ops, agent, len(old_lines), human_only=path in LOCKED)

    def protected_regions(self, agent: Optional[str] = None) -> dict[str, list[dict]]:
        return self.core.protected_regions(agent)

    # ------------------------------------------------------------------
    # Agent 的视图
    # ------------------------------------------------------------------
    def agent(self, agent: str, name: Optional[str] = None) -> dict:
        return self.core.agent(agent, name)

    def see(self, agent: str, path: str) -> None:
        self.core.see(agent, path)

    def unseen(self, agent: str, path: Optional[str] = None, untold: bool = False) -> dict[str, list[dict]]:
        return self.core.unseen(agent, path, untold)

    def tell(self, agent: str, regions: dict[str, list[dict]]) -> None:
        self.core.tell(agent, regions)

    def note(self, agent: str, text: str) -> None:
        self.core.note(agent, text)

    def take_notes(self, agent: str) -> list[str]:
        return self.core.take_notes(agent)

    # ------------------------------------------------------------------
    # 观察：执行之外的变化
    # ------------------------------------------------------------------
    def observe(self) -> list[tuple[str, str, str]]:
        """Agent 的执行之外发生的变化：[(路径, 标记, 作者)]。谁做的由核心定（rules.observed_author）：都算人的——
        除了人做的 git 操作（HEAD 变了、文件和新的 HEAD 一样）记为 git。有 Agent 的命令在执行（不是原子的）时，
        先不记（说不准是谁的），等那条命令结算。人提交（commit）之后，提交进去的行交还（变成基线）。"""
        self._expire()
        running, busy = self.core.open_scope()
        if running:
            return []
        changes = self.scan(skip=frozenset(busy))
        head = self.head()
        moved = self.git and head != self.state.get("head")
        tree = self._head_tree() if moved and head else {}
        out = []
        for path, _old, new in changes:
            author = R.observed_author(HUMAN, git=bool(moved and new is not None and tree.get(path) == git_blob_id(new)))
            mark = self.apply_change(path, new, author)
            if mark:
                out.append((path, mark, author))
        if moved:
            self.state["head"] = head
            self.release_committed(tree)
        return out

    def _expire(self) -> None:
        """没等到结束通知的执行（core：positional.expired）：编辑照实际结果结算；命令这期间的变化说不准是谁的。"""
        for tid, action in self.core.expired(edit_after=EDIT_EXPIRE):
            if action == "settle":
                self.settle_edit(tid)
            else:
                self.settle_bash(tid, author=UNATTRIBUTED)

    def release_committed(self, tree: Optional[dict[str, str]] = None) -> list[str]:
        """人提交之后（核心：交还，变成基线）：和 HEAD 一样的行、已经提交的删除不再保护。"""
        tree = self._head_tree() if tree is None else tree
        released = []
        for path, e in sorted(self.files.items()):
            if not self.core.has_held(path):
                continue
            cur = self.content(path)
            head_blob = self._git_cat(tree[path]) if path in tree else None
            if cur is None and head_blob is None:
                if e.get("points"):
                    self.core.drop_points(path)
                    released.append(path)
                continue
            if head_blob is None:
                continue
            cur_lines = as_lines(cur)
            ops = L.diff(as_lines(head_blob), cur_lines)
            same = [k for j1, j2 in equal_spans(ops) for k in range(j1, j2)]

            def committed(p: dict, ops=ops) -> bool:      # HEAD 在这里也没有多出来的行：删除已经提交了
                return not any(tag != "equal" and j1 <= p["pos"] <= j2 for tag, _i1, _i2, j1, j2 in ops)
            if self.core.settle_to_baseline(path, len(cur_lines), same, committed):
                released.append(path)
        if released:
            self.log("commit_release", paths=released)
        return released

    # ------------------------------------------------------------------
    # Agent 的一次执行（许可单）
    # ------------------------------------------------------------------
    def begin_edit(self, agent: str, tid: str, path: str, intended: Optional[bytes], tool: str) -> None:
        e = self.files.get(path)
        self.core.open_permit(tid, agent, "edit", tool=tool, path=path, base=(e or {}).get("sha"),
                              intended=self.put(intended) if intended is not None else None)

    def begin_bash(self, agent: str, tid: str, timeout_s: float) -> None:
        self.core.open_permit(tid, agent, "command", until=round(time.time() + timeout_s + 60, 3), head=self.head())

    def cancel(self, tid: str) -> None:
        self.core.end_permit(tid)

    def settle_edit(self, tid: str) -> dict:
        """一次编辑（Edit、Write……）结束：按磁盘上的实际内容结算（核心：rules.settle_predicted）。"""
        op = self.core.end_permit(tid)
        if op is None:
            return {"status": "unknown"}
        agent, path = op["agent"], op["path"]
        disk = self.read_disk(path)
        known = self.content(path) if path in self.files else None
        predicted = op.get("intended")
        hits = self.check(agent, path, disk) if predicted is None and sha256(disk) != sha256(known) else {}
        d = R.settle_predicted(actual=sha256(disk), known=sha256(known), predicted=predicted,
                               hits_if_unpredicted=bool(hits))
        if d.status == "unchanged":
            return {"status": "unchanged", "path": path}
        if d.status == "applied_extra":
            m1 = self.apply_change(path, self.blob(predicted), agent)
            m2 = self.apply_change(path, disk, UNATTRIBUTED)
            return {"status": "applied", "path": path, "mark": m1, "unattributed": m2}
        if d.status == "restore":
            backup = self.revert(path, known, disk, tid)
            return {"status": "reverted", "path": path, "hits": hits, "backup": backup}
        return {"status": "applied", "path": path, "mark": self.apply_change(path, disk, agent)}

    def settle_bash(self, tid: str, author: Optional[str] = None) -> dict:
        """一条命令（Bash）结束，按磁盘上的变化结算（核心：rules.settle_window，每个文件一个结论）：
        算这个 Agent 的、改到了不能动的行的文件整个放回执行前（AI 的版本另存一份）；说不准是谁的记为来源不明、不放回，
        改动之前的版本另存一份。Agent 的命令里做的提交（commit）不算人交还。
        author=UNATTRIBUTED：命令没等到结束通知，变化一律说不准是谁的。"""
        op = self.core.end_permit(tid)
        if op is None:
            return {"status": "unknown", "files": [], "reverted": [], "unsure": []}
        agent = op["agent"]
        _running, busy = self.core.open_scope()
        changes = self.scan(skip=frozenset(busy))
        took = time.time() - op.get("time", time.time())
        refs: dict[str, set] = {}
        if changes and self.git:
            for table in (self._head_tree(), self._git_index()):
                for p, blob in table.items():
                    refs.setdefault(p, set()).add(blob)
        files, reverted, unsure = [], [], []
        for path, old, new in changes:
            hits = self.check(agent, path, new)
            d = R.settle_window(hits=bool(hits), quick=took <= BASH_WINDOW,
                                from_reference=new is not None and git_blob_id(new) in refs.get(path, ()),
                                deleted=new is None, lost=author is not None, human_only=path in LOCKED)
            if d.restore:
                reverted.append({"path": path, "hits": hits, "backup": self.revert(path, old, new, tid)})
                continue
            if d.author == "agent":
                if self.apply_change(path, new, agent):
                    files.append(path)
                continue
            backup = self._backup(path, old, tid, "before") if d.backup_before and old is not None else None
            if self.apply_change(path, new, UNATTRIBUTED):
                unsure.append({"path": path, "hits": hits, "backup": backup})
        head = self.head()
        if head != self.state.get("head"):
            self.state["head"] = head                 # Agent 的 git 操作：不算人提交，不解除保护
        return {"status": "done", "files": files, "reverted": reverted, "unsure": unsure,
                "head_moved": head != op.get("head"), "seconds": round(took, 2)}

    # ------------------------------------------------------------------
    # 放回（这一侧的动作）
    # ------------------------------------------------------------------
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
        if path in self.files:
            try:
                st = os.stat(full)
                stat = [st.st_mtime_ns, st.st_size] if old is not None else None
            except OSError:
                stat = None
            self.core.update_container(path, stat=stat)
        self.log("revert", path=path, backup=backup)
        return backup

    # ------------------------------------------------------------------
    # 人交还
    # ------------------------------------------------------------------
    def release(self, path: Optional[str] = None, first: Optional[int] = None, last: Optional[int] = None) -> list[dict]:
        """人交还（核心：hand_back）：path 为 None 时全部；只给 path 时整个文件；给了行号（从 1 开始、两端都含）时只交还这几行。
        返回交还了的 [{"path", "units": [(起, 止)], "points": n}]。"""
        return self.core.hand_back(path, first, last)


def dump(led: CodeLedger) -> str:
    """调试用：账本的整份内容。"""
    return json.dumps(led.state, ensure_ascii=False, indent=1)
