"""一致性测试（spec/core.md 第 8 节）：给写适配的人用。照 Driver 的几个动作接上一个应用侧（和它的 AI 侧），
跑一遍就知道有没有守住保证：

    python -m rightofway.conformance            对能跑的应用侧都跑一遍，打印能力报告（有 bpy 时也跑 Blender）

一套和媒介无关的场景，一个模拟的 AI，加上每个应用侧都要有的"模拟人改一下"的入口，一步步跑，每一步都查三条不变量。
结果分三档（core.interfaces.GRADES）：
  protected  保护住了；
  reported   保护不住，但如实报告了（例如恢复不了被删的对象：记违规、提醒人、暂停 AI）；
  lied       说了假话：账本和实际对不上，或者说"已恢复""已生效"其实没有，或者人的修改丢了却没人知道。只有这一档不及格。

单元的说法和媒介无关：A、B 这样的名字，值是一个短字符串。每个 Driver 自己决定 A 是什么（一个对象的一个属性、
文件里的一行、一个字符），以及"人改一下""AI 改一下"怎么做。
"""
from __future__ import annotations

import contextlib
import difflib
import os
import random
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .core import rules as R
from .core.interfaces import capability_report
from .core.positional import PositionalLedger


@dataclass
class Step:
    """AI 的一次执行之后：告诉它的是不是"生效了"、告诉了它什么、提醒了人什么、记没记违规、它被暂停了没有。"""
    applied: bool = False
    told: str = ""
    alert: str = ""
    breach: bool = False
    suspended: bool = False


class Driver:
    """一个应用侧（加上驱动它的 AI 侧）。子类实现下面这些动作。"""
    name = "?"
    reserve = False                 # 两个 AI 冲突后会不会给被拒的那个预留（差分测试的参照模型要知道）
    see_after_run = False           # AI 执行完一次，是不是就算看过了整个场景

    def setup(self, units: dict[str, str]) -> None: ...
    def look(self, agent: str) -> None: ...
    def human_set(self, unit: str, value: str) -> None: ...
    def human_delete(self, unit: str) -> None: ...
    def hand_back(self, unit: str) -> None: ...
    def ai_set(self, agent: str, unit: str, value: str) -> Step: ...
    def ai_delete(self, agent: str, unit: str) -> Step: ...
    def ai_create(self, agent: str, unit: str, value: str) -> Step: ...
    def ai_set_lost(self, agent: str, unit: str, value: str) -> None: ...
    def resolve(self, agent: str) -> Step: ...
    def value(self, unit: str) -> Optional[str]: ...
    def violations(self) -> list[str]: ...
    def capabilities(self) -> dict: ...
    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------
@dataclass
class Result:
    grade: str
    detail: str = ""
    steps: list[Step] = field(default_factory=list)


def _judge(d: Driver, human: dict[str, Optional[str]], steps: list[Step],
           ai: Optional[dict[str, tuple[Step, Optional[str]]]] = None) -> Result:
    """human：人定下的、必须保住的 {单元: 值}（None：人删掉了，必须不在）；ai：{单元: (那一步, AI 改成的值)}，
    说了"生效了"就必须是这个值，说了"没生效"就必须不是。"""
    bad = d.violations()
    if bad:
        return Result("lied", "；".join(bad[:3]), steps)
    for unit, (st, want) in (ai or {}).items():
        got = d.value(unit)
        if st.applied and got != want:
            return Result("lied", f"说 {unit} 已生效，实际是 {got!r}", steps)
        if not st.applied and want is not None and got == want and human.get(unit, "?") != want:
            return Result("lied", f"说 {unit} 没有生效，实际是 AI 改成的 {want!r}", steps)
    lost = [u for u, w in human.items() if d.value(u) != w]
    if not lost:
        return Result("protected", "", steps)
    if any(s.breach for s in steps) and any(s.alert for s in steps):
        return Result("reported", f"{'、'.join(lost)} 没保住：如实记为违规、提醒了人"
                      + ("，AI 已暂停" if any(s.suspended for s in steps) else ""), steps)
    return Result("lied", f"{'、'.join(lost)} 是人的，被改掉了，却没有报告", steps)


