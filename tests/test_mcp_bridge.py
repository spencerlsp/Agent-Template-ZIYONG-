"""MCP 桥接层的测试：远端工具怎么映射进本地工具表。

这里只钉一件事——**服务端声明的只读提示要传到本地工具表**。
它不是装饰性字段：`read_only` 同时决定「能不能并发」和「要不要人工审批」，
映射错了，纯读的检索工具会每问一句弹一次确认，在脚本 / CI 里直接被拒绝。
"""

from __future__ import annotations

from typing import Any

from agent_template.mcp.bridge import register_mcp_tools
from agent_template.mcp.client import MCPTool
from agent_template.tools.registry import ToolRegistry


class StubManager:
    """只实现 bridge 真正用到的那两个方法的替身。

    不连真 server：这一层要验证的是「字段映射」，不是协议。
    """

    def __init__(self, tools: list[MCPTool]) -> None:
        self._tools = tools
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def all_tools(self) -> list[MCPTool]:
        return self._tools

    async def call(self, server: str, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((server, name, arguments))
        return f"{server}/{name}"


def mcp_tool(name: str, *, read_only: bool) -> MCPTool:
    return MCPTool(
        server="ragkit",
        name=name,
        description="测试用远端工具",
        parameters={"type": "object", "properties": {}},
        read_only=read_only,
    )


async def test_read_only_hint_reaches_the_registry() -> None:
    """服务端声明只读 → 本地按只读处理（可并发、免审批）。"""
    registry = ToolRegistry()

    await register_mcp_tools(
        registry, StubManager([mcp_tool("search_knowledge_base", read_only=True)])
    )

    assert registry.is_parallel_safe("search_knowledge_base") is True


async def test_absent_hint_stays_side_effecting() -> None:
    """服务端没声明 → 仍然按「有副作用」处理。

    宁可多问一次人，也不要因为"没写"就静默放行。
    """
    registry = ToolRegistry()

    await register_mcp_tools(registry, StubManager([mcp_tool("dangerous", read_only=False)]))

    assert registry.is_parallel_safe("dangerous") is False


async def test_remote_tool_is_callable_through_the_bridge() -> None:
    """注册的不只是名字：handler 要能把参数原样转发到远端。"""
    registry = ToolRegistry()
    manager = StubManager([mcp_tool("search_knowledge_base", read_only=True)])
    await register_mcp_tools(registry, manager)

    result = await registry.call(
        "search_knowledge_base", {"query": "怎么建索引", "top_k": 3}
    )

    assert result == "ragkit/search_knowledge_base"
    assert manager.calls == [
        ("ragkit", "search_knowledge_base", {"query": "怎么建索引", "top_k": 3})
    ]


async def test_local_tool_wins_on_name_conflict() -> None:
    """重名时本地工具优先，远端那个被跳过——这是既有行为，别在这次改动里丢掉。"""
    registry = ToolRegistry()

    @registry.register
    def search_knowledge_base(query: str) -> str:
        """本地实现。"""
        return "local"

    await register_mcp_tools(
        registry, StubManager([mcp_tool("search_knowledge_base", read_only=True)])
    )

    assert await registry.call("search_knowledge_base", {"query": "x"}) == "local"
