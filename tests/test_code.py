"""代码接入（spec/design_code.md）：人在编辑器里改、Claude Code 同时改同一批文件。

这里不启动真的 Claude Code：Harness 按 Claude Code 的做法调用 hooks（工具执行前 PreToolUse、执行后 PostToolUse），
中间自己执行工具（Edit 就是字符串替换，Bash 就是真的跑命令）。人的修改就是直接写文件（存盘）。
"""
import json
import os
import subprocess
import sys

import pytest

from rightofway.code import lines as L
from rightofway.code.hooks import handle, simulate
from rightofway.code.ledger import CodeLedger, FileLock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_NOSYSTEM": "1"}

APP = ("def price(x):\n"
       "    return x * 1.1\n"
       "\n"
       "\n"
       "def tax(x):\n"
       "    return x * 0.2\n"
       "\n"
       "\n"
       "def total(x):\n"
       "    return price(x) + tax(x)\n")


class Harness:
    """一个项目目录 + 按 Claude Code 的顺序调用 hooks 的 Agent。"""

    def __init__(self, tmp_path, git=True, files=None):
        self.repo = str(tmp_path / "repo")
        self.home = str(tmp_path / "home")
        os.makedirs(self.repo)
        for name, text in (files or {"app.py": APP, "util.py": "def helper():\n    return 1\n"}).items():
            self.human_write(name, text)
        self.git = git
        if git:
            self.run_git("init", "-q")
            self.run_git("add", ".")
            self.run_git("commit", "-qm", "init")
        self.n = 0
        self.last = None
        self.hook("SessionStart")

    # ---- 人
    def path(self, name):
        return os.path.join(self.repo, *name.split("/"))

    def human_write(self, name, text):
        os.makedirs(os.path.dirname(self.path(name)), exist_ok=True)
        with open(self.path(name), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def human_replace(self, name, old, new):
        self.human_write(name, self.text(name).replace(old, new, 1))

    def text(self, name):
        try:
            with open(self.path(name), encoding="utf-8", newline="") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def run_git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True,
                              env={**os.environ, **GIT_ENV})

    # ---- Claude Code
    def hook(self, event, session="sess-a-0001", **kw):
        payload = {"hook_event_name": event, "session_id": session, "cwd": self.repo, **kw}
        self.last = handle(payload, home=self.home)
        return self.last

    def _tid(self):
        self.n += 1
        return f"toolu_{self.n:04d}"

    def read(self, name, session="sess-a-0001"):
        r = self.hook("PostToolUse", session=session, tool_name="Read", tool_input={"file_path": self.path(name)},
                      tool_use_id=self._tid())
        return context(r)

    def tool(self, tool, ti, effect, session="sess-a-0001"):
        """调用一个工具：PreToolUse 拒绝就不执行；否则执行 effect()，再 PostToolUse。返回 (是否执行, 拒绝理由, 之后的说明)。"""
        tid = self._tid()
        pre = self.hook("PreToolUse", session=session, tool_name=tool, tool_input=ti, tool_use_id=tid)
        out = pre.out or {}
        if out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
            return False, out["hookSpecificOutput"]["permissionDecisionReason"], out.get("systemMessage")
        effect()
        post = self.hook("PostToolUse", session=session, tool_name=tool, tool_input=ti, tool_use_id=tid)
        return True, context(post), (post.out or {}).get("systemMessage")

    def edit(self, name, old, new, session="sess-a-0001", replace_all=False):
        ti = {"file_path": self.path(name), "old_string": old, "new_string": new, "replace_all": replace_all}

        def effect():
            cur = self.text(name)
            assert cur is not None and old in cur, "Claude Code 的 Edit 找不到 old_string 会报错"
            self.human_write(name, cur.replace(old, new) if replace_all else cur.replace(old, new, 1))
        return self.tool("Edit", ti, effect, session)

    def write(self, name, content, session="sess-a-0001"):
        return self.tool("Write", {"file_path": self.path(name), "content": content},
                         lambda: self.human_write(name, content), session)

    def bash(self, cmd, session="sess-a-0001"):
        return self.tool("Bash", {"command": cmd},
                         lambda: subprocess.run(cmd, shell=True, cwd=self.repo, env={**os.environ, **GIT_ENV},
                                                capture_output=True), session)

    def prompt(self, text, session="sess-a-0001"):
        return self.hook("UserPromptSubmit", session=session, prompt=text)

    # ---- 核对
    def ledger(self):
        led = CodeLedger(self.repo, self.home)
        with led.open():
            pass
        return led

    def consistent(self):
        """不变量：账本里每个文件的内容都和磁盘上一样（P1）。"""
        led = CodeLedger(self.repo, self.home)
        bad = []
        with led.open():
            for path, e in led.files.items():
                if led.content(path) != led.read_disk(path):
                    bad.append(path)
        return bad