def s_human_then_ai(d: Driver) -> Result:
    """人改了，AI 再改同一处：人的保住，AI 被告知没生效。"""
    d.setup({"A": "1", "B": "1"})
    d.look("ai1")
    d.human_set("A", "2")
    st = d.ai_set("ai1", "A", "3")
    return _judge(d, {"A": "2"}, [st], {"A": (st, "3")})


def s_ai_elsewhere(d: Driver) -> Result:
    """人改了 A，AI 改别处（B）：AI 的生效，人的不动。"""
    d.setup({"A": "1", "B": "1"})
    d.look("ai1")
    d.human_set("A", "2")
    st = d.ai_set("ai1", "B", "3")
    r = _judge(d, {"A": "2"}, [st], {"B": (st, "3")})
    if r.grade == "protected" and not st.applied:
        return Result("lied", "AI 改别处也没生效（不该拦的被拦了）", [st])
    return r


def s_human_deletes_ai_recreates(d: Driver) -> Result:
    """人删了，AI 又建回来：人交还之前建不回来。"""
    d.setup({"A": "1", "B": "1"})
    d.look("ai1")
    d.human_delete("A")
    st = d.ai_create("ai1", "A", "1")
    return _judge(d, {"A": None}, [st], {"A": (st, "1")})


def s_ai_deletes_human_edited(d: Driver) -> Result:
    """AI 删人改过的东西：保住；恢复不了删除的应用如实记违规、提醒人、暂停 AI。"""
    d.setup({"A": "1", "B": "1"})
    d.look("ai1")
    d.human_set("A", "2")
    st = d.ai_delete("ai1", "A")
    return _judge(d, {"A": "2"}, [st])


def s_lost_result(d: Driver) -> Result:
    """结果没收到：执行了，结果没送回来；期间人改了 A。查清之后人的保住，账本和实际一致，AI 之后也改不了 A。"""
    d.setup({"A": "1", "B": "1"})
    d.look("ai1")
    d.ai_set_lost("ai1", "B", "3")
    d.human_set("A", "2")
    st = d.resolve("ai1")
    st2 = d.ai_set("ai1", "A", "4")
    return _judge(d, {"A": "2"}, [st, st2], {"A": (st2, "4")})


def s_after_hand_back(d: Driver) -> Result:
    """交还之后：人改过的，人交还了，AI 就能改。"""
    d.setup({"A": "1", "B": "1"})
    d.look("ai1")
    d.human_set("A", "2")
    st1 = d.ai_set("ai1", "A", "3")
    d.hand_back("A")
    st2 = d.ai_set("ai1", "A", "4")
    r = _judge(d, {}, [st1, st2], {"A": (st2, "4")})
    if r.grade == "protected" and (st1.applied or not st2.applied):
        return Result("lied", "交还前改成了，或者交还后还是改不了", [st1, st2])
    return r


def s_two_ais(d: Driver) -> Result:
    """两个 AI：先到的生效；后到的按旧印象改同一处不生效，看过最新的之后才能改。"""
    d.setup({"A": "1", "B": "1"})
    d.look("ai1")
    d.look("ai2")
    st1 = d.ai_set("ai1", "A", "2")
    st2 = d.ai_set("ai2", "A", "3")
    d.look("ai2")
    st3 = d.ai_set("ai2", "A", "4")
    r = _judge(d, {}, [st1, st2, st3], {"A": (st3, "4")})
    if r.grade == "protected" and not (st1.applied and not st2.applied and st3.applied):
        return Result("lied", "先到的没生效、后到的按旧印象生效了，或者看过之后还改不了", [st1, st2, st3])
    return r


SCENARIOS: dict[str, Callable[[Driver], Result]] = {
    "人改了，AI 再改同一处": s_human_then_ai,
    "AI 改别处": s_ai_elsewhere,
    "人删了，AI 又建回来": s_human_deletes_ai_recreates,
    "AI 删人改过的东西": s_ai_deletes_human_edited,
    "结果没收到": s_lost_result,
    "交还之后": s_after_hand_back,
    "两个 AI": s_two_ais,
}


def run(make: Callable[[], Driver], only: Optional[list[str]] = None) -> dict:
    """每个场景用一个新的 Driver 跑一遍，返回能力报告（core.interfaces.capability_report）。"""
    grades, details, caps, name = {}, {}, {}, "?"
    for title, fn in SCENARIOS.items():
        if only is not None and title not in only:
            continue
        d = make()
        try:
            r = fn(d)
            caps, name = d.capabilities(), d.name
        finally:
            d.close()
        grades[title] = r.grade
        if r.detail:
            details[title] = r.detail
    rep = capability_report(name, caps, grades)
    rep["details"] = details
    return rep


# ---------------------------------------------------------------------------
# 随机的差分测试：同一串随机操作，应用侧的结果和一个独立写的判断表模型比
# ---------------------------------------------------------------------------
class Oracle:
    """判断表（spec/core.md 第 3 节）的独立模型，只用来对照：全局一个时钟，每次修改记下时间；AI 看过 = 它最后一次看
    在这次修改之后。reserve：被别的 AI 抢先而被拒的单元，留给被拒的那个，直到它下一次执行结束。"""

    def __init__(self, units, reserve: bool, see_after_run: bool) -> None:
        self.t = 0
        self.last = {u: None for u in units}           # 单元 → (作者, 时间)
        self.held = {u: False for u in units}
        self.seen: dict[str, int] = {}
        self.reserved: dict[str, str] = {}
        self.reserve, self.see_after_run = reserve, see_after_run

    def look(self, agent):
        self.t += 1
        self.seen[agent] = self.t

    def human(self, u):
        self.t += 1
        self.last[u], self.held[u] = ("human", self.t), True

    def hand_back(self, u):
        self.held[u] = False

    def can(self, agent, u) -> tuple[bool, Optional[str]]:
        holder = self.reserved.get(u)
        if holder is not None and holder != agent:
            return False, "reserved"
        if self.held[u]:
            return False, "human"
        last = self.last[u]
        if last is None or last[0] in (agent, "human"):
            return True, None
        if self.seen.get(agent, -1) > last[1]:
            return True, None
        return False, "stale"

    def ai(self, agent, u) -> bool:
        ok, why = self.can(agent, u)
        if self.reserve:                                # 它这一次执行结束：之前给它的预留用完了
            self.reserved = {k: v for k, v in self.reserved.items() if v != agent}
        if ok:
            self.t += 1
            self.last[u] = (agent, self.t)
        elif why == "stale" and self.reserve:
            self.reserved[u] = agent
        if self.see_after_run:
            self.look(agent)
        return ok


