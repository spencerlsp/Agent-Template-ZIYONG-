"""RAG 演示，分两幕。

第一幕：直接调用检索，把向量路和关键词路的排名都打出来，看清楚混合检索在做什么。
第二幕：把 search_knowledge_base 交给模型，让模型自己决定检索，并要求它标注来源。

    uv run python scripts/rag_demo.py
    uv run python scripts/rag_demo.py "MCP 在 agent 里起什么作用？"
"""

from __future__ import annotations

import asyncio
import sys

from agent_template.config import Settings
from agent_template.llm import build_llm, Message
from agent_template.rag.pipeline import RagPipeline
from agent_template.rag.tools import register_rag_tools
from agent_template.tools import ToolRegistry

DEFAULT_PROMPT = "根据本地知识库回答：RAG 的基本流程分几步，分别是什么？"

async def main(prompt: str) -> None:
    settings = Settings()
    llm = build_llm(settings)
    pipeline = RagPipeline(settings)

    try:
        pipeline.ensure_ready()

        # ---- 第一幕：直接看检索 ----
        chunks = await pipeline.search(prompt)
        print("=== 直接检索 ===")
        print("向量路排名  ：", pipeline.retriever.last_debug.get("vector"))
        print("关键词路排名：", pipeline.retriever.last_debug.get("keyword"))
        for order, chunk in enumerate(chunks, start=1):
            preview = chunk.text[:90].replace("\n", " ")
            print(f"  [{order}] {chunk.source}  RRF={chunk.score:.5f}  {preview}...")

        # ---- 第二幕：交给模型 ----
        registry = register_rag_tools(ToolRegistry(), pipeline)
        system = f"{settings.effective_system_prompt}\n\n可用工具：\n{registry.describe()}"
        messages = [Message.system(system), Message.user(prompt)]

        print("\n=== 交给模型 ===")
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
    finally:
        await pipeline.aclose()
        await llm.aclose()  

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT))