def context(res):
    out = (res.out or {}).get("hookSpecificOutput") or {}
    return out.get("additionalContext", "")


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


# ---------------------------------------------------------------------------
# 按行：差异、标记、删除点
# ---------------------------------------------------------------------------
def test_marks_follow_lines_through_later_changes():
    old = [b"a\n", b"b\n", b"c\n"]
    owners, points = L.apply([None] * 3, [], L.diff(old, [b"a\n", b"B\n", b"c\n"]), "h1", old)
    assert owners == [None, "h1", None]
    new = [b"x\n", b"a\n", b"B\n", b"c\n"]                       # 别人在开头插了一行：标记跟着往下移
    owners, points = L.apply(owners, points, L.diff([b"a\n", b"B\n", b"c\n"], new), "a1", [b"a\n", b"B\n", b"c\n"])
    assert owners == ["a1", None, "h1", None]


def test_deleting_lines_leaves_a_point_and_line_endings_do_not_count():
    old = [b"a\n", b"b\n", b"c\n"]
    owners, points = L.apply([None] * 3, [], L.diff(old, [b"a\n", b"c\n"]), "h1", old)
    assert points == [{"id": "h1", "pos": 1, "count": 1, "text": ["b"]}]
    assert not L.changed(L.diff([b"a\n", b"c\n"], [b"a\r\n", b"c\r\n"]))


def test_hits_are_overlaps_not_neighbours():
    owners = [None, "h1", None]
    prot = lambda m: m == "h1"                                  # noqa: E731
    cur = [b"a\n", b"B\n", b"c\n"]
    assert L.hits(owners, [], L.diff(cur, [b"a\n", b"B\n", b"c2\n"]), prot) == {}            # 改别的行
    assert L.hits(owners, [], L.diff(cur, [b"a\n", b"B\n", b"new\n", b"c\n"]), prot) == {}   # 紧挨着插入
    assert set(L.hits(owners, [], L.diff(cur, [b"a\n", b"b\n", b"c\n"]), prot)) == {"h1"}    # 改回去
    point = {"id": "h1", "pos": 1, "count": 1}
    assert L.hits([None, None], [point], L.diff([b"a\n", b"c\n"], [b"a\n", b"b\n", b"c\n"]), prot)  # 在人删掉的地方写回去


def test_simulate_edit_like_claude_code():
    cur = b"a = 1\nb = 1\n"
    assert simulate("Edit", {"old_string": "a = 1", "new_string": "a = 2"}, cur) == b"a = 2\nb = 1\n"
    assert simulate("Edit", {"old_string": "= 1", "new_string": "= 2"}, cur) is None             # 不唯一
    assert simulate("Edit", {"old_string": "= 1", "new_string": "= 2", "replace_all": True}, cur) == b"a = 2\nb = 2\n"
    assert simulate("Edit", {"old_string": "a = 1\nb", "new_string": "a = 3\nb"}, b"a = 1\r\nb = 1\r\n") == b"a = 3\r\nb = 1\r\n"
    assert simulate("Write", {"content": "x\n"}, cur) == b"x\n"


