"""文本文件这一侧 + 核心，对 AI 侧的那一层（core/interfaces.py 的 Session）。

AI 侧（例如 Claude Code 的 hooks）只说"AI 想做什么、做完了"，这里：读账本、看见人的修改（ledger.observe）、
事先能算出结果的修改先算出来（按字符串替换的编辑、整份写入）、问核心能不能改、照做（开许可单、放回文件）、
写说明（messages.py）、存盘。每个方法是一次完整的来回（加锁、读账本、做完存回去）。"""
from __future__ import annotations

import os
from typing import Optional

from ..core import loops as LOOPS
from ..core import rules as R
from ..core.interfaces import Decision, Intent, Outcome
from . import messages as M
from .ledger import LOCKED, CodeLedger
from .messages import changes_text, human_revert_text, human_unsure_text, loop_text, refusal_text, revert_text, \
    unsure_text

EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
REPEAT = 3                                                             # 同一处被拒到第几次时提醒人
# 这一侧的能力（不是 3D 那样连接时自测，而是固定的：文件就是字节）
CAPABILITIES = {"predict": True, "atomic": False, "around": False, "restore": True, "restore_deleted": True,
                "identity": True, "journal": False, "editing": False}


def simulate(tool: str, ti: dict, cur: Optional[bytes]) -> Optional[bytes]:
    """按编辑的参数算出执行之后文件的内容（字符串替换，和 Claude Code 的 Edit 一样）。算不出来返回 None。"""
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


