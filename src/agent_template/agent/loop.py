"""Agent 主循环：推理 → 行动 → 观察，直到给出最终答案。

这是把前面所有部件串起来的地方，但注意它自己几乎不干活：

    模型层  提供对话与工具调用能力
    工具表  提供可执行的动作
    技能 / RAG / MCP  都只是往工具表里添加条目
    记忆    负责把历史带进这一轮

循环只做两件事：维护消息序列、按顺序推进状态。所有中间状态都通过 AgentEvent
抛出去，由调用方决定怎么呈现——CLI 打印、API 转 SSE、测试直接断言。

为什么"模型轮次"要写成生成器 + holder：
    Python 的 async generator 不能像普通函数那样 return 值，而流式输出又必须
    边收边抛事件。所以最终结果通过一个 dict 回传：事件照常 yield，终值写进 holder。
    这是这个场景下的标准做法，不是临时凑合。
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager, nullcontext
from typing import Any, AsyncIterator

from agent_template.agent.events import AgentEvent
from agent_template.agent.prompts import build_system_prompt
from agent_template.config import Settings
from agent_template.llm.base import LLMClient, Message, Usage
from agent_template.memory.store import MemoryStore
from agent_template.obs.tracing import TokenAccountant, Tracer
from agent_template.skills.loader import SkillsIndex
from agent_template.tools.registry import ToolRegistry

logger = logging.getLogger("agent.loop")


class AgentLoop:
    """一轮对话的完整流程。"""

    def __init__(
        self,
        *,
        llm: LLMClient,
        registry: ToolRegistry,
        memory: MemoryStore,
        settings: Settings,
        skills: SkillsIndex | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.memory = memory
        self.settings = settings
        self.skills = skills
        self.tracer = tracer
        self.accountant = TokenAccountant()

    # ------------------------------------------------------------------ 入口
    async def run(
        self, session_id: str, user_input: str, *, stream: bool | None = None
    ) -> AsyncIterator[AgentEvent]:
        """跑一轮完整对话，把过程以事件形式抛出去。"""
        use_stream = self.settings.stream if stream is None else stream

        # 系统提示每轮重建：技能目录和工具清单都来自当前装配状态
        system = build_system_prompt(self.settings, self.registry, self.skills)

        # 历史先读出来，再追加本轮输入——顺序不能反，否则 user 消息会出现两次
        history = self.memory.history(session_id)
        user_message = Message.user(user_input)
        self.memory.append(session_id, user_message)
        messages: list[Message] = [Message.system(system), *history, user_message]

        yield AgentEvent(
            kind="started",
            data={"session_id": session_id, "max_steps": self.settings.max_steps},
        )

        for step in range(self.settings.max_steps):
            holder: dict[str, Any] = {}
            async for event in self._model_turn(messages, step, use_stream, holder):
                yield event

            message: Message | None = holder.get("message")
            if message is None:
                yield AgentEvent(kind="error", step=step, text="模型没有返回任何内容")
                return

            usage: Usage | None = holder.get("usage")
            if usage:
                self.accountant.add(usage.prompt_tokens, usage.completion_tokens)
                yield AgentEvent(
                    kind="usage",
                    step=step,
                    data={
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                    },
                )

            # assistant 消息必须入历史：即使它只有 tool_calls、content 为空，
            # 缺了它，下一轮的 tool 结果就找不到配对，接口会直接拒绝
            messages.append(message)
            self.memory.append(session_id, message)

            if not message.tool_calls:
                # 没有工具调用 = 模型认为可以回答了，这一轮结束
                yield AgentEvent(kind="finished", step=step, text=message.content or "")
                return

            for call in message.tool_calls:
                yield AgentEvent(
                    kind="tool_call",
                    step=step,
                    tool_name=call.name,
                    tool_call_id=call.id,
                    tool_arguments=call.arguments,
                )

                with self._span("tool.call", tool=call.name):
                    # registry.call 从不抛异常，失败会变成 "错误：..." 文本，
                    # 让模型自己看到并纠正，而不是把循环打断
                    result = await self.registry.call(call.name, call.arguments)

                tool_message = Message.tool_result(call.id, result, name=call.name)
                messages.append(tool_message)
                self.memory.append(session_id, tool_message)

                yield AgentEvent(
                    kind="tool_result",
                    step=step,
                    tool_name=call.name,
                    tool_call_id=call.id,
                    tool_result=result,
                )

        # for 循环跑满都没 return，说明一直在调工具、没给出最终答案
        yield AgentEvent(
            kind="error",
            step=self.settings.max_steps - 1,
            text=f"达到 {self.settings.max_steps} 步上限仍未给出最终答案",
        )

    # -------------------------------------------------------------- 模型轮次
    async def _model_turn(
        self,
        messages: list[Message],
        step: int,
        stream: bool,
        holder: dict[str, Any],
    ) -> AsyncIterator[AgentEvent]:
        """跑一次模型调用：过程中抛增量事件，终值写进 holder。"""
        tools = self.registry.specs()

        with self._span("llm.turn", step=step, stream=stream, tools=len(tools)):
            if not stream:
                response = await self.llm.chat(messages, tools=tools)
                holder["message"] = response.message
                holder["usage"] = response.usage
                # 非流式也抛成同样的增量事件：调用方的渲染逻辑只需要写一套
                if response.message.reasoning:
                    yield AgentEvent(kind="reasoning", step=step, text=response.message.reasoning)
                if response.message.content:
                    yield AgentEvent(kind="text", step=step, text=response.message.content)
                return

            async for chunk in self.llm.stream_chat(messages, tools=tools):
                if chunk.kind == "text":
                    yield AgentEvent(kind="text", step=step, text=chunk.text)
                elif chunk.kind == "reasoning":
                    yield AgentEvent(kind="reasoning", step=step, text=chunk.text)
                elif chunk.kind == "done":
                    holder["message"] = chunk.message
                    holder["usage"] = chunk.usage

    # ------------------------------------------------------------------ 辅助
    def _span(self, name: str, **attributes: Any) -> AbstractContextManager[Any]:
        """没有配置 tracer 时退化成空上下文，调用处不用写 if。"""
        if self.tracer is None:
            return nullcontext()
        return self.tracer.span(name, **attributes)

    @property
    def stats(self) -> str:
        """给 CLI 收尾时打印的一行统计。"""
        return self.accountant.summary()