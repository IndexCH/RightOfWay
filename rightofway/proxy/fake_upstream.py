"""一个假的"应用 MCP 服务器"（测试和演示用）：内存里的假应用（rightofway/fakeapp.py），工具的样子仿照 BlenderMCP。

    execute_code(code)                         执行 Python（命名空间里有 app 和 scene），返回打印的内容
    get_scene_info()                           只读：列出对象
    set_object_property(object_name, property, value)
    create_object(name, kind)
    delete_object(object_name)

除了 execute_code，这些工具都直接改应用，不经过 RightOfWay：代理只能把它们当成不透明的工具调用包起来。

    python -m rightofway.proxy.fake_upstream            通过标准输入输出提供服务（代理的端到端测试用）
"""
from __future__ import annotations

import contextlib
import io
import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..fakeapp import AgentScene, FakeApp


def build(app: FakeApp) -> FastMCP:
    mcp = FastMCP("fake-app")

    @mcp.tool()
    def execute_code(code: str) -> str:
        """Run Python in the app (names available: app, scene). Whatever it prints is returned."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(code, {"app": app, "scene": AgentScene(app)})
        return "Code executed successfully: " + out.getvalue()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_scene_info() -> str:
        """List the objects in the scene."""
        return json.dumps({o["name"]: o["faces"] for o in app.objects.values() if not o.get("data")},
                          ensure_ascii=False, sort_keys=True)

    @mcp.tool()
    def set_object_property(object_name: str, property: str, value: Any) -> str:
        """Set one property of an object."""
        app.set(object_name, property, value)
        return f"{object_name}.{property} = {value!r}"

    @mcp.tool()
    def create_object(name: str, kind: str = "MESH") -> str:
        """Create an object."""
        app.add(name, {"location": [0, 0, 0], "rotation": [0, 0, 0], "scale": 1.0}, prefix="a-", kind=kind)
        return f"created {name}"

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    def delete_object(object_name: str) -> str:
        """Delete an object."""
        app.delete(object_name)
        return f"deleted {object_name}"

    return mcp


def main() -> None:
    import sys
    restore = "--no-restore" not in sys.argv
    build(FakeApp(restore_deleted=restore, unique_names=True)).run("stdio")


if __name__ == "__main__":
    main()
