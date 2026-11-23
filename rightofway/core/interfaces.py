"""两侧接口（spec/core.md 第 4 节）。两侧互相独立：任何一个 AI 侧都能配任何一个应用侧，中间是核心。

AI 侧（在哪里截住 AI，例如 Claude Code 的 hooks、MCP 代理）
  交给核心：AI 想做什么（Intent：这次编辑、这条命令、这次工具调用），以及"做完了"；
  拿回：放行还是拒绝，以及要对 AI 说的话、要提醒人的话（Decision、Outcome）。
  它只和 Session 说话：intro（会话开始）、prompt（人发了消息，可能带交还）、begin（执行前）、end（执行后）、
  look（AI 读了东西）。

应用侧（东西存在哪、怎么看见、怎么放回，例如文本文件、Blender、模拟应用）
  交给核心：单元和指纹、单元的名字和值（写说明用）、身份跟踪（认回的配对，或者位置的对应关系）、能力；
  拿回：这次要保持哪些单元、要清掉哪些重建出来的东西。
  能力不手写，靠自测得出（P4），名字见 CAPABILITIES；组合起来能有多强的保证取决于两侧各自的能力，
  做不到的照实说（P1），一致性测试按三档报告（tests/conformance）。

Session 是两侧之间的一层：实现它的是某个应用侧 + 核心（文本文件：rightofway/code/session.py；
按编号的应用：rightofway/blender/hook_session.py）。AI 侧不知道对面是什么应用。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

# 应用侧的能力（连接时自测得出；文本文件这一侧是固定的几项，见 code/session.py）
CAPABILITIES = {
    "predict": "执行前就能算出结果（碰到不能改的单元就整次拒绝，什么都不改）",
    "atomic": "能把 快照 → 执行 → 放回 包成一次做完（执行期间人插不进来）",
    "around": "能把一次不透明的工具调用包在快照和放回之间",
    "restore": "能把改掉的单元放回去",
    "restore_deleted": "能把删掉的东西放回去",
    "identity": "能认出删了又建的是同一个东西（身份跟踪）",
    "journal": "按许可单编号记执行结果：结果没收到时能查回来，同一张许可单只执行一次",
    "editing": "能看见人正在改哪里（占用）",
}

GRADES = ("protected", "reported", "lied")      # 保护住了 / 保护不住、但如实报告了 / 说了假话（只有这一档不及格）


@dataclass
class Intent:
    """AI 想做什么。kind：edit（事先能算出结果的修改）、command（事后才知道改了什么）、tool（一次工具调用）、
    script（一段代码）。tool 是 AI 侧的工具名；target 是它提到的东西（文件、对象名）；payload 是参数；
    ticket 是 AI 侧给这次执行的编号（例如 tool_use_id；没有就由 Session 自己对上）。"""
    kind: str
    tool: str = ""
    target: Optional[str] = None
    payload: dict = field(default_factory=dict)
    ticket: Optional[str] = None


@dataclass
class Decision:
    allowed: bool = True
    message: str = ""          # 给 AI 的话（拒绝的理由）
    alert: str = ""            # 给人的提醒


@dataclass
class Outcome:
    texts: list[str] = field(default_factory=list)     # 给 AI 的话
    alerts: list[str] = field(default_factory=list)    # 给人的提醒
    report: Any = None

    @property
    def message(self) -> str:
        return "\n\n".join(self.texts)

    @property
    def alert(self) -> str:
        return "\n".join(self.alerts)


class Session(Protocol):
    """AI 侧只和这个说话。每个方法是一次完整的来回：读账本、看见变化、问核心、照做、存盘。"""

    def intro(self, agent: str, name: str) -> list[str]:
        """会话开始：告诉 AI 规则，以及现在哪些部分受保护。"""

    def prompt(self, agent: str, name: str, text: str, human: bool = True) -> tuple[bool, list[str]]:
        """人发了一条消息（human=False：AI 写给子 AI 的，里面的交还不算）。
        返回 (这条消息只有交还命令、不用发给 AI, 要说的话)。"""

    def begin(self, agent: str, name: str, intent: Intent) -> Decision:
        """AI 要做一件事：放行还是拒绝。"""

    def end(self, agent: str, name: str, intent: Intent, failed: bool = False) -> Outcome:
        """这件事做完了（failed：工具自己报错了）：按实际状态结算，告诉 AI 和人。"""

    def look(self, agent: str, name: str, target: Optional[str]) -> list[str]:
        """AI 读了一个东西：告诉它上次看之后的变化，记下它看过了。"""


def capability_report(app: str, caps: dict, grades: Optional[dict] = None) -> dict:
    """一个应用侧的能力报告：自测得出的能力，以及每个一致性场景的结果（protected / reported / lied）。"""
    out = {"app": app, "capabilities": {k: bool(caps.get(k)) for k in CAPABILITIES}}
    if grades is not None:
        out["scenarios"] = dict(grades)
        out["passed"] = all(g != "lied" for g in grades.values())
    return out
