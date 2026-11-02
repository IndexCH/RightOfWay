"""Claude Code 的 hooks（spec/design_code.md 第 3 节）。不改 Claude Code，只加配置：Claude Code 每次调用工具前后、
人每次发消息、会话开始时，运行 `python -m rightofway.code hook`，从标准输入读事件（JSON），按账本决定，输出 JSON。

- 会话开始（SessionStart）：告诉 Agent 规则，以及现在哪些行受保护。
- 人发消息（UserPromptSubmit）：人在消息里写"交还 路径:行"就交还；告诉 Agent 它上次看之后别人改了什么（R22）。
- 编辑之前（PreToolUse：Edit、MultiEdit、Write、NotebookEdit）：先算出这次编辑之后文件的样子，碰到不能动的行，
  整次编辑拒绝（一行都不写），告诉它是哪几行、现在是什么内容。
- 命令之前（PreToolUse：Bash）：命令会改什么事先不知道，只记下"开始了"。
- 之后（PostToolUse、PostToolUseFailure）：按磁盘上的实际内容记账。命令改到了不能动的行的文件，整个改回执行前，
  Agent 的版本另存一份；读文件（Read）之后，告诉它这个文件里它没看过的变化。

人在编辑器里的修改，在下一次 hook 运行时看到（只看存盘的内容）：两次 Agent 执行之间磁盘上的变化，都是人的。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from . import lines as L
from .ledger import GIT, HUMAN, LOCKED, UNATTRIBUTED, CodeLedger, find_root

EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
MAX_SHOW = 12                                                          # 说明里每一段最多列几行内容


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
# 编辑之前
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
        hits = led.check(agent, path, new)
    elif tool == "NotebookEdit":                         # 算不出结果：文件里有对它受保护的部分，就整次拒绝
        hits = _all_protected(led, agent, path)
    else:
        hits = {}                                       # 例如 old_string 找不到：工具多半会自己报错；万一改了，事后核对
    if hits:
        a = led.agent(agent)
        loops = []
        for m in hits:
            a["refusals"][m] = a["refusals"].get(m, 0) + 1
            if a["refusals"][m] == 3:
                loops.append(m)
        res = _deny(refusal_text(led, agent, path, hits, tool))
        if loops:
            res.out["systemMessage"] = loop_text(led, path, hits, loops)
        led.log("refused", agent=agent, path=path, marks=sorted(hits))
        return res
    led.begin_edit(agent, tid, path, new, tool)
    return Result()


def _all_protected(led: CodeLedger, agent: str, path: str) -> dict[str, dict]:
    """这个文件里对这个 Agent 受保护的全部（算不出编辑结果的工具只能这样粗查，例如 NotebookEdit）。"""
    e = led.files.get(path) or {}
    out: dict[str, dict] = {}
    for k, m in enumerate(L.unrle(e.get("owners"))):
        if m is not None and led.protected(m, agent, path):
            out.setdefault(m, {"lines": [], "points": []})["lines"].append(k)
    for p in e.get("points") or ():
        if led.protected(p["id"], agent, path):
            out.setdefault(p["id"], {"lines": [], "points": []})["points"].append(p)
    return out


def _deny(reason: str) -> Result:
    return Result(0, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}})


# ---------------------------------------------------------------------------
# 之后
# ---------------------------------------------------------------------------
def _match(led: CodeLedger, agent: str, tool: str, ti: dict, tid: str) -> str:
    """执行之后的事件对上执行之前记下的那一次：按 tool_use_id；没有的话，找这个 Agent 最近一次同类、同一个文件的。"""
    if tid in led.pending:
        return tid
    full = ti.get("file_path") or ti.get("notebook_path")
    path = led.relpath(str(full)) if full else None
    cands = [(op.get("time", 0), t) for t, op in led.pending.items() if op.get("agent") == agent and (
        (tool == "Bash" and op.get("kind") == "bash") or
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
                texts.append(f"注意：{rep['path']} 在这次编辑期间还有别的变化（说不准是谁改的），已按人的修改保护。")
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
        what = "；".join(f"{d['path']}" + (" " + "、".join(_span(r) for r in d["lines"]) if d["lines"] else "")
                        + (f"（{d['points']} 处删除）" if d["points"] else "") for d in done)
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
    regions = led.protected_regions()
    lines = [
        "这个项目开着 RightOfWay（人和 AI 同时改代码时，人的修改优先，由 hooks 强制执行）：",
        "- 人改过、还没交还（也还没提交）的行，你不能改。碰到这些行的 Edit/Write 会整次被拒（一行都不写）；"
        "命令（Bash）改到了这些行，那个文件会被整个改回去。",
        "- 被拒时会告诉你是哪几行、现在是什么内容：以现在的内容为准，只改别的部分。确实需要改，请人交还"
        "（人可以在对话里输入：交还 路径:起-止）。",
        "- 别的 AI 在你上次读某个文件之后改的行，你也不能按旧印象改：先重新读一遍这个文件。",
    ]
    if regions:
        lines.append("现在受保护的：" + "；".join(
            f"{p} " + "、".join(_span(r) for g in rs for r in g["lines"]) + (
                f"（另有 {sum(len(g['points']) for g in rs)} 处删除）" if any(g["points"] for g in rs) else "")
            for p, rs in sorted(regions.items())) + "。")
    return Result(0, {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}},
                  texts=lines)


# ---------------------------------------------------------------------------
# 说明文字（只从账本生成）
# ---------------------------------------------------------------------------
def who(led: CodeLedger, mark: str) -> str:
    if mark == "locked":
        return "RightOfWay 的配置（只有人能改）"
    author = (led.marks.get(mark) or {}).get("author", "")
    if author == HUMAN:
        return "人"
    if author == UNATTRIBUTED:
        return "来源不明（说不准是谁改的，按人的修改保护）"
    if author.startswith("external:"):
        return "来源不明"
    if author == GIT:
        return "git 操作（切换分支、拉取等）"
    return (led.agents.get(author) or {}).get("name") or author


def _span(r: tuple[int, int]) -> str:
    return f"第 {r[0]} 行" if r[0] == r[1] else f"第 {r[0]}–{r[1]} 行"


def _excerpt(led: CodeLedger, path: str, r: tuple[int, int]) -> str:
    data = led.content(path)
    rows = L.split_lines(data)
    out = []
    for k in range(r[0] - 1, min(r[1], r[0] - 1 + MAX_SHOW)):
        if k < len(rows):
            out.append(f"{k + 1:>5}| " + rows[k].decode("utf-8", "replace").rstrip("\r\n"))
    if r[1] - r[0] + 1 > MAX_SHOW:
        out.append(f"     | ……（还有 {r[1] - r[0] + 1 - MAX_SHOW} 行）")
    return "\n".join(out)


def _point_text(p: dict) -> str:
    if p.get("file"):
        return "整个文件被删掉了"
    head = f"第 {p['pos']} 行之后" if p["pos"] else "文件开头"
    gone = "；".join(t for t in p.get("text") or () if t.strip())
    return f"{head}删掉了 {p.get('count', '?')} 行" + (f"（{gone[:120]}）" if gone else "")


def region_lines(led: CodeLedger, path: str, d: dict, with_text: bool = True) -> list[str]:
    out = []
    for r in L.ranges(d.get("lines") or []) if isinstance((d.get("lines") or [None])[0], int) else d.get("lines") or []:
        out.append(f"- {path} {_span(r)}" + ("，现在是：\n" + _excerpt(led, path, r) if with_text else ""))
    for p in d.get("points") or ():
        out.append(f"- {path}：{_point_text(p)}")
    return out


def refusal_text(led: CodeLedger, agent: str, path: str, hits: dict[str, dict], tool: str) -> str:
    parts = [f"这次 {tool} 没有执行（整次拒绝，一行都没写）：它会改到按规则不能动的行。"]
    for m, d in sorted(hits.items(), key=lambda kv: (kv[1]["lines"] or [p["pos"] for p in kv[1]["points"]])[0]):
        why = led.reason(m)
        if why == "stale":
            parts.append(f"{who(led, m)} 在你上次读 {path} 之后改了这些行（你还没看过，不能按旧印象改）：")
        else:
            parts.append(f"{who(led, m)}改过、还没交还的：")
        parts += region_lines(led, path, d)
    if any(led.reason(m) == "stale" for m in hits):
        parts.append("先重新读一遍这个文件，再只改别的部分。")
    if any(led.reason(m) != "stale" for m in hits):
        lo = min((r[0] for d in hits.values() for r in L.ranges(d["lines"])), default=None)
        hi = max((r[1] for d in hits.values() for r in L.ranges(d["lines"])), default=None)
        where = (f"{path}:{lo}" if lo == hi else f"{path}:{lo}-{hi}") if lo else path
        parts.append(f"以现在的内容为准，写进你之后的计划；只改别的部分。确实需要改这些行，请人交还（人可以在对话里输入：交还 {where}）。")
    return "\n".join(parts)


def loop_text(led: CodeLedger, path: str, hits: dict[str, dict], loops: list[str]) -> str:
    spans = "、".join(_span(r) for m in loops for r in L.ranges(hits[m]["lines"])) or "删掉的地方"
    lo = [r for m in loops for r in L.ranges(hits[m]["lines"])]
    where = (f"{path}:{lo[0][0]}" if lo[0][0] == lo[-1][1] else f"{path}:{lo[0][0]}-{lo[-1][1]}") if lo else path
    return (f"RightOfWay：AI 已经 3 次想改你改过的 {path} {spans}，都被拦下了。"
            f"如果你同意它改，在对话里输入：交还 {where}")


def changes_text(led: CodeLedger, agent: str, regions: dict[str, list[dict]]) -> str:
    out = []
    shown = 0
    for path, rs in sorted(regions.items()):
        for g in rs:
            if shown >= 8:
                out.append("- ……（还有更多，读一下相应的文件）")
                return "\n".join(out)
            tag = "（你不能改）" if g.get("protected") else ""
            for r in g["lines"]:
                out.append(f"- {who(led, g['mark'])} 改了 {path} {_span(r)}{tag}：\n" + _excerpt(led, path, r))
            for p in g["points"]:
                out.append(f"- {who(led, g['mark'])}：{path} {_point_text(p)}{tag}")
            shown += 1
    return "\n".join(out)


def revert_text(led: CodeLedger, agent: str, reps: list[dict], tool: str, files_changed: Optional[list[str]] = None) -> str:
    parts = [f"注意：这次 {tool} 改到了按规则不能动的行。下面这些文件已经整个改回执行前的样子（这些文件里你的改动都没有生效）："]
    for r in reps:
        parts.append(f"- {r['path']}" + (f"（你改的版本另存在 {r['backup']}）" if r.get("backup") else "（你删掉了它，已放回）"))
        for m, d in sorted(r["hits"].items()):
            parts.append(f"  碰到了{who(led, m)}的：" + "、".join(
                [_span(x) for x in L.ranges(d["lines"])] + [_point_text(p) for p in d["points"]]))
    if files_changed:
        parts.append("其余文件照常生效：" + "、".join(files_changed[:12]) + ("……" if len(files_changed) > 12 else "")
                     + "。这条命令可能只生效了一半（例如改名只改了一部分），请检查。")
    parts.append("以现在的内容为准；需要的话，只改别的部分再做一次。确实需要改这些行，请人交还。")
    return "\n".join(parts)


def human_revert_text(led: CodeLedger, reps: list[dict]) -> str:
    names = "、".join(r["path"] for r in reps)
    kept = [r["backup"] for r in reps if r.get("backup")]
    where = f"（AI 的版本另存在 {os.path.dirname(kept[0]) if len(kept) > 1 else kept[0]}）" if kept else ""
    return f"RightOfWay：AI 的操作改到了你改过的行，已把 {names} 改回去了{where}。"


def unsure_text(rep: dict) -> str:
    names = "、".join(u["path"] for u in rep["unsure"][:12]) + ("……" if len(rep["unsure"]) > 12 else "")
    return (f"注意：这条命令执行了 {rep['seconds']:.0f} 秒，这期间这些文件有变化，说不准是命令改的还是人改的"
            f"（人可能同时在存盘），已按人的修改保护：{names}。这些变化你现在不能改，除非人交还；先读一下这些文件。")


def human_unsure_text(led: CodeLedger, rep: dict) -> str:
    parts = []
    for u in rep["unsure"]:
        if not u["hits"]:
            continue
        spans = "、".join(_span(r) for d in u["hits"].values() for r in L.ranges(d["lines"])) or "删掉的地方"
        parts.append(f"{u['path']} {spans}" + (f"（命令执行前的版本在 {u['backup']}）" if u.get("backup") else ""))
    return (f"RightOfWay：AI 的命令执行期间（{rep['seconds']:.0f} 秒），你改过的这些地方变了：" + "；".join(parts)
            + "。说不准是你改的还是命令改的：现在的内容保留，按你的修改保护。如果不是你改的，可以用上面的版本改回去。")
