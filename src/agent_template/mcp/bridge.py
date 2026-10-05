"""把 MCP 工具注册进本地工具登记表，让模型不必区分本地工具与远端工具。"""

from __future__ import annotations

import logging
from typing import Any, Callable

from agent_template.mcp.client import MCPManager, MCPTool
from agent_template.tools.registry import ToolRegistry

logger = logging.getLogger("agent.mcp")

def _handler_for(manager: MCPManager, tool: MCPTool) -> Callable[..., Any]:
    """为单个远端工具造一个本地 handler。

    关键：登记表把模型的参数**以关键字形式展开**后调用 handler
    （RawArguments 用 extra="allow" 把它们收集成 dict），
    所以这里必须写成 **kwargs 接收，再原样打包成 MCP 需要的 arguments。
    """
    async def handler(**kwargs: Any) -> str:
        return await manager.call(tool.server, tool.name, kwargs)

    return handler

async def register_mcp_tools(
    registry: ToolRegistry, manager: MCPManager
) -> ToolRegistry:
    """发现所有 MCP 工具并注册进登记表；重名时跳过并告警，不覆盖本地工具。"""

    for tool in await manager.all_tools():
        if tool.name in registry:
            logger.warning(
                "MCP 工具 `%s`（来自 %s）与已有工具重名，已跳过",
                tool.name,
                tool.server,
            )
            continue
        registry.add_described(
            name=tool.name,
            description=tool.description,
            parameters=tool.parameters,
            handler=_handler_for(manager, tool),
            source=f"mcp:{tool.server}",
            # 远端工具是不是只读，由服务端在 tools/list 里声明（readOnlyHint）。
            # 这一步决定它在本地工具表里的两个行为：能否并发、要不要人工审批。
            # 服务端没声明就按 False 处理——宁可多问一次人，也不要静默放行。
            read_only=tool.read_only,
        )
    return registry