def differential(make: Callable[[], Driver], seed: int, steps: int = 30) -> list[str]:
    """随机做人改、AI 改、AI 看、人交还，每一步都对照判断表的模型、查三条不变量。返回发现的问题（空：都对上了）。"""
    rnd = random.Random(seed)
    units = ["A", "B", "C"]
    d = make()
    problems = []
    try:
        d.setup({u: "0" for u in units})
        o = Oracle(units, d.reserve, d.see_after_run)
        for a in ("ai1", "ai2"):
            d.look(a)
            o.look(a)
        n = 0
        for k in range(steps):
            op = rnd.choice(["human", "ai", "ai", "ai", "look", "hand_back"])
            u = rnd.choice(units)
            a = rnd.choice(["ai1", "ai2"])
            n += 1
            v = rnd.choice([x for x in "123456789" if x != d.value(u)])     # 和现在的值不一样：生效没生效看得出来
            if op == "human":
                d.human_set(u, v)
                o.human(u)
            elif op == "look":
                d.look(a)
                o.look(a)
            elif op == "hand_back":
                if o.held[u]:
                    d.hand_back(u)
                    o.hand_back(u)
            else:
                want = o.ai(a, u)
                st = d.ai_set(a, u, v)
                if st.applied != want:
                    problems.append(f"第 {k + 1} 步 {a} 改 {u}：判断表说{'能' if want else '不能'}，应用侧"
                                    f"{'生效了' if st.applied else '没生效'}（种子 {seed}）")
                    break
                if (d.value(u) == v) != want:
                    problems.append(f"第 {k + 1} 步 {a} 改 {u}：实际的值和说的不一样（种子 {seed}）")
                    break
            bad = d.violations()
            if bad:
                problems.append(f"第 {k + 1} 步之后不变量不成立：{bad[0]}（种子 {seed}）")
                break
    finally:
        d.close()
    return problems


# ---------------------------------------------------------------------------
# 应用侧：按编号的（模拟应用、Blender），AI 侧是会话本身（MCP 代理用的那一层）
# ---------------------------------------------------------------------------
class KeyedDriver(Driver):
    """SharedSession + 一个按编号的应用。A 是名叫 A 的对象的一个面（模拟应用：v；Blender：location 的 x）。"""
    reserve = True
    see_after_run = True

    def __init__(self, kind: str = "fake", **app_kw) -> None:
        self.kind, self.app_kw = kind, app_kw
        self.name = {"fake": "模拟应用", "blender": "Blender（bpy）"}[kind] + (
            "（恢复不了删除）" if app_kw.get("restore_deleted") is False else "")
        self.bad: list[str] = []
        self.lost_next = False

    # ---- 应用
    def setup(self, units):
        from .blender.shared_session import SharedSession
        from .runtime import Runtime
        if self.kind == "fake":
            from .fakeapp import FakeApp, FakeBridge
            self.app = FakeApp(**self.app_kw)
            for u, v in units.items():
                self.app.add(u, {"v": v})
            bridge = FakeBridge(self.app)
            real = self.app.run_agent

            def run_agent(args, real=real):
                out = real(args)
                if self.lost_next:
                    self.lost_next = False
                    raise ConnectionError("结果在路上丢了")
                return out
            self.app.run_agent = run_agent
        else:
            from .blender.bridge import InProcessBridge
            import bpy
            InProcessBridge.reset()
            for u, v in units.items():
                o = bpy.data.objects.new(u, None)
                o.location.x = float(v)
                bpy.context.scene.collection.objects.link(o)
            driver = self

            class Lossy(InProcessBridge):
                def call(self, fn, args):
                    out = super().call(fn, args)
                    if fn == "run_agent" and driver.lost_next:
                        driver.lost_next = False
                        raise ConnectionError("结果在路上丢了")
                    return out
            bridge = Lossy()
        self.s = SharedSession(Runtime(), bridge)
        self.s.start()
        self.s.add_agent("ai1")
        self.s.add_agent("ai2")

    def _code_set(self, unit, value):
        if self.kind == "fake":
            return f"scene.set({unit!r}, 'v', {value!r})"
        return f"import bpy\nbpy.data.objects[{unit!r}].location.x = {float(value)}\n"

    def _code_delete(self, unit):
        if self.kind == "fake":
            return f"scene.delete({unit!r})"
        return f"import bpy\nbpy.data.objects.remove(bpy.data.objects[{unit!r}])\n"

    def _code_create(self, unit, value):
        if self.kind == "fake":
            return f"scene.create({unit!r}, {{'v': {value!r}}})"
        return (f"import bpy\no = bpy.data.objects.new({unit!r}, None)\no.location.x = {float(value)}\n"
                "bpy.context.scene.collection.objects.link(o)\n")

    def human_set(self, unit, value):
        if self.kind == "fake":
            self.app.set(unit, "v", value)
        else:
            import bpy
            bpy.data.objects[unit].location.x = float(value)
        self.s.poll()

    def human_delete(self, unit):
        if self.kind == "fake":
            self.app.delete(unit)
        else:
            import bpy
            bpy.data.objects.remove(bpy.data.objects[unit])
        self.s.poll()

    def hand_back(self, unit):
        self.s.hand_back(self.s.find(unit))

    def value(self, unit):
        if self.kind == "fake":
            ids = [c for c, o in self.app.objects.items() if o["name"] == unit]
            return str(self.app.objects[ids[0]]["faces"]["v"]) if ids else None
        import bpy
        o = bpy.data.objects.get(unit)
        return None if o is None else f"{o.location.x:g}"

    # ---- AI
    def look(self, agent):
        self.s.observe(agent)

    def _run(self, agent, code) -> Step:
        from .invariants import check_run
        rep = self.s.run_agent(code, agent=agent)
        if rep.status == "ok":
            self.bad += [str(v) for v in check_run(self.s.rt, rep, self.s.granularity, labels=self.s.labels)]
        return Step(applied=bool(rep.committed) and not rep.skipped and not rep.breach, told=rep.text,
                    alert=rep.alert, breach=bool(rep.breach), suspended=rep.suspended)

    def ai_set(self, agent, unit, value):
        return self._run(agent, self._code_set(unit, value))

    def ai_delete(self, agent, unit):
        return self._run(agent, self._code_delete(unit))

    def ai_create(self, agent, unit, value):
        return self._run(agent, self._code_create(unit, value))

    def ai_set_lost(self, agent, unit, value):
        self.lost_next = True
        self.s.run_agent(self._code_set(unit, value), agent=agent)

    def resolve(self, agent):
        self.s.poll()
        return Step()

    def violations(self):
        from .invariants import check_session
        return self.bad + [str(v) for v in check_session(self.s)]

    def capabilities(self):
        c = self.s.capabilities
        return {"predict": hasattr(self.s.bridge, "compile_commands"), "atomic": bool(c.get("atomic")),
                "around": bool(c.get("around")), "restore": bool(c.get("restore_face", True)),
                "restore_deleted": bool(c.get("restore_deleted", True)), "identity": bool(c.get("identity")),
                "journal": bool(c.get("journal")), "editing": False}

    def close(self):
        pass


