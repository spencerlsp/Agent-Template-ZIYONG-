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

import asyncio
import logging
import uuid
from contextlib import AbstractContextManager, nullcontext
from typing import Any, AsyncIterator

from agent_template.agent.events import AgentEvent
from agent_template.agent.prompts import build_system_prompt
from agent_template.config import Settings
from agent_template.llm.base import LLMClient, Message, Usage, ToolCall
from agent_template.memory.store import MemoryStore
from agent_template.obs.tracing import TokenAccountant, TraceSpan, Tracer
from agent_template.skills.loader import SkillsIndex
from agent_template.tools.registry import ToolRegistry
from agent_template.agent.approval import (
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequest,
    approve_all,
    rejected_message,
)

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
        approve: ApprovalHandler | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.memory = memory
        self.settings = settings
        self.skills = skills
        self.tracer = tracer
        self.accountant = TokenAccountant()
        # 没注入就用“全部批准”： 审批时显式开启的能力
        # 默认关闭能保证现有代码路径一行都不变
        self._approve: ApprovalHandler = approve or approve_all

    # ------------------------------------------------------------------ 入口
    async def run(
        self, session_id: str, user_input: str, *, stream: bool | None = None
    ) -> AsyncIterator[AgentEvent]:
        """跑一轮完整对话，把过程以事件形式抛出去。"""
        use_stream = self.settings.stream if stream is None else stream

        # 一次问答一个 run_id：同一个进程里可能跑很多轮，
        # Tracer 的 trace_id 是进程级的，切不出"这一轮"，所以另起一个
        run_id = uuid.uuid4().hex[:8]
        if self.tracer is not None:
            # 注入给本轮所有 span（llm.turn / tool.call / tools.batch 都会带上），
            # 事后就能按 会话 或 会话+轮次 聚合 token
            self.tracer.set_context(session=session_id, run=run_id)

        # 系统提示每轮重建：技能目录和工具清单都来自当前装配状态
        system = build_system_prompt(self.settings, self.registry, self.skills)

        # 历史先读出来，再追加本轮输入——顺序不能反，否则 user 消息会出现两次
        history = self.memory.history(session_id)
        user_message = Message.user(user_input)
        self.memory.append(session_id, user_message)
        messages: list[Message] = [Message.system(system), *history, user_message]

        yield AgentEvent(
            kind="started",
            data={
                "session_id": session_id,
                "run_id": run_id,
                "max_steps": self.settings.max_steps,
            },
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

            # for call in message.tool_calls:
            #     yield AgentEvent(
            #         kind="tool_call",
            #         step=step,
            #         tool_name=call.name,
            #         tool_call_id=call.id,
            #         tool_arguments=call.arguments,
            #     )

            #     with self._span("tool.call", tool=call.name):
            #         # registry.call 从不抛异常，失败会变成 "错误：..." 文本，
            #         # 让模型自己看到并纠正，而不是把循环打断
            #         result = await self.registry.call(call.name, call.arguments)

            #     tool_message = Message.tool_result(call.id, result, name=call.name)
            #     messages.append(tool_message)
            #     self.memory.append(session_id, tool_message)

            #     yield AgentEvent(
            #         kind="tool_result",
            #         step=step,
            #         tool_name=call.name,
            #         tool_call_id=call.id,
            #         tool_result=result,
            #     )

            # 把这一轮的工具调用按"能不能并发"分批（见 group_parallel_calls）。
            # 注意回填顺序：tool 消息必须和 assistant 的 tool_calls 一一对应，
            # 所以下面严格按原顺序走，不能"谁先跑完谁先写"。
            for batch in group_parallel_calls(message.tool_calls, self.registry):
                # 先把这一批次的调用时间抛完 —— 让用户看到“模型同时发起了这几个”
                for call in batch:
                    yield AgentEvent(
                        kind="tool_call",
                        step=step,
                        tool_name=call.name,
                        tool_call_id=call.id,
                        tool_arguments=call.arguments,
                    )

                # 执行。 批里只有一个就直连await(省掉并发调度的开销)
                if len(batch) == 1:
                    results = [await self._call_tool(batch[0], session_id=session_id, step=step)]
                else:
                    with self._span("tools.batch", count=len(batch)):
                        results = await asyncio.gather(
                            *(self._call_tool(call, session_id=session_id, step=step) 
                              for call in batch
                              ),
                        )

                # 回填。gather 的返回值时**按传入顺序**排的(不是按完成顺序)
                # 所以zip一下就正好对应上协议要求的顺序
                for call, result in zip(batch, results):
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

        # span 要绑出来：token 得写进这一次调用自己的属性里
        with self._span("llm.turn", step=step, stream=stream, tools=len(tools)) as span:
            if not stream:
                response = await self.llm.chat(messages, tools=tools)
                holder["message"] = response.message
                holder["usage"] = response.usage
                note_tokens(span, response.usage)
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
                    note_tokens(span, chunk.usage)

    async def _call_tool(self, call: ToolCall,  *, session_id: str, step: int) -> str:
        """执行单个工具调用。

        有副作用的工具（`read_only=False`）必须先过审批：**决定权在人手里，
        不在模型手里**。只读工具直接放行——否则每读一个文件都弹一次确认，
        没人受得了。

        注意 `registry.call` 从不抛异常（失败会变成「错误：...」文本），
        所以并发时不需要额外的异常处理，这一点让 gather 特别安全。
        """

        if not self.registry.is_parallel_safe(call.name):
            result = await self._approve(
                ApprovalRequest(call=call, session_id=session_id, step=step)
            )
            if not result.approved:
                # 拒绝就**必定**返回：这里不能嵌在 `if result.reason:` 里面，
                # 否则"没给理由"就会掉进下面的执行语句——拒绝变照做。
                return rejected_message(call, result.reason)
            
        # span 名字保持 "tool.call"：obs 的文档和 tracing 的示例都用它，
        # 改成别的名字会让按名字过滤 trace 的人漏掉这条
        with self._span("tool.call", tool=call.name):
            return await self.registry.call(call.name, call.arguments)
    # ------------------------------------------------------------------ 辅助
    def _span(self, name: str, **attributes: Any) -> AbstractContextManager[Any]:
        """没有配置 tracer 时退化成空上下文，调用处不用写 if。

        注意这里**照样产出一个 `TraceSpan`**（只是没人会写它）：契约是
        "`with ... as span` 拿到的永远是个 span 对象"。如果这里直接给
        `nullcontext()`，绑出来的就是 `None`，调用点写 `span.attributes[...]`
        会在没配 tracer 时炸掉——测试里就是这么炸出来的。
        """
        if self.tracer is None:
            return nullcontext(TraceSpan(name=name, attributes=attributes))
        return self.tracer.span(name, **attributes)


    @property
    def stats(self) -> str:
        """给 CLI 收尾时打印的一行统计。"""
        return self.accountant.summary()


def note_tokens(span: Any, usage: Usage | None) -> None:
    """把这次模型调用的 token 记到它自己的 span 上。

    为什么不只留在 `TokenAccountant` 里：那个是内存累加器，进程一退就归零，
    只能回答"这一次问答花了多少"。而 span 会立刻被追加进 `.agent/traces.jsonl`，
    于是"哪个会话、哪一轮、哪一步贵"变成可以事后聚合的事实。

    这里只写 token（每次调用独有的数字）；session / run 这类公共上下文由
    `Tracer.set_context()` 自动带上，不在这里重复。

    什么时候不写：`usage` 为空（有些兼容端点在流式下不返回用量）。宁可缺一条，
    也不要记 0——0 会被当成"这次不花钱"，而缺失是能被发现的。
    """
    if usage is None:
        return
    span.attributes.update(
        {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
        }
    )


def group_parallel_calls(
    calls: list[ToolCall], registry: ToolRegistry
) -> list[list[ToolCall]]:
    """把同一轮的多个工具调用按"能不能并发"分批。

    规则：**连续的只读调用并成一批，有副作用的调用单独成批。**

    为什么用"连续段"而不是"把只读的全挑出来凑一堆"：
        模型给出的顺序本身就是它对任务的理解。"写 A 再读 A"这种依赖，
        只有保持原顺序才成立——如果先把所有读挑出来一起跑，那个读就会在
        写之前执行，拿到旧数据。按连续段分批，既保住了顺序语义，
        又让相邻的只读调用拿到并行收益。

    为什么副作用调用不跟任何东西合并：
        两个写操作之间的先后是模型指定的，合并执行会让顺序变得不确定。
        代价是连续两个写操作不会并行——这是刻意的，正确性优先。

    纯函数：只读参数、返回新列表，不碰 registry 以外的任何状态。
    """
    batches: list[list[ToolCall]] = []
    current: list[ToolCall] = []

    for call in calls:
        if registry.is_parallel_safe(call.name):
            current.append(call)
            continue

        # 遇到“副作用”的调用：先把攒着的只读批次收掉，再让她单独成一批
        if current:
            batches.append(current)
            current = []
        batches.append([call])
    if current:
        batches.append(current)
    return batches