# ---------------------------------------------------------------------------
# Edit / Write：碰到人的行，整次拒绝
# ---------------------------------------------------------------------------
def test_an_edit_that_touches_the_humans_line_is_refused_whole(h):
    h.read("app.py")
    h.human_replace("app.py", "x * 1.1", "x * 1.25")                     # 人改了第 2 行（存盘）
    ok, reason, _ = h.edit("app.py", "x * 1.25", "x * 1.3")
    assert not ok and "第 2 行" in reason and "x * 1.25" in reason and "交还 app.py:2" in reason
    assert "x * 1.25" in h.text("app.py")
    ok, _, _ = h.edit("app.py", "x * 0.2", "x * 0.25")                    # 同一个文件里别的行照常
    assert ok and "x * 0.25" in h.text("app.py") and "x * 1.25" in h.text("app.py")
    assert h.consistent() == []


def test_a_stale_full_rewrite_is_refused(h):
    h.read("app.py")
    stale = APP.replace("x * 0.2", "x * 0.3")                             # Agent 按它读到的旧内容整份重写
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    ok, reason, _ = h.write("app.py", stale)
    assert not ok and "第 2 行" in reason
    assert "x * 1.25" in h.text("app.py") and "x * 0.3" not in h.text("app.py")      # 一行都没写
    ok, _, _ = h.write("app.py", h.text("app.py").replace("x * 0.2", "x * 0.3"))       # 保留人的行再写：照常
    assert ok and "x * 0.3" in h.text("app.py") and "x * 1.25" in h.text("app.py")


def test_inserting_next_to_the_humans_line_is_allowed(h):
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    ok, _, _ = h.edit("app.py", "def tax(x):\n", "def tax(x):\n    # 税率\n")
    assert ok
    ok, reason, _ = h.edit("app.py", "def price(x):\n    return x * 1.25\n", "def price(x):\n    return x\n")
    assert not ok and "第 2 行" in reason


def test_what_the_human_deleted_cannot_be_written_back(h):
    h.human_write("app.py", APP.replace("def tax(x):\n    return x * 0.2\n", ""))       # 人删掉 tax
    ok, reason, _ = h.write("app.py", APP)                                             # Agent 按旧内容写回
    assert not ok and "删掉了" in reason
    os.remove(h.path("util.py"))                                                       # 人删掉整个文件
    ok, reason, _ = h.write("util.py", "def helper():\n    return 2\n")
    assert not ok and "整个文件被删掉了" in reason and h.text("util.py") is None


def test_the_hooks_config_is_human_only(h):
    h.human_write(".gitignore", ".claude/settings.local.json\n")
    h.human_write(".claude/settings.local.json", '{"hooks": {"PreToolUse": []}}\n')   # 人装的 hooks（git 忽略的文件）
    h.prompt("开始")
    ok, reason, _ = h.write(".claude/settings.local.json", "{}")
    assert not ok and "只有人能改" in reason
    ok, text, _ = h.bash("echo '{}' > .claude/settings.local.json")               # 用命令改：改回去
    assert ok and "PreToolUse" in h.text(".claude/settings.local.json") and "配置" in text


# ---------------------------------------------------------------------------
# Bash：事后核对，碰到人的行的文件整个改回去
# ---------------------------------------------------------------------------
def test_git_checkout_by_the_agent_does_not_lose_the_humans_edit(h):
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    h.prompt("帮我看看")                                                   # 人的修改在这里被记下
    ok, text, alert = h.bash("git checkout -- app.py")
    assert ok and "改回执行前" in text and "x * 1.25" in h.text("app.py")
    assert alert and "app.py" in alert
    backup = text.split("另存在 ")[1].split("）")[0]
    assert "x * 1.1" in open(backup, encoding="utf-8").read()             # Agent 的版本另存了一份
    assert h.consistent() == []


