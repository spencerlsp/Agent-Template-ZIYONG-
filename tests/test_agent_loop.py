"""主循环的测试：用 mock 模型跑完整流程，全程不需要网络。

这些用例守护的是主循环的**行为契约**：事件顺序、工具是否被真正执行、
消息是否按协议落库、步数上限是否兜住。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_template.agent.loop import AgentLoop
from agent_template.agent.runtime import AgentRuntime
from agent_template.config import Settings
from agent_template.llm.base import Message, ToolCall
from agent_template.llm.mock import MockLLM
from agent_template.memory.store import MemoryStore
from agent_template.tools.registry import ToolRegistry


def build_loop(
    tmp_path: Path, scripted: list[Message], *, max_steps: int = 4
) -> tuple[AgentLoop, MemoryStore, list[str]]:
    """造一个只带 ping 工具的循环，模型按脚本回答。"""
    calls: list[str] = []
    registry = ToolRegistry()

    @registry.register
    def ping(target: str) -> str:
        """回显目标文本。"""
        calls.append(target)
        return f"pong:{target}"

    memory = MemoryStore(tmp_path / "memory.sqlite3")
    settings = Settings(
        llm_provider="mock", max_steps=max_steps, stream=True, project_root=tmp_path
    )
    loop = AgentLoop(
        llm=MockLLM(scripted=scripted),
        registry=registry,
        memory=memory,
        settings=settings,
    )
    return loop, memory, calls


async def collect(loop: AgentLoop, session: str = "s1") -> list:
    return [event async for event in loop.run(session, "帮我 ping 一下")]


async def test_tool_call_then_final_answer(tmp_path: Path) -> None:
    loop, memory, calls = build_loop(
        tmp_path,
        [
            Message.assistant(
                tool_calls=[ToolCall(id="c1", name="ping", arguments={"target": "x"})]
            ),
            Message.assistant("已经 ping 过了"),
        ],
    )
    try:
        events = await collect(loop)
        kinds = [event.kind for event in events]

        assert kinds[0] == "started"
        assert "tool_call" in kinds and "tool_result" in kinds
        assert kinds[-1] == "finished"
        assert events[-1].text == "已经 ping 过了"
        # 工具真的被执行了，而不是只发了个事件
        assert calls == ["x"]
        # 按协议落库：user → assistant(tool_calls) → tool → assistant
        assert [message.role for message in memory.history("s1")] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
    finally:
        memory.close()


async def test_answer_without_tool_call(tmp_path: Path) -> None:
    loop, memory, calls = build_loop(tmp_path, [Message.assistant("直接回答")])
    try:
        events = await collect(loop)
        kinds = [event.kind for event in events]

        assert kinds[0] == "started"
        assert kinds[-1] == "finished"
        assert "usage" in kinds  # 用量事件要有
        assert calls == []
        assert [message.role for message in memory.history("s1")] == [
            "user",
            "assistant",
        ]
    finally:
        memory.close()


async def test_max_steps_is_enforced(tmp_path: Path) -> None:
    """模型一直调工具不给结论时，必须在上限处停下并报错。"""
    scripted = [
        Message.assistant(
            tool_calls=[
                ToolCall(id=f"c{index}", name="ping", arguments={"target": str(index)})
            ]
        )
        for index in range(3)
    ]
    loop, memory, calls = build_loop(tmp_path, scripted, max_steps=2)
    try:
        events = await collect(loop)

        assert events[-1].kind == "error"
        assert len(calls) == 2  # 只跑了 2 步
    finally:
        memory.close()


@pytest.mark.parametrize("stream", [True, False])
async def test_stream_and_non_stream_agree(tmp_path: Path, stream: bool) -> None:
    """流式与非流式必须给出同样的最终结果，只是事件粒度不同。"""
    loop, memory, _ = build_loop(tmp_path, [Message.assistant("一样的答案")])
    try:
        events = [event async for event in loop.run("s1", "问一句", stream=stream)]
        finished = [event for event in events if event.kind == "finished"]

        assert finished
        assert finished[0].text == "一样的答案"
    finally:
        memory.close()


async def test_runtime_assembles_offline(tmp_path: Path) -> None:
    """不连 MCP、不启用 RAG 时，运行时也该能装配并跑完一轮。"""
    settings = Settings(
        llm_provider="mock",
        project_root=tmp_path,
        mcp_servers=[],
        embedding_provider="local_hash",
        embedding_dim=64,
    )

    runtime = await AgentRuntime.create(
        settings, connect_mcp=False, enable_rag=False
    )
    try:
        # 4 个内置工具 + load_skill / list_skills
        assert len(runtime.registry) == 6

        kinds = [event.kind async for event in runtime.ask("你好")]

        assert "finished" in kinds
    finally:
        await runtime.aclose()
