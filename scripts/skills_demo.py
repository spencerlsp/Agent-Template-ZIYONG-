"""技能加载演示。

展示渐进式披露的两半：
  - 放进系统提示的只有技能目录（名字 + 一句话描述）
  - 正文只有在模型调用 load_skill 时才会被取走
"""

from __future__ import annotations

import asyncio
import sys

from agent_template.config import Settings
from agent_template.llm import Message, build_llm
from agent_template.skills import SkillsIndex, register_skill_tools
from agent_template.tools import ToolRegistry


DEFAULT_PROMPT = "先列出这个项目里有哪些技能，然后按技能规定的风格回答：什么是函数调用？"

async def main(prompt: str) -> None:
    settings = Settings()
    llm = build_llm(settings)
    index = SkillsIndex.from_dir(settings.resolve(settings.skills_dir))

    registry = ToolRegistry()
    register_skill_tools(registry, index)

    body_chars = sum(len(index.get(name).body) for name in index.names())
    print(f"磁盘上的技能      ：{index.names()}")
    print(f"放进提示的目录    ：{len(index.catalog())} 字符")
    print(f"被扣住的正文      ：{body_chars} 字符（模型要才会取）")
    print(f"可用工具          ：{registry.names()}\n")

    system = f"{settings.effective_system_prompt}\n\n可用技能：\n{index.catalog()}"
    messages = [Message.system(system), Message.user(prompt)]

    for step in range(settings.max_steps):
        response = await llm.chat(messages, tools=registry.specs())
        messages.append(response.message)
        if not response.message.tool_calls:
            print(f"[第 {step} 步] 最终回答：\n{response.message.content}")
            break
        for call in response.message.tool_calls:
            result = await registry.call(call.name, call.arguments)
            print(f"[第 {step} 步] {call.name}({call.arguments}) -> 返回 {len(result)} 字符")
            messages.append(Message.tool_result(call.id, result, name=call.name))
    else:
        print(f"达到 {settings.max_steps} 步上限仍未给出最终答案")

    await llm.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT))