def test_a_formatter_keeps_the_rest_and_says_it_may_be_half_done(h):
    h.human_replace("app.py", "x * 1.1", "x*1.25")
    fmt = (f'"{sys.executable}" -c "import pathlib;'
           "[p.write_text(p.read_text().replace('x*', 'x * ').replace('return 1', 'return  1')) for p in pathlib.Path('.').glob('*.py')]\"")
    ok, text, _ = h.bash(fmt)
    assert ok and "x*1.25" in h.text("app.py")                            # 人的那个文件整个改回
    assert "return  1" in h.text("util.py") and "只生效了一半" in text      # 别的文件照常生效
    assert h.consistent() == []


def test_a_rebuild_by_command_is_held_like_a_write(h):
    os.remove(h.path("util.py"))
    h.prompt("继续")
    ok, text, _ = h.bash("git checkout -- util.py")                        # 把人删掉的文件找回来
    assert ok and h.text("util.py") is None and "整个文件被删掉了" in text


# ---------------------------------------------------------------------------
# 交还、提交
# ---------------------------------------------------------------------------
def test_release_by_prompt_then_the_agent_can_change_it(h):
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    assert not h.edit("app.py", "x * 1.25", "x * 1.3")[0]
    r = h.prompt("交还 app.py:2")
    assert r.out["decision"] == "block" and "已交还" in r.out["reason"]   # 只有命令：不发给 AI
    ok, _, _ = h.edit("app.py", "x * 1.25", "x * 1.3")
    assert ok and "x * 1.3" in h.text("app.py")
    h.human_replace("app.py", "x * 0.2", "x * 0.21")
    r = h.prompt("交还 全部\n然后把 tax 改成 0.22")                        # 命令加上别的话：照常发给 AI，带上说明
    assert "decision" not in (r.out or {}) and "交还" in context(r)


def test_the_humans_commit_releases_but_the_agents_commit_does_not(h):
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    h.human_replace("util.py", "return 1", "return 7")
    h.prompt("先看看")
    ok, _, _ = h.bash("git commit -qam wip")                               # Agent 提交：不算人交还
    assert ok and not h.edit("app.py", "x * 1.25", "x * 1.3")[0]
    h.run_git("commit", "-q", "--allow-empty", "-m", "human")              # 人自己提交（HEAD 变了）
    ok, _, _ = h.edit("app.py", "x * 1.25", "x * 1.3")
    assert ok


def test_only_committed_lines_are_released(h):
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    h.prompt("看看")
    h.run_git("commit", "-qam", "human commits price")
    h.human_replace("app.py", "x * 0.2", "x * 0.21")                       # 提交之后又改了一行，没提交
    h.prompt("继续")
    assert h.edit("app.py", "x * 1.25", "x * 1.3")[0]                     # 提交了的：可以改
    assert not h.edit("app.py", "x * 0.21", "x * 0.3")[0]                 # 没提交的：还受保护


def test_switching_branches_is_not_a_human_edit(h):
    main = h.run_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    h.run_git("checkout", "-qb", "other")
    h.human_replace("util.py", "return 1", "return 3")
    h.run_git("commit", "-qam", "other")
    h.prompt("看看")                         # 会话开始时在 main；现在 HEAD 变了，util.py 和新的 HEAD 一样：git 带来的
    led = h.ledger()
    assert not led.protected_regions()
    marks = {m["author"] for m in led.marks.values()}
    assert marks == {"git"}
    h.read("app.py", session="sess-b-0002")
    h.run_git("checkout", "-q", main)                                      # 切回去
    ok, reason, _ = h.write("util.py", "def helper():\n    return 3\n", session="sess-b-0002")
    assert not ok and "git 操作" in reason                                  # B 没看过切换之后的 util.py：不能按旧印象写