class HooksKeyedDriver(KeyedDriver):
    """跨组合：AI 侧是 Claude Code 的 hooks（模拟的 Claude Code，code/fakeagent.py），应用侧是按编号的模拟应用。
    AI 调的是一个 MCP 工具（改一个对象的一个面）：hooks 在执行前开许可单、快照，执行后放回、结算（blender/hook_session.py）。"""
    see_after_run = True

    def __init__(self) -> None:
        super().__init__("fake")
        self.name = "模拟应用 + Claude Code hooks（跨组合）"
        self.tmp = tempfile.mkdtemp(prefix="rightofway_cross_")
        self.cc: dict = {}

    def setup(self, units):
        from .blender.hook_session import KeyedSession
        super().setup(units)
        self.ks = KeyedSession(self.s)

    def _ai(self, agent):
        from .code.fakeagent import FakeClaudeCode
        if agent not in self.cc:
            sid = {"ai1": "sess-1111", "ai2": "sess-2222"}.get(agent, agent)
            self.cc[agent] = FakeClaudeCode(self.tmp, home=os.path.join(self.tmp, "home"), session=sid, app=self.ks)
        return self.cc[agent]

    def look(self, agent):
        cc = self._ai(agent)
        cc.read("scene")

    def _tool(self, agent, args, effect) -> Step:
        from .invariants import check_run
        self.ks.last = None
        r = self._ai(agent).tool("mcp__app__edit", args, effect)
        rep = self.ks.last
        if rep is None:
            return Step(applied=r.status == "ok", told=r.text, alert=r.alert)
        if rep.status == "ok":
            self.bad += [str(v) for v in check_run(self.s.rt, rep, self.s.granularity, labels=self.s.labels)]
        return Step(applied=rep.status == "ok" and bool(rep.committed) and not rep.skipped and not rep.breach,
                    told=rep.text, alert=rep.alert, breach=bool(rep.breach), suspended=rep.suspended)

    def ai_set(self, agent, unit, value):
        return self._tool(agent, {"object": unit, "face": "v", "value": value},
                          lambda: self.app.set(unit, "v", value))

    def ai_delete(self, agent, unit):
        return self._tool(agent, {"object": unit, "delete": True}, lambda: self.app.delete(unit))

    def ai_create(self, agent, unit, value):
        return self._tool(agent, {"name": unit, "faces": {"v": value}},
                          lambda: self.app.add(unit, {"v": value}, prefix="a-") and None)

    def human_set(self, unit, value):
        self.app.set(unit, "v", value)            # hooks 这边没有定时轮询：人的修改在下一次 hook 时才看见

    def human_delete(self, unit):
        self.app.delete(unit)

    def ai_set_lost(self, agent, unit, value):
        cc = self._ai(agent)                      # 执行前的 hook 到了，执行了，执行后的 hook 没到（进程没了，票据也没了）
        tid = cc._tid()
        cc._hook("PreToolUse", tool_name="mcp__app__edit", tool_input={"object": unit}, tool_use_id=tid)
        self.app.set(unit, "v", value)
        self.ks._tickets.clear()

    def resolve(self, agent):
        with _clock(3600):                        # 一小时之后的下一次 hook：先查清没结束的那次
            self._ai(agent).prompt("继续")
        return Step()


