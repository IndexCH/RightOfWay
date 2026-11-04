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

from ..core import loops as LOOPS
from ..core import rules as R
from . import messages as M
from .ledger import LOCKED, CodeLedger, find_root
from .messages import _span, changes_text, human_revert_text, human_unsure_text, loop_text, refusal_text, \
    revert_text, unsure_text, who   # noqa: F401  （旧的导入位置）

__all__ = ["EDIT_TOOLS", "Result", "agent_of", "handle", "simulate", "parse_release", "_norm", "_span", "who",
           "refusal_text", "changes_text", "revert_text", "human_revert_text", "unsure_text", "human_unsure_text",
           "loop_text"]

EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
REPEAT = 3                                                             # 同一处被拒到第几次时提醒人


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


def handle(payload: dict, home: Optional[str] = None) -> Result:
    """处理一次 hook 事件。返回退出码、要输出的 JSON、给 Claude Code 的标准错误。"""
    event = payload.get("hook_event_name") or ""
    cwd = payload.get("cwd") or os.getcwd()
    led = CodeLedger(find_root(cwd), home)
    agent, name = agent_of(payload)
    with led.open():
        led.agent(agent, name)
        led.observe()                                   # 上一次 hook 之后磁盘上的变化：人的
        if event == "SessionStart":
            return _session_start(led, agent)
        if event == "UserPromptSubmit":
            # 交还只能来自人：子 Agent（Task 工具）收到的提示是主 Agent 写的，里面的"交还"不算
            return _prompt(led, agent, str(payload.get("prompt") or ""), human=not payload.get("agent_id"))
        tool = str(payload.get("tool_name") or "")
        ti = payload.get("tool_input") or {}
        tid = str(payload.get("tool_use_id") or f"t-{led.state['n']}")
        if event == "PreToolUse":
            return _pre(led, agent, tool, ti, tid)
        if event in ("PostToolUse", "PostToolUseFailure"):
            return _post(led, agent, event, tool, ti, tid)
    return Result()


# ---------------------------------------------------------------------------
# 编辑之前：AI 想做什么；事先能算出结果的先算出来
# ---------------------------------------------------------------------------
def simulate(tool: str, ti: dict, cur: Optional[bytes]) -> Optional[bytes]:
    """按 Claude Code 工具的输入，算出执行之后文件的内容。算不出来（例如 old_string 找不到）返回 None。"""
    if tool == "Write":
        c = ti.get("content")
        return c.encode("utf-8", "surrogateescape") if isinstance(c, str) else None
    if tool == "Edit":
        return _edit(cur, ti.get("old_string"), ti.get("new_string"), bool(ti.get("replace_all")))
    if tool == "MultiEdit":
        data = cur
        for e in ti.get("edits") or []:
            data = _edit(data, e.get("old_string"), e.get("new_string"), bool(e.get("replace_all")))
            if data is None:
                return None
        return data
    return None


def _edit(cur: Optional[bytes], old, new, replace_all: bool) -> Optional[bytes]:
    if cur is None or not isinstance(old, str) or not isinstance(new, str) or old == "":
        return None
    text = cur.decode("utf-8", "surrogateescape")
    if text.count(old) == 0 and "\r\n" in text and "\r\n" not in old:      # 文件是 CRLF、编辑里写的是 LF
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    n = text.count(old)
    if n == 0 or (n > 1 and not replace_all):
        return None
    out = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return out.encode("utf-8", "surrogateescape")


def _ignored(led: CodeLedger, path: str) -> bool:
    if not led.git:
        return False
    return led._run_git("check-ignore", "-q", "--", path) is not None


def _pre(led: CodeLedger, agent: str, tool: str, ti: dict, tid: str) -> Result:
    if tool == "Bash":
        timeout = ti.get("timeout")
        led.begin_bash(agent, tid, (float(timeout) / 1000.0) if isinstance(timeout, (int, float)) else 600.0)
        return Result()
    if tool not in EDIT_TOOLS:
        return Result()
    full = ti.get("file_path") or ti.get("notebook_path")
    path = led.relpath(str(full)) if full else None
    if path is None:
        return Result()                                 # 不在这个项目里
    if path in LOCKED:
        return _deny(f"这次编辑没有执行：{path} 是 RightOfWay 的配置，只有人能改。")
    known = path in led.files
    if not known and (os.path.exists(led.abspath(path)) or _ignored(led, path)):
        return Result()                                 # git 忽略的文件、太大的文件：不管
    cur = led.content(path) if known else None
    new = simulate(tool, ti, cur)
    if new is not None:
        hits = led.check(agent, path, new)              # 事先算出了结果：按结果核对
    elif tool == "NotebookEdit":                         # 算不出结果、执行了一定会改：文件里有对它受保护的部分，就整次拒绝
        hits = led.core.all_protected(path, agent)
    else:
        hits = {}                                       # 例如 old_string 找不到：工具多半会自己报错；万一改了，事后核对
    if R.refuse_whole(hits, separable=False):           # 一次编辑里的几行互相关联（例如改名）：整次拒绝
        loops = LOOPS.count(led.agent(agent)["refusals"], hits, REPEAT)
        res = _deny(refusal_text(led, agent, path, hits, tool))
        if loops:
            res.out["systemMessage"] = loop_text(led, path, hits, loops)
        led.log("refused", agent=agent, path=path, marks=sorted(hits))
        return res
    led.begin_edit(agent, tid, path, new, tool)
    return Result()