# ---------------------------------------------------------------------------
# 告诉 Agent 它错过了什么；多个 Agent
# ---------------------------------------------------------------------------
def test_the_agent_is_told_what_changed_since_it_last_looked_once(h):
    h.read("app.py")
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    text = h.read("app.py")
    assert "人 改了 app.py 第 2 行" in text and "x * 1.25" in text and "你不能改" in text
    assert h.read("app.py") == ""                                          # 同一件事只说一次
    h.human_replace("util.py", "return 1", "return 2")
    assert "util.py 第 2 行" in context(h.prompt("下一步"))


def test_a_second_agent_cannot_overwrite_what_the_first_wrote_after_it_read(h):
    h.read("app.py", session="sess-b-0002")                                # B 先读
    assert h.edit("app.py", "x * 0.2", "x * 0.25", session="sess-a-0001")[0]   # A 改
    ok, reason, _ = h.write("app.py", APP.replace("x * 1.1", "x * 1.2"), session="sess-b-0002")
    assert not ok and "你上次读 app.py 之后改了" in reason and "Claude Code（会话 sess-a-0" in reason
    h.read("app.py", session="sess-b-0002")                                # B 重新读过：可以改 A 的行了
    assert h.write("app.py", h.text("app.py").replace("x * 0.25", "x * 0.3"), session="sess-b-0002")[0]


def test_repeated_attempts_alert_the_human(h):
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    alerts = [h.edit("app.py", "x * 1.25", f"x * 1.{k}")[2] for k in range(3, 6)]
    assert alerts[:2] == [None, None] and "3 次" in alerts[2] and "交还 app.py:2" in alerts[2]


# ---------------------------------------------------------------------------
# 结果和预计不一样、没等到结束通知
# ---------------------------------------------------------------------------
def test_an_edit_whose_result_could_not_be_predicted_is_checked_after(h):
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    h.prompt("看看")
    ti = {"file_path": h.path("app.py"), "old_string": "return x * 1.25  ", "new_string": "return x"}
    ok, text, _ = h.tool("Edit", ti, lambda: h.human_replace("app.py", "x * 1.25", "x"))   # 工具"模糊匹配"改到了人的行
    assert ok and "改回执行前" in text and "x * 1.25" in h.text("app.py")


def test_a_human_save_during_the_edit_is_protected_not_given_to_the_agent(h):
    def effect():
        h.human_replace("app.py", "x * 0.2", "x * 0.25")                   # Agent 的编辑
        h.human_replace("app.py", "x * 1.1", "x * 1.15")                   # 同时人也存了盘
    ti = {"file_path": h.path("app.py"), "old_string": "x * 0.2", "new_string": "x * 0.25"}
    ok, text, _ = h.tool("Edit", ti, effect)
    assert ok and "说不准是谁改的" in text
    assert not h.edit("app.py", "x * 1.15", "x * 1.3")[0]                  # 多出来的那行按人的保护


def test_a_command_that_never_reported_back_is_unattributed(h, monkeypatch):
    tid = "toolu_lost"
    h.hook("PreToolUse", tool_name="Bash", tool_input={"command": "x", "timeout": 1000}, tool_use_id=tid)
    h.human_replace("app.py", "x * 0.2", "x * 0.9")                       # 命令期间的变化
    import rightofway.code.ledger as led_mod
    real = led_mod.time.time
    monkeypatch.setattr(led_mod.time, "time", lambda: real() + 3600)     # 一小时后：命令早该结束了
    h.prompt("继续")
    monkeypatch.undo()
    assert not h.edit("app.py", "x * 0.9", "x * 1")[0]                    # 说不准是谁的：按人的保护


def test_a_failed_edit_closes_cleanly(h):
    tid = "toolu_fail"
    ti = {"file_path": h.path("app.py"), "old_string": "x * 0.2", "new_string": "x * 0.25"}
    h.hook("PreToolUse", tool_name="Edit", tool_input=ti, tool_use_id=tid)
    h.hook("PostToolUseFailure", tool_name="Edit", tool_input=ti, tool_use_id=tid)
    assert not h.ledger().pending


