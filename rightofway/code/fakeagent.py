"""一个按 Claude Code 的方式调用工具、调用 hooks 的假 Agent（测试、实验用，像 fakeapp.py 之于 Blender）。

工具的行为照 Claude Code：Edit 是字符串替换，old_string 找不到或者不唯一就报错（文件不动）；Write 整份写；
Bash 真的在项目目录里执行命令。protect=True 时每次工具调用前后都调用 hooks（rightofway.code.hooks.handle），
和真的 Claude Code 配好 hooks 时一样：PreToolUse 拒绝就不执行，执行了再 PostToolUse。protect=False 是对照组：
没有 RightOfWay，工具直接执行。"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from .hooks import handle


@dataclass
class ToolResult:
    status: str                 # ok | denied | error
    text: str = ""              # 拒绝理由，或者执行之后 hooks 给 Agent 的说明
    alert: str = ""             # hooks 给人的提醒（systemMessage）


class FakeClaudeCode:
    def __init__(self, repo: str, home: Optional[str] = None, session: str = "sess-0001", protect: bool = True,
                 env: Optional[dict] = None) -> None:
        self.repo, self.home, self.session, self.protect = repo, home, session, protect
        self.env = {**os.environ, **(env or {})}
        self.n = 0
        self.applied = self.refused = 0
        if protect:
            self._hook("SessionStart")

    def _hook(self, event: str, **kw):
        return handle({"hook_event_name": event, "session_id": self.session, "cwd": self.repo, **kw}, home=self.home)

    def _tid(self) -> str:
        self.n += 1
        return f"toolu_{self.session[-4:]}_{self.n:04d}"

    def path(self, name: str) -> str:
        return os.path.join(self.repo, *name.split("/"))

    def text(self, name: str) -> Optional[str]:
        try:
            with open(self.path(name), encoding="utf-8", newline="") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def _put(self, name: str, text: str) -> None:
        os.makedirs(os.path.dirname(self.path(name)), exist_ok=True)
        with open(self.path(name), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    # ------------------------------------------------------------------
    def prompt(self, text: str) -> str:
        if not self.protect:
            return ""
        r = self._hook("UserPromptSubmit", prompt=text)
        out = r.out or {}
        return out.get("reason") or (out.get("hookSpecificOutput") or {}).get("additionalContext", "")

    def read(self, name: str) -> tuple[Optional[str], str]:
        text = self.text(name)
        if not self.protect:
            return text, ""
        r = self._hook("PostToolUse", tool_name="Read", tool_input={"file_path": self.path(name)},
                       tool_use_id=self._tid())
        return text, ((r.out or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")

    def _call(self, tool: str, ti: dict, effect) -> ToolResult:
        tid = self._tid()
        if self.protect:
            pre = self._hook("PreToolUse", tool_name=tool, tool_input=ti, tool_use_id=tid)
            out = pre.out or {}
            if (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny":
                self.refused += 1
                return ToolResult("denied", out["hookSpecificOutput"]["permissionDecisionReason"],
                                  out.get("systemMessage", ""))
        err = effect()
        if err:
            if self.protect:
                self._hook("PostToolUseFailure", tool_name=tool, tool_input=ti, tool_use_id=tid)
            return ToolResult("error", err)
        self.applied += 1
        if not self.protect:
            return ToolResult("ok")
        post = self._hook("PostToolUse", tool_name=tool, tool_input=ti, tool_use_id=tid)
        out = post.out or {}
        text = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
        if "改回执行前" in text:
            self.applied -= 1
            self.refused += 1
        return ToolResult("ok", text, out.get("systemMessage", ""))

    def edit(self, name: str, old: str, new: str, replace_all: bool = False) -> ToolResult:
        def effect():
            cur = self.text(name)
            if cur is None:
                return f"File does not exist: {name}"
            n = cur.count(old)
            if n == 0:
                return "String to replace not found in file."
            if n > 1 and not replace_all:
                return f"Found {n} matches of the string to replace, but replace_all is false."
            self._put(name, cur.replace(old, new) if replace_all else cur.replace(old, new, 1))
            return None
        return self._call("Edit", {"file_path": self.path(name), "old_string": old, "new_string": new,
                                   "replace_all": replace_all}, effect)

    def write(self, name: str, content: str) -> ToolResult:
        return self._call("Write", {"file_path": self.path(name), "content": content},
                          lambda: self._put(name, content))

    def bash(self, command: str) -> ToolResult:
        def effect():
            subprocess.run(command, shell=True, cwd=self.repo, env=self.env, capture_output=True, timeout=120)
            return None
        return self._call("Bash", {"command": command}, effect)
