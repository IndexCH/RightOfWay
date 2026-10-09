"""实验 P：经过 MCP 代理（rightofway/proxy）的同一份协作。

AI 客户端只连代理，代理连应用现成的 MCP 服务器（design_v0.5.md 第 9 节，prior_art_solutions.md 第 4 项）。
这里的"AI"是写好的脚本，扮演 Claude Desktop 里的大模型，直接调用代理的工具（MCP 线路本身由 tests/test_proxy.py 测）。

和实验 A 同一个故事：
  1. AI 用执行代码的工具布置场景；
  2. 人删掉 Rock_2、把 Leaf_3 往上挪、新建一个立方体；
  3. AI 没先看场景，按旧印象整体调整（执行代码的工具）：Leaf_3 的高度保留人的，补回 Rock_2 被拦下；
  4. AI 再用几个工具：代理的小工具改 Leaf_3 的位置（执行前就拒绝）、删 Cube（拒绝）、改 Trunk 的缩放（生效）；
     上游自己的工具（只读的、以及假应用上的类型化修改）照样转发，事后核对。
看人的修改有没有被覆盖、AI 的修改有没有丢失、工具结果的 isError 和说明、不变量。

    python -m experiments.exp_p_proxy                                    不需要任何软件：假的应用 MCP 服务器
    python -m experiments.exp_p_proxy --upstream "uvx blender-mcp"       打开着的 Blender（BlenderMCP 插件，端口 9876）
                                                                         + 现成的 BlenderMCP 服务器（需要装 uv）
需要 pip install -e ".[proxy]"。
"""
from __future__ import annotations

import argparse
import shlex
import time

from rightofway.blender.evaluate import evaluate
from rightofway.blender.shared_session import SharedSession
from rightofway.runtime import Runtime
from experiments import scenario_fake as sf
from experiments import scenario_tree as sc
from experiments.common import InvariantLog, banner, say, use_utf8_console, wait_for_human, write_row

_SETUP = '''
import bpy
sc = bpy.data.scenes.get({name!r}) or bpy.data.scenes.new({name!r})
for w in bpy.context.window_manager.windows:
    w.scene = sc
'''


