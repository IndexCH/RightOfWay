"""AI 这一侧：Claude Code 的 hooks（spec/design_code.md 第 3 节）。不改 Claude Code，只加配置：Claude Code 每次调用
工具前后、人每次发消息、会话开始时，运行 `python -m rightofway.code hook`，从标准输入读事件（JSON），输出 JSON。

这里只做 AI 这一侧的事（P9）：把事件翻成"AI 想做什么"（这次编辑、这条命令）和"做完了"，事先能算出结果的
（Edit、MultiEdit、Write：和 Claude Code 一样做字符串替换）先算出来；放行还是拒绝、怎么结算、能不能改，都问核心
（rightofway/core，经由文本文件这一侧 ledger.py）；要对 AI 说的话由 messages.py 从账本写出来。

- 会话开始（SessionStart）：告诉 Agent 规则，以及现在哪些行受保护。
- 人发消息（UserPromptSubmit）：人在消息里写"交还 路径:行"就交还；告诉 Agent 它上次看之后别人改了什么（R22）。
- 编辑之前（PreToolUse：Edit、MultiEdit、Write、NotebookEdit）：先算出这次编辑之后文件的样子，碰到不能动的行，
  整次编辑拒绝（一行都不写），告诉它是哪几行、现在是什么内容。
- 命令之前（PreToolUse：Bash）：命令会改什么事先不知道，只记下"开始了"（开许可单）。
- 之后（PostToolUse、PostToolUseFailure）：按磁盘上的实际内容结算。命令改到了不能动的行的文件，整个改回执行前，
  Agent 的版本另存一份；读文件（Read）之后，告诉它这个文件里它没看过的变化。

人在编辑器里的修改，在下一次 hook 运行时看到（只看存盘的内容）：两次 Agent 执行之间磁盘上的变化，都是人的。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from ..core.interfaces import Intent, Outcome, Session
from .ledger import CodeLedger, find_root
from .messages import _span, changes_text, human_revert_text, human_unsure_text, loop_text, refusal_text, \
    revert_text, unsure_text, who   # noqa: F401  （旧的导入位置）
from .session import EDIT_TOOLS, TextSession, simulate

__all__ = ["EDIT_TOOLS", "Result", "agent_of", "handle", "intent_of", "simulate", "parse_release", "_norm", "_span", "who",
           "refusal_text", "changes_text", "revert_text", "human_revert_text", "unsure_text", "human_unsure_text",
           "loop_text"]



@dataclass
class Result:
    code: int = 0
    out: Optional[dict] = None
    err: str = ""
    texts: list[str] = field(default_factory=list)


def agent_of(payload: dict) -> tuple[str, str]:
    sid = str(payload.get("session_id") or "unknown")[:8]
    sub = payload.get("agent_id")
    ident = f"claude:{sid}" + (f"/{str(sub)[:8]}" if sub else "")
    name = f"Claude Code（会话 {sid}" + (f"，{payload.get('agent_type') or '子 Agent'}" if sub else "") + "）"
    return ident, name


def intent_of(tool: str, ti: dict, tid: Optional[str]) -> Intent:
    """Claude Code 的一次工具调用 → AI 想做什么。Edit、MultiEdit、Write、NotebookEdit 是对一个文件的修改；
    Bash 是一条命令（事后才知道改了什么）；其余的（例如 MCP 的工具）是一次工具调用。"""
    if tool in EDIT_TOOLS:
        return Intent("edit", tool, ti.get("file_path") or ti.get("notebook_path"), ti, tid)
    if tool == "Bash":
        return Intent("command", tool, None, ti, tid)
    return Intent("tool", tool, None, ti, tid)


def handle(payload: dict, home: Optional[str] = None, session: Optional[Session] = None) -> Result:
    """处理一次 hook 事件。返回退出码、要输出的 JSON、给 Claude Code 的标准错误。
    session：对面的应用侧（core.interfaces.Session）；默认是 cwd 所在项目的文本文件（code/session.py）。"""
    event = payload.get("hook_event_name") or ""
    cwd = payload.get("cwd") or os.getcwd()
    s = session if session is not None else TextSession(find_root(cwd), home)
    agent, name = agent_of(payload)
    if event == "SessionStart":
        lines = s.intro(agent, name)
        return Result(0, {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": "\n".join(lines)}}, texts=lines)
    if event == "UserPromptSubmit":
        # 交还只能来自人：子 Agent（Task 工具）收到的提示是主 Agent 写的，里面的"交还"不算
        only, texts = s.prompt(agent, name, str(payload.get("prompt") or ""), human=not payload.get("agent_id"))
        if only:
            return Result(0, {"decision": "block", "reason": "\n".join(texts)})
        if not texts:
            return Result()
        return Result(0, {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": "\n\n".join(texts)}}, texts=texts)
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    tid = str(payload["tool_use_id"]) if payload.get("tool_use_id") else None
    if event == "PreToolUse":
        d = s.begin(agent, name, intent_of(tool, ti, tid))
        if d.allowed:
            return Result()
        res = _deny(d.message)
        if d.alert:
            res.out["systemMessage"] = d.alert
        return res
    if event in ("PostToolUse", "PostToolUseFailure"):
        if tool == "Read":
            texts = s.look(agent, name, ti.get("file_path"))
            o = Outcome(texts=texts)
        else:
            o = s.end(agent, name, intent_of(tool, ti, tid), failed=event == "PostToolUseFailure")
        out: dict = {}
        if o.texts:
            out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": o.message}
        if o.alerts:
            out["systemMessage"] = o.alert
        return Result(0, out or None, texts=o.texts)
    return Result()


def _deny(reason: str) -> Result:
    return Result(0, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}})


# ---------------------------------------------------------------------------
# 人发消息、会话开始
# ---------------------------------------------------------------------------
_RELEASE = re.compile(r"^\s*(?:交还|rightofway\s+release)[ \t:：]+(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_SPEC = re.compile(r"^(?P<path>.+?)(?::(?P<a>\d+)(?:\s*[-–~]\s*(?P<b>\d+))?)?$")


def parse_release(prompt: str, led: CodeLedger) -> tuple[list[tuple[Optional[str], Optional[int], Optional[int]]], bool]:
    """消息里的交还命令：每行"交还 路径[:起[-止]] ..."或"交还 全部"。返回 ([(路径, 起, 止)], 消息是不是只有命令)。"""
    specs: list[tuple[Optional[str], Optional[int], Optional[int]]] = []
    for m in _RELEASE.finditer(prompt):
        for arg in re.split(r"[\s,，、]+", m.group(1).strip()):
            if not arg:
                continue
            if arg.lower() in ("全部", "all", "*"):
                specs.append((None, None, None))
                continue
            g = _SPEC.match(arg)
            if not g:
                continue
            rel = _norm(led, g.group("path"))
            if rel is None:
                continue
            a = int(g.group("a")) if g.group("a") else None
            b = int(g.group("b")) if g.group("b") else a
            specs.append((rel, a, b))
    only = bool(specs) and not _RELEASE.sub("", prompt).strip()
    return specs, only


def _norm(led: CodeLedger, p: str) -> Optional[str]:
    """人写的路径 → 项目里的相对路径（/ 分隔）。"""
    if os.path.isabs(p):
        return led.relpath(p)
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p or None
