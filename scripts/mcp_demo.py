"""MCP 演示：连上示例 server，把它的工具注册进登记表，再让模型调用。

不需要网络，也不需要任何外部 MCP server。
"""

from __future__ import annotations

import asyncio
import sys

from agent_template.config import Settings
from agent_template.llm import Message, build_llm
from agent_template.mcp import MCPManager, register_mcp_tools
from agent_template.tools import ToolRegistry

DEFAULT_PROMPT = "帮我算一下 12.5 加 7.25，然后用 echo 工具原样回显这句话：链路已通"

async def main(prompt: str) -> None:
    settings = Settings()
    llm = build_llm(settings)

    manager = MCPManager(settings.mcp_servers)
    registry = ToolRegistry()

    try:
        connected = await manager.connect_all()
        print(f"已连接的 MCP 服务器：{connected}")
        if not connected:
            print("没有可用的 MCP 服务器，退出")
            return 

        await register_mcp_tools(registry, manager)
        print("注册后的工具清单：")
        print(registry.describe())

        system = f"{settings.effective_system_prompt}\n\n可用工具：\n{registry.describe()}"
        messages = [Message.system(system), Message.user(prompt)]

        for step in range(settings.max_steps):
            response = await llm.chat(messages, tools=registry.specs())
            messages.append(response.message)
            if not response.message.tool_calls:
                print(f"\n[第 {step} 步] 最终回答：\n{response.message.content}")
                break
            for call in response.message.tool_calls:
                result = await registry.call(call.name, call.arguments)
                print(f"[第 {step} 步] {call.name}({call.arguments}) -> {result[:120]}")
                messages.append(Message.tool_result(call.id, result, name=call.name))
        else:
            print(f"达到 {settings.max_steps} 步上限仍未给出最终答案")
    finally:
        # 无论中间发生什么，子进程都要收掉，否则会残留 python 进程
        await manager.aclose()


if __name__ == "__main__":
    # 命令行传参：python xxx.py "你的prompt"；不传则使用DEFAULT_PROMPT默认提示词
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT))