"""Function calling smoke test.

Asks a question that needs tools, runs what the model asks for, feeds the
results back, and prints the final answer. This is the agent loop in miniature;
the real one lives in agent_template.agent.
"""

from __future__ import annotations

import asyncio
import sys

from agent_template.config import Settings
from agent_template.llm import Message, build_llm
from agent_template.tools import ToolRegistry, register_builtin_tools

DEFAULT_PROMPT = "现在几点了？ 另外帮我算一下（5+3）*7等于多少。"

async def main(prompt: str) -> None:
    settings = Settings()
    llm = build_llm(settings)
    registry = register_builtin_tools(ToolRegistry(), settings)
    print("tools:", registry.names())

    messages = [Message.system(settings.effective_system_prompt), Message.user(prompt)]
    for step in range(settings.max_steps):
        response = await llm.chat(messages, tools=registry.specs())
        messages.append(response.message)
        if not response.message.tool_calls:
            print(f"\n[step {step}] final answer:\n{response.message.content}")
            break

        for call in response.message.tool_calls:
            print(f"\n[step {step}] model calls {call.name}({call.arguments})")
            result = await registry.call(call.name, call.arguments)
            print(f"          -> {result[:200]}")
            messages.append(Message.tool_result(call.id, result, name=call.name))
    else:
        print(f"stopped after {settings.max_steps} steps without a final answer")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT))
