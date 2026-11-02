"""代码接入（spec/design_code.md）：人在编辑器里改代码，编程 Agent（现在是 Claude Code）同时改同一批文件。

人改过、还没交还（也还没提交）的行，Agent 不能改；由 Claude Code 的 hooks 强制执行，不改 Claude Code。
用法：python -m rightofway.code install（在项目目录里），之后重新打开 Claude Code 的会话。
"""
from .ledger import CodeLedger, find_root
from .hooks import handle

__all__ = ["CodeLedger", "find_root", "handle"]