# ---------------------------------------------------------------------------
# 没有 git、二进制文件、锁、安装
# ---------------------------------------------------------------------------
def test_works_without_git(tmp_path):
    h = Harness(tmp_path, git=False)
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    assert not h.edit("app.py", "x * 1.25", "x * 1.3")[0]
    assert h.edit("app.py", "x * 0.2", "x * 0.3")[0]


def test_binary_files_are_one_unit(tmp_path):
    h = Harness(tmp_path, files={"logo.png": "", "app.py": APP})
    with open(h.path("logo.png"), "wb") as fh:
        fh.write(b"\x89PNG\0\0human")
    h.prompt("看看")
    ok, text, _ = h.bash(f'"{sys.executable}" -c "open(\'logo.png\',\'wb\').write(b\'\\x89PNG\\0\\0agent\')"')
    assert ok and "改回执行前" in text and open(h.path("logo.png"), "rb").read().endswith(b"human")


def test_the_lock_serializes_hooks(tmp_path):
    path = str(tmp_path / "lock")
    with FileLock(path):
        with pytest.raises(TimeoutError):
            with FileLock(path, timeout=0.2):
                pass


def test_install_merges_and_is_idempotent(tmp_path):
    from rightofway.code.__main__ import install, uninstall
    proj = tmp_path / "p"
    (proj / ".claude").mkdir(parents=True)
    other = {"permissions": {"allow": ["Bash(ls)"]},
             "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}}
    (proj / ".claude" / "settings.local.json").write_text(json.dumps(other))
    install(str(proj), "/py", False)
    path = install(str(proj), "/py", False)
    st = json.loads(open(path).read())
    assert st["permissions"] == other["permissions"]
    pre = st["hooks"]["PreToolUse"]
    assert len(pre) == 2 and pre[0]["hooks"][0]["command"] == "echo hi"
    assert pre[1]["matcher"] == "Edit|MultiEdit|Write|NotebookEdit|Bash" and pre[1]["hooks"][0]["onFailure"] == "block"
    assert set(st["hooks"]) == {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure"}
    uninstall(str(proj))
    st = json.loads(open(path).read())
    assert st["hooks"] == {"PreToolUse": [other["hooks"]["PreToolUse"][0]]}


def test_the_hook_command_end_to_end(tmp_path):
    """真的起一个进程：python -m rightofway.code hook，标准输入是 Claude Code 的事件。"""
    h = Harness(tmp_path)
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    payload = {"hook_event_name": "PreToolUse", "session_id": "sess-a-0001", "cwd": h.repo, "tool_name": "Edit",
               "tool_use_id": "toolu_x", "tool_input": {"file_path": h.path("app.py"), "old_string": "x * 1.25",
                                                        "new_string": "x * 1.3"}}
    out = subprocess.run([sys.executable, "-m", "rightofway.code", "hook"], input=json.dumps(payload).encode(),
                         capture_output=True, cwd=ROOT, env={**os.environ, "RIGHTOFWAY_HOME": h.home,
                                                             "PYTHONPATH": ROOT}, timeout=60)
    assert out.returncode == 0, out.stderr.decode()
    res = json.loads(out.stdout.decode())
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_an_internal_error_blocks_edits_instead_of_letting_them_through(tmp_path):
    h = Harness(tmp_path)
    with open(os.path.join(h.ledger().dir, "state.json"), "w") as fh:
        fh.write('{"format": 1, "root": ' + json.dumps(os.path.realpath(h.repo)) + ', "files": 5}')   # 坏掉的账本
    payload = {"hook_event_name": "PreToolUse", "session_id": "s", "cwd": h.repo, "tool_name": "Write",
               "tool_use_id": "t", "tool_input": {"file_path": h.path("app.py"), "content": "x\n"}}
    out = subprocess.run([sys.executable, "-m", "rightofway.code", "hook"], input=json.dumps(payload).encode(),
                         capture_output=True, cwd=ROOT, env={**os.environ, "RIGHTOFWAY_HOME": h.home,
                                                             "PYTHONPATH": ROOT}, timeout=60)
    assert out.returncode == 2 and "RightOfWay 出错" in out.stderr.decode()


# ---------------------------------------------------------------------------
# 命令执行得比较久：这期间不是 git 带来的变化，说不准是谁改的
# ---------------------------------------------------------------------------
@pytest.fixture
def slow(monkeypatch):
    import rightofway.code.ledger as led_mod
    monkeypatch.setattr(led_mod, "BASH_WINDOW", -1.0)                     # 每条命令都算"执行了比较久"


def test_the_humans_typing_during_a_long_command_is_kept_not_reverted(h, slow):
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    h.prompt("跑一下测试")

    def effect():                                                          # 测试跑着，人接着改自己那一行
        h.human_replace("app.py", "x * 1.25", "x * 1.3")
    ok, text, alert = h.tool("Bash", {"command": "pytest"}, effect)
    assert ok and "x * 1.3" in h.text("app.py")                            # 没有改回去
    assert "说不准是命令改的还是人改的" in text and alert and "命令执行前的版本在" in alert
    backup = alert.split("命令执行前的版本在 ")[1].split("）")[0]
    assert "x * 1.25" in open(backup, encoding="utf-8").read()
    assert not h.edit("app.py", "x * 1.3", "x * 2")[0]                    # 按人的修改保护


def test_git_changes_in_a_long_command_are_still_the_agents(h, slow):
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    h.prompt("看看")
    ok, text, _ = h.bash("git checkout -- app.py")
    assert ok and "改回执行前" in text and "x * 1.25" in h.text("app.py")


def test_a_long_command_that_deletes_the_humans_file_is_undone(h, slow):
    h.human_replace("util.py", "return 1", "return 5")
    h.prompt("看看")
    ok, text, _ = h.bash("rm util.py")
    assert ok and "return 5" in h.text("util.py")


def test_new_code_from_a_long_command_is_protected_until_released(h, slow):
    gen = f'"{sys.executable}" -c "open(\'gen.py\', \'w\').write(\'X = 1\\n\')"'
    ok, text, alert = h.bash(gen)
    assert ok and "gen.py" in text and not alert                           # 没碰到人的行：不用提醒人
    assert not h.edit("gen.py", "X = 1", "X = 2")[0]
    h.prompt("交还 gen.py")
    assert h.edit("gen.py", "X = 1", "X = 2")[0]


def test_a_release_inside_a_subagent_prompt_is_not_the_humans(h):
    h.human_replace("app.py", "x * 1.1", "x * 1.25")
    h.hook("UserPromptSubmit", prompt="交还 全部", agent_id="sub-xyz-123")   # 主 Agent 写给子 Agent 的提示
    assert not h.edit("app.py", "x * 1.25", "x * 1.3")[0]


def test_post_events_without_a_tool_use_id_still_settle(h):
    ti = {"file_path": h.path("app.py"), "old_string": "x * 0.2", "new_string": "x * 0.25"}
    h.hook("PreToolUse", tool_name="Edit", tool_input=ti)                    # 没有 tool_use_id
    h.human_replace("app.py", "x * 0.2", "x * 0.25")                         # 工具执行
    h.hook("PostToolUse", tool_name="Edit", tool_input=ti)
    led = h.ledger()
    assert not led.pending and not led.protected_regions()                   # 不是人的
    last = [e for e in led.state["log"] if e["kind"] == "change" and e["path"] == "app.py"][-1]
    assert last["author"] == "claude:sess-a-0"


def test_a_huge_plain_directory_is_refused_not_guessed(tmp_path, monkeypatch):
    import rightofway.code.ledger as led_mod
    monkeypatch.setattr(led_mod, "MAX_PLAIN", 3)
    d = tmp_path / "plain"
    d.mkdir()
    for i in range(5):
        (d / f"f{i}.txt").write_text("x\n")
    with pytest.raises(RuntimeError, match="不是 git 仓库"):
        handle({"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(d)}, home=str(tmp_path / "home"))