def _deny(reason: str) -> Result:
    return Result(0, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}})


# ---------------------------------------------------------------------------
# 之后：做完了
# ---------------------------------------------------------------------------
def _match(led: CodeLedger, agent: str, tool: str, ti: dict, tid: str) -> str:
    """执行之后的事件对上执行之前记下的那一次：按 tool_use_id；没有的话，找这个 Agent 最近一次同类、同一个文件的。"""
    if tid in led.pending:
        return tid
    full = ti.get("file_path") or ti.get("notebook_path")
    path = led.relpath(str(full)) if full else None
    cands = [(op.get("time", 0), t) for t, op in led.pending.items() if op.get("agent") == agent and (
        (tool == "Bash" and op.get("kind") == "command") or
        (tool in EDIT_TOOLS and op.get("kind") == "edit" and op.get("path") == path))]
    return max(cands)[1] if cands else tid


def _post(led: CodeLedger, agent: str, event: str, tool: str, ti: dict, tid: str) -> Result:
    tid = _match(led, agent, tool, ti, tid)
    texts, alerts = led.take_notes(agent), []
    if tool in EDIT_TOOLS and tid in led.pending:
        rep = led.settle_edit(tid)
        if rep["status"] == "reverted":
            texts.append(revert_text(led, agent, [rep], tool))
            alerts.append(human_revert_text(led, [rep]))
        elif rep["status"] == "applied":
            if rep.get("unattributed"):
                texts.append(M.extra_text(rep["path"]))
            fresh = led.unseen(agent, rep["path"], untold=True)
            if fresh:
                texts.append("你改的这个文件里，还有你没看过的变化：\n" + changes_text(led, agent, fresh))
                led.tell(agent, fresh)
    elif tool == "Bash" and tid in led.pending:
        rep = led.settle_bash(tid)
        if rep["reverted"]:
            texts.append(revert_text(led, agent, rep["reverted"], tool, files_changed=rep["files"]))
            alerts.append(human_revert_text(led, rep["reverted"]))
        if rep["unsure"]:
            texts.append(unsure_text(rep))
            if any(u["hits"] for u in rep["unsure"]):
                alerts.append(human_unsure_text(led, rep))
    elif tool == "Read":
        full = ti.get("file_path")
        path = led.relpath(str(full)) if full else None
        if path is not None and path in led.files:
            fresh = led.unseen(agent, path, untold=True)
            if fresh:
                texts.append("这个文件在你上次看之后有这些变化：\n" + changes_text(led, agent, fresh))
                led.tell(agent, fresh)
            led.see(agent, path)
    out: dict = {}
    if texts:
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": "\n\n".join(texts)}
    if alerts:
        out["systemMessage"] = "\n".join(alerts)
    return Result(0, out or None, texts=texts)


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


def _prompt(led: CodeLedger, agent: str, prompt: str, human: bool = True) -> Result:
    specs, only = parse_release(prompt, led) if human else ([], False)
    done = []
    for path, a, b in specs:
        done += led.release(path, a, b)
    if done:
        what = M.release_summary(done)
        for other in list(led.agents):
            led.note(other, f"人把这些交还给了 AI，现在可以改了：{what}。")
    if only:
        msg = ("RightOfWay：已交还 " + "；".join(d["path"] for d in done) + "。") if done else \
            "RightOfWay：没有找到可以交还的（这些行没有受保护）。"
        return Result(0, {"decision": "block", "reason": msg})
    texts = led.take_notes(agent)
    fresh = led.unseen(agent, untold=True)
    if fresh:
        texts.append("你上次看之后，别人改了这些地方：\n" + changes_text(led, agent, fresh))
        led.tell(agent, fresh)
    if not texts:
        return Result()
    return Result(0, {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": "\n\n".join(texts)}}, texts=texts)


def _session_start(led: CodeLedger, agent: str) -> Result:
    lines = M.session_start_lines(led.protected_regions())
    return Result(0, {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}},
                  texts=lines)