def _text(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


def main(argv=None) -> int:
    use_utf8_console()
    ap = argparse.ArgumentParser(description="实验 P：经过 MCP 代理的同一份协作")
    ap.add_argument("--upstream", default="fake", help='fake（默认），或者启动应用 MCP 服务器的命令，例如 "uvx blender-mcp"')
    ap.add_argument("--human", choices=["sim", "real"], default="sim")
    ap.add_argument("--port", type=int, default=9876, help="Blender 插件的端口（--upstream 是真的服务器时）")
    ap.add_argument("--no-csv", action="store_true")
    args = ap.parse_args(argv)
    try:
        from rightofway.proxy.client import SyncMCPClient
        from rightofway.proxy.core import Proxy
        from rightofway.proxy.sources import BlenderSource, FakeSource, MCPBridge, detect_code_tool
    except ImportError:
        print('需要先安装 MCP：pip install -e ".[proxy]"')
        return 0

    fake = args.upstream == "fake"
    scene = None
    if fake:
        from rightofway.fakeapp import FakeApp
        from rightofway.proxy import fake_upstream
        app = FakeApp(unique_names=True)
        client = SyncMCPClient.in_memory(fake_upstream.build(app))
        build, adjust = sf.ai_build(), sf._code(sf._ADJUST)
        human = lambda: sf.human_a(app)                                 # noqa: E731
        name = "假的应用 MCP 服务器"
    else:
        from rightofway.blender.bridge import SocketBridge
        blender = SocketBridge(port=args.port)
        scene = "RightOfWay实验_代理_" + time.strftime("%H%M%S")
        blender.call("cleanup_scenes", {"prefix": "RightOfWay实验_"})
        blender.execute(_SETUP.format(name=scene))
        print(f"已在 Blender 里新建并切换到场景「{scene}」，你原来的场景不受影响。")
        client = SyncMCPClient.stdio(shlex.split(args.upstream), env={"BLENDER_PORT": str(args.port)})
        build, adjust = sc.ai_build(scene), sc.ai_adjust(scene)
        human = lambda: blender.execute(sc.human_sim(scene))            # noqa: E731
        name = args.upstream
    tools = client.list_tools()
    code_tool, code_param = detect_code_tool(tools)
    bridge = MCPBridge(client, code_tool, code_param, FakeSource() if fake else BlenderSource())
    rt = Runtime()
    session = SharedSession(rt, bridge, scene=scene)
    session.start()
    proxy = Proxy(session, client, code_tool, code_param)
    inv = InvariantLog(True)
    banner(f"实验 P：经过 MCP 代理（上游：{name}；执行代码的工具：{code_tool}）")
    say("info", "代理给 AI 的工具：" + "、".join(t.name for t in proxy.list_tools()))

    def call(tool: str, arguments: dict, note: str):
        say("ai", f"{note}（{tool}）")
        r = proxy.call_tool(tool, arguments)
        say("rt", ("isError=true\n" if r.isError else "") + _text(r)[:1500])
        return r

    try:
        call(code_tool, {code_param: build}, "第 1 步：布置场景")
        if args.human == "real" and not fake:
            wait_for_human(sc.HUMAN_STEPS)
        else:
            say("human", "删除 Rock_2；把 Leaf_3 往上挪；新建一个立方体 Cube")
            human()
        results = [call(code_tool, {code_param: adjust}, "第 2 步：没先看场景，按旧印象整体调整")]
        results.append(call("rightofway_set_property", {"object": "Leaf_3", "property": "location", "value": [0, 0, 1.6]},
                            "把 Leaf_3 放回 1.6"))
        results.append(call("rightofway_delete_object", {"object": "Cube"}, "删掉 Cube"))
        results.append(call("rightofway_set_property", {"object": "Trunk", "property": "scale",
                                                         "value": 1.5 if fake else [1.5, 1.5, 1.5]}, "树干加粗"))
        read = "get_scene_info" if "get_scene_info" in proxy.upstream else None
        if read:
            results.append(call(read, {}, "看场景（上游自己的只读工具）"))
        if fake:
            results.append(call("delete_object", {"object_name": "Leaf_3"}, "删掉 Leaf_3（上游自己的类型化工具）"))
        inv.at_end(session)
        final = session.scene_records()
        ev = evaluate(rt, final, session.human, granularity="aspect", labels=session.labels)
        errors = sum(r.isError for r in results)
        blocked = sum(len(r.meta["rightofway"]["identity"]["recreate_blocked"]) for r in results
                      if r.meta and "identity" in r.meta.get("rightofway", {}))
        breach = sum(len(r.meta["rightofway"].get("breach", [])) for r in results if r.meta)
        banner("结果")
        say("info", ev.summary())
        say("info", inv.summary())
        say("info", f"AI 的 {len(results)} 次调用里，{errors} 次返回 isError（有没生效、被拒的部分）；补回人删掉的对象被拦下 {blocked} 个")
        if proxy.alerts:
            say("info", "给人的提醒：" + "；".join(proxy.alerts))
        if not args.no_csv:
            path = write_row({
                "实验": "P 经过 MCP 代理", "做法": "有保护（" + ("假的应用 MCP 服务器" if fake else "BlenderMCP") + "）",
                "人": "真人" if args.human == "real" else "模拟",
                "人的修改被覆盖": len(ev.human_overwritten), "AI的修改丢失": len(ev.ai_lost),
                "被删对象被AI重建": 0 if blocked else "", "恢复或合并出错": 0, "不变量违反": inv.column(),
                "违规": breach, "AI候选": 0, "AI修改生效": "", "AI修改未采用": "", "人看到AI结果要等(秒)": "",
                "备注": f"{len(results)} 次调用，{errors} 次 isError；补回被拦下 {blocked} 个"})
            print(f"\n结果已追加到 {path}")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