# ---------------------------------------------------------------------------
# 应用侧：文本文件，AI 侧是 Claude Code 的 hooks（code/fakeagent.py）
# ---------------------------------------------------------------------------
class TextDriver(Driver):
    """一个 git 仓库里的 f.txt，每个单元一行："A = 1"。人改 = 直接存盘；AI 改 = Write 整份（按现在的内容改它要改的那行，
    这是对保护最不利的写法：AI 知道人的版本，照样要改）。"""
    name = "文本文件 + Claude Code hooks"

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="rightofway_text_")
        self.repo, self.home = os.path.join(self.tmp, "repo"), os.path.join(self.tmp, "home")
        self.cc: dict = {}
        self.bad: list[str] = []
        self.order: list[str] = []

    def _path(self):
        return os.path.join(self.repo, "f.txt")

    def _read(self) -> dict[str, str]:
        try:
            with open(self._path(), encoding="utf-8", newline="") as fh:
                text = fh.read()
        except FileNotFoundError:
            return {}
        return dict(line.split(" = ", 1) for line in text.splitlines() if " = " in line)

    def _render(self, units: dict[str, str]) -> str:
        return "".join(f"{u} = {units[u]}\n" for u in self.order if u in units)

    def _write(self, text: str) -> None:
        with open(self._path(), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def setup(self, units):
        import subprocess
        os.makedirs(self.repo)
        self.order = list(units)
        self._write(self._render(units))
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_NOSYSTEM": "1"}
        for a in (["init", "-q"], ["add", "."], ["commit", "-qm", "init"]):
            subprocess.run(["git", *a], cwd=self.repo, env=env, check=True, capture_output=True)
        for a in ("ai1", "ai2"):
            self._ai(a)

    def _ai(self, agent):
        from .code.fakeagent import FakeClaudeCode
        if agent not in self.cc:
            sid = {"ai1": "sess-1111", "ai2": "sess-2222"}.get(agent, agent)
            self.cc[agent] = FakeClaudeCode(self.repo, home=self.home, session=sid)
        return self.cc[agent]

    def look(self, agent):
        self._ai(agent).read("f.txt")

    def human_set(self, unit, value):
        units = self._read()
        units[unit] = value
        self._write(self._render(units))

    def human_delete(self, unit):
        units = self._read()
        units.pop(unit, None)
        self._write(self._render(units))

    def hand_back(self, unit):
        lines = [ln.split(" = ")[0] for ln in self._render(self._read()).splitlines()]
        k = lines.index(unit) + 1
        self._ai("ai1").prompt(f"交还 f.txt:{k}")

    def value(self, unit):
        return self._read().get(unit)

    def _step(self, agent, new_units) -> Step:
        before = self._read()
        r = self._ai(agent).write("f.txt", self._render(new_units))
        after = self._read()
        if r.status == "denied" and after != before:
            self.bad.append("I3 说没有执行，文件却变了")
        applied = r.status == "ok" and "改回执行前" not in r.text
        if applied and after != new_units:
            self.bad.append("I3 说生效了，文件不是 AI 写的样子")
        return Step(applied=applied, told=r.text, alert=r.alert)

    def ai_set(self, agent, unit, value):
        units = self._read()
        units[unit] = value
        return self._step(agent, units)

    def ai_delete(self, agent, unit):
        units = self._read()
        units.pop(unit, None)
        return self._step(agent, units)

    def ai_create(self, agent, unit, value):
        units = self._read()
        units[unit] = value
        return self._step(agent, units)

    def ai_set_lost(self, agent, unit, value):
        cc = self._ai(agent)                      # 命令开始了（执行前的 hook 到了），改了文件，结束通知没到
        cc._hook("PreToolUse", tool_name="Bash", tool_input={"command": "edit", "timeout": 1000},
                 tool_use_id=cc._tid())
        units = self._read()
        units[unit] = value
        self._write(self._render(units))

    def resolve(self, agent):
        with _clock(3600):                        # 一小时之后：那条命令早该结束了
            self._ai(agent).prompt("继续")
        return Step()

    def violations(self):
        from .code.ledger import CodeLedger
        led = CodeLedger(self.repo, self.home)
        bad = list(self.bad)
        with led.open():
            led.observe()                         # 和下一次 hook 一样：先看见人存的盘（文本这一侧只在 hook 时看）
            for path in led.files:
                if led.content(path) != led.read_disk(path):
                    bad.append(f"I1 账本里 {path} 和磁盘上不一样")
        return bad

    def capabilities(self):
        from .code.session import CAPABILITIES as C
        return dict(C)

    def close(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


@contextlib.contextmanager
def _clock(shift: float):
    real = time.time
    time.time = lambda: real() + shift            # noqa: E731
    try:
        yield
    finally:
        time.time = real


# ---------------------------------------------------------------------------
# 应用侧：玩具容器，单元是字符（只在测试里用，证明核心不认识"行"）
# ---------------------------------------------------------------------------
class CharDriver(Driver):
    """一个字符串，每个单元是一个字符："A1B1"里 A 后面那个字符就是单元 A 的值。直接用核心的按位置的容器
    （core.positional），对应关系按字符算（difflib）。AI 的修改事先能算出结果：碰到不能改的字符就整次拒绝。"""
    name = "玩具容器（单元是字符）"
    CID = "doc"

    def __init__(self) -> None:
        self.text = ""
        self.recorded = ""                        # 账本以为的样子（核心不存内容，内容由应用侧存）
        self.led = PositionalLedger()
        self.bad: list[str] = []

    def _ops(self, old: str, new: str):
        return difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()

    def _record(self, new: str, author: str) -> None:
        old = self.recorded
        if new != old:
            self.led.record(self.CID, self._ops(old, new), author, len(old), whole_deleted=False,
                            describe=lambda i, j: list(old[i:j]))
        self.recorded = new

    def setup(self, units):
        self.text = "".join(f"{u}{v}" for u, v in units.items())
        self.recorded = self.text
        self.led.baseline(self.CID, {})
        for a in ("ai1", "ai2"):
            self.led.agent(a)

    def _find(self, text: str, unit: str) -> int:
        return text.find(unit)

    def value(self, unit):
        k = self._find(self.text, unit)
        return None if k < 0 else self.text[k + 1]

    def _set(self, text, unit, value):
        k = self._find(text, unit)
        return text[:k + 1] + value + text[k + 2:] if k >= 0 else text + unit + value

    def look(self, agent):
        self.led.see(agent, self.CID)

    def human_set(self, unit, value):
        self.text = self._set(self.text, unit, value)
        self._record(self.text, R.HUMAN)

    def human_delete(self, unit):
        k = self._find(self.text, unit)
        self.text = self.text[:k] + self.text[k + 2:]
        self._record(self.text, R.HUMAN)

    def hand_back(self, unit):
        k = self._find(self.text, unit)
        self.led.hand_back(self.CID, k + 1, k + 2)

    def _ai(self, agent, new: str) -> Step:
        ops = self._ops(self.recorded, new)
        hits = self.led.hits(self.CID, ops, agent, len(self.recorded))
        if R.refuse_whole(hits, separable=False):
            return Step(applied=False, told="没有执行：碰到了不能改的字符")
        self.text = new
        self._record(new, agent)
        return Step(applied=True)

    def ai_set(self, agent, unit, value):
        return self._ai(agent, self._set(self.text, unit, value))

    def ai_delete(self, agent, unit):
        k = self._find(self.text, unit)
        return self._ai(agent, self.text[:k] + self.text[k + 2:])

    def ai_create(self, agent, unit, value):
        old = "".join(sorted([self.text[i:i + 2] for i in range(0, len(self.text), 2)] + [unit + value]))
        return self._ai(agent, old)

    def ai_set_lost(self, agent, unit, value):
        self.text = self._set(self.text, unit, value)        # 执行了，结果没收到：先不记
        self.lost = agent

    def resolve(self, agent):
        d = R.settle_window(hits=False, quick=False, from_reference=False, deleted=False, lost=True)
        self._record(self.text, agent if d.author == "agent" else R.UNATTRIBUTED)
        return Step()

    def violations(self):
        return self.bad + ([] if self.recorded == self.text else ["I1 账本以为的样子和实际不一样"])

    def capabilities(self):
        return {"predict": True, "atomic": True, "around": False, "restore": True, "restore_deleted": True,
                "identity": True, "journal": False, "editing": False}

    def close(self):
        pass


def drivers() -> dict[str, Callable[[], Driver]]:
    """能跑的应用侧（有 bpy 时也有 Blender）。"""
    out: dict[str, Callable[[], Driver]] = {
        "fake": lambda: KeyedDriver("fake"),
        "fake_no_restore_deleted": lambda: KeyedDriver("fake", restore_deleted=False),
        "text": TextDriver,
        "chars": CharDriver,
        "cross": HooksKeyedDriver,
    }
    try:
        import importlib.util
        if importlib.util.find_spec("bpy") is not None:
            out["blender"] = lambda: KeyedDriver("blender")
    except (ImportError, ValueError):
        pass
    return out


def main() -> int:
    import json
    reports = []
    for key, make in drivers().items():
        rep = run(make)
        reports.append(rep)
        print(f"\n{rep['app']}：" + ("通过" if rep["passed"] else "不及格"))
        print("  能力：" + "、".join(f"{k}{'✔' if v else '✗'}" for k, v in rep["capabilities"].items()))
        for title, g in rep["scenarios"].items():
            print(f"  {title}：{ {'protected': '保护住了', 'reported': '保护不住，如实报告了', 'lied': '说了假话'}[g]}"
                  + (f"（{rep['details'][title]}）" if title in rep["details"] else ""))
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "experiments", "results")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "conformance.json"), "w", encoding="utf-8") as fh:
        json.dump(reports, fh, ensure_ascii=False, indent=1)
    return 0 if all(r["passed"] for r in reports) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