class TextSession:
    def __init__(self, root: str, home: Optional[str] = None) -> None:
        self.led = CodeLedger(root, home)

    def capabilities(self) -> dict:
        return dict(CAPABILITIES)

    def _enter(self, agent: str, name: str) -> None:
        self.led.agent(agent, name)
        self.led.observe()                              # 上一次之后磁盘上的变化：人的

    # ------------------------------------------------------------------
    def intro(self, agent: str, name: str) -> list[str]:
        with self.led.open():
            self._enter(agent, name)
            return M.session_start_lines(self.led.protected_regions())

    def prompt(self, agent: str, name: str, text: str, human: bool = True) -> tuple[bool, list[str]]:
        from .hooks import parse_release
        led = self.led
        with led.open():
            self._enter(agent, name)
            specs, only = parse_release(text, led) if human else ([], False)
            done = []
            for path, a, b in specs:
                done += led.release(path, a, b)
            if done:
                what = M.release_summary(done)
                for other in list(led.agents):
                    led.note(other, f"人把这些交还给了 AI，现在可以改了：{what}。")
            if only:
                return True, [("RightOfWay：已交还 " + "；".join(d["path"] for d in done) + "。") if done else
                              "RightOfWay：没有找到可以交还的（这些行没有受保护）。"]
            texts = led.take_notes(agent)
            fresh = led.unseen(agent, untold=True)
            if fresh:
                texts.append("你上次看之后，别人改了这些地方：\n" + changes_text(led, agent, fresh))
                led.tell(agent, fresh)
            return False, texts

    def begin(self, agent: str, name: str, intent: Intent) -> Decision:
        led = self.led
        with led.open():
            self._enter(agent, name)
            tid = intent.ticket or f"t-{led.state['n']}"
            if intent.kind == "command":
                timeout = intent.payload.get("timeout")
                led.begin_bash(agent, tid, (float(timeout) / 1000.0) if isinstance(timeout, (int, float)) else 600.0)
                return Decision()
            if intent.kind != "edit" or intent.tool not in EDIT_TOOLS:
                return Decision()
            path = led.relpath(str(intent.target)) if intent.target else None
            if path is None:
                return Decision()                       # 不在这个项目里
            if path in LOCKED:
                return Decision(False, f"这次编辑没有执行：{path} 是 RightOfWay 的配置，只有人能改。")
            known = path in led.files
            if not known and (os.path.exists(led.abspath(path)) or self._ignored(path)):
                return Decision()                       # git 忽略的文件、太大的文件：不管
            cur = led.content(path) if known else None
            new = simulate(intent.tool, intent.payload, cur)
            if new is not None:
                hits = led.check(agent, path, new)      # 事先算出了结果：按结果核对
            elif intent.tool == "NotebookEdit":         # 算不出结果、执行了一定会改：文件里有对它受保护的部分，就整次拒绝
                hits = led.core.all_protected(path, agent)
            else:
                hits = {}                               # 例如 old_string 找不到：工具多半会自己报错；万一改了，事后核对
            if R.refuse_whole(hits, separable=False):   # 一次编辑里的几行互相关联（例如改名）：整次拒绝
                loops = LOOPS.count(led.agent(agent)["refusals"], hits, REPEAT)
                d = Decision(False, refusal_text(led, agent, path, hits, intent.tool))
                if loops:
                    d.alert = loop_text(led, path, hits, loops)
                led.log("refused", agent=agent, path=path, marks=sorted(hits))
                return d
            led.begin_edit(agent, tid, path, new, intent.tool)
            return Decision()

    def end(self, agent: str, name: str, intent: Intent, failed: bool = False) -> Outcome:
        led = self.led
        with led.open():
            self._enter(agent, name)
            tid = self._match(agent, intent, intent.ticket or f"t-{led.state['n']}")
            out = Outcome(texts=led.take_notes(agent))
            if intent.kind == "edit" and intent.tool in EDIT_TOOLS and tid in led.pending:
                rep = led.settle_edit(tid)
                out.report = rep
                if rep["status"] == "reverted":
                    out.texts.append(revert_text(led, agent, [rep], intent.tool))
                    out.alerts.append(human_revert_text(led, [rep]))
                elif rep["status"] == "applied":
                    if rep.get("unattributed"):
                        out.texts.append(M.extra_text(rep["path"]))
                    fresh = led.unseen(agent, rep["path"], untold=True)
                    if fresh:
                        out.texts.append("你改的这个文件里，还有你没看过的变化：\n" + changes_text(led, agent, fresh))
                        led.tell(agent, fresh)
            elif intent.kind == "command" and tid in led.pending:
                rep = led.settle_bash(tid)
                out.report = rep
                if rep["reverted"]:
                    out.texts.append(revert_text(led, agent, rep["reverted"], intent.tool, files_changed=rep["files"]))
                    out.alerts.append(human_revert_text(led, rep["reverted"]))
                if rep["unsure"]:
                    out.texts.append(unsure_text(rep))
                    if any(u["hits"] for u in rep["unsure"]):
                        out.alerts.append(human_unsure_text(led, rep))
            return out

    def look(self, agent: str, name: str, target: Optional[str]) -> list[str]:
        led = self.led
        with led.open():
            self._enter(agent, name)
            path = led.relpath(str(target)) if target else None
            texts: list[str] = []
            if path is not None and path in led.files:
                fresh = led.unseen(agent, path, untold=True)
                if fresh:
                    texts.append("这个文件在你上次看之后有这些变化：\n" + changes_text(led, agent, fresh))
                    led.tell(agent, fresh)
                led.see(agent, path)
            return texts

    # ------------------------------------------------------------------
    def _ignored(self, path: str) -> bool:
        if not self.led.git:
            return False
        return self.led._run_git("check-ignore", "-q", "--", path) is not None

    def _match(self, agent: str, intent: Intent, tid: str) -> str:
        """执行之后的事件对上执行之前记下的那一次：按编号；没有的话，找这个 AI 最近一次同类、同一个文件的。"""
        led = self.led
        if tid in led.pending:
            return tid
        path = led.relpath(str(intent.target)) if intent.target else None
        cands = [(op.get("time", 0), t) for t, op in led.pending.items() if op.get("agent") == agent and (
            (intent.kind == "command" and op.get("kind") == "command") or
            (intent.kind == "edit" and op.get("kind") == "edit" and op.get("path") == path))]
        return max(cands)[1] if cands else tid
