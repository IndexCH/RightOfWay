"""按编号的应用侧（Blender、模拟应用、Unity……）+ 核心，对 AI 侧的那一层（core/interfaces.py 的 Session）。

有了它，像 Claude Code 的 hooks 那样"执行前、执行后各通知一次"的 AI 侧也能驱动这类应用：工具调用由 AI 侧自己去做，
这里在执行前开许可单、预检查、让接入代码快照（SharedSession.begin_tool），执行后让接入代码认回、恢复、核对，
再按实际状态结算（end_tool）。规则仍然全在核心里；这里只把 Intent 翻成会话的调用。

局限：一张票据（begin 和 end 之间）记在这个对象里。真的 Claude Code 每个 hook 事件是一个新进程：会话要从账本接着用
（SharedSession.start(ledger=...)），没结算的那次执行由账本里的"结果未知"记录查清（design_v0.5.md 14.2），
这里的测试在同一个进程里驱动。"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..core.interfaces import Decision, Intent, Outcome
from .shared_session import SharedSession

_RELEASE = re.compile(r"^\s*(?:交还|rightofway\s+release)[ \t:：]+(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


class KeyedSession:
    def __init__(self, session: SharedSession) -> None:
        self.s = session
        self._tickets: dict[str, dict] = {}

    def capabilities(self) -> dict:
        return dict(self.s.capabilities)

    def _agent(self, agent: str, name: str) -> None:
        if agent not in self.s.agent_names:
            self.s.add_agent(agent, name)

    def _targets(self, payload: Any) -> set[str]:
        """参数里出现的对象名 → 对象编号（和 MCP 代理一样，不认识任何工具的参数，P4）。"""
        names: dict[str, str] = {}
        for cid, r in sorted(self.s.view.items(), key=lambda kv: kv[1].get("kind") == "data"):
            names.setdefault(r["name"], cid)
        found: set[str] = set()

        def walk(v: Any) -> None:
            if isinstance(v, str) and v in names:
                found.add(names[v])
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    walk(x)
        walk(payload)
        return found

    # ------------------------------------------------------------------
    def intro(self, agent: str, name: str) -> list[str]:
        self._agent(agent, name)
        obs = self.s.observe(agent)
        lines = ["这个应用开着 RightOfWay（人和 AI 同时改时，人的修改优先，由运行时强制执行）：",
                 "- 人改过、还没交还的部分，你改了也不会生效（执行之后放回人的版本）；人删掉的东西不要建回来。",
                 "- 结果里会告诉你哪些没生效、现在的值是什么：以现在的值为准。确实需要改，请人交还（交还 对象名）。"]
        prot = self.s.protected(for_agent=agent)
        if prot:
            view = self.s.view
            lines.append("现在不能改的：" + "；".join(
                view[c]["name"] + ("（整个对象）" if "*" in faces else "：" + "、".join(self.s.labels.get(f, f) for f in faces))
                for c, faces in prot.items() if c in view) + "。")
        if obs.lines:
            lines.append(obs.text)
        return lines

    def prompt(self, agent: str, name: str, text: str, human: bool = True) -> tuple[bool, list[str]]:
        self._agent(agent, name)
        done = []
        if human:
            for m in _RELEASE.finditer(text):
                for nm in re.split(r"[\s,，、]+", m.group(1).strip()):
                    cid = self.s.find(nm) if nm else None
                    if cid is not None:
                        self.s.hand_back(cid)
                        done.append(nm)
            if done:
                for a in self.s.agent_names:
                    self.s._notes.setdefault(a, []).append(f"人把 {'、'.join(done)} 交还给了 AI：现在可以改了。")
        only = bool(done) and not _RELEASE.sub("", text).strip()
        if only:
            return True, ["RightOfWay：已交还 " + "、".join(done) + "。"]
        texts = self.s._notes.pop(agent, [])
        obs = self.s.observe(agent)
        if obs.lines:
            texts.append(obs.text)
        return False, texts

    def begin(self, agent: str, name: str, intent: Intent) -> Decision:
        if intent.kind != "tool":
            return Decision()                          # 文件的编辑、命令：不是这个应用侧的事
        self._agent(agent, name)
        try:
            rep, ticket = self.s.begin_tool(label=intent.tool, agent=agent, targets=self._targets(intent.payload))
        except ValueError as e:                        # 应用侧拆不开工具调用：事后放不回，宁可不执行（P1、P5）
            return Decision(False, f"这次没有执行：{e}。RightOfWay 没法在工具调用之后把不能动的部分放回去。")
        if ticket is None:
            return Decision(False, rep.text, rep.alert)
        self._tickets[intent.ticket or agent] = ticket
        return Decision()

    def end(self, agent: str, name: str, intent: Intent, failed: bool = False) -> Outcome:
        ticket = self._tickets.pop(intent.ticket or agent, None)
        if ticket is None:
            return Outcome()
        rep = self.s.end_tool(ticket, error="工具自己报错了" if failed else None)
        return Outcome(texts=[rep.text] if rep.text else [], alerts=[rep.alert] if rep.alert else [], report=rep)

    def look(self, agent: str, name: str, target: Optional[str]) -> list[str]:
        self._agent(agent, name)
        obs = self.s.observe(agent)
        return [obs.text] if obs.lines else []
