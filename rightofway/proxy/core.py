"""代理的规则（design_v0.5.md 第 9 节、12.8，prior_art_solutions.md 第 4 项）。

每个工具调用都是一次操作，走和执行脚本同一条流水线：运行时开许可单 → 应用照单执行 → 按实际状态核对 → 告知。
代理自己不判断哪些该保护（P9），只是决定一次调用怎么包进这条流水线：

1. 执行代码的工具（execute_blender_code……）：交给 SharedSession.run_agent，原子地执行、恢复、核对。
2. 应用 MCP 服务器的其他工具（类型化，代理不知道它会改哪些面）：
   - 调用参数里出现的名字就是它要动的对象（不手写每个工具的参数映射，P4）；
   - 按对象预检查（12.8 第 1 步）：整个对象都要保持的，不执行；接入代码事后恢复不了的
     （能力表里没有 begin/finish，或者恢复不了删除、而这个工具可能删除），不执行——只对恢复不了的失败即拒；
   - 其余的：接入代码 begin_agent（核对、快照）→ 调用上游 → finish_agent（恢复、核对），SharedSession.run_tool；
   - 只读的工具（上游标了 readOnlyHint；注解不可信，只用来分流，调用后照样核对）：先告诉 AI 它错过了什么。
3. 代理自己的小工具（第 9 节）：rightofway_set_property / create_object / delete_object 编成应用的脚本，
   执行前按面预检查（Permit.screen），没有恢复这一步；rightofway_observe 告诉 AI 它错过了什么、哪些现在不能改。

结果：给 AI 看的文字（失效的事实、新的目标……）放在 content 里；结构化的结果放在 _meta.rightofway；
有没生效、被拒、违规的部分时 isError=true（MCP 规定工具执行错误要让模型能自我纠正）。
给人的提醒（违规、反复改、认回核对不一致）写进日志，接入代码支持的话也显示在应用里（notify）。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from mcp import types

from ..blender.shared_session import SharedSession
from .sources import detect_code_tool

log = logging.getLogger("rightofway.proxy")

_OBJECT = {"type": "string", "description": "对象名（也可以是材质等数据块的名字）"}
OWN_TOOLS = [
    types.Tool(
        name="rightofway_observe",
        description="看场景：你上次看之后发生了哪些变化（谁改的、改成了什么），以及现在哪些部分不能改（人改过、人正在改，"
                    "或者别的 AI 刚改过）。动手之前先看一下，可以少做无用功。",
        inputSchema={"type": "object", "properties": {}},
        annotations=types.ToolAnnotations(readOnlyHint=True)),
    types.Tool(
        name="rightofway_set_property",
        description="改一个对象（或材质等数据块）的一个属性，例如 location、rotation_euler、scale、parent、diffuse_color。"
                    "属性名就是应用里的属性名。人改过的属性不会被改，结果里会说明现在的值。",
        inputSchema={"type": "object", "properties": {"object": _OBJECT, "property": {"type": "string"},
                                                      "value": {"description": "新的值：数字、列表、字符串；引用别的对象时写它的名字"}},
                     "required": ["object", "property", "value"]}),
    types.Tool(
        name="rightofway_create_object",
        description="新建一个对象。kind：EMPTY、MESH、LIGHT、CAMERA、CURVE；properties：新建后要设置的属性。",
        inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "kind": {"type": "string"},
                                                      "parent": {"type": "string"},
                                                      "properties": {"type": "object"}},
                     "required": ["name"]}),
    types.Tool(
        name="rightofway_delete_object",
        description="删除一个对象。人改过的对象（以及它们的父对象）不能删除。",
        inputSchema={"type": "object", "properties": {"object": _OBJECT}, "required": ["object"]},
        annotations=types.ToolAnnotations(destructiveHint=True)),
]
OWN_NAMES = {t.name for t in OWN_TOOLS}


def _text(text: str) -> types.TextContent:
    return types.TextContent(type="text", text=text)


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[_text(text)], isError=True)


class Proxy:
    def __init__(self, session: SharedSession, client, code_tool: Optional[str] = None,
                 code_param: Optional[str] = None, agent: Optional[str] = None, own_tools: bool = True) -> None:
        self.s, self.client = session, client
        self.agent = agent or session.agent
        if self.agent not in session.agent_names:
            session.add_agent(self.agent)
        self.lock = threading.Lock()                 # SharedSession 不是线程安全的：一次只处理一个调用
        self.upstream = {t.name: t for t in client.list_tools()}
        if code_tool is None:
            code_tool, code_param = detect_code_tool(self.upstream.values())
        self.code_tool, self.code_param = code_tool, code_param or "code"
        self.own_tools = own_tools and hasattr(session.bridge, "compile_commands")
        self.alerts: list[str] = []

    # ------------------------------------------------------------------
    def list_tools(self) -> list[types.Tool]:
        """上游的工具原样转出（去掉 outputSchema：被拒、被包起来的调用给不出上游那种结构化结果），加上自己的小工具。"""
        return ([t.model_copy(update={"outputSchema": None}) for t in self.upstream.values()]
                + (OWN_TOOLS if self.own_tools else []))

    def call_tool(self, name: str, args: Optional[dict] = None) -> types.CallToolResult:
        args = dict(args or {})
        with self.lock:
            try:
                if name == self.code_tool:
                    return self._code(args)
                if self.own_tools and name in OWN_NAMES:
                    return self._own(name, args)
                if name in self.upstream:
                    return self._typed(name, args)
                return _error(f"没有这个工具：{name}")
            except Exception as e:                   # noqa: BLE001
                log.exception("工具调用出错：%s", name)
                return _error(f"RightOfWay 代理出错：{type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    def _code(self, args: dict) -> types.CallToolResult:
        code = args.get(self.code_param)
        if not isinstance(code, str):
            return _error(f"缺少参数 {self.code_param}")
        rep = self.s.run_agent(code, label=self.code_tool, agent=self.agent)
        out = rep.stdout.rstrip()
        return self._result(rep, [_text((f"脚本输出：\n{out}\n\n" if out else "") + rep.text)])

    def _typed(self, name: str, args: dict) -> types.CallToolResult:
        ann = self.upstream[name].annotations
        read_only = bool(ann is not None and ann.readOnlyHint)
        destructive = not read_only and (ann is None or ann.destructiveHint is not False)
        missed = self.s.observe(self.agent).lines if read_only else []
        rep, res = self.s.run_tool(lambda: self.client.call_tool(name, args), label=name, agent=self.agent,
                                   targets=self._targets(args), destructive=destructive)
        content = list(res.content) if res is not None else []
        notes = []
        if missed:
            notes.append("你上次看场景之后，发生了这些变化：\n" + "\n".join("- " + x for x in missed))
        if rep.status != "ok" or self._notable(rep):
            notes.append(rep.text)
        if notes:
            content.append(_text("【RightOfWay】" + "\n".join(notes)))
        return self._result(rep, content, res)

    def _own(self, name: str, args: dict) -> types.CallToolResult:
        if name == "rightofway_observe":
            obs = self.s.observe(self.agent)
            prot = self.s.protected(for_agent=self.agent)
            view = self.s.view
            lines = [obs.text]
            caps = self.s.capabilities
            if caps.get("restore_deleted") is False:
                lines.append("注意：这个应用恢复不了被删掉的对象。人改过的对象（以及它们的父对象）请不要删除，删了就是违规。")
            if prot:
                lines.append("现在不能改的部分（人改过、人正在改，或者别的 AI 刚改过）：" + "；".join(
                    view[c]["name"] + ("（整个对象）" if "*" in faces else "：" + "、".join(
                        self.s.labels.get(f, f) for f in faces)) for c, faces in prot.items() if c in view))
            return types.CallToolResult(content=[_text("\n".join(lines))], isError=False,
                                        _meta={"rightofway": {"missed": obs.lines, "protected": {
                                            view[c]["name"]: faces for c, faces in prot.items() if c in view}}})
        if name == "rightofway_set_property":
            cmd = {"action": "modify", "target": args.get("object"), "set": {args.get("property"): args.get("value")}}
        elif name == "rightofway_create_object":
            cmd = {"action": "create", "name": args.get("name"), "kind": args.get("kind"),
                   "parent": args.get("parent"), "set": args.get("properties") or {}}
        else:
            cmd = {"action": "delete", "target": args.get("object")}
        rep = self.s.run_commands([cmd], label=name, agent=self.agent)[0]
        return self._result(rep, [_text(rep.text)])

    # ------------------------------------------------------------------
    def _targets(self, args: dict) -> set[str]:
        """调用参数里出现的对象名 → 对象编号（对象和数据块同名时取对象）。不认识任何工具的参数（P4）。"""
        names: dict[str, str] = {}
        for cid, r in sorted(self.s.view.items(), key=lambda kv: kv[1].get("kind") == "data"):
            names.setdefault(r["name"], cid)
        found: set[str] = set()

        def walk(v: Any) -> None:
            if isinstance(v, str):
                if v in names:
                    found.add(names[v])
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    walk(x)
        walk(args)
        return found

    @staticmethod
    def _notable(rep) -> bool:
        return bool(rep.conflicts or rep.breach or rep.missed or rep.derived or rep.loops or rep.recreate_blocked
                    or rep.identity_errors or rep.reidentified or rep.rebound or rep.side_effects or rep.error)

    def _result(self, rep, content: list, upstream=None) -> types.CallToolResult:
        if rep.alert:
            self.alerts.append(rep.alert)
            log.warning("提醒人：%s", rep.alert)
            if self.s.capabilities.get("notify"):
                try:
                    self.s.bridge.call("notify", {"text": rep.alert})
                except Exception:                    # noqa: BLE001
                    log.exception("在应用里显示提醒失败")
        is_error = SharedSession.is_error(rep) or bool(upstream is not None and upstream.isError)
        return types.CallToolResult(content=content, isError=is_error,
                                    structuredContent=getattr(upstream, "structuredContent", None),
                                    _meta={"rightofway": self.s.payload(rep)})
