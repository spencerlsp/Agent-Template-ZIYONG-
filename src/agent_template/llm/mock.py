"""Offline, deterministic model.

It is deliberately not smart: it exists so the whole pipeline - agent loop,
tools, skills, MCP, RAG - can run in CI and be demoed with no network and no
API key.
"""
# 离线确定性模型。
# 它刻意不具备智能能力：设计它的目的，是让整套流水线（智能体循环、工具、技能、MCP、RAG）
# 能够在 CI 环境中运行，并且无需网络、无需 API 密钥即可演示


from __future__ import annotations

from typing import Any, AsyncIterator

from agent_template.llm.base import (
    ChatResponse,
    LLMClient,
    Message,
    StreamChunk,
    ToolCall,
    ToolSpec,
    Usage,
)


class MockLLM(LLMClient):
    def __init__(
            self, model: str = "mock-01", scripted: list | None = None
    ) -> None:
        self.model = model
        self.scripted = list(scripted or [])
        self.calls: list[list[Message]] = []


    def _pick_tool(self, text: str, tools: list[ToolSpec]) -> ToolCall | None:
        """Naive keyword routing, enough to exercise the tool loop."""
        # 简易关键词路由，仅用于跑通工具调用循环
        available = {spec.name for spec in tools}
        lowered = text.lower()
        rules: list[tuple[tuple[str, ...], str, dict[str, Any]]] = [
            (("几点", "时间", "time", "date"), "get_current_time", {}),
            (("计算", "calculate", "等于"), "calculator", {"expression": "1+1"}),
            (
                ("知识库", "文档", "检索", "search", "index"),
                "search_knowledge_base",
                {"query": text},
            ),
            (("技能", "skill", "流程"), "load_skill", {"name": "example-chat-style"}),
        ]
        for keywords, name, arguments in rules:
            if name is available and any(keyword in lowered for keyword in keywords):
                return ToolCall(id=f"mock_{name}", name=name, arguments=arguments)
        return None


    async def chat(
            self,
            messages: list[Message],
            *,
            tools: list[ToolSpec] | None = None,
            temperature: float | None = None,
            max_tokens: int | None = None,
            response_format: dict[str, Any] | None = None,
    ) -> ChatResponse:
        self.calls.append(list(messages))
        if self.scripted:
            message = self.scripted.pop(0)
        else:
            last_tool = next(
                (m for m in reversed(messages) if m.role == "tool"), None
            )
            last_user = next(
                (m for m in reversed(messages) if m.role == "user"), None
            )
            if last_tool is not None:
                message = Message.assistant(
                    f"[mock] tool `{last_tool.name}` returned: "
                    f"{(last_tool.content or '')[:400]}"
                )
            else:
                text = (last_user.content if last_user else "") or ""
                call = self._pick_tool(text, tools or [])
                message = (
                    Message.assistant(tool_calls=[call])
                    if call
                    else Message.assistant(f"[mock] echo: {text}")
                )

        usage = Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        return ChatResponse(message=message, usage=usage)        
    async def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Reuse chat() and emit the answer token by token."""
        # 复用 chat()，把答案拆成一个个片段吐出来
        response = await self.chat(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens
        )
        if response.message.content:
            for token in response.message.content.split(" "):
                yield StreamChunk(kind="text", text=token + " ")
        yield StreamChunk(kind="done", message=response.message, usage=response.usage)

    async def aclose(self) -> None:
        return None