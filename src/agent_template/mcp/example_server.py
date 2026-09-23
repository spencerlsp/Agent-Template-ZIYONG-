"""一个最小的 MCP 服务器（stdio），随模板一起发布。

两个用处：让 MCP 这条链路开箱即可跑通（不需要外部 server、不需要网络），
以及作为你写自己 server 时的样板。

手动跑起来观察协议交互：
    python -m agent_template.mcp.example_server

版本注意：mcp 2.x 的服务端基类是 MCPServer，1.x 里叫 FastMCP。
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("example")


@mcp.tool()
def echo(text: str) -> str:
    """原样返回传入的文本，用于验证链路是否通畅。"""
    return text


@mcp.tool()
def add(a: float, b: float) -> float:
    """返回两个数之和。"""
    return a + b


if __name__ == "__main__":
    mcp.run()  # 默认就是 stdio 传输