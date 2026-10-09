"""MCP 代理（design_v0.5.md 第 9 节，prior_art_solutions.md 第 4 项）。

AI 客户端连代理，代理连应用现成的 MCP 服务器（上游），应用和上游都不用改（P3）：
    AI 客户端 ──MCP──▶ 代理（运行时在这里）──MCP──▶ 应用的 MCP 服务器 ──▶ 应用

- client.py   作为 MCP 客户端连上游（同步接口，后台线程里跑 asyncio）
- sources.py  运行时自己的调用（poll、run_agent……）怎么经过上游的"执行代码"工具发进应用
- core.py     代理的规则：每个工具调用走许可 → 执行 → 核对 → 告知（Proxy）
- server.py   作为 MCP 服务器给 AI 客户端用（python -m rightofway.proxy）
需要 pip install "mcp>=1.20,<2"。
"""
